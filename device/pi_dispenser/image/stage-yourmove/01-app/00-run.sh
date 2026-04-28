#!/bin/bash
# Copy application code and systemd services into the image.
# Runs on HOST with ${ROOTFS_DIR} pointing to the image filesystem.
set -euo pipefail

# The build script symlinks our repo into the pi-gen tree.
# SCRIPT_DIR is this script's directory inside pi-gen.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${SCRIPT_DIR}/repo"

APP_DIR="${ROOTFS_DIR}/opt/yourmove"

# Application code
install -d "${APP_DIR}/device/pi_dispenser/systemd"
install -d "${APP_DIR}/device/pi_dispenser/templates"
install -d "${APP_DIR}/edge"

# device package init
install -m 644 /dev/null "${APP_DIR}/device/__init__.py"

# Pi dispenser module
for f in __init__.py config.py gpio.py camera.py camera_activate.py portal.py dispenser.py button.py orchestrator.py requirements.txt updater.sh; do
    if [ -f "${REPO_DIR}/device/pi_dispenser/$f" ]; then
        install -m 644 "${REPO_DIR}/device/pi_dispenser/$f" "${APP_DIR}/device/pi_dispenser/$f"
    fi
done

# Make updater executable
chmod +x "${APP_DIR}/device/pi_dispenser/updater.sh" 2>/dev/null || true

# Templates
install -m 644 "${REPO_DIR}/device/pi_dispenser/templates/setup.html" \
    "${APP_DIR}/device/pi_dispenser/templates/"

# Edge supervisor
install -m 644 "${REPO_DIR}/edge/edge_supervisor.py" "${APP_DIR}/edge/"

# Systemd services and timers
for f in "${REPO_DIR}/device/pi_dispenser/systemd/"*.service; do
    install -m 644 "$f" "${ROOTFS_DIR}/etc/systemd/system/"
done
for f in "${REPO_DIR}/device/pi_dispenser/systemd/"*.timer; do
    install -m 644 "$f" "${ROOTFS_DIR}/etc/systemd/system/"
done

# Enable services
install -d "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/yourmove-orchestrator.service \
    "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/"
ln -sf /etc/systemd/system/yourmove-button.service \
    "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/"

install -d "${ROOTFS_DIR}/etc/systemd/system/timers.target.wants"
ln -sf /etc/systemd/system/yourmove-updater.timer \
    "${ROOTFS_DIR}/etc/systemd/system/timers.target.wants/"

echo "Application code installed to /opt/yourmove"
