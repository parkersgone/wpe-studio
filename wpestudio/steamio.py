"""Steam: the Workshop, and taking over the Play button.

Two jobs.

1. Workshop browsing. There is no unauthenticated API that returns a browsable
   Workshop list, so we read the same HTML page the Steam client shows and pull
   the item cards out of it. Subscribing needs Steam's own session, so the
   Subscribe button deep-links into the client (steam://) rather than pretending
   to do it here and silently failing.

2. Making Steam's Play button open THIS app instead of the Windows one under
   Proton. That is a per-user launch option in localconfig.vdf. Steam rewrites
   that file when it exits, so an edit made while Steam is running is thrown
   away -- install() refuses to pretend otherwise.

   The launch command has to escape the flatpak sandbox, which needs a
   permission Steam does not ship with. `flatpak override --user` grants it
   without root.
"""
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

from . import paths

APPID = "431960"
FLATPAK_ID = "com.valvesoftware.Steam"
UA = "Mozilla/5.0 (X11; Linux x86_64) wpe-studio"


# --- is Steam a flatpak, and is it up? --------------------------------------

def is_flatpak():
    return "/.var/app/%s/" % FLATPAK_ID in paths.STEAM_ROOT


def steam_running():
    """Is the Steam client itself up?

    NOT `pgrep -f ubuntu12_32/steam`: linux-wallpaperengine is launched with an
    --assets-dir under the Steam tree, so that pattern matches a running
    wallpaper and reports Steam as up when it is closed. Match the process
    NAME, which only the client itself has.
    """
    for pattern in ("^steam$", "^steamwebhelper$"):
        if subprocess.run(["pgrep", "-x", pattern.strip("^$")],
                          capture_output=True).returncode == 0:
            return True
    return False


def userdata_ids():
    try:
        return [d for d in sorted(os.listdir(paths.STEAM_USERDATA))
                if d.isdigit() and d != "0"]
    except OSError:
        return []


def localconfig(uid):
    return os.path.join(paths.STEAM_USERDATA, uid, "config", "localconfig.vdf")


# --- the launch option ------------------------------------------------------

def launch_command():
    """What Steam should run when he presses Play."""
    target = os.path.join(paths.BIN_DIR, "wpe-studio")
    if is_flatpak():
        # flatpak-spawn is in every runtime; --host puts us back on the real
        # machine, where our code and the X display actually live.
        return "/usr/bin/flatpak-spawn --host %s --from-steam %%command%%" % target
    return "%s --from-steam %%command%%" % target


def _find_app_block(text, appid=APPID):
    """Byte range of the "<appid>" { ... } block inside the apps section.

    Deliberately not a full VDF parse: this file holds his entire Steam client
    config and a round-trip through a hand-rolled writer is a bad trade for one
    string. We locate exactly one block by brace counting and touch nothing
    else.
    """
    # The apps section sits under Software/Valve/Steam. Anchor on the deepest
    # "apps" key that is followed by our appid.
    for m in re.finditer(r'"apps"\s*\n\s*\{', text):
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            c = text[i]
            if c == '"':                       # skip strings, braces can hide inside
                i += 1
                while i < len(text) and text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        section = text[start:i]
        am = re.search(r'"%s"\s*\n(\s*)\{' % re.escape(appid), section)
        if not am:
            continue
        bstart = start + am.end()
        indent = am.group(1)
        depth = 1
        j = bstart
        while j < len(text) and depth > 0:
            c = text[j]
            if c == '"':
                j += 1
                while j < len(text) and text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        return bstart, j - 1, indent
    return None


def read_launch_options(uid):
    p = localconfig(uid)
    try:
        text = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    loc = _find_app_block(text)
    if not loc:
        return ""
    bstart, bend, _indent = loc
    m = re.search(r'"LaunchOptions"\s*"((?:[^"\\]|\\.)*)"', text[bstart:bend])
    return m.group(1) if m else ""


