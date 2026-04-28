"""MQTT device client for Pi treat dispenser. Adapted from device/treat_dispenser/dispenser.py."""
from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt
import requests as http_requests

log = logging.getLogger(__name__)

FIRMWARE_VERSION = "pi-dispenser-0.1.0"
DEFAULT_DURATION_MS = 500
MAX_DURATION_MS = 5000
COOLDOWN_S = 5.0
TELEMETRY_INTERVAL_S = 30
MAX_RECENT_COMMANDS = 50


def _utc_ts() -> float:
    return time.time()


@dataclass
class DispenserState:
    runtime_type: str = "pi_gateway"
    device_type: str = "treat_dispenser"
    protocol_version: int = 1
    firmware_version: str = FIRMWARE_VERSION
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "idle"
    last_completed_command_id: str | None = None
    last_error: str | None = None
    last_dispense_at: float = 0.0
    startup_time: float = field(default_factory=time.time)
    recent_commands: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class PiDispenser:
    def __init__(self, config: dict, adapter=None):
        self.config = config
        self.node_id = config["mqtt_node_id"]
        self.adapter = adapter
        self.state = DispenserState()
        self.client = mqtt.Client(
            client_id=f"yourmove-pi-{config.get('hardware_serial', 'unknown')}",
            clean_session=True,
        )
        self.client.username_pw_set(config["mqtt_username"], config["mqtt_password"])
        lwt = json.dumps({
            "state": "offline",
            "runtime_type": "pi_gateway",
            "reason": "disconnect",
            "timestamp": _utc_ts(),
        })
        self.client.will_set(self._topic("presence"), lwt, qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def _topic(self, suffix: str) -> str:
        return f"nodes/{self.node_id}/{suffix}"

    def _publish(self, suffix: str, payload: dict, qos: int = 1, retain: bool = False):
        self.client.publish(self._topic(suffix), json.dumps(payload), qos=qos, retain=retain)

    def build_capabilities(self) -> dict:
        return {
            "schema_version": 2,
            "node_type": self.state.device_type,
            "runtime_type": self.state.runtime_type,
            "protocol_version": self.state.protocol_version,
            "firmware_version": self.state.firmware_version,
            "commands": {
                "dispense": {
                    "class": "action",
                    "replaceable": False,
                    "params": {
                        "duration_ms": {
                            "type": "int",
                            "default": DEFAULT_DURATION_MS,
                            "min": 50,
                            "max": MAX_DURATION_MS,
                            "step": 50,
                            "unit": "ms",
                            "label": "Duration",
                            "quick_edit": True,
                        },
                    },
                },
                "camera_reset": {
                    "class": "action",
                    "replaceable": False,
                    "params": {},
                },
            },
            "tuning": {},
            "limits": {
                "rates": {
                    "dispense": {
                        "per_minute": {"default": 6, "configurable": True, "label": "Dispenses/min"},
                        "per_hour": {"default": 60, "configurable": True, "label": "Dispenses/hr"},
                    },
                },
                "per_user": {
                    "dispense": {
                        "per_minute": {"default": 2, "configurable": True, "label": "Per user/min"},
                        "per_hour": {"default": 10, "configurable": True, "label": "Per user/hr"},
                    },
                },
                "cooldown_ms": {
                    "dispense": {"default": int(COOLDOWN_S * 1000), "configurable": True, "label": "Cooldown"},
                },
            },
            "telemetry": {
                "uptime": {"unit": "s"},
                "wifi_rssi": {"unit": "dBm"},
                "last_error": {},
            },
        }

    def get_state(self) -> dict:
        with self.state.lock:
            return {
                "runtime_type": self.state.runtime_type,
                "device_type": self.state.device_type,
                "protocol_version": self.state.protocol_version,
                "firmware_version": self.state.firmware_version,
                "session_id": self.state.session_id,
                "state": self.state.state,
                "last_completed_command_id": self.state.last_completed_command_id,
                "last_error": self.state.last_error,
            }

    def get_telemetry(self) -> dict:
        uptime = int(time.time() - self.state.startup_time)
        rssi = self._get_wifi_rssi()
        return {
            "uptime": uptime,
            "wifi_rssi": rssi,
            "last_error": self.state.last_error,
        }

    @staticmethod
    def _get_wifi_rssi() -> int | None:
        try:
            import subprocess
            result = subprocess.run(
                ["iwconfig", "wlan0"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if "Signal level" in line:
                    part = line.split("Signal level=")[1].split()[0]
                    return int(part.replace("dBm", ""))
        except Exception:
            pass
        return None

    def on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed: rc=%d", rc)
            return
        log.info("MQTT connected, subscribing to commands")
        client.subscribe(self._topic("commands"), qos=1)
        self._publish("presence", {
            "state": "online", "runtime_type": "pi_gateway",
            "reason": "boot", "timestamp": _utc_ts(),
        }, retain=True)
        self._publish("capabilities", self.build_capabilities(), retain=True)
        self._publish("state/reported", self.get_state(), retain=True)

    def on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        command_id = data.get("command_id")
        command_type = data.get("type")
        payload = data.get("payload", {})
        expires_at = data.get("expires_at")
        if not command_id or not command_type:
            return

        log.info("Received command %s type=%s state=%s", command_id, command_type, self.state.state)

        with self.state.lock:
            if command_id in self.state.recent_commands:
                log.info("Replaying cached response for %s", command_id)
                ack, result = self.state.recent_commands[command_id]
                self._publish("ack", ack)
                self._publish("result", result)
                return

        if expires_at and _utc_ts() > expires_at:
            log.warning("Command %s expired (expires_at=%s now=%s)", command_id, expires_at, _utc_ts())
            self._reject(command_id, command_type, "expired")
            return

        if command_type == "dispense":
            self._handle_dispense(command_id, payload)
        elif command_type == "camera_reset":
            self._handle_camera_reset(command_id, payload)
        else:
            self._reject(command_id, command_type, "unknown_command")

    def _handle_dispense(self, command_id: str, payload: dict):
        now = time.time()
        with self.state.lock:
            elapsed = now - self.state.last_dispense_at
            if elapsed < COOLDOWN_S:
                log.info("Rejecting %s: cooldown (%.1fs elapsed, need %.1fs)", command_id, elapsed, COOLDOWN_S)
                self._reject(command_id, "dispense", "cooldown")
                return
            log.info("Accepting dispense %s (state=%s, elapsed=%.1fs)", command_id, self.state.state, elapsed)

        duration = payload.get("duration_ms", DEFAULT_DURATION_MS)
        duration = max(50, min(duration, MAX_DURATION_MS))

        ack = {"command_id": command_id, "type": "dispense", "status": "accepted", "timestamp": _utc_ts()}
        self._publish("ack", ack)

        def _run():
            try:
                with self.state.lock:
                    self.state.state = "executing"
                self._publish("state/reported", self.get_state(), retain=True)
                try:
                    self.adapter.dispense(duration)
                except Exception as e:
                    log.error("Dispense failed: %s", e)
                    self.state.last_error = str(e)
                with self.state.lock:
                    self.state.last_dispense_at = time.time()
                    self.state.last_completed_command_id = command_id
                    self.state.state = "cooldown"
                result = {"command_id": command_id, "type": "dispense", "status": "completed", "timestamp": _utc_ts()}
                self._publish("result", result)
                self._publish("state/reported", self.get_state(), retain=True)
                self._remember(command_id, ack, result)
                time.sleep(COOLDOWN_S)
            except Exception as e:
                log.error("Dispense run thread error: %s", e)
            finally:
                with self.state.lock:
                    self.state.state = "idle"
                try:
                    self._publish("state/reported", self.get_state(), retain=True)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _handle_camera_reset(self, command_id: str, payload: dict):
        ack = {"command_id": command_id, "type": "camera_reset", "status": "accepted", "timestamp": _utc_ts()}
        self._publish("ack", ack)

        def _run():
            try:
                self.adapter.camera_reset(3000)
            except Exception as e:
                log.error("Camera reset failed: %s", e)
                self.state.last_error = str(e)
            result = {"command_id": command_id, "type": "camera_reset", "status": "completed", "timestamp": _utc_ts()}
            self._publish("result", result)
            self._remember(command_id, ack, result)

        threading.Thread(target=_run, daemon=True).start()

    def _reject(self, command_id: str, command_type: str, reason: str):
        with self.state.lock:
            self.state.last_error = reason
        ack = {"command_id": command_id, "type": command_type, "status": "rejected", "reason": reason, "timestamp": _utc_ts()}
        result = {"command_id": command_id, "type": command_type, "status": "rejected", "reason": reason, "timestamp": _utc_ts()}
        self._publish("ack", ack)
        self._publish("result", result)
        self._publish("state/reported", self.get_state(), retain=True)
        self._remember(command_id, ack, result)

    def _remember(self, command_id: str, ack: dict, result: dict):
        with self.state.lock:
            self.state.recent_commands[command_id] = (ack, result)
            if len(self.state.recent_commands) > MAX_RECENT_COMMANDS:
                oldest = next(iter(self.state.recent_commands))
                del self.state.recent_commands[oldest]

    def _report_presence_http(self, state: str, reason: str = "heartbeat"):
        base_url = self.config.get("base_url", "https://yourmove.live").rstrip("/")
        node_slug = self.config.get("node_slug", "")
        auth_token = self.config.get("auth_token", "")
        if not node_slug or not auth_token:
            return
        try:
            http_requests.post(
                f"{base_url}/api/nodes/{node_slug}/operator/edge-heartbeat",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"health": state, "firmware_version": FIRMWARE_VERSION},
                timeout=10,
            )
        except Exception as e:
            log.debug("HTTP presence failed: %s", e)

    def _telemetry_loop(self):
        while True:
            time.sleep(TELEMETRY_INTERVAL_S)
            self._publish("telemetry", self.get_telemetry(), qos=0)
            self._publish("presence", {
                "state": "online", "runtime_type": "pi_gateway",
                "reason": "heartbeat", "timestamp": _utc_ts(),
            }, retain=True)
            self._report_presence_http("online")

    def run(self):
        log.info("Connecting to MQTT %s:%d as node %d",
                 self.config["mqtt_host"], self.config["mqtt_port"], self.node_id)
        self.client.connect(self.config["mqtt_host"], self.config["mqtt_port"], keepalive=60)

        tel_thread = threading.Thread(target=self._telemetry_loop, daemon=True)
        tel_thread.start()

        def _shutdown(sig, frame):
            log.info("Shutting down (signal %s)", sig)
            self._publish("presence", {
                "state": "offline", "runtime_type": "pi_gateway",
                "reason": "shutdown", "timestamp": _utc_ts(),
            }, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
            if self.adapter and hasattr(self.adapter, "cleanup"):
                self.adapter.cleanup()
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
        self.client.loop_forever()


def main():
    from device.pi_dispenser.config import load_config, CONFIG_PATH
    from device.pi_dispenser.gpio import RelayAdapter

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    cfg = load_config(CONFIG_PATH)
    adapter = RelayAdapter()
    dispenser = PiDispenser(cfg, adapter=adapter)
    dispenser.run()


if __name__ == "__main__":
    main()
