"""Config read/write for Pi dispenser — atomic JSON file at /etc/yourmove/config.json."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONFIG_PATH = Path("/etc/yourmove/config.json")

DEFAULT_CONFIG: dict = {
    "hardware_serial": "",
    "provisioned": False,
    "wifi_ssid": "",
    "wifi_password": "",
    "claim_code": "",
    "node_slug": "",
    "mqtt_host": "yourmove.live",
    "mqtt_port": 1883,
    "mqtt_node_id": 0,
    "mqtt_username": "",
    "mqtt_password": "",
    "auth_token": "",
    "base_url": "https://yourmove.live",
    "camera": {
        "ip": "",
        "rtsp_main": "",
        "rtsp_sub": "",
        "configured": False,
    },
    "go2rtc_port": 8554,
}

_cached_serial: str | None = None


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = {**DEFAULT_CONFIG, "camera": {**DEFAULT_CONFIG["camera"]}}
    if path.exists():
        with open(path, "r") as f:
            stored = json.load(f)
        cfg.update(stored)
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_wifi(path: Path = CONFIG_PATH) -> None:
    cfg = load_config(path)
    cfg["wifi_ssid"] = ""
    cfg["wifi_password"] = ""
    cfg["provisioned"] = False
    save_config(cfg, path)


def factory_reset(path: Path = CONFIG_PATH) -> None:
    serial = load_config(path).get("hardware_serial", "")
    cfg = {**DEFAULT_CONFIG, "camera": {**DEFAULT_CONFIG["camera"]}}
    if serial:
        cfg["hardware_serial"] = serial
    save_config(cfg, path)


def has_wifi_creds(path: Path = CONFIG_PATH) -> bool:
    cfg = load_config(path)
    return bool(cfg.get("provisioned") and cfg.get("wifi_ssid"))


def is_provisioned(path: Path = CONFIG_PATH) -> bool:
    cfg = load_config(path)
    return bool(cfg.get("api_provisioned") and cfg.get("mqtt_node_id", 0) > 0)


def get_hardware_serial() -> str:
    global _cached_serial
    if _cached_serial is not None:
        return _cached_serial
    try:
        with open("/sys/firmware/devicetree/base/serial-number", "r") as f:
            raw = f.read().strip().strip("\x00")
        suffix = raw[-6:].upper()
    except Exception:
        import uuid
        suffix = uuid.uuid4().hex[:6].upper()
    _cached_serial = f"YM-PI-{suffix}"
    return _cached_serial