def _vdf_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def write_launch_options(uid, value):
    p = localconfig(uid)
    text = open(p, encoding="utf-8", errors="replace").read()
    loc = _find_app_block(text)
    if not loc:
        return {"ok": False, "error": "no 431960 block in %s" % p}
    bstart, bend, indent = loc
    block = text[bstart:bend]
    line = '\n%s\t"LaunchOptions"\t\t"%s"' % (indent, _vdf_escape(value))

    m = re.search(r'\n[^\n]*"LaunchOptions"\s*"(?:[^"\\]|\\.)*"', block)
    if m:
        newblock = block[:m.start()] + line + block[m.end():]
    else:
        newblock = "\n" + line.lstrip("\n") + block

    out = text[:bstart] + newblock + text[bend:]

    if out.count("{") != text.count("{") or out.count("}") != text.count("}"):
        return {"ok": False, "error": "brace balance changed, refusing to write"}

    backup = p + ".wpe-studio.bak"
    if not os.path.exists(backup):
        shutil.copy2(p, backup)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(out)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return {"ok": True, "path": p, "backup": backup, "value": value}


def flatpak_permissions():
    """Grant Steam the two things the launch hook needs. No root required."""
    if not is_flatpak():
        return {"ok": True, "skipped": "steam is not a flatpak"}
    cmds = [
        ["flatpak", "override", "--user", "--talk-name=org.freedesktop.Flatpak", FLATPAK_ID],
        ["flatpak", "override", "--user", "--filesystem=%s" % paths.ROOT, FLATPAK_ID],
    ]
    errs = []
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode != 0:
            errs.append(" ".join(c) + ": " + (r.stderr or "").strip())
    return {"ok": not errs, "errors": errs}


def hook_status():
    uids = userdata_ids()
    want = launch_command()
    out = {
        "flatpak": is_flatpak(),
        "steam_running": steam_running(),
        "users": [],
        "want": want,
        "installed": False,
    }
    for uid in uids:
        cur = read_launch_options(uid)
        out["users"].append({"uid": uid, "launch_options": cur,
                             "installed": cur == want})
    out["installed"] = bool(out["users"]) and all(u["installed"] for u in out["users"])
    return out


def install_hook(uid=None):
    if steam_running():
        return {"ok": False, "error": "steam_running",
                "message": "Steam rewrites localconfig.vdf when it exits, so this "
                           "has to happen with Steam closed."}
    perms = flatpak_permissions()
    results = []
    for u in ([uid] if uid else userdata_ids()):
        results.append(write_launch_options(u, launch_command()))
    return {"ok": all(r.get("ok") for r in results) and perms.get("ok", True),
            "permissions": perms, "results": results}


def uninstall_hook(uid=None):
    if steam_running():
        return {"ok": False, "error": "steam_running"}
    results = [write_launch_options(u, "") for u in ([uid] if uid else userdata_ids())]
    return {"ok": all(r.get("ok") for r in results), "results": results}


def shutdown_steam():
    if not steam_running():
        return True
    if is_flatpak():
        subprocess.run(["flatpak", "run", FLATPAK_ID, "-shutdown"], capture_output=True)
    else:
        subprocess.run(["steam", "-shutdown"], capture_output=True)
    for _ in range(60):
        if not steam_running():
            return True
        time.sleep(1)
    return False


