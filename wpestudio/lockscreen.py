"""Lock screen: use Mint's own, and give it the wallpaper as a backdrop.

Cinnamon already ships a lock screen -- the pill with the avatar and the
password box -- and it already does PAM correctly. Replacing it means owning
authentication, and an earlier attempt here (xsecurelock plus a custom saver)
bought a live animated background at the cost of two competing password fields
and a stack of moving parts. Not worth it.

So this does the small, reliable thing instead: render one frame of the chosen
wallpaper and hand it to cinnamon-screensaver as its background.
cinnamon-screensaver reads `org.cinnamon.desktop.background picture-uri`
(see /usr/share/cinnamon-screensaver/util/settings.py:9), so setting that key
puts the wallpaper's imagery behind the stock pill. Nothing about the login
flow changes.

Side effect, and it is a good one: the same key is the desktop's static
background, so if the live wallpaper is ever stopped the desktop falls back to
a still of it rather than to black.

The one thing Cinnamon cannot do is the greeter -- LightDM runs before the
session exists. That still frame is installed by a root step.
"""
import os
import re
import subprocess

from . import engine, library, paths, state

STILL_DIR = os.path.expanduser("~/.local/share/wpe-studio")


def still_path(wid):
    """One file per wallpaper, on purpose.

    Cinnamon caches the background by URI. Rewriting the same
    lockscreen.png and setting the same picture-uri leaves the old image on
    screen -- which looked exactly like "it ignored my selection".
    """
    return os.path.join(STILL_DIR, "lockscreen-%s.png" % re.sub(r"\W", "_", str(wid)))


def newest_still():
    try:
        files = [os.path.join(STILL_DIR, f) for f in os.listdir(STILL_DIR)
                 if f.startswith("lockscreen-") and f.endswith(".png")]
    except OSError:
        return None
    return max(files, key=os.path.getmtime) if files else None


SUDO_SCRIPT = os.path.join(paths.CACHE_DIR, "wpe-studio-login-bg.sh")
GREETER_CONF = "/etc/lightdm/lightdm-gtk-greeter.conf.d/99-wpe-studio.conf"


def _gs_get(schema, key):
    r = subprocess.run(["gsettings", "get", schema, key],
                       capture_output=True, text=True)
    return r.stdout.strip().strip("'")


def _gs_set(schema, key, value):
    return subprocess.run(["gsettings", "set", schema, key, value],
                          capture_output=True, text=True).returncode == 0


def status():
    st = state.load()
    ss = st["screensaver"]
    return {
        "screensaver": ss,
        "still": newest_still(),
        "current_background": _gs_get("org.cinnamon.desktop.background", "picture-uri"),
        "idle_delay_min": int(_gs_get("org.cinnamon.desktop.session",
                                      "idle-delay").split()[-1] or 0) // 60,
        "lock_enabled": _gs_get("org.cinnamon.desktop.screensaver",
                                "lock-enabled") == "true",
        "login_installed": os.path.exists(GREETER_CONF),
        "provider": "cinnamon-screensaver",
    }


def enable(wallpaper_id=None, timeout_min=None, lock=None, login_background=None):
    """Render a still from the wallpaper and point the lock screen at it."""
    def _f(st):
        ss = st["screensaver"]
        if wallpaper_id is not None:
            ss["wallpaper_id"] = str(wallpaper_id)
        if timeout_min is not None:
            ss["timeout_min"] = int(timeout_min)
        if lock is not None:
            ss["lock"] = bool(lock)
        if login_background is not None:
            ss["login_background"] = bool(login_background)
        ss["enabled"] = True
        # Remember what the background was, once, so disable() can put it back.
        ss.setdefault("previous_background",
                      _gs_get("org.cinnamon.desktop.background", "picture-uri"))
    st = state.update(_f)
    ss = st["screensaver"]

    wid = ss.get("wallpaper_id")
    if not wid or not library.get(wid):
        return {"ok": False, "error": "no wallpaper selected"}

    os.makedirs(STILL_DIR, exist_ok=True)
    still = still_path(wid)
    it = library.get(wid)
    if not engine.screenshot(wid, still, delay=120):
        return {"ok": False, "error":
                "could not capture a frame from \u201c%s\u201d (%s). "
                "linux-wallpaperengine's --screenshot does not work for every "
                "wallpaper type." % (it["title"], it["type"])}

    _gs_set("org.cinnamon.desktop.background", "picture-uri", "file://" + still)
    _gs_set("org.cinnamon.desktop.background", "picture-options", "zoom")
    _gs_set("org.cinnamon.desktop.session", "idle-delay",
            str(max(60, int(ss["timeout_min"]) * 60)))
    _gs_set("org.cinnamon.desktop.screensaver", "lock-enabled",
            "true" if ss.get("lock", True) else "false")
    _gs_set("org.cinnamon.desktop.screensaver", "idle-activation-enabled", "true")

    return {"ok": True, "still": still, "title": it["title"], "status": status()}


def disable():
    st = state.load()
    prev = (st["screensaver"] or {}).get("previous_background")

    def _f(s):
        s["screensaver"]["enabled"] = False
    state.update(_f)

    if prev:
        _gs_set("org.cinnamon.desktop.background", "picture-uri", prev)
    return {"ok": True, "restored": prev}


def test():
    """Lock the screen now, with Cinnamon's own locker."""
    r = subprocess.run(["cinnamon-screensaver-command", "--lock"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout or "").strip()}
    return {"ok": True}


# --- the greeter, which needs root ------------------------------------------

def write_sudo_script():
    lines = [
        "#!/usr/bin/env bash",
        "# Generated by wpe-studio. Sets the LightDM greeter background.",
        "set -euo pipefail",
        'echo "== wpe-studio: login screen background =="',
        "install -Dm644 %s /usr/share/backgrounds/wpe-studio-login.png"
        % (newest_still() or ""),
        "install -d /etc/lightdm/lightdm-gtk-greeter.conf.d",
        "cat > %s <<'EOF'" % GREETER_CONF,
        "[greeter]",
        "background=/usr/share/backgrounds/wpe-studio-login.png",
        "EOF",
        'echo "Done. Close this window."',
        'read -r -p "press enter " _ || true',
    ]
    with open(SUDO_SCRIPT, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(SUDO_SCRIPT, 0o755)
    return SUDO_SCRIPT


def pop_login_installer():
    if not newest_still():
        return {"ok": False, "error": "render the still frame first (enable it)"}
    script = write_sudo_script()
    ask = "/ai/bin/ask-sudo"
    if not os.path.exists(ask):
        return {"ok": False, "popped": False, "script": script,
                "error": "ask-sudo not found"}
    # ask-sudo's title is a FLAG. Passing it positionally makes it the command
    # and the popped window fails with a bare "not found".
    subprocess.Popen([ask, "--title", "Wallpaper Engine - login background",
                      "bash", script], start_new_session=True)
    return {"ok": True, "popped": True, "script": script}
