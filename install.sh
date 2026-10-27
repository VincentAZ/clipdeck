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

missing=()      # things Clipdeck cannot run without
packages=()     # the apt packages that provide them
command -v ffmpeg  >/dev/null || { missing+=(ffmpeg);  packages+=(ffmpeg); }
command -v ffprobe >/dev/null || { missing+=(ffprobe); packages+=(ffmpeg); }
python3 - <<'PY' || { missing+=("GTK4 + libadwaita >= 1.7 Python bindings"); packages+=(python3-gi gir1.2-adw-1); }
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 7)
PY
# The trim timeline and crop overlay draw with cairo; without this bridge every draw
# raises and both tools stay blank.
python3 - <<'PY' || { missing+=("PyGObject cairo bridge"); packages+=(python3-gi-cairo); }
import gi
gi.require_foreign("cairo")
PY
# Previews play through GStreamer (ffmpeg only does the processing). Without the libav
# plugin, H.264/AAC clips convert fine but show a notice instead of a preview. The app
# can install this itself later, but doing it here means it just works on first launch.
decoders_ok=1
if ! PYTHONPATH="$HERE" python3 -c "import sys; from clipdeck import decoders; sys.exit(1 if decoders.common_missing() else 0)" 2>/dev/null; then
    decoders_ok=0
    packages+=(gstreamer1.0-libav gstreamer1.0-plugins-bad)
fi

if [ ${#packages[@]} -gt 0 ]; then
    pkgs=$(printf '%s\n' "${packages[@]}" | sort -u | tr '\n' ' ')
    [ ${#missing[@]} -gt 0 ] && echo "Missing: ${missing[*]}" >&2
    [ $decoders_ok = 0 ] && echo "No GStreamer decoder for H.264/AAC: video previews would not play." >&2
    if command -v apt-get >/dev/null && [ -t 0 ]; then
        read -r -p "Install them now with sudo apt (${pkgs})? [Y/n] " answer
        case "${answer:-Y}" in
            [Yy]*) sudo apt-get install -y $pkgs ;;
            *) echo "Skipped. Install later with: sudo apt install $pkgs" >&2
               [ ${#missing[@]} -gt 0 ] && exit 1 ;;
        esac
    else
        echo "On Ubuntu/Debian: sudo apt install $pkgs" >&2
        [ ${#missing[@]} -gt 0 ] && exit 1
    fi
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
