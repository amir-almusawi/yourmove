"""ISAPI camera discovery and configuration for Annke C500 / Hikvision OEM."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from requests.auth import HTTPDigestAuth

log = logging.getLogger(__name__)

LEASE_FILE = "/var/lib/misc/dnsmasq.leases"
ISAPI_DEVICE_INFO = "/ISAPI/System/deviceInfo"
FACTORY_USER = "admin"
RTSP_PORT = 554
ISAPI_TIMEOUT = 5


@dataclass
class CameraConfig:
    ip: str
    username: str = ""
    password: str = ""
    configured: bool = False


def _read_leases() -> str:
    try:
        with open(LEASE_FILE, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _parse_leases(text: str, subnet_prefix: str = "192.168.50.") -> list[str]:
    ips = []
    for line in text.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].startswith(subnet_prefix):
            ips.append(parts[2])
    return ips


def _derive_camera_password(hardware_serial: str) -> str:
    clean = hardware_serial.replace("-", "").replace(" ", "")
    return f"ym-{clean[-8:]}"


def _isapi_probe(ip: str) -> str | None:
    """Probe an IP for ISAPI. Returns 'active', 'not_activated', or None."""
    try:
        r = requests.get(
            f"http://{ip}{ISAPI_DEVICE_INFO}",
            timeout=ISAPI_TIMEOUT,
        )
        if "notActivated" in r.text:
            return "not_activated"
        if r.status_code == 401:
            return "active"
        if r.status_code == 200 and "DeviceInfo" in r.text:
            return "active"
        return None
    except Exception:
        return None


def _activate_camera(ip: str, password: str) -> bool:
    """Activate an unactivated Hikvision/ISAPI camera using challenge-response crypto."""
    try:
        from device.pi_dispenser.camera_activate import activate_camera
        return activate_camera(ip, password)
    except Exception as e:
        log.error("Camera activation failed: %s", e)
        return False


def discover() -> str | None:
    lease_text = _read_leases()
    ips = _parse_leases(lease_text)
    if not ips:
        log.warning("No DHCP leases found on eth0 segment")
        return None
    for ip in ips:
        status = _isapi_probe(ip)
        if status in ("active", "not_activated"):
            log.info("Camera discovered via ISAPI at %s (status=%s)", ip, status)
            return ip
    log.warning("No ISAPI camera found among eth0 leases: %s", ips)
    return None


def configure_credentials(ip: str, hardware_serial: str) -> CameraConfig:
    password = _derive_camera_password(hardware_serial)

    status = _isapi_probe(ip)
    if status == "not_activated":
        log.info("Camera not activated — activating with derived password")
        if not _activate_camera(ip, password):
            return CameraConfig(ip=ip, username=FACTORY_USER, password=password, configured=False)

    # Verify we can auth with admin + derived password
    try:
        r = requests.get(
            f"http://{ip}{ISAPI_DEVICE_INFO}",
            auth=HTTPDigestAuth(FACTORY_USER, password),
            timeout=ISAPI_TIMEOUT,
        )
        if r.status_code == 200 and "DeviceInfo" in r.text:
            log.info("Camera credentials verified at %s", ip)
            return CameraConfig(ip=ip, username=FACTORY_USER, password=password, configured=True)
        log.warning("Camera auth check returned %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Failed to verify camera credentials: %s", e)
    return CameraConfig(ip=ip, username=FACTORY_USER, password=password, configured=False)


def get_rtsp_urls(config: CameraConfig) -> dict:
    cred = f"{config.username}:{config.password}"
    base = f"rtsp://{cred}@{config.ip}:{RTSP_PORT}/H.264"
    return {
        "rtsp_main": f"{base}/ch1/main/av_stream",
        "rtsp_sub": f"{base}/ch1/sub/av_stream",
    }


def verify_rtsp(config: CameraConfig) -> bool:
    import subprocess
    url = get_rtsp_urls(config)["rtsp_sub"]
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-rtsp_transport", "tcp",
             "-i", url, "-show_entries", "stream=codec_type",
             "-of", "csv=p=0"],
            capture_output=True, text=True, timeout=10,
        )
        return "video" in result.stdout
    except Exception as e:
        log.warning("RTSP verify failed: %s", e)
        return False
