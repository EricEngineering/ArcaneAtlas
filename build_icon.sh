#!/usr/bin/env bash
# =============================================================================
# build_icon.sh — regenerate arcaneatlas/resources/icon.ico from icon.png
#
# Produces a MULTI-RESOLUTION Windows .ico (16/24/32/48/64/128/256). Windows,
# macOS and Linux are picky about which sizes are embedded — a single 256x256
# entry (what the old icon.ico had) looks wrong in small contexts like the
# taskbar/Explorer, so we bake the full standard set into one file.
#
# Operates in place: reads arcaneatlas/resources/icon.png, writes icon.ico
# alongside it. Needs ImageMagick (`magick`, or legacy `convert`).
#   Arch:   sudo pacman -S imagemagick
#   Debian: sudo apt install imagemagick
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES_DIR="$SCRIPT_DIR/arcaneatlas/resources"
SRC="$RES_DIR/icon.png"
OUT="$RES_DIR/icon.ico"
SIZES="16,24,32,48,64,128,256"

if [[ ! -f "$SRC" ]]; then
    echo "[build_icon] ERROR: source image not found: $SRC" >&2
    exit 1
fi

# Prefer ImageMagick 7 (`magick`); fall back to the legacy `convert`.
if command -v magick >/dev/null 2>&1; then
    IM=(magick)
elif command -v convert >/dev/null 2>&1; then
    IM=(convert)
else
    echo "[build_icon] ERROR: ImageMagick not found (need 'magick' or 'convert')." >&2
    echo "             Install it, e.g.  sudo pacman -S imagemagick" >&2
    exit 1
fi

echo "[build_icon] ${SRC#"$SCRIPT_DIR"/} -> ${OUT#"$SCRIPT_DIR"/}  (sizes: $SIZES)"

# -background none keeps the alpha channel; icon:auto-resize emits every listed
# size into a single .ico (256 is stored PNG-compressed, the rest as BMP).
"${IM[@]}" "$SRC" -background none -define icon:auto-resize="$SIZES" "$OUT"

echo "[build_icon] wrote $OUT"
if command -v identify >/dev/null 2>&1; then
    echo "[build_icon] embedded images:"
    identify -format '    %wx%h  %[bit-depth]-bit  %m\n' "$OUT"
fi
