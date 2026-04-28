#!/bin/bash
# YourMove OTA updater — standalone, no Python dependencies.
# Triggered by flag file written by edge supervisor.
set -euo pipefail

FLAG="/tmp/yourmove-update-requested"
REPO="/opt/yourmove"
VERSION_FILE="/etc/yourmove/version"
LOG_TAG="yourmove-updater"

log() { logger -t "$LOG_TAG" "$1"; echo "$1"; }

if [ ! -f "$FLAG" ]; then
    exit 0
fi

log "Update requested — pulling latest code"

cd "$REPO"
BEFORE=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

if ! git pull origin main 2>&1; then
    log "git pull failed — will retry next tick"
    exit 1
fi

AFTER=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

if [ "$BEFORE" = "$AFTER" ]; then
    log "Already up to date ($AFTER) — clearing flag"
    rm -f "$FLAG"
    exit 0
fi

log "Updated $BEFORE -> $AFTER — restarting services"

cp "$REPO/device/pi_dispenser/systemd/"*.service /etc/systemd/system/ 2>/dev/null || true
cp "$REPO/device/pi_dispenser/systemd/"*.timer /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload

systemctl restart yourmove-device yourmove-edge yourmove-go2rtc yourmove-orchestrator 2>/dev/null || true

echo "$AFTER" > "$VERSION_FILE"
rm -f "$FLAG"
log "Update complete — now running $AFTER"
