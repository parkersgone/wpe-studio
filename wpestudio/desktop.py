"""What desktop are we on, and what can we actually do here.

This started as Linux Mint / Cinnamon / X11 code with those assumptions spread
through it. They are collected here instead, so every other module asks rather
than assumes, and so the UI can say "that part does not work on your desktop"
instead of silently doing nothing.

The honest support matrix:

  rendering       X11 only. The whole approach is "take a normal window and
                  demote it to the desktop layer", which is an X11 idea driven
                  through xrandr/wmctrl/xprop. linux-wallpaperengine itself can
                  do Wayland via wlr-layer-shell, but none of this wrapper can.
  desktop icons   the file manager that owns the root window differs per
                  desktop; known for Cinnamon, GNOME, MATE and Xfce.
  lock screen     the background key differs per desktop, and on KDE it is not
                  a gsetting at all -- so that one is reported unsupported
                  rather than half-done.
"""
import os
import shutil
import subprocess

X11 = "x11"
WAYLAND = "wayland"


def session_type():
    t = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    if t in (X11, WAYLAND):
        return t
    if os.environ.get("WAYLAND_DISPLAY"):
        return WAYLAND
    if os.environ.get("DISPLAY"):
        return X11
    return "unknown"


def desktop_env():
    """A lowercase key: cinnamon, gnome, kde, xfce, mate, lxqt, or 'other'."""
    raw = " ".join(filter(None, (
        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        os.environ.get("DESKTOP_SESSION", ""),
        os.environ.get("XDG_SESSION_DESKTOP", ""),
    ))).lower()
    for key in ("cinnamon", "gnome", "kde", "plasma", "xfce", "mate", "lxqt", "budgie"):
        if key in raw:
            return "kde" if key == "plasma" else key
    return "other"


# schema, key, and the matching "how should it be scaled" key
BACKGROUND_KEYS = {
    "cinnamon": ("org.cinnamon.desktop.background", "picture-uri", "picture-options"),
    "gnome":    ("org.gnome.desktop.background", "picture-uri", "picture-options"),
    "budgie":   ("org.gnome.desktop.background", "picture-uri", "picture-options"),
    "mate":     ("org.mate.background", "picture-filename", "picture-options"),
    # Xfce uses xfconf, not gsettings; handled separately.
    # KDE stores it in a plasma config that needs a scripting call -- not worth
    # guessing at, so it reports unsupported.
}

# the process that owns the root window, and how to ask it to stop
DESKTOP_ICON_KEYS = {
    "cinnamon": ("org.nemo.desktop", "show-desktop-icons"),
    "gnome":    ("org.gnome.desktop.background", "show-desktop-icons"),
    "mate":     ("org.mate.background", "show-desktop-icons"),
}


def _gs_get(schema, key):
    r = subprocess.run(["gsettings", "get", schema, key],
                       capture_output=True, text=True)
    return r.stdout.strip().strip("'") if r.returncode == 0 else None


def _gs_set(schema, key, value):
    return subprocess.run(["gsettings", "set", schema, key, value],
                          capture_output=True, text=True).returncode == 0


def _has_schema(schema):
    r = subprocess.run(["gsettings", "list-schemas"], capture_output=True, text=True)
    return schema in (r.stdout or "").split()


# --- background -------------------------------------------------------------

def background_supported():
    de = desktop_env()
    if de in BACKGROUND_KEYS:
        return _has_schema(BACKGROUND_KEYS[de][0])
    if de == "xfce":
        return bool(shutil.which("xfconf-query"))
    return False


def get_background():
    de = desktop_env()
    if de in BACKGROUND_KEYS:
        schema, key, _opts = BACKGROUND_KEYS[de]
        return _gs_get(schema, key)
    if de == "xfce":
        r = subprocess.run(
            ["xfconf-query", "-c", "xfce4-desktop", "-l"],
            capture_output=True, text=True)
        for line in (r.stdout or "").splitlines():
            if line.strip().endswith("/last-image"):
                got = subprocess.run(["xfconf-query", "-c", "xfce4-desktop",
                                      "-p", line.strip()],
                                     capture_output=True, text=True)
                return got.stdout.strip()
    return None


