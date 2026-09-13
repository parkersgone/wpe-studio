"""Read the wallpaper library straight off disk.

Sources, in the order Wallpaper Engine itself uses them:
  workshop/content/431960/<id>   subscribed Workshop items
  wallpaper_engine/projects/defaultprojects/<name>   the ones that ship with it
  wallpaper_engine/projects/myprojects/<name>        anything he made locally

A "wallpaper id" here is the directory name. For Workshop items that is the
numeric Workshop id, which is also what linux-wallpaperengine wants on the
command line; for local ones it is the folder name and we hand the engine the
absolute path instead.
"""
import json
import os
import re
import time

from . import paths

PREVIEW_NAMES = ("preview.gif", "preview.jpg", "preview.png", "preview.webp", "preview.jpeg")

_cache = {"stamp": 0.0, "items": [], "sig": None}


def _preview(d):
    for n in PREVIEW_NAMES:
        p = os.path.join(d, n)
        if os.path.exists(p):
            return p
    # some authors name it whatever they like and point project.json at it
    try:
        j = _project(d)
        cand = j.get("preview")
        if cand:
            p = os.path.join(d, cand)
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return None


def _project(d):
    with open(os.path.join(d, "project.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _dirsize(d):
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _norm_type(t, d):
    t = (t or "").lower()
    if t in ("scene", "video", "web", "application"):
        return t
    # A Workshop "preset" is not a wallpaper: it is a bag of property values
    # plus a `dependency` naming the wallpaper they belong to. Running one
    # directly is what produced the five silent failures on this box.
    try:
        j = _project(d)
        if j.get("dependency") and isinstance(j.get("preset"), dict):
            return "preset"
    except Exception:
        pass
    # No type field: infer. A scene.json means scene, an index.html means web,
    # otherwise if the `file` is a media container it is a video.
    if os.path.exists(os.path.join(d, "scene.json")):
        return "scene"
    if os.path.exists(os.path.join(d, "index.html")):
        return "web"
    try:
        f = (_project(d).get("file") or "").lower()
        if f.endswith((".mp4", ".webm", ".mkv", ".avi", ".m4v", ".mov")):
            return "video"
    except Exception:
        pass
    return "unknown"


def _sources():
    out = []
    if os.path.isdir(paths.WORKSHOP):
        out.append(("workshop", paths.WORKSHOP))
    for sub, tag in (("defaultprojects", "default"), ("myprojects", "local")):
        p = os.path.join(paths.WPE_PROJECTS, sub)
        if os.path.isdir(p):
            out.append((tag, p))
    return out


def _signature():
    """Cheap change detector: (dir name, mtime) for every wallpaper folder.

    Rescanning 45+ folders with a du walk on every poll would make the grid
    janky; this catches a new subscription within one poll without the cost.
    """
    sig = []
    for _src, base in _sources():
        try:
            for name in sorted(os.listdir(base)):
                d = os.path.join(base, name)
                try:
                    sig.append((name, int(os.path.getmtime(d))))
                except OSError:
                    pass
        except OSError:
            pass
    return tuple(sig)


def scan(force=False):
    sig = _signature()
    if not force and _cache["sig"] == sig and _cache["items"]:
        return _cache["items"]

    items = []
    for src, base in _sources():
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            d = os.path.join(base, name)
            if not os.path.isdir(d):
                continue
            if not os.path.exists(os.path.join(d, "project.json")):
                continue
            try:
                j = _project(d)
            except Exception:
                j = {}
            wtype = _norm_type(j.get("type"), d)
            prev = _preview(d)
            props = ((j.get("general") or {}).get("properties") or {})
            tags = j.get("tags") or []
            dependency = str(j.get("dependency") or "") or None
            preset_props = j.get("preset") if isinstance(j.get("preset"), dict) else None
            items.append({
                "id": name,
                "dir": d,
                "source": src,
                "title": (j.get("title") or "").strip() or "(%s)" % name,
                "description": j.get("description") or "",
                "type": wtype,
                "file": j.get("file") or "",
                "preview": os.path.basename(prev) if prev else None,
                "has_preview": bool(prev),
                "preview_animated": bool(prev and prev.endswith(".gif")),
                "tags": tags,
                "contentrating": j.get("contentrating") or "Everyone",
                "ratingsex": j.get("ratingsex") or "none",
                "ratingviolence": j.get("ratingviolence") or "none",
                "workshopid": str(j.get("workshopid") or (name if name.isdigit() else "")),
                "workshopurl": j.get("workshopurl") or (
                    "https://steamcommunity.com/sharedfiles/filedetails/?id=%s" % name
                    if name.isdigit() else ""),
                "official": bool(j.get("official")),
                "dependency": dependency,
                "preset_props": preset_props,
                "customizable": bool([p for p in props.values()
                                      if isinstance(p, dict) and p.get("type") != "group"])
                                or bool(preset_props),
                "prop_count": len([p for p in props.values()
                                   if isinstance(p, dict) and p.get("type") != "group"])
                              or len(preset_props or {}),
                "mtime": int(os.path.getmtime(d)),
            })
    by_id = {i["id"]: i for i in items}
    for it in items:
        dep = it.get("dependency")
        if not dep:
            continue
        base = by_id.get(dep)
        it["dependency_ok"] = bool(base)
        it["dependency_title"] = base["title"] if base else None
        # A preset inherits the look of whatever it customises, so show the
        # base wallpaper's type rather than "preset" everywhere.
        if base and it["type"] == "preset":
            it["base_type"] = base["type"]

    items.sort(key=lambda i: (-i["mtime"], i["title"].lower()))
    _cache.update(sig=sig, items=items, stamp=time.time())
    return items


def get(wid):
    for it in scan():
        if it["id"] == str(wid):
            return it
    return None


def size_of(wid):
    it = get(wid)
    return _dirsize(it["dir"]) if it else 0


# --- properties -------------------------------------------------------------

_COND_SIMPLE = re.compile(
    r"^\s*([A-Za-z_][\w]*)\s*(?:\.value\s*)?(===|==|!==|!=|>=|<=|>|<)\s*"
    r"(true|false|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")\s*;?\s*$"
)
_COND_BARE = re.compile(r"^\s*([A-Za-z_][\w]*)(?:\.value)?\s*;?\s*$")


def eval_condition(cond, values):
    """Best-effort evaluation of project.json `condition` strings.

    These are fragments of JavaScript that the real app runs in its own engine.
    We handle the two shapes that actually appear in the wild -- `x.value ==
    true` and a bare `x` -- and SHOW the property for anything else. Showing a
    control that should be hidden is a cosmetic bug; hiding one that should be
    visible loses him a setting with no way to get it back.
    """
    if not cond:
        return True
    m = _COND_SIMPLE.match(cond)
    if m:
        name, op, want = m.groups()
        cur = values.get(name)
        if want == "true":
            want_v = True
        elif want == "false":
            want_v = False
        elif want[:1] in "'\"":
            want_v = want[1:-1]
        else:
            want_v = float(want)
        try:
            if op in ("==", "==="):
                return _loose_eq(cur, want_v)
            if op in ("!=", "!=="):
                return not _loose_eq(cur, want_v)
            cur_f, want_f = float(cur), float(want_v)
            return {">": cur_f > want_f, "<": cur_f < want_f,
                    ">=": cur_f >= want_f, "<=": cur_f <= want_f}[op]
        except Exception:
            return True
    m = _COND_BARE.match(cond)
    if m:
        return bool(values.get(m.group(1), True))
    return True


def _loose_eq(a, b):
    if isinstance(b, bool):
        return bool(a) == b
    if isinstance(b, float):
        try:
            return float(a) == b
        except Exception:
            return False
    return str(a) == str(b)


def resolve_target(wid):
    """(item to render, property values to start from) for any library entry.

    For a normal wallpaper that is itself and {}. For a preset it is the
    wallpaper it depends on, plus the preset's stored values.
    """
    it = get(wid)
    if not it:
        return None, {}
    if it["type"] == "preset" and it.get("dependency"):
        base = get(it["dependency"])
        if base:
            return base, dict(it.get("preset_props") or {})
    return it, {}


def properties(wid, overrides=None):
    """Property schema for one wallpaper, with current values folded in.

    Returns a flat, ordered list. `group` entries are kept -- they are the
    section headers the real UI draws -- and each one carries the properties
    that follow it, so the frontend does not have to re-derive the grouping.
    """
    it, preset_values = resolve_target(wid)
    if not it:
        return []
    try:
        j = _project(it["dir"])
    except Exception:
        return []
    raw = ((j.get("general") or {}).get("properties") or {})
    # preset values sit under the user's own overrides
    overrides = dict(preset_values, **(overrides or {}))

    entries = []
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        entries.append((key, spec))
    # `order` is the author's intended layout; `index` is the fallback the
    # editor writes. Anything with neither goes last, alphabetically.
    entries.sort(key=lambda kv: (
        kv[1].get("order", kv[1].get("index", 10 ** 6)),
        kv[0].lower(),
    ))

    values = {}
    for key, spec in entries:
        values[key] = overrides.get(key, spec.get("value"))

    out = []
    for key, spec in entries:
        ptype = (spec.get("type") or "text").lower()
        item = {
            "key": key,
            "type": ptype,
            "label": paths.tr(spec.get("text") or key),
            "value": values.get(key),
            "default": spec.get("value"),
            "order": spec.get("order", spec.get("index", 0)),
            "visible": eval_condition(spec.get("condition"), values),
            "condition": spec.get("condition") or "",
            "editable": ptype not in ("group", "text"),
        }
        if ptype == "slider":
            item.update(
                min=spec.get("min", 0),
                max=spec.get("max", 1),
                step=spec.get("step", 0.01),
                precision=spec.get("precision", 2),
                fraction=bool(spec.get("fraction")),
            )
        elif ptype == "combo":
            item["options"] = [
                {"label": paths.tr(o.get("label", "")), "value": o.get("value")}
                for o in (spec.get("options") or [])
            ]
        elif ptype in ("file", "scenetexture", "textureslot"):
            item["editable"] = False
            item["note"] = "asset picker not supported"
        out.append(item)
    return out


def defaults_for(wid):
    return {p["key"]: p["default"] for p in properties(wid)
            if p["type"] not in ("group", "text")}
