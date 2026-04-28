#!/bin/bash
# YourMove Pi Dispenser — First Boot Setup
# Runs once via systemd.run from cmdline.txt.
# All packages are pre-installed by setup-sd.sh. This script only does config
# that requires a running system (systemd, NetworkManager).
set -euo pipefail

LOG="/var/log/yourmove-firstrun.log"
exec > >(tee -a "$LOG") 2>&1
echo "=== YourMove firstrun.sh starting at $(date) ==="

# ── 1. Mask hostapd and dnsmasq (orchestrator manages them) ──

systemctl stop hostapd dnsmasq 2>/dev/null || true
systemctl disable hostapd dnsmasq 2>/dev/null || true
systemctl mask hostapd

# ── 2. Configure NetworkManager: eth0 static IP for camera segment ──

nmcli connection delete yourmove-camera 2>/dev/null || true
nmcli connection add \
  type ethernet \
  con-name yourmove-camera \
  ifname eth0 \
  ipv4.method manual \
  ipv4.addresses 192.168.50.1/24 \
  ipv4.never-default yes \
  connection.autoconnect yes \
  connection.autoconnect-priority 10

# ── 3. Tell NetworkManager to ignore wlan0 (orchestrator controls it) ──

mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/yourmove-unmanage-wlan.conf <<'NM'
[keyfile]
unmanaged-devices=interface-name:wlan0
NM

systemctl restart NetworkManager

# ── 4. Write initial version ──

if [ -d /opt/yourmove/.git ]; then
    VERSION=$(cd /opt/yourmove && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
else
    VERSION="sd-install-$(date +%Y%m%d)"
fi
echo "$VERSION" > /etc/yourmove/version
echo "Version: $VERSION"

# ── 5. Clean up — prevent re-run ──

CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g' "$CMDLINE"
    echo "Cleaned cmdline.txt"
fi

rm -f /boot/firmware/firstrun.sh
rm -f /boot/firstrun.sh

echo "=== YourMove firstrun.sh complete at $(date) ==="
echo "System will reboot via systemd.run_success_action..."