def set_background(path_or_uri):
    """Set the desktop (and therefore lock screen) background. True on success."""
    de = desktop_env()
    if de in BACKGROUND_KEYS:
        schema, key, opts = BACKGROUND_KEYS[de]
        # MATE wants a bare path; the GNOME-lineage ones want a file:// URI.
        value = path_or_uri
        if key.endswith("filename"):
            value = path_or_uri[len("file://"):] if path_or_uri.startswith("file://") else path_or_uri
        elif not value.startswith("file://") and value.startswith("/"):
            value = "file://" + value
        ok = _gs_set(schema, key, value)
        _gs_set(schema, opts, "zoom")
        return ok
    if de == "xfce":
        plain = path_or_uri[len("file://"):] if path_or_uri.startswith("file://") else path_or_uri
        r = subprocess.run(["xfconf-query", "-c", "xfce4-desktop", "-l"],
                           capture_output=True, text=True)
        touched = False
        for line in (r.stdout or "").splitlines():
            if line.strip().endswith("/last-image"):
                subprocess.run(["xfconf-query", "-c", "xfce4-desktop",
                                "-p", line.strip(), "-s", plain],
                               capture_output=True)
                touched = True
        return touched
    return False


# --- desktop icons ----------------------------------------------------------

def icons_supported():
    de = desktop_env()
    return de in DESKTOP_ICON_KEYS and _has_schema(DESKTOP_ICON_KEYS[de][0])


def icons_on():
    de = desktop_env()
    if de not in DESKTOP_ICON_KEYS:
        return False
    schema, key = DESKTOP_ICON_KEYS[de]
    return _gs_get(schema, key) == "true"


def set_icons(on):
    de = desktop_env()
    if de not in DESKTOP_ICON_KEYS:
        return False
    schema, key = DESKTOP_ICON_KEYS[de]
    return _gs_set(schema, key, "true" if on else "false")


# --- lock screen ------------------------------------------------------------

def lockscreen_provider():
    """Which locker reads the desktop background, if any."""
    de = desktop_env()
    if de == "cinnamon" and shutil.which("cinnamon-screensaver-command"):
        return "cinnamon-screensaver"
    if de in ("gnome", "budgie"):
        # GNOME's shield uses the background too, but only via the *screensaver*
        # schema on some versions; report it and let the UI say "may vary".
        return "gnome-shell"
    if de == "mate":
        return "mate-screensaver"
    return None


def lock_now():
    for cmd in (["cinnamon-screensaver-command", "--lock"],
                ["loginctl", "lock-session"],
                ["xdg-screensaver", "lock"],
                ["mate-screensaver-command", "--lock"]):
        if not shutil.which(cmd[0]):
            continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return {"ok": True, "used": cmd[0]}
    return {"ok": False, "error": "no known screen locker on PATH"}


def idle_settings_supported():
    return desktop_env() == "cinnamon" and _has_schema("org.cinnamon.desktop.session")


# --- the summary the UI uses ------------------------------------------------

def capabilities():
    de, session = desktop_env(), session_type()
    render_ok = session == X11
    return {
        "desktop": de,
        "session": session,
        "render": render_ok,
        "render_note": None if render_ok else
            "This wrapper drives X11 windows directly (xrandr/wmctrl/xprop). "
            "On Wayland nothing will appear, even though linux-wallpaperengine "
            "itself supports wlr-layer-shell.",
        "background": background_supported(),
        "desktop_icons": icons_supported(),
        "lockscreen": bool(lockscreen_provider()),
        "lockscreen_provider": lockscreen_provider(),
        "idle_settings": idle_settings_supported(),
        "tested_on": "Linux Mint 22 / Cinnamon / X11",
    }
