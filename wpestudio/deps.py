"""What is missing, and the one command that installs it on THIS distro.

The alternative was shipping a per-distro variant of the app, or a README with
five install sections and a note asking the reader to work out which one applies
to them. Both are worse than reading /etc/os-release and printing the right
line.

Everything here is detection plus a lookup table. Nothing installs anything --
the walkthrough hands the assembled command to `ask-sudo`, which pops a terminal
with it ready to run, so the user sees exactly what is about to happen as root.

Adding a distro is a column in PACKAGES and an entry in MANAGERS.
"""
import os
import shutil
import subprocess

# package manager -> (install command prefix, human name)
MANAGERS = {
    "apt": (["apt", "install", "-y"], "apt"),
    "dnf": (["dnf", "install", "-y"], "dnf"),
    "pacman": (["pacman", "-S", "--needed", "--noconfirm"], "pacman"),
    "zypper": (["zypper", "install", "-y"], "zypper"),
    "xbps": (["xbps-install", "-y"], "xbps"),
    "apk": (["apk", "add"], "apk"),
    "eopkg": (["eopkg", "install", "-y"], "eopkg"),
}

# Which manager each distro family uses. Checked against ID first, then ID_LIKE.
FAMILIES = {
    "debian": "apt", "ubuntu": "apt", "linuxmint": "apt", "pop": "apt",
    "elementary": "apt", "zorin": "apt", "raspbian": "apt", "devuan": "apt",
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf", "rocky": "dnf",
    "almalinux": "dnf", "nobara": "dnf",
    "arch": "pacman", "manjaro": "pacman", "endeavouros": "pacman",
    "cachyos": "pacman", "garuda": "pacman", "artix": "pacman",
    "opensuse": "zypper", "opensuse-tumbleweed": "zypper",
    "opensuse-leap": "zypper", "suse": "zypper",
    "void": "xbps", "alpine": "apk", "solus": "eopkg",
}

# One row per requirement. `check` is how we know it is present; the rest are
# the package that provides it, per manager. None means "not packaged there" and
# the UI says so rather than printing a command that will fail.
REQUIREMENTS = [
    {
        "key": "xrandr", "what": "xrandr",
        "why": "reads your monitor layout (X11 only)",
        "check": ("bin_unless_wayland", "xrandr"),
        "apt": "x11-xserver-utils", "dnf": "xrandr", "pacman": "xorg-xrandr",
        "zypper": "xrandr", "xbps": "xrandr", "apk": "xrandr", "eopkg": "xorg-xrandr",
    },
    {
        "key": "wmctrl", "what": "wmctrl",
        "why": "demotes the wallpaper window to the desktop layer (X11 only)",
        "check": ("bin_unless_wayland", "wmctrl"),
        "apt": "wmctrl", "dnf": "wmctrl", "pacman": "wmctrl",
        "zypper": "wmctrl", "xbps": "wmctrl", "apk": None, "eopkg": "wmctrl",
    },
    {
        "key": "xprop", "what": "xprop",
        "why": "sets the window type so the wallpaper sits behind everything (X11 only)",
        "check": ("bin_unless_wayland", "xprop"),
        "apt": "x11-utils", "dnf": "xorg-x11-utils", "pacman": "xorg-xprop",
        "zypper": "xprop", "xbps": "xprop", "apk": "xprop", "eopkg": "xorg-xprop",
    },
    {
        "key": "xdotool", "what": "xdotool",
        "why": "window geometry and input, used by the renderer hooks (X11 only)",
        "check": ("bin_unless_wayland", "xdotool"),
        "apt": "xdotool", "dnf": "xdotool", "pacman": "xdotool",
        "zypper": "xdotool", "xbps": "xdotool", "apk": "xdotool", "eopkg": "xdotool",
    },
    {
        "key": "gtk", "what": "Python GTK bindings",
        "why": "the app window; without it the UI opens in your browser instead",
        "check": ("gi", "Gtk", "3.0"),
        "apt": "python3-gi gir1.2-gtk-3.0", "dnf": "python3-gobject gtk3",
        "pacman": "python-gobject gtk3", "zypper": "python3-gobject typelib-1_0-Gtk-3_0",
        "xbps": "python3-gobject gtk+3", "apk": "py3-gobject3 gtk+3.0",
        "eopkg": "python3-gobject",
    },
    {
        "key": "webkit", "what": "WebKit2GTK",
        "why": "renders the app window and the embedded Steam Workshop",
        "check": ("gi", "WebKit2", "4.1", "4.0"),
        "apt": "gir1.2-webkit2-4.1", "dnf": "webkit2gtk4.1",
        "pacman": "webkit2gtk-4.1", "zypper": "typelib-1_0-WebKit2-4_1",
        "xbps": "webkit2gtk", "apk": "webkit2gtk-4.1", "eopkg": "libwebkit-gtk3",
    },
    {
        "key": "wlr-randr", "what": "wlr-randr",
        "why": "reads your monitor layout on Wayland; only needed there",
        "optional": True,
        "check": ("bin_or_x11", "wlr-randr"),
        "apt": "wlr-randr", "dnf": "wlr-randr", "pacman": "wlr-randr",
        "zypper": "wlr-randr", "xbps": "wlr-randr", "apk": "wlr-randr",
        "eopkg": None,
    },
    {
        "key": "tray", "what": "Tray icon support",
        "why": "the panel icon; optional",
        "optional": True,
        "check": ("gi_any", ("AyatanaAppIndicator3", "0.1"), ("XApp", "1.0")),
        "apt": "gir1.2-ayatanaappindicator3-0.1",
        "dnf": "libayatana-appindicator-gtk3", "pacman": "libayatana-appindicator",
        "zypper": "typelib-1_0-AyatanaAppIndicator3-0_1",
        "xbps": "libayatana-appindicator", "apk": None, "eopkg": None,
    },
]


