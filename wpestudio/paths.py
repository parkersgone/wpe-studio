"""Every path this thing touches, resolved once.

Steam is the flatpak build on this box, so the library lives under
~/.var/app/com.valvesoftware.Steam. All of it is overridable by env so the
Docker image (which mounts the same trees at the same places) needs no code
change, and so a future non-flatpak Steam is a one-line fix.
"""
import os
import json

HOME = os.path.expanduser("~")


def _env(name, default):
    v = os.environ.get(name)
    return v if v else default


# --- Steam -----------------------------------------------------------------
STEAM_ROOT = _env(
    "WPE_STEAM_ROOT",
    os.path.join(HOME, ".var/app/com.valvesoftware.Steam/.local/share/Steam"),
)
STEAMAPPS = os.path.join(STEAM_ROOT, "steamapps")
WORKSHOP = os.path.join(STEAMAPPS, "workshop/content/431960")
WPE_APP = os.path.join(STEAMAPPS, "common/wallpaper_engine")
WPE_UI = os.path.join(WPE_APP, "ui/dist")
WPE_LOCALE = os.path.join(WPE_APP, "locale")
WPE_PROJECTS = os.path.join(WPE_APP, "projects")
WPE_ASSETS = os.path.join(WPE_APP, "assets")
STEAM_USERDATA = os.path.join(STEAM_ROOT, "userdata")

# --- the renderer ----------------------------------------------------------
ENGINE = _env("WPE_ENGINE_BIN", os.path.join(HOME, ".local/bin/linux-wallpaperengine"))

# --- our own state ---------------------------------------------------------
CONFIG_DIR = _env("WPE_CONFIG_DIR", os.path.join(HOME, ".config/wpe-studio"))
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
CACHE_DIR = _env("WPE_CACHE_DIR", os.path.join(HOME, ".cache/wpe-studio"))
LOG_DIR = os.path.join(CACHE_DIR, "logs")
RUN_DIR = os.path.join(CACHE_DIR, "run")

# --- our own code ----------------------------------------------------------
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG_DIR)
WEB_DIR = os.path.join(ROOT, "web")
BIN_DIR = os.path.join(ROOT, "bin")

for d in (CONFIG_DIR, CACHE_DIR, LOG_DIR, RUN_DIR):
    os.makedirs(d, exist_ok=True)


_locale_cache = {}


def ui_strings(lang="en-us"):
    """Wallpaper Engine's own UI strings, so labels read exactly like the app.

    Missing file is not fatal -- every caller falls back to the key, which is
    at least greppable, rather than to an empty label.
    """
    if lang in _locale_cache:
        return _locale_cache[lang]
    out = {}
    p = os.path.join(WPE_LOCALE, "ui_%s.json" % lang)
    try:
        with open(p, encoding="utf-8") as fh:
            out = json.load(fh)
    except Exception:
        out = {}
    _locale_cache[lang] = out
    return out


def tr(key, lang="en-us"):
    if not key:
        return ""
    if not isinstance(key, str) or not key.startswith("ui_"):
        return key
    return ui_strings(lang).get(key, key)
