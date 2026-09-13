"""Persisted state. One JSON file, written atomically.

Two things worth knowing:
  * per-wallpaper property overrides are keyed by wallpaper id, NOT by monitor.
    That matches Wallpaper Engine -- tweak a wallpaper once, it stays tweaked
    wherever you put it.
  * every write goes through save(), which writes a temp file and renames. A
    half-written state.json would silently reset every setting on this box, and
    the wallpaper daemon reads it at login when nobody is watching.
"""
import json
import os
import threading

from . import paths

_lock = threading.RLock()

DEFAULTS = {
    "global": {
        "fps": 30,
        "volume": 15,
        "mute": False,
        "auto_mute": True,          # mute when another app plays sound
        "audio_processing": True,   # audio-reactive wallpapers
        "pause_on_fullscreen": True,
        "particles": True,
        "mouse": True,
        "parallax": True,
        "scaling": "default",       # default|stretch|fit|fill
        "clamp": "clamp",           # clamp|border|repeat
        "skin": "dark",
        "autostart": True,
        "restore_on_launch": True,
        "translate": False,          # run non-Latin labels through a local model
        "opacity": 100,              # window background opacity, 40-100
        "tray_panel": True,          # tray click opens the mini panel, not a menu
        "seen_walkthrough": False,
    },
    "monitors": {},     # name -> {"id": str|None}
    "props": {},        # wallpaper id -> {prop: value}
    "presets": {},      # wallpaper id -> {preset name: {prop: value}}
    "favorites": [],
    "incompatible": {},   # wallpaper id -> last failure, set by engine.apply
    "playlists": {},    # name -> {"items": [ids], "interval_min": 30, "order": "sequential"}
    "active_playlist": None,
    "filters": {},
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    with _lock:
        raw = {}
        try:
            with open(paths.STATE_FILE, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raw = {}
        except Exception:
            # A corrupt state file must not brick the app. Keep the bad copy
            # for forensics and carry on with defaults.
            try:
                os.replace(paths.STATE_FILE, paths.STATE_FILE + ".corrupt")
            except Exception:
                pass
            raw = {}
        return _merge(DEFAULTS, raw)


def save(st):
    with _lock:
        tmp = paths.STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, paths.STATE_FILE)
        return st


def update(fn):
    """Read-modify-write under the lock. fn mutates the dict in place."""
    with _lock:
        st = load()
        fn(st)
        save(st)
        return st


def props_for(wid):
    return load()["props"].get(str(wid), {})


def set_props(wid, props):
    def _f(st):
        st["props"][str(wid)] = props
    update(_f)