def start_steam():
    if is_flatpak():
        subprocess.Popen(["flatpak", "run", FLATPAK_ID],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        subprocess.Popen(["steam"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


# --- deep links -------------------------------------------------------------

def open_url(url):
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def open_item(wid):
    return open_url("steam://url/CommunityFilePage/%s" % wid)


def open_workshop():
    return open_url("steam://url/SteamWorkshopPage/%s" % APPID)


def open_library():
    return open_url("steam://open/games")


# --- workshop browse --------------------------------------------------------

# Steam's Workshop browse is a React app with hashed, churning class names --
# the old `div.workshopItem` selector stopped matching anything and the grid
# silently came back empty. These anchor on semantic markup instead: the link to
# the item and the preview image's alt text, which is also the title. Less
# pretty, considerably less likely to break again.
_CARD_RE = re.compile(
    r'<a\s+href="[^"]*sharedfiles/filedetails/\?id=(\d+)"[^>]*>\s*'
    r'<img\s+([^>]*?)/?>', re.I)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _strip(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def browse(query="", sort="trend", page=1, days=-1, tags=None, timeout=12):
    """Workshop browse, parsed out of the public HTML page.

    Returns [] on any failure rather than raising: a Workshop tab that shows
    nothing is annoying, one that takes the whole UI down is not acceptable.
    """
    params = {
        "appid": APPID,
        "section": "readytouseitems",
        "browsesort": sort,
        "p": str(max(1, int(page))),
        "numperpage": "30",
    }
    if query:
        params["searchtext"] = query
    if days and int(days) > 0:
        params["days"] = str(days)
    for t in (tags or []):
        params.setdefault("requiredtags[]", t)
    url = "https://steamcommunity.com/workshop/browse/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": str(e), "items": [], "url": url}

    items, seen = [], set()
    for wid, attrs in _CARD_RE.findall(body):
        if wid in seen:
            continue
        seen.add(wid)
        a = dict(_ATTR_RE.findall(attrs))
        items.append({
            "id": wid,
            "title": _strip(a.get("alt", "")) or "(untitled)",
            "author": "",
            "preview": a.get("src", ""),
            "stars": 0,
            "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=%s" % wid,
        })
    return {"ok": True, "items": items, "url": url, "page": int(page)}


def item_details(ids, timeout=12):
    """Public details for Workshop ids -- title, preview, subscriptions, tags."""
    ids = [str(i) for i in ids]
    if not ids:
        return {}
    data = {"itemcount": str(len(ids))}
    for i, wid in enumerate(ids):
        data["publishedfileids[%d]" % i] = wid
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        data=body, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.load(r)
    except Exception:
        return {}
    out = {}
    for d in (j.get("response") or {}).get("publishedfiledetails") or []:
        out[str(d.get("publishedfileid"))] = d
    return out


# --- subscribing without leaving the app -------------------------------------
#
# The Workshop tab used to be the steamcommunity site in a WebView, which works
# but does not look or behave like Wallpaper Engine's own browser. Rendering the
# results natively means the Subscribe button has to do what the page's button
# does: POST to /sharedfiles/subscribe with the logged-in session.
#
# The session is the one the embedded WebView already holds -- the same login
# the user did in the Workshop tab -- read out of its cookie jar. Nothing new is
# asked of them, and if they are not signed in there we fall back to opening the
# item so they can press the real button.

COOKIE_DB = os.path.join(paths.CONFIG_DIR, "steam-webview", "cookies.sqlite")


def session_cookies():
    """{name: value} of the steamcommunity cookies the embedded view holds."""
    import sqlite3
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % COOKIE_DB, uri=True)
        for name, value in con.execute(
            "SELECT name, value FROM moz_cookies WHERE host LIKE '%steamcommunity.com%'"
        ):
            out[name] = value
        con.close()
    except Exception:
        pass
    return out


def signed_in():
    return bool(session_cookies().get("steamLoginSecure"))


def _post_form(url, fields, cookies, timeout=15):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://steamcommunity.com",
        "Referer": "https://steamcommunity.com/workshop/browse/?appid=%s" % APPID,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": "; ".join("%s=%s" % (k, v) for k, v in cookies.items()),
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")[:400]


def set_subscription(wid, subscribe=True):
    """Subscribe to or unsubscribe from a Workshop item as the signed-in user."""
    cookies = session_cookies()
    if not cookies.get("steamLoginSecure"):
        return {"ok": False, "error": "not_signed_in",
                "message": "Sign in to Steam once in the Workshop tab."}

    # Steam only requires that the sessionid form field matches the cookie, so
    # if the jar has not got one yet, set one on both sides.
    sessionid = cookies.get("sessionid")
    if not sessionid:
        sessionid = secrets.token_hex(12)
        cookies["sessionid"] = sessionid

    verb = "subscribe" if subscribe else "unsubscribe"
    try:
        status, body = _post_form(
            "https://steamcommunity.com/sharedfiles/%s" % verb,
            {"id": str(wid), "appid": APPID, "sessionid": sessionid},
            cookies,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

    # Steam answers {"success":1} on success and 8 for "not found".
    ok = status == 200 and ('"success":1' in body.replace(" ", "")
                            or body.strip() in ("", "null"))
    return {"ok": ok, "status": status, "body": body, "subscribed": subscribe}
