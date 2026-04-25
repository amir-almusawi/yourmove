#!/usr/bin/env bash
set -euo pipefail

INSTANCE="${1:-chicken-blaster}"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "$UNIT_DIR"

# Purge any legacy non-templated unit for this instance. Two supervisors on the
# same config fight over the shared ffmpeg segment dir and double-restart the
# docker publisher.
LEGACY_UNIT="yourmove-edge-${INSTANCE}.service"
if systemctl --user list-unit-files "$LEGACY_UNIT" --no-legend 2>/dev/null | grep -q "$LEGACY_UNIT"; then
    echo "Removing legacy unit $LEGACY_UNIT"
    systemctl --user disable --now "$LEGACY_UNIT" || true
    rm -f "$UNIT_DIR/$LEGACY_UNIT"
fi

install -m 0644 "/home/amir/websites/yourmove/edge/systemd/yourmove-edge@.service" "$UNIT_DIR/yourmove-edge@.service"
systemctl --user daemon-reload
systemctl --user enable --now "yourmove-edge@${INSTANCE}.service"
systemctl --user restart "yourmove-edge@${INSTANCE}.service"
systemctl --user --no-pager --full status "yourmove-edge@${INSTANCE}.service" | sed -n '1,60p'
