#!/usr/bin/env bash
# install.sh -- wire wpe-studio into this desktop, from wherever it is cloned.
#
#   ./install.sh              install for the current user
#   ./install.sh --uninstall  take it all back out
#
# Everything goes under $HOME. Nothing here needs root: the only step that ever
# does is installing missing packages, and that is offered separately by the app
# so you can see the command before it runs.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
UNITS="$HOME/.config/systemd/user"
AUTOSTART="$HOME/.config/autostart"
ICONS="$HOME/.local/share/icons/hicolor"

say() { printf '  %s\n' "$*"; }

uninstall() {
  echo "Removing wpe-studio from this user account"
  systemctl --user disable --now wpe-studio.service 2>/dev/null
  rm -f "$UNITS/wpe-studio.service"
  systemctl --user daemon-reload 2>/dev/null
  rm -f "$BIN"/wpe-studio "$BIN"/wpe-apply "$BIN"/wpe-tray "$BIN"/wpe-panel
  rm -f "$APPS/wpe-studio.desktop" "$APPS/wpe-tray.desktop"
  rm -f "$AUTOSTART/wpe-studio.desktop" "$AUTOSTART/wpe-tray.desktop"
  rm -f "$ICONS"/*/status/wpe-studio-tray.png "$ICONS"/scalable/status/wpe-studio-tray.svg
  say "Left alone: your settings in ~/.config/wpe-studio and your wallpapers."
  echo "Done."
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall

echo "Installing wpe-studio from $ROOT"

# --- the commands ------------------------------------------------------------
mkdir -p "$BIN"
for cmd in wpe-studio wpe-apply wpe-tray wpe-panel; do
  ln -sf "$ROOT/bin/$cmd" "$BIN/$cmd"
done
say "commands -> $BIN (wpe-studio, wpe-apply, wpe-tray, wpe-panel)"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "NOTE: $BIN is not on your PATH; add it to use the commands by name." ;;
esac

# --- icon --------------------------------------------------------------------
if [ -f "$ROOT/assets/wpe-tray.png" ]; then
  for size in 22x22 24x24 48x48; do
    mkdir -p "$ICONS/$size/status"
    cp -f "$ROOT/assets/wpe-tray.png" "$ICONS/$size/status/wpe-studio-tray.png"
  done
  gtk-update-icon-cache -f -t "$ICONS" >/dev/null 2>&1
  say "tray icon -> icon theme"
fi

# --- desktop entries ---------------------------------------------------------
mkdir -p "$APPS"
sed "s|__WPE_ROOT__|$ROOT|g" "$ROOT/wpe-studio.desktop" > "$APPS/wpe-studio.desktop"
cat > "$APPS/wpe-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Wallpaper Engine tray icon
Comment=Show the Wallpaper Engine tray icon
Exec=$ROOT/bin/wpe-tray
Icon=$ROOT/assets/wpe-tray.png
Terminal=false
Categories=Graphics;Settings;
EOF
update-desktop-database "$APPS" >/dev/null 2>&1
say "menu entries -> $APPS"

# --- the daemon --------------------------------------------------------------
mkdir -p "$UNITS"
sed "s|__WPE_ROOT__|$ROOT|g" "$ROOT/systemd/wpe-studio.service" > "$UNITS/wpe-studio.service"
systemctl --user daemon-reload 2>/dev/null
if systemctl --user enable --now wpe-studio.service 2>/dev/null; then
  say "daemon -> running on http://127.0.0.1:${WPE_PORT:-8014}"
else
  say "NOTE: could not start the user service; run '$ROOT/bin/wpe-studio --server' by hand."
fi

# --- tray at login -----------------------------------------------------------
mkdir -p "$AUTOSTART"
cat > "$AUTOSTART/wpe-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Wallpaper Engine tray
Exec=$ROOT/bin/wpe-tray
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=10
NoDisplay=true
Terminal=false
EOF
say "tray starts at login"

# --- what is missing ---------------------------------------------------------
echo
python3 - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
try:
    from wpestudio import deps
except Exception as e:
    print("  could not run the dependency check: %s" % e)
    raise SystemExit(0)

r = deps.report()
d = r["distro"]
print("  %s%s" % (d["name"], " (packages via %s)" % d["manager"] if d["manager"] else ""))

for row in r["requirements"]:
    if row["ok"]:
        continue
    tag = "optional" if row["optional"] else "REQUIRED"
    pkg = row["package"] or "no package for this distro"
    print("  missing [%s] %-22s %s" % (tag, row["what"], pkg))

if r["install_command"]:
    print("\n  Install them with:\n    sudo %s" % r["install_command"])

if not r["engine"]["ok"]:
    print("\n  linux-wallpaperengine was not found. It is built from source on every")
    print("  distro -- see github.com/Almamu/linux-wallpaperengine -- then either put")
    print("  it on your PATH or set WPE_ENGINE_BIN.")

if not r["missing"] and r["engine"]["ok"]:
    print("  Everything it needs is installed.")
PY

echo
echo "Done. Start it with:  wpe-studio"
echo "First launch walks you through the rest."
