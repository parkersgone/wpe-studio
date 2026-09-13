"""Driving linux-wallpaperengine, and getting its window to behave on Cinnamon.

The hard-won bit is the window handling. `--screen-root` renders *underneath*
Cinnamon's own desktop window, so you get a black screen and a process that
looks healthy -- that is the "it's broken on this box" everyone reports. What
works is to ask for an ordinary window at the monitor's geometry and then demote
it by hand:

    _NET_WM_WINDOW_TYPE = _NET_WM_WINDOW_TYPE_DESKTOP   <- below the desklet
                                                           layer, not just
                                                           below normal windows
    remove above, remove fullscreen                     <- it maps with both
                                                           set, and BELOW will
                                                           not stick until they
                                                           come off
    add below, sticky, skip_taskbar, skip_pager
    _MOTIF_WM_HINTS = undecorated

Order matters. Doing BELOW before removing ABOVE silently does nothing.
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time

from . import desktop, library, paths, state

PROC_NAME = "linux-wallpaper"   # /proc/<pid>/comm is capped at 15 chars
SCALINGS = ("default", "stretch", "fit", "fill")
CLAMPS = ("clamp", "border", "repeat")


def _env():
    e = dict(os.environ)
    e.setdefault("DISPLAY", ":0")
    # NVIDIA: upstream's own recommendation, stops the first frames stuttering
    e["__GL_THREADED_OPTIMIZATIONS"] = "0"
    return e


def _run(cmd, **kw):
    return subprocess.run(cmd, env=_env(), capture_output=True, text=True,
                          timeout=kw.pop("timeout", 15), **kw)


# --- monitors ---------------------------------------------------------------

_MON_RE = re.compile(r"^\s*(\d+):\s+\+?\*?(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)")


def wayland():
    return desktop.session_type() == desktop.WAYLAND


def _wayland_monitors():
    """Outputs as wlr-randr reports them.

    xrandr under XWayland answers, but it answers about XWayland's own idea of
    the screen, which is one rectangle covering everything. The engine needs
    real output names to hand to --screen-root, so ask the compositor.
    """
    if not shutil.which("wlr-randr"):
        return []
    try:
        r = _run(["wlr-randr", "--json"])
        data = json.loads(r.stdout)
    except Exception:
        return _wlr_randr_text()

    out = []
    for i, o in enumerate(data):
        if not o.get("enabled", True):
            continue
        mode = next((m for m in o.get("modes", []) if m.get("current")), None)
        pos = o.get("position") or {}
        out.append({
            "index": i,
            "name": o.get("name") or "output-%d" % i,
            "width": int((mode or {}).get("width") or 1920),
            "height": int((mode or {}).get("height") or 1080),
            "x": int(pos.get("x") or 0),
            "y": int(pos.get("y") or 0),
            "primary": i == 0,
        })
    return out


_WLR_LINE = re.compile(r"^(\S+)\s")
_WLR_MODE = re.compile(r"(\d+)x(\d+)\s+px.*current", re.I)
_WLR_POS = re.compile(r"Position:\s*(-?\d+),(-?\d+)")


def _wlr_randr_text():
    """wlr-randr without --json, for the versions that predate it."""
    try:
        r = _run(["wlr-randr"])
    except Exception:
        return []
    out, cur = [], None
    for line in r.stdout.splitlines():
        if line and not line[0].isspace():
            m = _WLR_LINE.match(line)
            if m:
                cur = {"index": len(out), "name": m.group(1), "width": 1920,
                       "height": 1080, "x": 0, "y": 0, "primary": not out}
                out.append(cur)
            continue
        if cur is None:
            continue
        m = _WLR_MODE.search(line)
        if m:
            cur["width"], cur["height"] = int(m.group(1)), int(m.group(2))
        m = _WLR_POS.search(line)
        if m:
            cur["x"], cur["y"] = int(m.group(1)), int(m.group(2))
    return out


def monitors():
    if wayland():
        found = _wayland_monitors()
        if found:
            return found
        # No wlr-randr: fall through to xrandr, which under XWayland at least
        # gives a plausible single rectangle to render into.
    out = []
    try:
        r = _run(["xrandr", "--listmonitors"])
        for line in r.stdout.splitlines():
            m = _MON_RE.match(line)
            if not m:
                continue
            idx, name, w, h, x, y = m.groups()
            out.append({
                "index": int(idx),
                "name": name.lstrip("+*"),
                "width": int(w), "height": int(h),
                "x": int(x), "y": int(y),
                "primary": "*" in line.split()[1] if len(line.split()) > 1 else False,
            })
    except Exception:
        pass
    if not out:
        out = [{"index": 0, "name": "HDMI-0", "width": 1920, "height": 1080,
                "x": 0, "y": 0, "primary": True}]
    return out


def monitor(name):
    for m in monitors():
        if m["name"] == name:
            return m
    return monitors()[0]


# --- process bookkeeping ----------------------------------------------------

def _pidfile(mon):
    return os.path.join(paths.RUN_DIR, "wpe-%s.pid" % re.sub(r"\W", "_", mon))


def _logfile(mon):
    return os.path.join(paths.LOG_DIR, "wpe-%s.log" % re.sub(r"\W", "_", mon))


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/comm" % pid) as fh:
            return fh.read().strip() == PROC_NAME
    except OSError:
        return False


def running_pid(mon):
    try:
        with open(_pidfile(mon)) as fh:
            pid = int(fh.read().strip())
    except Exception:
        return None
    return pid if _alive(pid) else None


def _geometry_pids(geom):
    """Every engine process rendering at this exact --window geometry.

    The pidfile alone is not enough: a service restart, a CLI apply and a UI
    apply can each leave a process the next pidfile does not know about, and
    two engines on one monitor is two GPU contexts fighting over one screen.
    Matching on the geometry is precise enough to leave other monitors alone.
    """
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/comm" % pid) as fh:
                if fh.read().strip() != PROC_NAME:
                    continue
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                args = fh.read().split(b"\0")
        except OSError:
            continue
        if geom.encode() in args:
            out.append(pid)
    return out


_GEOM_ARG = re.compile(r"^\d+x\d+x\d+x\d+$")


def _screen_root_pids(name):
    """Every engine process rendering to this Wayland output."""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/comm" % pid) as fh:
                if fh.read().strip() != PROC_NAME:
                    continue
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                args = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0")]
        except OSError:
            continue
        for i, a in enumerate(args):
            if a in ("--screen-root", "--screen-span") and i + 1 < len(args) \
                    and name in args[i + 1].split(","):
                out.append(pid)
                break
    return out


def _orphan_pids():
    """Renderers drawing to a rectangle no monitor has any more.

    Unplug a screen, or change the layout, and the engine that was painting the
    old rectangle keeps running -- and becomes unstoppable, because everything
    that stops a wallpaper matches on the geometry of a monitor that is now
    gone. It cannot belong to anybody, so it goes.
    """
    live = {"%dx%dx%dx%d" % (m["x"], m["y"], m["width"], m["height"])
            for m in monitors()}
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/comm" % pid) as fh:
                if fh.read().strip() != PROC_NAME:
                    continue
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                args = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0")]
        except OSError:
            continue
        if "--screen-root" in args or "--screen-span" in args:
            # Named by output, not by rectangle; a stale name is the
            # compositor's business, not ours.
            continue
        geoms = [a for a in args if _GEOM_ARG.match(a)]
        if geoms and not any(g in live for g in geoms):
            out.append(pid)
    return out


def stop(mon=None, quiet=True):
    """Stop one monitor's wallpaper, or every one of ours plus strays."""
    stopped = []
    targets = [mon] if mon else [m["name"] for m in monitors()]
    for name in targets:
        pids = set()
        pid = running_pid(name)
        if pid:
            pids.add(pid)
        m = monitor(name)
        pids.update(_geometry_pids(
            "%dx%dx%dx%d" % (m["x"], m["y"], m["width"], m["height"])))
        if wayland():
            # Started with --screen-root <output>, so that name is what
            # identifies it -- there is no geometry on the command line.
            pids.update(_screen_root_pids(m["name"]))
        # Reap anything left painting a rectangle that no longer exists, on
        # every stop rather than only on a full one -- otherwise it survives
        # until the next reboot.
        pids.update(_orphan_pids())
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
                stopped.append(p)
            except OSError:
                pass
        # SIGTERM is handled by the engine and it does not always finish -- a
        # wedged teardown left the old renderer alive while the new one started,
        # so two GL contexts fought over the same screen. Escalate rather than
        # hope.
        deadline = time.time() + 2.5
        while time.time() < deadline and any(_alive(p) for p in pids):
            time.sleep(0.1)
        for p in pids:
            if _alive(p):
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass
        try:
            os.remove(_pidfile(name))
        except OSError:
            pass
    if mon is None:
        # Anything left over from the old `wpe` script or a crashed run. -x
        # against the TRUNCATED comm; matching the full name never fires and
        # matching with -f can kill the caller.
        subprocess.run(["pkill", "-x", PROC_NAME], capture_output=True)
        subprocess.run([os.path.expanduser("~/.local/bin/livewall"), "stop"],
                       capture_output=True)
    if stopped:
        time.sleep(0.3)
    return stopped


