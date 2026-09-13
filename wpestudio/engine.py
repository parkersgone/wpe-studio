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
import os
import re
import shlex
import signal
import subprocess
import time

from . import library, paths, state

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


def monitors():
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
        time.sleep(0.4)
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
    try:
        r = _run(["gsettings", "get", "org.nemo.desktop", "show-desktop-icons"])
        return r.stdout.strip() == "true"
    except Exception:
        return False


def set_desktop_icons(on):
    _run(["gsettings", "set", "org.nemo.desktop", "show-desktop-icons",
          "true" if on else "false"])


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
    stop(mon)

    before = _windows_by_class()
    log = open(_logfile(mon), "wb")
    proc = subprocess.Popen(argv, env=_env(), stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    with open(_pidfile(mon), "w") as fh:
        fh.write(str(proc.pid))

    win = _find_window(proc.pid, before=before) if wait_window else None
    if win:
        demote(win)

    time.sleep(0.6)
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
        "workshop": paths.WORKSHOP,
        "workshop_present": os.path.isdir(paths.WORKSHOP),
    }


def screenshot(wid, out_path, delay=90):
    """One still frame, for the lock screen and the login screen.

    The window is demoted to the desktop layer FIRST. An earlier version left
    it as an ordinary window, which meant a full-screen render sat on top of
    everything for as long as the grab took and locked the user out of their
    own desktop. A still frame is never worth covering the screen for.
    """
    target, preset_values = library.resolve_target(wid)
    if not target:
        return False
    preset_values = _absolutise(preset_values, library.get(wid))

    m = monitors()[0]
    argv = [paths.ENGINE, "--window", "0x0x%dx%d" % (m["width"], m["height"]),
            "--silent", "--fps", "30",
            "--screenshot", out_path, "--screenshot-delay", str(delay)]
    if os.path.isdir(paths.WPE_ASSETS):
        argv += ["--assets-dir", paths.WPE_ASSETS]
    props = dict(preset_values, **state.props_for(wid))
    for k, v in sorted(props.items()):
        if v is not None:
            argv += ["--set-property", "%s=%s" % (k, _fmt_value(v))]
    argv.append(target["id"] if target["source"] == "workshop" else target["dir"])

    # Remove any previous frame FIRST. Without this, a failed render leaves the
    # old file in place, the "did it work" check finds a non-empty file, and the
    # caller is told the new wallpaper was captured when nothing happened.
    try:
        os.remove(out_path)
    except OSError:
        pass

    before = _windows_by_class()
    proc = subprocess.Popen(argv, env=_env(), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    win = _find_window(proc.pid, timeout=20, before=before)
    if win:
        demote(win)

    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                break
            time.sleep(0.5)
    finally:
        # Always take it down, including on a timeout. This process covering
        # the desktop is the failure mode that matters.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass

    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


def restore():
    """Re-apply whatever was last on each monitor. Used at login."""
    st = state.load()
    done = []
    for name, cfg in (st.get("monitors") or {}).items():
        wid = cfg.get("id")
        if wid and library.get(wid):
            done.append(apply(wid, name, persist=False))
    return done
