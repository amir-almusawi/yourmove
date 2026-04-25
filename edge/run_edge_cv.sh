#!/usr/bin/env bash
# edge/run_edge_cv.sh — Run the edge CV runtime
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${1:?Usage: $0 <config.json>}"

if ! python3 -c "import ultralytics" 2>/dev/null; then
    echo "Installing CV dependencies..."
    pip install -r "$SCRIPT_DIR/requirements-cv.txt"
fi

exec python3 "$SCRIPT_DIR/edge_cv_runtime.py" --config "$CONFIG"