# --- argv -------------------------------------------------------------------

def _fmt_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        s = ("%.6f" % v).rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def build_argv(wid, mon, st=None, props=None, window=None, extra=None):
    st = st or state.load()
    g = st["global"]
    target, preset_values = library.resolve_target(wid)
    if not target:
        raise ValueError("unknown wallpaper %r" % (wid,))
    preset_values = _absolutise(preset_values, library.get(wid))

    m = monitor(mon)
    geom = window or "%dx%dx%dx%d" % (m["x"], m["y"], m["width"], m["height"])

    # Two different ways to be a wallpaper.
    #
    # X11: start an ordinary window at the monitor's rectangle and demote it to
    # the desktop layer afterwards. --screen-root exists but renders UNDER the
    # desktop that Cinnamon and friends already draw on the root window, so it
    # is invisible.
    #
    # Wayland: --screen-root asks the compositor for the background layer via
    # wlr-layer-shell, which is the actual supported mechanism. There is no
    # root window to be hidden behind and nothing to demote.
    if wayland() and not window:
        argv = [paths.ENGINE, "--screen-root", m["name"]]
    else:
        argv = [paths.ENGINE, "--window", geom]

    scaling = g.get("scaling", "default")
    if scaling in SCALINGS:
        argv += ["--scaling", scaling]
    clamp = g.get("clamp", "clamp")
    if clamp in CLAMPS:
        argv += ["--clamp", clamp]

    argv += ["--fps", str(int(g.get("fps", 30)))]

    if g.get("mute"):
        argv += ["--silent"]
    else:
        argv += ["--volume", str(int(g.get("volume", 15)))]
    if not g.get("auto_mute", True):
        argv += ["--noautomute"]
    if not g.get("audio_processing", True):
        argv += ["--no-audio-processing"]
    if not g.get("pause_on_fullscreen", True):
        argv += ["--no-fullscreen-pause"]
    if not g.get("particles", True):
        argv += ["--disable-particles"]
    if not g.get("mouse", True):
        argv += ["--disable-mouse"]
    if not g.get("parallax", True):
        argv += ["--disable-parallax"]

    if os.path.isdir(paths.WPE_ASSETS):
        argv += ["--assets-dir", paths.WPE_ASSETS]

    # A preset supplies the base values; the user's own overrides win over them.
    user_props = props if props is not None else st["props"].get(str(wid), {})
    props = dict(preset_values, **(user_props or {}))
    for k, v in sorted((props or {}).items()):
        if v is None:
            continue
        argv += ["--set-property", "%s=%s" % (k, _fmt_value(v))]

    argv += list(extra or [])

    # Local (non-Workshop) projects are addressed by path, Workshop ones by id.
    argv.append(target["id"] if target["source"] == "workshop" else target["dir"])
    return argv