def distro():
    info = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" not in line:
                    continue
                k, _, v = line.strip().partition("=")
                info[k] = v.strip('"')
    except OSError:
        pass
    ident = (info.get("ID") or "").lower()
    like = (info.get("ID_LIKE") or "").lower().split()

    manager = FAMILIES.get(ident)
    if not manager:
        for candidate in like:
            manager = FAMILIES.get(candidate)
            if manager:
                break
    if not manager:
        # Last resort: whichever manager is actually on PATH.
        for name in MANAGERS:
            if shutil.which(name) or shutil.which(name.replace("xbps", "xbps-install")):
                manager = name
                break

    return {
        "name": info.get("PRETTY_NAME") or info.get("NAME") or "unknown",
        "id": ident,
        "like": like,
        "version": info.get("VERSION_ID", ""),
        "manager": manager,
    }


def _present(check):
    kind = check[0]
    if kind == "bin":
        return bool(shutil.which(check[1]))
    if kind == "bin_unless_wayland":
        # X11-only tools. A Wayland session drives the compositor directly and
        # never calls these, so do not ask anyone to install them.
        from . import desktop
        if desktop.session_type() == desktop.WAYLAND:
            return True
        return bool(shutil.which(check[1]))
    if kind == "bin_or_x11":
        # Wayland-only tools. On X11 there is nothing to install and nothing
        # to warn about, so report them satisfied rather than listing a
        # package the user does not need.
        from . import desktop
        if desktop.session_type() != desktop.WAYLAND:
            return True
        return bool(shutil.which(check[1]))
    if kind == "gi":
        try:
            import gi
            for version in check[2:]:
                try:
                    gi.require_version(check[1], version)
                    return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False
    if kind == "gi_any":
        try:
            import gi
            for name, version in check[1:]:
                try:
                    gi.require_version(name, version)
                    return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False
    return False


def report():
    d = distro()
    manager = d["manager"]
    rows, missing_pkgs = [], []

    for req in REQUIREMENTS:
        ok = _present(req["check"])
        pkg = req.get(manager) if manager else None
        rows.append({
            "key": req["key"],
            "what": req["what"],
            "why": req["why"],
            "ok": ok,
            "optional": bool(req.get("optional")),
            "package": pkg,
            "packaged": pkg is not None,
        })
        if not ok and pkg:
            missing_pkgs.extend(pkg.split())

    command = ""
    if missing_pkgs and manager:
        prefix, _label = MANAGERS[manager]
        command = " ".join(prefix + missing_pkgs)

    # linux-wallpaperengine is built from source everywhere, so it is reported
    # separately rather than pretending a package exists.
    from . import paths
    engine_ok = os.path.exists(paths.ENGINE)

    return {
        "distro": d,
        "requirements": rows,
        "missing": [r["what"] for r in rows if not r["ok"]],
        "missing_required": [r["what"] for r in rows if not r["ok"] and not r["optional"]],
        "install_command": command,
        "engine": {
            "ok": engine_ok,
            "path": paths.ENGINE,
            "note": None if engine_ok else
                "linux-wallpaperengine is built from source on every distro; "
                "there is no package. See github.com/Almamu/linux-wallpaperengine",
        },
    }
