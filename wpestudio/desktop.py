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


# --- steam -------------------------------------------------------------------

def steam_note():
    """Where Steam and the wallpapers were found, so a wrong guess is visible."""
    from . import paths
    return {
        "root": paths.STEAM_ROOT,
        "flatpak": "/.var/app/com.valvesoftware.Steam/" in paths.STEAM_ROOT,
        "workshop_present": os.path.isdir(paths.WORKSHOP),
        "app_present": os.path.isdir(paths.WPE_APP),
        "extra_libraries": paths.extra_library_folders(),
        "engine": paths.ENGINE,
        "engine_present": os.path.exists(paths.ENGINE),
    }


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
        "desktop_icons": icons_supported(),
        "desktop_icons_note": None if icons_supported() else
            "No known desktop-icons setting for this desktop. If icons cover the "
            "wallpaper, turn them off in your file manager's preferences.",
        "steam": steam_note(),
        "tested_on": "Linux Mint 22 / Cinnamon / X11",
    }