def _absolutise(values, source_item):
    """Rewrite a preset's relative asset paths to absolute ones.

    A preset ships its own `files/` folder and refers to it relatively
    ("customimage": "files/017 The Obsidian Spire.png"). The engine resolves
    relative paths against the BASE wallpaper's directory, where that file does
    not exist -- so the custom image silently never loads.
    """
    if not values or not source_item:
        return values or {}
    base = source_item["dir"]
    out = {}
    for k, v in values.items():
        if isinstance(v, str) and v and not v.startswith(("/", "http:", "https:")):
            cand = os.path.join(base, v)
            if os.path.exists(cand):
                v = cand
        out[k] = v
    return out


# --- window demotion --------------------------------------------------------

def _geometry(mon):
    m = monitor(mon)
    return "%dx%dx%dx%d" % (m["x"], m["y"], m["width"], m["height"])


def _windows_by_class():
    """{window id: pid} for every linux-wallpaperengine window on screen."""
    out = {}
    try:
        r = _run(["wmctrl", "-lxp"], timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split(None, 5)
            if len(parts) >= 4 and "linux-wallpaperengine" in parts[3]:
                out[parts[0]] = int(parts[2]) if parts[2].isdigit() else 0
    except Exception:
        pass
    return out


def _find_window(pid, timeout=25.0, before=None):
    """The wallpaper's X window.

    Matching on our own pid is the honest way, but a `web` wallpaper renders
    through CEF, which puts the window on a CHILD process -- pid matching there
    waits the full timeout and then reports "no window", which is how a working
    web wallpaper ends up looking broken. So: pid first, then any
    linux-wallpaperengine window that was not on screen before we launched.
    """
    before = before or {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _windows_by_class()
        for win, wpid in found.items():
            if wpid == pid:
                return win
        for win in found:
            if win not in before:
                return win
        if not _alive(pid):
            return None
        # Poll fast. At 0.4s this alone added a visible fraction of a second to
        # every switch, for no reason -- wmctrl is cheap.
        time.sleep(0.08)
    return None


def demote(win):
    if not win:
        return False
    _run(["xprop", "-id", win, "-f", "_NET_WM_WINDOW_TYPE", "32a",
          "-set", "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_DESKTOP"])
    _run(["wmctrl", "-i", "-r", win, "-b", "remove,above"])
    _run(["wmctrl", "-i", "-r", win, "-b", "remove,fullscreen"])
    for flag in ("below", "sticky", "skip_taskbar", "skip_pager"):
        _run(["wmctrl", "-i", "-r", win, "-b", "add,%s" % flag])
    _run(["xprop", "-id", win, "-f", "_MOTIF_WM_HINTS", "32c",
          "-set", "_MOTIF_WM_HINTS", "2, 0, 0, 0, 0"])
    return True


def desktop_icons_on():
    return desktop.icons_on()


def set_desktop_icons(on):
    return desktop.set_icons(on)


# --- apply ------------------------------------------------------------------

def apply(wid, mon=None, props=None, persist=True, wait_window=True):
    st = state.load()
    mon = mon or monitors()[0]["name"]
    it = library.get(wid)
    if not it:
        return {"ok": False, "error": "unknown wallpaper %s" % wid}
    if not os.path.exists(paths.ENGINE):
        return {"ok": False, "error": "linux-wallpaperengine not found at %s" % paths.ENGINE}
    if it["type"] == "preset" and not it.get("dependency_ok"):
        return {"ok": False, "error":
                "this is a preset for wallpaper %s, which is not subscribed"
                % it.get("dependency")}

    if props is not None and persist:
        state.set_props(wid, props)
        st = state.load()

    argv = build_argv(wid, mon, st=st, props=props)

    # HAND OVER, do not stop-then-start.
    #
    # Killing the old renderer first means the bare desktop is on screen for as
    # long as the new one takes to load its assets -- a second or two, and
    # longer for a big scene. Instead the new one is started underneath, and the
    # old is only killed once the new one's window exists. The switch then looks
    # instant, and a wallpaper that fails to start leaves the previous one up
    # instead of dropping you to a blank desktop.
    #
    # The cost is both renderers being resident for that moment. On a small card
    # with two heavy scenes that is a real spike, so it can be turned off.
    # On Wayland there is no X window to wait for, so the handover cannot be
    # timed against one; the compositor swaps layer surfaces itself.
    handover = bool(st["global"].get("fast_switch", True)) and wait_window \
        and not wayland()
    previous = _geometry_pids(_geometry(mon)) if handover else []

    if not handover:
        stop(mon)

    before = _windows_by_class()
    log = open(_logfile(mon), "wb")
    proc = subprocess.Popen(argv, env=_env(), stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=True)

    # Wayland surfaces are not X windows: there is nothing to find and nothing
    # to demote, because the engine asked the compositor for the background
    # layer instead of taking an ordinary window and pushing it down.
    win = None
    if wait_window and not wayland():
        win = _find_window(proc.pid, before=before)
        if win:
            demote(win)

    if handover:
        # The new window is mapped last so it sits above the old one at the same
        # desktop layer; retiring the old now is invisible.
        for old in previous:
            if old == proc.pid:
                continue
            try:
                os.kill(old, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 2.0
        while time.time() < deadline and any(_alive(p) for p in previous):
            time.sleep(0.05)
        for old in previous:
            if _alive(old) and old != proc.pid:
                try:
                    os.kill(old, signal.SIGKILL)
                except OSError:
                    pass

    with open(_pidfile(mon), "w") as fh:
        fh.write(str(proc.pid))

    time.sleep(0.15)
    ok = _alive(proc.pid)

    if persist:
        def _f(s):
            s["monitors"].setdefault(mon, {})["id"] = str(wid)
        state.update(_f)

    # Remember failures. With 80-odd wallpapers, "which of these actually run
    # on this box" is information worth keeping rather than rediscovering.
    def _mark(s2):
        bad = s2.setdefault("incompatible", {})
        if ok:
            bad.pop(str(wid), None)
        else:
            bad[str(wid)] = (tail_log(mon, 6) or "process exited immediately").strip()[-400:]
    state.update(_mark)

    res = {
        "ok": ok,
        "monitor": mon,
        "id": str(wid),
        "title": it["title"],
        "pid": proc.pid,
        "window": win,
        "demoted": bool(win),
        "cmd": " ".join(shlex.quote(a) for a in argv),
    }
    if not ok:
        res["error"] = "process exited immediately"
        res["log"] = tail_log(mon, 25)
    elif not win:
        res["warning"] = ("running but no window appeared in 25s -- it may be "
                          "rendering behind the desktop")
    if desktop_icons_on():
        res["warning"] = ("desktop icons are on, so nemo-desktop owns the root "
                          "window and will cover the wallpaper")
    return res


def tail_log(mon, n=40):
    try:
        with open(_logfile(mon), errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except Exception:
        return ""


def status():
    st = state.load()
    out = []
    for m in monitors():
        pid = running_pid(m["name"])
        assigned = (st["monitors"].get(m["name"]) or {}).get("id")
        it = library.get(assigned) if assigned else None
        out.append({
            "monitor": m,
            "pid": pid,
            "running": bool(pid),
            "id": assigned,
            "title": it["title"] if it else None,
            "type": it["type"] if it else None,
        })
    return {
        "monitors": out,
        "engine": paths.ENGINE,
        "engine_present": os.path.exists(paths.ENGINE),
        "desktop_icons": desktop_icons_on(),
        "capabilities": desktop.capabilities(),
        "workshop": paths.WORKSHOP,
        "workshop_present": os.path.isdir(paths.WORKSHOP),
    }


def restore():
    """Re-apply whatever was last on each monitor. Used at login."""
    st = state.load()
    done = []
    for name, cfg in (st.get("monitors") or {}).items():
        wid = cfg.get("id")
        if wid and library.get(wid):
            done.append(apply(wid, name, persist=False))
    return done
