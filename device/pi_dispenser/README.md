# Pi Treat Dispenser

Flashable Raspberry Pi 3B runtime for the YourMove platform. Non-technical user plugs it in, connects to the WiFi setup portal from their phone, enters WiFi credentials and a claim code, and the node is live.

## Hardware

- Raspberry Pi 3B (ARMv7, 1GB RAM)
- Annke C500 IP camera, ethernet to Pi
- Relay on GPIO 24 — treat dispenser (active high)
- Relay on GPIO 25 — camera power cycle (active high, normally closed)
- Momentary button on GPIO 17 — reset (active low, uses internal pull-up)

## Wiring

```
Pi GPIO 24  →  Relay IN (dispenser solenoid/motor)
Pi GPIO 25  →  Relay IN (camera power, normally closed)
Pi GPIO 17  →  Button  →  GND
Pi ETH0     →  Annke C500 ethernet port
```

No external resistor needed on GPIO 17 — the code enables the Pi's internal pull-up.

## Flash an SD Card

### 1. Write Raspberry Pi OS Lite

Use Raspberry Pi Imager or `dd`:

- Image: **Raspberry Pi OS Lite (Bookworm, 32-bit)**
- No desktop, no recommended software
- In Imager advanced settings: enable SSH, set username `pi`, set a password

### 2. Copy device code to boot partition

After writing the image, mount the boot partition and copy the code:

```bash
# Mount the boot partition (usually /dev/sdX1)
# Copy the entire repo to a folder on the boot partition
mkdir -p /mnt/boot/yourmove-code
cp -r device/ edge/ /mnt/boot/yourmove-code/
```

### 3. Copy firstrun.sh to boot partition

```bash
cp device/pi_dispenser/firstrun.sh /mnt/boot/firstrun.sh
```

For Pi OS Bookworm, the boot partition may be at `/boot/firmware/` on the Pi itself. The firstrun script handles both locations.

### 4. Enable firstrun on boot

Edit `/mnt/boot/cmdline.txt` — append to the end of the single line:

```
systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot
```

Or use the Pi OS `firstrun.sh` convention if your image supports it.

### 5. Eject, insert, power on

The Pi will:
1. Boot into Pi OS
2. Run `firstrun.sh` — installs packages, downloads go2rtc, copies code, configures systemd
3. Reboot
4. Orchestrator starts, configures eth0, scans for camera
5. Enters AP mode (not yet provisioned)

## Provision the Device

### 1. Create a claim code

In the YourMove dashboard, create a new node and generate a claim code (format: `YM-ABCD1234`).

### 2. Connect to the Pi's WiFi

On your phone, connect to the WiFi network:

```
YourMove-Setup-XXXX
```

(Last 4 characters are from the Pi's hardware serial. No password.)

### 3. Open the setup portal

Your phone should auto-open the captive portal. If not, browse to `http://192.168.4.1`.

The portal shows:
- Hardware serial
- Camera detection status
- WiFi network dropdown (scanned)
- Manual SSID field (for hidden networks)
- WiFi password field
- Claim code field

### 4. Fill in credentials and submit

- Select your home WiFi network
- Enter the WiFi password
- Enter the claim code (or scan the QR code to pre-fill it)
- Hit "Provision Device"

The portal will:
1. Test the WiFi connection
2. Call the provisioning API
3. Save config to `/etc/yourmove/config.json`
4. Reboot

### 5. Done

After reboot, the Pi:
- Connects to your home WiFi
- Starts go2rtc (relays camera RTSP to `{Pi IP}:8554`)
- Starts the MQTT device client (online on the dashboard, accepts commands)
- Starts the edge supervisor (health monitoring, clip capture)
- The dashboard shows the node as online

## QR Code Provisioning

For faster provisioning, generate a QR code encoding:

```
http://192.168.4.1/?claim=YM-ABCD1234
```

Stick it on the device. User connects to the AP WiFi, scans the QR, portal opens with claim code pre-filled — just needs WiFi credentials.

## Reset Button

- **Hold 5 seconds:** Clears WiFi credentials, reboots into AP mode. Claim code and node identity are preserved — just re-enter WiFi credentials.
- **To factory reset:** Reflash the SD card.

## Network Architecture

```
                    Home WiFi (wlan0)
                         |
    [Phone/Dashboard] ←--+-→ [Pi 3B] ←--eth0-→ [Annke C500]
                         |      |                192.168.50.x
                    MQTT broker  |
                    yourmove.live |
                                 |
                         go2rtc relay
                    rtsp://{pi-ip}:8554/stream
                                 |
                    [CV machine on LAN]
```

- **eth0** — Dedicated camera segment. Pi is `192.168.50.1`, runs DHCP for camera.
- **wlan0** — Home WiFi. All platform communication goes over WiFi.
- **go2rtc** — Relays camera RTSP from eth0 to LAN on port 8554. CV machines connect here.

## Camera Auto-Configuration

The Pi auto-configures the Annke C500 on first boot:

1. Reads dnsmasq DHCP leases on eth0
2. Probes each IP with ISAPI (`/ISAPI/System/deviceInfo`)
3. Sets camera credentials: username `yourmove`, password `ym-{last 8 of Pi serial}`
4. Saves RTSP URLs to config

If the camera is factory-reset, the Pi detects auth failure on next boot and re-configures automatically.

## Files

```
device/pi_dispenser/
  orchestrator.py     # Boot flow manager (runs as systemd service)
  dispenser.py        # MQTT device client
  camera.py           # ISAPI camera discovery + configuration
  portal.py           # Captive portal Flask server
  button.py           # Reset button GPIO monitor
  config.py           # Config read/write (/etc/yourmove/config.json)
  gpio.py             # Relay adapter (GPIO 24 + 25)
  firstrun.sh         # First-boot setup script
  requirements.txt    # Python deps
  systemd/            # 5 systemd service files
  templates/
    setup.html        # Captive portal page
```

## Systemd Services

| Service | Purpose | Starts |
|---------|---------|--------|
| `yourmove-orchestrator` | Boot flow, network, camera | Always (on boot) |
| `yourmove-go2rtc` | RTSP relay | After provisioned + camera configured |
| `yourmove-device` | MQTT client | After provisioned + WiFi connected |
| `yourmove-edge` | Edge supervisor | After go2rtc ready |
| `yourmove-button` | Reset button | Always (on boot) |

Check status: `systemctl status yourmove-orchestrator`

View logs: `journalctl -u yourmove-orchestrator -f`

## Troubleshooting

**Pi stays in AP mode after provisioning:**
Check `/var/log/yourmove-firstrun.log` for setup errors. Check `journalctl -u yourmove-orchestrator` for boot flow issues.

**Camera not detected:**
Make sure the camera is plugged into the Pi's ethernet port (not a switch). Check `cat /var/lib/misc/dnsmasq.leases` for DHCP leases on eth0.

**Dashboard shows node offline:**
Check WiFi: `wpa_cli -i wlan0 status`. Check MQTT: `journalctl -u yourmove-device -f`.

**go2rtc not streaming:**
Check `journalctl -u yourmove-go2rtc -f`. Verify camera RTSP: `ffprobe rtsp://yourmove:ym-XXXXXXXX@192.168.50.100:554/H.264/ch1/sub/av_stream`.

**Need to change WiFi:**
Hold the reset button for 5 seconds. Pi reboots into AP mode.
