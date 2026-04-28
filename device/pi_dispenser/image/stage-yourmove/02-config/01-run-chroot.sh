#!/bin/bash
# System configuration — runs inside the chroot (as the image's root).
set -euo pipefail

# ── 1. Mask hostapd and dnsmasq (orchestrator manages them at runtime) ──

systemctl disable hostapd dnsmasq 2>/dev/null || true
systemctl mask hostapd

# ── 2. NetworkManager: eth0 static IP for camera segment ──
# nmcli isn't available in chroot, so write the connection file directly.

install -d /etc/NetworkManager/system-connections
cat > /etc/NetworkManager/system-connections/yourmove-camera.nmconnection <<'NM'
[connection]
id=yourmove-camera
type=ethernet
interface-name=eth0
autoconnect=true
autoconnect-priority=10

[ipv4]
method=manual
addresses=192.168.50.1/24
never-default=true

[ipv6]
method=disabled
NM
chmod 600 /etc/NetworkManager/system-connections/yourmove-camera.nmconnection

# ── 3. Tell NetworkManager to ignore wlan0 (orchestrator controls it) ──

install -d /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/yourmove-unmanage-wlan.conf <<'NM'
[keyfile]
unmanaged-devices=interface-name:wlan0
NM

# ── 4. Create config directories ──

install -d /etc/yourmove
install -d /var/lib/yourmove-edge

# ── 5. Generate self-signed TLS cert for captive portal HTTPS redirect ──

openssl req -x509 -newkey rsa:2048 \
    -keyout /etc/yourmove/portal.key \
    -out /etc/yourmove/portal.crt \
    -days 3650 -nodes -subj /CN=portal.yourmove.local 2>/dev/null
cat /etc/yourmove/portal.key /etc/yourmove/portal.crt > /etc/yourmove/portal.pem
chmod 600 /etc/yourmove/portal.key /etc/yourmove/portal.pem

# ── 6. Write version stamp ──

echo "image-$(date +%Y%m%d)" > /etc/yourmove/version

# ── 7. Add SSH key for debug access ──
# The pi user is created by pi-gen. Add authorized_keys for passwordless SSH.

install -d -m 700 /home/pi/.ssh
if [ -f /tmp/yourmove-ssh-key.pub ]; then
    install -m 600 /tmp/yourmove-ssh-key.pub /home/pi/.ssh/authorized_keys
    chown -R 1000:1000 /home/pi/.ssh
    rm /tmp/yourmove-ssh-key.pub
fi

echo "System configuration complete"
