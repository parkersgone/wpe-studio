"""The HTTP layer. Stdlib only -- no pip install to get a wallpaper picker up.

Binds 127.0.0.1 by default, where anything that can reach it can already run
`wpe-apply`, so a token would be theatre.

The moment it is bound anywhere else -- WPE_BIND=0.0.0.0 for the Docker compose
file or the remote dashboard -- it becomes an unauthenticated API that starts
processes and writes gsettings on someone's desktop. So a non-loopback bind
REQUIRES a token: one is generated into the config directory on first use, and
requests must carry it as `Authorization: Bearer` or `?t=`. The page picks it up
from the query string and reuses it, so a bookmarked URL still just works.

This is not a substitute for putting it behind Tailscale. It is the difference
between "one more step" and "no steps at all".
"""
import json
import mimetypes
import os
import posixpath
import random
import re
import subprocess
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import deps, desktop, engine, library, paths, state, steamio, translate

HOST = os.environ.get("WPE_BIND", "127.0.0.1")
PORT = int(os.environ.get("WPE_PORT", "8014"))

LOOPBACK = ("127.0.0.1", "::1", "localhost")
TOKEN_FILE = os.path.join(paths.CONFIG_DIR, "api-token")


def api_token(host):
    """The token required for this bind, or None on loopback."""
    if host in LOOPBACK:
        return None
    try:
        with open(TOKEN_FILE) as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    old = os.umask(0o077)
    try:
        with open(TOKEN_FILE, "w") as fh:
            fh.write(tok + "\n")
    finally:
        os.umask(old)
    return tok


REQUIRED_TOKEN = None

SKINS = [
    {"key": "dark", "label": "Dark", "bg": "#222222", "accent": "#4183f5"},
    {"key": "obsidian", "label": "Obsidian", "bg": "#090909", "accent": "#5bc0de"},
    {"key": "space", "label": "Space", "bg": "#0f1015", "accent": "#1976d2"},
    {"key": "moss", "label": "Moss", "bg": "#141614", "accent": "#7acc00"},
    {"key": "rust", "label": "Rust", "bg": "#111111", "accent": "#ff8700"},
    {"key": "halloween", "label": "Halloween", "bg": "#111115", "accent": "#ff5722"},
    {"key": "winter", "label": "Winter", "bg": "#0e1a22", "accent": "#6ea0b9"},
    {"key": "white", "label": "White", "bg": "#f3f3f3", "accent": "#2973f3"},
]


# --- playlist rotation ------------------------------------------------------

class Rotator(threading.Thread):
    """Advances the active playlist. One thread, checks once a minute.

    Deliberately dumb: it re-reads state every tick instead of caching, so a
    change made in the UI takes effect on the next tick with no signalling.
    """

    daemon = True

    def __init__(self):
        super().__init__(name="wpe-rotator")
        self.stop_flag = threading.Event()
        self.last_switch = 0.0
        self.cursor = 0

    def run(self):
        while not self.stop_flag.wait(20):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self):
        st = state.load()
        name = st.get("active_playlist")
        if not name:
            return
        pl = (st.get("playlists") or {}).get(name)
        if not pl or not pl.get("items"):
            return
        interval = max(1, int(pl.get("interval_min", 30))) * 60
        if time.time() - self.last_switch < interval:
            return
        items = [i for i in pl["items"] if library.get(i)]
        if not items:
            return
        if pl.get("order") == "random":
            wid = random.choice(items)
        else:
            self.cursor = (self.cursor + 1) % len(items)
            wid = items[self.cursor]
        self.last_switch = time.time()
        for m in engine.monitors():
            engine.apply(wid, m["name"], persist=True)
            break


ROTATOR = Rotator()


# --- helpers ----------------------------------------------------------------

