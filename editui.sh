#!/usr/bin/env bash
# =============================================================================
# editui.sh — open arcaneatlas/mainwindow.ui in Qt Designer (PySide6).
#
# After editing and saving in Designer, regenerate the Python UI:
#     ./build_ui.sh        (runs pyside6-uic … --from-imports)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
UI="arcaneatlas/mainwindow.ui"

if [[ ! -f "$UI" ]]; then
    echo "[editui] ERROR: $UI not found." >&2
    exit 1
fi

# Prefer the project venv's designer; fall back to one on PATH.
if [[ -x ".venv/bin/pyside6-designer" ]]; then
    DESIGNER=".venv/bin/pyside6-designer"
elif command -v pyside6-designer >/dev/null 2>&1; then
    DESIGNER="pyside6-designer"
else
    echo "[editui] ERROR: pyside6-designer not found (is PySide6 installed / .venv present?)." >&2
    exit 1
fi

echo "[editui] Opening $UI in Qt Designer…"
echo "[editui] When done, run ./build_ui.sh to regenerate arcaneatlas/ui_mainwindow.py."
exec "$DESIGNER" "$UI"
