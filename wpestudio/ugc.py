"""Driving Steam's own downloader for subscribed wallpapers.

The one thing everybody assumes and nobody checks: subscribing to a Workshop
item does not download it. Steam records the subscription and waits for the
game to call ISteamUGC::DownloadItem. Wallpaper Engine does that on Windows.
Nothing does it on Linux, so a machine can hold hundreds of subscriptions with
sixty of them on disk and no error anywhere to explain the gap.

This module runs bin/wpe-ugc against the running client and reports what comes
back. See that file for why it is a separate process.
"""
import json
import os
import shutil
import subprocess
import threading
import time

from . import paths, steamio

MARK = "@@WPEUGC@@"
FLATPAK_HOME = os.path.join(paths.HOME, ".var/app/com.valvesoftware.Steam")
HELPER = os.path.join(paths.BIN_DIR, "wpe-ugc")
# Staged copy, for the Flatpak case. A dotfile in the sandbox home, which is
# the one directory both sides can see.
STAGED = os.path.join(FLATPAK_HOME, ".wpe-ugc")


def _flatpak_steam():
    """True when the Steam we are talking to is the Flatpak one."""
    return paths.STEAM_ROOT.startswith(FLATPAK_HOME) and shutil.which("flatpak")


def _command(args):
    """The argv that runs the helper somewhere it can reach the client."""
    if _flatpak_steam():
        # Stage on every call: the helper changes with the app, and copying a
        # 6KB file is cheaper than reasoning about whether it is stale.
        try:
            shutil.copyfile(HELPER, STAGED)
        except OSError as e:
            return None, "cannot stage the helper into the Steam sandbox: %s" % e
        return (["flatpak", "run", "--command=/usr/bin/python3",
                 "com.valvesoftware.Steam", os.path.join(paths.HOME, ".wpe-ugc")]
                + list(args)), None
    return [HELPER] + list(args), None


def _env():
    env = dict(os.environ)
    if _flatpak_steam():
        return env                      # the sandbox provides its own HOME
    # Native Steam: libsteam_api resolves steamclient.so through ~/.steam.
    env.setdefault("WPE_STEAM_ROOT", paths.STEAM_ROOT)
    return env


def _lines(proc):
    """The helper's JSON lines, with Steam's own chatter filtered out."""
    for raw in proc.stdout:
        i = raw.find(MARK)
        if i < 0:
            continue
        try:
            yield json.loads(raw[i + len(MARK):])
        except ValueError:
            continue


def status(timeout=25):
    """What Steam thinks is subscribed, and what is actually on disk."""
    if not steamio.steam_running():
        return {"ok": False, "error": "steam_not_running",
                "message": "Steam is not running, so it cannot download anything."}
    cmd, err = _command(["status"])
    if err:
        return {"ok": False, "error": err}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=_env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out talking to Steam"}
    for raw in p.stdout.splitlines():
        i = raw.find(MARK)
        if i >= 0:
            try:
                return json.loads(raw[i + len(MARK):])
            except ValueError:
                pass
    return {"ok": False, "error": (p.stderr or p.stdout or "no reply")[-300:]}


class Sync(threading.Thread):
    """A running download job, with progress worth showing.

    Only one at a time: two of these would fight over the same queue and the
    progress numbers would mean nothing.
    """
    daemon = True
    _current = None
    _lock = threading.Lock()

    def __init__(self, ids=None):
        super().__init__()
        self.ids = [str(i) for i in (ids or [])]
        self.total = len(self.ids)
        self.done = 0
        self.failed = 0
        self.remaining = self.total
        self.current = {}          # id -> (bytes done, bytes total)
        self.finished = False
        self.error = None
        self.started_at = time.time()
        self.proc = None

    @classmethod
    def active(cls):
        with cls._lock:
            job = cls._current
            return job if job and not job.finished else None

    @classmethod
    def start_job(cls, ids=None):
        with cls._lock:
            if cls._current and not cls._current.finished:
                return cls._current, False
            job = Sync(ids)
            cls._current = job
            job.start()
            return job, True

    def snapshot(self):
        return {
            "running": not self.finished,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "remaining": self.remaining,
            "error": self.error,
            "elapsed": round(time.time() - self.started_at, 1),
            "current": [{"id": k, "bytes": v[0], "total": v[1]}
                        for k, v in self.current.items()],
        }

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def run(self):
        try:
            self._run()
        except Exception as e:                      # never take the daemon down
            self.error = repr(e)[:200]
        finally:
            self.finished = True
            self.current = {}

    def _run(self):
        if not steamio.steam_running():
            self.error = "steam_not_running"
            return
        cmd, err = _command(["sync"] if not self.ids else ["get"] + self.ids)
        if err:
            self.error = err
            return
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=_env())
        for msg in _lines(self.proc):
            ev = msg.get("event")
            if ev == "start":
                self.total = msg.get("total", self.total)
                self.remaining = self.total
            elif ev == "progress":
                self.current[msg["id"]] = (msg.get("current", 0), msg.get("total", 0))
                self.remaining = msg.get("remaining", self.remaining)
            elif ev == "item":
                self.current.pop(msg.get("id"), None)
                self.done = msg.get("done", self.done)
                self.failed = msg.get("failed", self.failed)
                self.remaining = msg.get("remaining", self.remaining)
            elif ev == "done":
                self.done = msg.get("installed", self.done)
                self.failed = msg.get("failed", self.failed)
                self.remaining = msg.get("remaining", 0)
            elif msg.get("ok") is False and msg.get("error") and not ev:
                self.error = msg["error"]
        self.proc.wait()


class Watcher(threading.Thread):
    """Start the backlog moving the moment Steam appears.

    Subscribing while Steam is closed is the normal case -- you are browsing,
    the client is not open. Steam records it and, on Linux, that is where it
    stops forever. Watching for the client and kicking off a sync turns that
    into "it downloads when you open Steam", which is what everyone already
    believes happens.
    """
    daemon = True

    def __init__(self, enabled=lambda: True, interval=10):
        super().__init__()
        self.enabled = enabled
        self.interval = interval
        self.seen_running = steamio.steam_running()

    def run(self):
        while True:
            time.sleep(self.interval)
            try:
                running = steamio.steam_running()
                if running and not self.seen_running and self.enabled():
                    # Give the client a moment to finish logging in; asking
                    # before that gets an empty subscription list.
                    time.sleep(20)
                    if steamio.steam_running():
                        Sync.start_job()
                self.seen_running = running
            except Exception:
                pass
