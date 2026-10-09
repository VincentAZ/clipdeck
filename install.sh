#!/usr/bin/env bash
# Install Clipdeck for the current user: launcher, menu entry, icon.
# Runs from this checkout (no copying), so `git pull` updates the installed app.
# ./install.sh --uninstall removes those three files again.
set -euo pipefail

APP_ID=local.clipdeck.Clipdeck
HERE=$(cd "$(dirname "$0")" && pwd)
BIN_DIR=${XDG_BIN_HOME:-$HOME/.local/bin}
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
BIN=$BIN_DIR/clipdeck
DESKTOP=$DATA/applications/$APP_ID.desktop
ICON=$DATA/icons/hicolor/scalable/apps/$APP_ID.svg

refresh() {
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" || true
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$BIN" "$DESKTOP" "$ICON"
    refresh
    echo "Clipdeck uninstalled (settings in ~/.config/clipdeck and cache in ~/.cache/clipdeck kept)."
    exit 0
fi

missing=()
command -v ffmpeg  >/dev/null || missing+=(ffmpeg)
command -v ffprobe >/dev/null || missing+=(ffprobe)
python3 - <<'PY' || missing+=("python3-gi gir1.2-adw-1 (GTK4 + libadwaita >= 1.7)")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 7)
PY
if [ ${#missing[@]} -gt 0 ]; then
    echo "Missing: ${missing[*]}" >&2
    echo "On Ubuntu: sudo apt install ffmpeg python3-gi gir1.2-adw-1 gstreamer1.0-libav" >&2
    exit 1
fi

mkdir -p "$BIN_DIR" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
cat > "$BIN" <<SH
#!/usr/bin/env bash
export PYTHONPATH="$HERE\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m clipdeck "\$@"
SH
chmod +x "$BIN"
sed "s|@BIN@|$BIN|" "$HERE/data/$APP_ID.desktop.in" > "$DESKTOP"
cp "$HERE/data/$APP_ID.svg" "$ICON"
refresh
echo "Installed. Launch Clipdeck from the app grid, or run: clipdeck [files...]"
