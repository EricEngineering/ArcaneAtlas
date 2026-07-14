#!/usr/bin/env bash
# =================================
# ArcaneAtlas Linux Launcher
# =================================

echo "Launching ArcaneAtlas..."

# Activate virtualenv if it exists
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Run the module
python3 -m arcaneatlas
status=$?

if [ $status -ne 0 ]; then
    echo
    echo "[ERROR] ArcaneAtlas exited with code $status"
fi