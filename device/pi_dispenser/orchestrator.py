"""Boot flow orchestrator for Pi dispenser. Runs as yourmove-orchestrator.service."""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests as http_requests

from device.pi_dispenser.config import (
    load_config, save_config, has_wifi_creds, is_provisioned,
    get_hardware_serial, CONFIG_PATH,
)
from device.pi_dispenser.camera import discover, configure_credentials, get_rtsp_urls, verify_rtsp

log = logging.getLogger(__name__)

ETH0_IP = "192.168.50.1"
ETH0_NETMASK = "255.255.255.0"
GO2RTC_BIN = "/usr/local/bin/go2rtc"
GO2RTC_CONFIG = "/etc/yourmove/go2rtc.yaml"
DNSMASQ_ETH0_CONF = "/etc/dnsmasq.d/yourmove-eth0.conf"
DNSMASQ_AP_CONF = "/etc/dnsmasq.d/yourmove-ap.conf"
HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kwargs)


def setup_eth0():
    log.info("Configuring eth0: %s/24", ETH0_IP)

    result = _run(["ip", "-4", "-o", "addr", "show", "eth0"])
    if ETH0_IP in result.stdout:
        log.info("eth0 already configured")
    else:
        _run(["ip", "addr", "flush", "dev", "eth0"])
        _run(["ip", "addr", "add", f"{ETH0_IP}/24", "dev", "eth0"])
        _run(["ip", "link", "set", "eth0", "up"])

    Path(DNSMASQ_ETH0_CONF).parent.mkdir(parents=True, exist_ok=True)
    Path(DNSMASQ_ETH0_CONF).write_text(
        "interface=eth0\n"
        "dhcp-range=set:eth0,192.168.50.100,192.168.50.150,255.255.255.0,24h\n"
        "dhcp-option=tag:eth0,3\n"
        "dhcp-option=tag:eth0,6\n"
    )
    Path("/etc/dnsmasq.d/yourmove-base.conf").write_text(
        "bind-dynamic\n"
        "no-resolv\n"
    )
    _run(["systemctl", "unmask", "dnsmasq"])
    _run(["systemctl", "restart", "dnsmasq"])
    time.sleep(2)


def discover_camera(hardware_serial: str, config_path: Path) -> dict | None:
    log.info("Scanning for camera on eth0...")
    ip = discover()
    if not ip:
        log.warning("Camera not found on eth0 segment")
        return None

    log.info("Camera found at %s — configuring credentials", ip)
    cam_config = configure_credentials(ip, hardware_serial)
    if not cam_config.configured:
        log.warning("Camera at %s failed credential setup", ip)
        return None

    urls = get_rtsp_urls(cam_config)
    camera_data = {
        "ip": ip,
        "rtsp_main": urls["rtsp_main"],
        "rtsp_sub": urls["rtsp_sub"],
        "configured": True,
    }

    cfg = load_config(config_path)
    cfg["camera"] = camera_data
    save_config(cfg, config_path)
    log.info("Camera configured: %s", ip)
    return camera_data


def write_go2rtc_config(camera: dict, port: int = 8554):
    go2rtc_cfg = {
        "streams": {
            "main": camera["rtsp_main"],
            "sub": camera["rtsp_sub"],
        },
        "rtsp": {"listen": f":{port}"},
        "api": {"listen": ":1984"},
    }
    Path(GO2RTC_CONFIG).parent.mkdir(parents=True, exist_ok=True)
    Path(GO2RTC_CONFIG).write_text(json.dumps(go2rtc_cfg, indent=2))
    log.info("go2rtc config written to %s", GO2RTC_CONFIG)


