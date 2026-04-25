#!/usr/bin/env bash
# edge/setup_cv.sh — One-command CV setup: install deps, enable in config, restart supervisor
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${1:-$HOME/.config/yourmove-edge/chicken-blaster.json}"

if [ ! -f "$CONFIG" ]; then
    echo "Config not found: $CONFIG"
    exit 1
fi

# CV runtime needs Python 3.10+ (torch/ultralytics dropped 3.8 support)
CV_PYTHON=$(which python3.12 python3.11 python3.10 2>/dev/null | head -1)
if [ -z "$CV_PYTHON" ]; then
    echo "ERROR: Python 3.10+ required for CV runtime. Install python3.10 or newer."
    exit 1
fi
echo "=== Edge CV Setup (using $CV_PYTHON) ==="

# 1. Install CV dependencies
echo "[1/3] Installing CV dependencies..."
if "$CV_PYTHON" -c "import ultralytics" 2>/dev/null; then
    echo "  Already installed."
else
    "$CV_PYTHON" -m pip install -r "$SCRIPT_DIR/requirements-cv.txt"
fi

# 2. Enable cv_enabled in config
echo "[2/3] Enabling cv_enabled in config..."
if python3 -c "import json; c=json.load(open('$CONFIG')); exit(0 if c.get('cv_enabled') else 1)" 2>/dev/null; then
    echo "  Already enabled."
else
    python3 -c "
import json, pathlib
p = pathlib.Path('$CONFIG')
c = json.loads(p.read_text())
c['cv_enabled'] = True
p.write_text(json.dumps(c, indent=2) + '\n')
print('  Enabled.')
"
fi

# 3. Restart supervisor
echo "[3/3] Restarting edge supervisor..."
pkill -f "edge_cv_runtime.py" 2>/dev/null || true
SUPERVISOR_PID=$(pgrep -f "edge_supervisor.py" || true)
if [ -n "$SUPERVISOR_PID" ]; then
    kill "$SUPERVISOR_PID" 2>/dev/null || true
    sleep 2
    echo "  Stopped old supervisor (PID $SUPERVISOR_PID)."
fi

STATE_DIR="${HOME}/.local/state/yourmove-edge"
rm -f "$STATE_DIR/$(basename "${CONFIG%.json}").lock"

LOG_FILE="$STATE_DIR/$(basename "${CONFIG%.json}").log"
cd "$SCRIPT_DIR/.."
nohup bash edge/run_edge_supervisor.sh "$CONFIG" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
sleep 3

if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "  Supervisor started (PID $NEW_PID)."
else
    echo "  WARNING: Supervisor may not have started. Check $LOG_FILE"
fi

echo ""
echo "=== Done ==="
echo "CV runtime will start automatically. Watch logs:"
echo "  tail -f $LOG_FILE"
echo ""
echo "To check CV status from the dashboard: open the Game tab → Edge CV Status section."
