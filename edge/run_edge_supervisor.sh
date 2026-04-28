#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-$HOME/.config/yourmove-edge/chicken-blaster.json}"
LOG_DIR="${HOME}/.local/state/yourmove-edge"
mkdir -p "$LOG_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/env python3 "$SCRIPT_DIR/edge_supervisor.py" --config "$CONFIG_PATH" >>"$LOG_DIR/$(basename "${CONFIG_PATH%.json}").log" 2>&1
