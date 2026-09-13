"""What desktop are we on, and what can we actually do here.

This started as Linux Mint / Cinnamon / X11 code with those assumptions spread
through it. They are collected here instead, so every other module asks rather
than assumes, and so the UI can say "that part does not work on your desktop"
instead of silently doing nothing.

The honest support matrix:

  rendering       X11 everywhere, and Wayland on compositors that speak
                  wlr-layer-shell (sway, Hyprland, river, Wayfire, labwc).
                  The two paths are genuinely different: on X11 we start an
                  ordinary window and demote it to the desktop layer with
                  wmctrl/xprop, because that is the only thing that works
                  under a desktop that already owns the root window. On
                  Wayland the engine asks the compositor for the background
                  layer directly and there is nothing to demote.

                  GNOME and KDE do not implement wlr-layer-shell and have no
                  equivalent, so a Wayland session on those cannot show a live
                  wallpaper from any external program. That is said plainly
                  rather than failing quietly.
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


# Compositors known to implement wlr-layer-shell, which is what the engine
# needs for the background layer. Mutter and KWin deliberately do not.
LAYER_SHELL_DESKTOPS = ("sway", "hyprland", "river", "wayfire", "labwc",
                        "wlroots", "niri", "miracle", "qtile")
LAYER_SHELL_NEVER = ("gnome", "kde")


def layer_shell_likely():
    """Does this Wayland compositor support the background layer?

    There is no way to ask without connecting to the compositor, so go by who
    it is. Getting this wrong in the optimistic direction just means the engine
    fails with its own message; getting it wrong the other way would hide a
    working setup, so unknown compositors are given the benefit of the doubt.
    """
    raw = " ".join(filter(None, (
        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        os.environ.get("DESKTOP_SESSION", ""),
        os.environ.get("XDG_SESSION_DESKTOP", ""),
        os.environ.get("SWAYSOCK") and "sway" or "",
        os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and "hyprland" or "",
    ))).lower()
    if any(k in raw for k in LAYER_SHELL_DESKTOPS):
        return True
    if any(k in raw for k in LAYER_SHELL_NEVER):
        return False
    return True


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
    wayland = session == WAYLAND
    render_ok = (not wayland) or layer_shell_likely()
    return {
        "desktop": de,
        "session": session,
        "render": render_ok,
        "render_note": None if render_ok else
            "%s on Wayland does not implement wlr-layer-shell and has no "
            "equivalent, so no external program can put a live wallpaper on "
            "your desktop. Log in to an X11 session instead, or use a "
            "compositor that supports it (sway, Hyprland, river, Wayfire)."
            % de.upper() if de in LAYER_SHELL_NEVER else
            "This compositor may not support the background layer.",
        "desktop_icons": icons_supported(),
        "desktop_icons_note": None if icons_supported() else
            "No known desktop-icons setting for this desktop. If icons cover the "
            "wallpaper, turn them off in your file manager's preferences.",
        "steam": steam_note(),
        "tested_on": "Linux Mint 22 / Cinnamon / X11; Wayland tested on sway",
    }