def fetch_publish_ingress(cfg: dict) -> dict:
    """Fetch RTMP publish credentials from the platform."""
    base_url = cfg.get("base_url", "https://yourmove.live")
    node_slug = cfg.get("node_slug", "")
    auth_token = cfg.get("auth_token", "")
    if not node_slug or not auth_token:
        log.warning("Cannot fetch publish ingress: missing node_slug or auth_token")
        return {}
    try:
        r = http_requests.post(
            f"{base_url}/api/nodes/{node_slug}/operator/publish-ingress",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"input_type": "rtmp"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                log.info("Publish ingress: rtmp_url=%s", data.get("rtmp_url", "")[:60])
                return data
        log.warning("Publish ingress request returned %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Failed to fetch publish ingress: %s", e)
    return {}


def write_edge_config(cfg: dict):
    ingress = fetch_publish_ingress(cfg)

    edge_cfg = {
        "node_slug": cfg["node_slug"],
        "rtsp_url": cfg["camera"]["rtsp_sub"],
        "rtsp_main_url": cfg["camera"]["rtsp_main"],
        "base_url": cfg.get("base_url", "https://yourmove.live"),
        "auth_token": cfg.get("auth_token", ""),
        "go2rtc_port": cfg.get("go2rtc_port", 8554),
        "go2rtc_api_port": 1984,
        "probe_interval_seconds": 15,
        "state_dir": "/var/lib/yourmove-edge",
        "cv_enabled": False,
        "publisher_mode": "process",
        "rtmp_url": ingress.get("rtmp_url", ""),
        "rtmp_key": ingress.get("rtmp_key", ""),
    }
    edge_path = Path("/etc/yourmove/edge.json")
    edge_path.write_text(json.dumps(edge_cfg, indent=2))
    log.info("Edge supervisor config written to %s", edge_path)


def _get_wlan0_ip() -> str:
    try:
        result = _run(["ip", "-4", "-o", "addr", "show", "wlan0"])
        for line in result.stdout.splitlines():
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "inet":
                    return parts[i + 1].split("/")[0]
    except Exception:
        pass
    return "0.0.0.0"


def _setup_captive_nft():
    """Redirect all DNS and HTTP on wlan0 to the portal so captive detection works
    even when clients use private DNS (DoT/DoH)."""
    nft_rules = """
table ip yourmove_captive {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        iifname "wlan0" udp dport 53 dnat to 192.168.4.1:53
        iifname "wlan0" tcp dport 53 dnat to 192.168.4.1:53
        iifname "wlan0" tcp dport 80 dnat to 192.168.4.1:80
        iifname "wlan0" tcp dport 443 dnat to 192.168.4.1:443
    }
}
"""
    subprocess.run(
        ["nft", "delete", "table", "ip", "yourmove_captive"],
        capture_output=True, text=True, timeout=5,
    )
    result = subprocess.run(
        ["nft", "-f", "-"],
        input=nft_rules, capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        log.info("Captive portal NAT rules installed")
    else:
        log.warning("Failed to install NAT rules: %s", result.stderr.strip())


def _scan_wifi_networks() -> list[str]:
    """Scan for nearby WiFi networks while wlan0 is still in managed mode."""
    try:
        _run(["rfkill", "unblock", "wifi"])
        _run(["ip", "link", "set", "wlan0", "up"])
        time.sleep(2)
        result = subprocess.run(
            ["iwlist", "wlan0", "scan"],
            capture_output=True, text=True, timeout=15,
        )
        networks = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("ESSID:"):
                name = line.split(":", 1)[1].strip('"')
                if name and name not in networks:
                    networks.append(name)
        log.info("WiFi scan found %d networks", len(networks))
        return sorted(networks)
    except Exception as e:
        log.warning("WiFi scan failed: %s", e)
        return []


def start_ap_mode(hardware_serial: str, camera_detected: bool, config_path: Path):
    suffix = hardware_serial.replace("-", "")[-4:].upper()
    ssid = f"YourMove-Setup-{suffix}"
    log.info("Starting AP mode: %s", ssid)

    nearby_networks = _scan_wifi_networks()

    Path(HOSTAPD_CONF).parent.mkdir(parents=True, exist_ok=True)
    Path(HOSTAPD_CONF).write_text(
        f"interface=wlan0\n"
        f"driver=nl80211\n"
        f"ssid={ssid}\n"
        f"hw_mode=g\n"
        f"channel=7\n"
        f"wmm_enabled=0\n"
        f"auth_algs=1\n"
        f"wpa=0\n"
    )

    Path(DNSMASQ_AP_CONF).parent.mkdir(parents=True, exist_ok=True)
    Path(DNSMASQ_AP_CONF).write_text(
        "interface=wlan0\n"
        "dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h\n"
        "dhcp-option=3,192.168.4.1\n"
        "dhcp-option=6,192.168.4.1\n"
        "address=/#/192.168.4.1\n"
    )

    subprocess.run(["killall", "wpa_supplicant"], capture_output=True, timeout=5)
    time.sleep(1)
    _run(["rfkill", "unblock", "wifi"])
    _run(["ip", "addr", "flush", "dev", "wlan0"])
    _run(["ip", "addr", "add", "192.168.4.1/24", "dev", "wlan0"])
    _run(["ip", "link", "set", "wlan0", "up"])
    _run(["systemctl", "unmask", "dnsmasq"])
    _run(["systemctl", "restart", "dnsmasq"])
    _run(["systemctl", "unmask", "hostapd"])
    _run(["systemctl", "restart", "hostapd"])

    _setup_captive_nft()

    from device.pi_dispenser.portal import create_app

    def on_provisioned():
        log.info("Provisioning complete — scheduling reboot")
        _run(["systemctl", "stop", "hostapd"])
        time.sleep(1)
        _run(["systemctl", "reboot"])

    app = create_app(
        config_path=config_path,
        hardware_serial=hardware_serial,
        camera_detected=camera_detected,
        networks=nearby_networks,
        on_provisioned=on_provisioned,
    )
    log.info("Captive portal listening on 0.0.0.0:80")
    app.run(host="0.0.0.0", port=80, debug=False)


def connect_wifi(ssid: str, password: str) -> bool:
    log.info("Connecting to WiFi: %s", ssid)

    _run(["rfkill", "unblock", "wifi"])
    _run(["ip", "link", "set", "wlan0", "up"])

    wpa_conf = Path("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf")
    wpa_conf.parent.mkdir(parents=True, exist_ok=True)
    wpa_conf.write_text(
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
        "update_config=1\n"
        "country=US\n"
        f'\nnetwork={{\n  ssid="{ssid}"\n  psk="{password}"\n}}\n'
    )

    subprocess.run(["killall", "wpa_supplicant"], capture_output=True, timeout=5)
    time.sleep(1)
    _run(["wpa_supplicant", "-B", "-i", "wlan0", "-c", str(wpa_conf)])

    for attempt in range(30):
        time.sleep(1)
        result = _run(["wpa_cli", "-i", "wlan0", "status"])
        if "wpa_state=COMPLETED" in result.stdout:
            _run(["dhclient", "-1", "wlan0"])
            ip = _get_wlan0_ip()
            log.info("WiFi connected: %s (IP: %s)", ssid, ip)
            return True

    log.error("WiFi connection failed after 30s")
    return False


PROVISION_URL = "https://yourmove.live/api/device/provision"


def call_provision_api(
    hardware_serial: str, claim_code: str, config_path: Path,
) -> bool:
    """Call the platform provision API and save credentials to config.
    Returns True on success."""
    log.info("Calling provision API (serial=%s, claim=%s)", hardware_serial, claim_code or "(none)")
    try:
        payload = {
            "hardware_serial": hardware_serial,
            "firmware_version": "pi-dispenser-0.1.0",
            "runtime_type": "pi_gateway",
        }
        if claim_code:
            payload["claim_code"] = claim_code
        r = http_requests.post(PROVISION_URL, json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Provision API returned %s: %s", r.status_code, r.text[:200])
            return False
        data = r.json()
        if not data.get("ok"):
            log.error("Provision API returned ok=false: %s", data)
            return False
        if not data.get("node_slug"):
            log.error("Provision API response missing node_slug: %s", data)
            return False

        cfg = load_config(config_path)
        cfg["node_slug"] = data["node_slug"]
        cfg["platform_node_id"] = data.get("platform_node_id", 0)
        cfg["mqtt_node_id"] = data.get("platform_node_id", 0)
        cfg["mqtt_username"] = data.get("mqtt_username", "")
        cfg["mqtt_password"] = data.get("mqtt_password", "")
        cfg["auth_token"] = data.get("auth_token", "")
        cfg["base_url"] = data.get("base_url", "https://yourmove.live")
        cfg["api_provisioned"] = True
        save_config(cfg, config_path)
        log.info("Provisioned as node %s", data["node_slug"])
        return True
    except Exception as e:
        log.error("Provision API call failed: %s", e)
        return False


def enable_runtime_services():
    services = ["yourmove-go2rtc", "yourmove-device", "yourmove-edge"]
    for svc in services:
        _run(["systemctl", "enable", svc])
        _run(["systemctl", "start", svc])
        log.info("Started %s", svc)


def monitor_wifi(cfg: dict, config_path: Path):
    log.info("Monitoring WiFi health...")
    while True:
        time.sleep(30)
        result = _run(["wpa_cli", "-i", "wlan0", "status"])
        if "wpa_state=COMPLETED" not in result.stdout:
            log.warning("WiFi disconnected — reconnecting...")
            connect_wifi(cfg["wifi_ssid"], cfg["wifi_password"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [orchestrator] %(message)s")
    log.info("YourMove Pi Dispenser Orchestrator starting")

    hardware_serial = get_hardware_serial()
    log.info("Hardware serial: %s", hardware_serial)

    cfg = load_config(CONFIG_PATH)
    if not cfg["hardware_serial"]:
        cfg["hardware_serial"] = hardware_serial
        save_config(cfg, CONFIG_PATH)

    setup_eth0()
    time.sleep(3)

    camera = discover_camera(hardware_serial, CONFIG_PATH)
    camera_detected = camera is not None

    if not has_wifi_creds(CONFIG_PATH):
        log.info("No WiFi credentials — entering AP mode")
        start_ap_mode(hardware_serial, camera_detected, CONFIG_PATH)
        return

    log.info("WiFi credentials found — connecting")
    cfg = load_config(CONFIG_PATH)

    if not connect_wifi(cfg["wifi_ssid"], cfg["wifi_password"]):
        log.error("WiFi failed — entering AP mode for reconfiguration")
        start_ap_mode(hardware_serial, camera_detected, CONFIG_PATH)
        return

    if not is_provisioned(CONFIG_PATH):
        claim_code = cfg.get("claim_code", "")
        if not call_provision_api(hardware_serial, claim_code, CONFIG_PATH):
            log.error("Provision API failed — entering AP mode for reconfiguration")
            cfg["provisioned"] = False
            cfg["wifi_ssid"] = ""
            cfg["wifi_password"] = ""
            save_config(cfg, CONFIG_PATH)
            start_ap_mode(hardware_serial, camera_detected, CONFIG_PATH)
            return
        cfg = load_config(CONFIG_PATH)

    if camera_detected:
        write_go2rtc_config(camera, cfg.get("go2rtc_port", 8554))

    write_edge_config(cfg)
    enable_runtime_services()
    monitor_wifi(cfg, CONFIG_PATH)


if __name__ == "__main__":
    main()
