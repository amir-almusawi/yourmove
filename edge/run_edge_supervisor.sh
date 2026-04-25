#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-$HOME/.config/yourmove-edge/chicken-blaster.json}"
LOG_DIR="${HOME}/.local/state/yourmove-edge"
mkdir -p "$LOG_DIR"

exec /usr/bin/env python3 /home/amir/websites/yourmove/edge/edge_supervisor.py --config "$CONFIG_PATH" >>"$LOG_DIR/$(basename "${CONFIG_PATH%.json}").log" 2>&1