def _bootstrap():
    st = state.load()
    return {
        "state": st,
        "status": engine.status(),
        "monitors": engine.monitors(),
        "library": library.scan(),
        "skins": SKINS,
        "steam": steamio.hook_status(),
        "translate": translate.available(),
        # In a container the session-level things (gsettings, xdg-open,
        # systemctl --user, ask-sudo) would act on the container, not on his
        # desktop. The UI greys them out rather than appearing to work.
        "docker": bool(os.environ.get("WPE_IN_DOCKER")),
        "paths": {
            "workshop": paths.WORKSHOP,
            "engine": paths.ENGINE,
            "app": paths.WPE_APP,
            "root": paths.ROOT,
        },
        "strings": {k: v for k, v in paths.ui_strings().items()
                    if k.startswith(("ui_browse", "ui_settings", "ui_ok",
                                     "ui_cancel", "ui_caption", "ui_apply",
                                     "ui_common", "ui_shared"))},
        "version": "1.0",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "wpe-studio"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _authorised(self):
        if REQUIRED_TOKEN is None:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            # compare_digest, so a wrong token cannot be found one byte at a time
            if secrets.compare_digest(header[7:].strip(), REQUIRED_TOKEN):
                return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        supplied = (q.get("t") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, REQUIRED_TOKEN)

    def handle_one_request(self):
        # A browser closing a keep-alive socket is normal and is not worth a
        # traceback in the log; anything else still surfaces.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # -- plumbing ------------------------------------------------------------

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path, ctype=None, cache=False):
        if not path or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control",
                         "public, max-age=86400" if cache else "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # -- routes --------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        q = urllib.parse.parse_qs(u.query)
        if not self._authorised():
            return self._send(401, {"error": "token required",
                                    "hint": "Authorization: Bearer <token>, or ?t=<token>"})
        try:
            return self._get(p, q)
        except Exception as e:
            return self._send(500, {"error": repr(e)})

    def _get(self, p, q):
        if p in ("/", "/index.html"):
            return self._file(os.path.join(paths.WEB_DIR, "index.html"), "text/html")
        if p in ("/app.css", "/app.js"):
            return self._file(os.path.join(paths.WEB_DIR, p.lstrip("/")))

        # Fonts and images lifted straight out of the installed app, so the
        # icons are literally Wallpaper Engine's icons.
        if p.startswith("/wpe-asset/"):
            rel = posixpath.normpath(p[len("/wpe-asset/"):])
            if rel.startswith(".."):
                return self._send(403, {"error": "no"})
            return self._file(os.path.join(paths.WPE_UI, rel), cache=True)

        if p.startswith("/preview/"):
            wid = p.split("/")[2]
            it = library.get(wid)
            if not it or not it["preview"]:
                return self._send(404, {"error": "no preview"})
            return self._file(os.path.join(it["dir"], it["preview"]), cache=True)

        if p == "/api/bootstrap":
            return self._send(200, _bootstrap())
        if p == "/api/library":
            return self._send(200, library.scan(force=q.get("force") == ["1"]))
        if p == "/api/status":
            return self._send(200, engine.status())
        if p == "/api/state":
            return self._send(200, state.load())
        if p.startswith("/api/wallpaper/"):
            wid = p.split("/")[3]
            it = library.get(wid)
            if not it:
                return self._send(404, {"error": "unknown"})
            st = state.load()
            props = library.properties(wid, st["props"].get(str(wid), {}))
            description = it["description"]
            if st["global"].get("translate"):
                description = translate.apply_to_properties(props, description)
            return self._send(200, {
                "item": dict(it, size=library.size_of(wid), description=description),
                "properties": props,
                "overrides": st["props"].get(str(wid), {}),
                "presets": list((st["presets"].get(str(wid)) or {}).keys()),
                "favorite": str(wid) in st["favorites"],
                # Whether this wallpaper's options can do anything at all here.
                "script_driven": library.script_driven(wid),
            })
        if p == "/api/workshop":
            return self._send(200, steamio.browse(
                query=(q.get("q") or [""])[0],
                sort=(q.get("sort") or ["trend"])[0],
                page=int((q.get("page") or ["1"])[0]),
            ))
        if p == "/api/steam":
            return self._send(200, steamio.hook_status())
        if p == "/api/deps":
            return self._send(200, deps.report())
        if p == "/api/translate":
            return self._send(200, translate.available())
        if p == "/api/log":
            mon = (q.get("monitor") or [engine.monitors()[0]["name"]])[0]
            return self._send(200, {"log": engine.tail_log(mon, 200)})
        return self._send(404, {"error": "no route %s" % p})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not self._authorised():
            return self._send(401, {"error": "token required"})
        try:
            return self._post(u.path, self._body())
        except Exception as e:
            return self._send(500, {"error": repr(e)})

    def _post(self, p, b):
        if p == "/api/apply":
            wid = str(b.get("id"))
            mon = b.get("monitor")
            props = b.get("props")
            return self._send(200, engine.apply(wid, mon, props=props))

        if p == "/api/stop":
            return self._send(200, {"stopped": engine.stop(b.get("monitor"))})

        if p == "/api/props":
            wid = str(b.get("id"))
            props = b.get("props") or {}
            state.set_props(wid, props)
            out = {"ok": True, "reapplied": False}
            if b.get("apply", True):
                st = state.load()
                for name, cfg in (st["monitors"] or {}).items():
                    if str(cfg.get("id")) == wid and engine.running_pid(name):
                        out["result"] = engine.apply(wid, name, props=props)
                        out["reapplied"] = True
            return self._send(200, out)

        if p == "/api/props/reset":
            wid = str(b.get("id"))
            state.set_props(wid, {})
            return self._send(200, {"ok": True,
                                    "defaults": library.defaults_for(wid)})

        if p == "/api/settings":
            def _f(st):
                st["global"].update(b.get("global") or {})
            st = state.update(_f)
            if b.get("apply", True):
                for name, cfg in (st["monitors"] or {}).items():
                    if cfg.get("id") and engine.running_pid(name):
                        engine.apply(cfg["id"], name, persist=False)
            return self._send(200, {"ok": True, "global": st["global"]})

        if p == "/api/favorite":
            wid = str(b.get("id"))

            def _f(st):
                favs = st["favorites"]
                if wid in favs:
                    favs.remove(wid)
                else:
                    favs.append(wid)
            st = state.update(_f)
            return self._send(200, {"ok": True, "favorites": st["favorites"]})

        if p == "/api/preset":
            wid, name = str(b.get("id")), (b.get("name") or "").strip()
            action = b.get("action", "save")

            def _f(st):
                d = st["presets"].setdefault(wid, {})
                if action == "delete":
                    d.pop(name, None)
                else:
                    d[name] = b.get("props") or st["props"].get(wid, {})
            st = state.update(_f)
            return self._send(200, {"ok": True,
                                    "presets": st["presets"].get(wid, {})})

        if p == "/api/preset/load":
            wid, name = str(b.get("id")), b.get("name")
            st = state.load()
            props = (st["presets"].get(wid) or {}).get(name)
            if props is None:
                return self._send(404, {"error": "no such preset"})
            state.set_props(wid, props)
            return self._send(200, {"ok": True, "props": props})

        if p == "/api/playlist":
            action = b.get("action")
            name = b.get("name")

            def _f(st):
                pls = st["playlists"]
                if action == "delete":
                    pls.pop(name, None)
                    if st.get("active_playlist") == name:
                        st["active_playlist"] = None
                elif action == "activate":
                    st["active_playlist"] = name if name in pls else None
                elif action == "deactivate":
                    st["active_playlist"] = None
                else:
                    pls[name] = {
                        "items": [str(i) for i in (b.get("items") or [])],
                        "interval_min": int(b.get("interval_min", 30)),
                        "order": b.get("order", "sequential"),
                    }
            st = state.update(_f)
            return self._send(200, {"ok": True, "playlists": st["playlists"],
                                    "active": st.get("active_playlist")})

        if p == "/api/steam":
            action = b.get("action")
            if action == "install":
                return self._send(200, steamio.install_hook())
            if action == "uninstall":
                return self._send(200, steamio.uninstall_hook())
            if action == "shutdown_steam_only":
                return self._send(200, {"ok": steamio.shutdown_steam()})
            if action == "start_steam":
                steamio.start_steam()
                return self._send(200, {"ok": True})
            if action == "shutdown_install_restart":
                steamio.shutdown_steam()
                res = steamio.install_hook()
                if b.get("restart", True):
                    steamio.start_steam()
                return self._send(200, res)
            if action == "open_item":
                return self._send(200, {"ok": steamio.open_item(b.get("id"))})
            if action == "open_workshop":
                return self._send(200, {"ok": steamio.open_workshop()})
            if action == "open_library":
                return self._send(200, {"ok": steamio.open_library()})
            if action == "open_url":
                return self._send(200, {"ok": steamio.open_url(b.get("url"))})
            return self._send(400, {"error": "unknown action"})

        if p == "/api/desktop-icons":
            engine.set_desktop_icons(bool(b.get("on")))
            return self._send(200, {"ok": True, "on": engine.desktop_icons_on()})

        if p == "/api/autostart":
            on = bool(b.get("on"))
            path = install_autostart(on)
            def _f(st):
                st["global"]["autostart"] = on
            state.update(_f)
            return self._send(200, {"ok": True, "on": on, "path": path})

        if p == "/api/install-deps":
            r = deps.report()
            cmd = r["install_command"]
            if not cmd:
                return self._send(200, {"ok": False, "error": "nothing to install"})
            ask = "/ai/bin/ask-sudo"
            if not os.path.exists(ask):
                return self._send(200, {"ok": False, "command": cmd,
                                        "error": "ask-sudo not found; run it yourself"})
            # ask-sudo's title is a FLAG; positional makes it the command.
            subprocess.Popen([ask, "--title", "wpe-studio - install dependencies",
                              "bash", "-c", cmd], start_new_session=True)
            return self._send(200, {"ok": True, "popped": True, "command": cmd})

        if p == "/api/walkthrough-done":
            def _f(st):
                st["global"]["seen_walkthrough"] = True
            state.update(_f)
            return self._send(200, {"ok": True})

        if p == "/api/rescan":
            return self._send(200, {"ok": True, "items": library.scan(force=True)})

        if p == "/api/quit":
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "no route %s" % p})


def install_autostart(on):
    d = os.path.expanduser("~/.config/autostart")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "wpe-studio.desktop")
    # The old picker's autostart fought with this one; remove it.
    for stale in ("wpe.desktop",):
        try:
            os.remove(os.path.join(d, stale))
        except OSError:
            pass
    if not on:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    with open(path, "w") as fh:
        fh.write("""[Desktop Entry]
Type=Application
Name=Wallpaper Engine (wpe-studio)
Comment=Restore the desktop wallpaper at login
Exec=%s --restore
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
NoDisplay=true
Terminal=false
""" % os.path.join(paths.BIN_DIR, "wpe-apply"))
    return path


def serve(host=HOST, port=PORT, background=False):
    global REQUIRED_TOKEN
    REQUIRED_TOKEN = api_token(host)
    if REQUIRED_TOKEN:
        print("wpe-studio: bound to %s, so a token is required.\n"
              "  token file: %s\n"
              "  open:       http://%s:%d/?t=%s"
              % (host, TOKEN_FILE, host, port, REQUIRED_TOKEN), flush=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    ROTATOR.start()
    if background:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd
    httpd.serve_forever()
    return httpd
