"""MQTT device client for Pi treat dispenser — gmqtt (asyncio) transport."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field

import gmqtt
import requests as http_requests

log = logging.getLogger(__name__)

FIRMWARE_VERSION = "pi-dispenser-0.1.0"
DEFAULT_DURATION_MS = 500
MAX_DURATION_MS = 5000
COOLDOWN_S = 5.0
TELEMETRY_INTERVAL_S = 15
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


class PiDispenser:
    def __init__(self, config: dict, adapter=None):
        self.config = config
        self.node_id = config["mqtt_node_id"]
        self.adapter = adapter
        self.state = DispenserState()
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

        self.client = gmqtt.Client(
            f"yourmove-pi-{config.get('hardware_serial', 'unknown')}",
            clean_session=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.client.set_auth_credentials(config["mqtt_username"], config["mqtt_password"])

        lwt = gmqtt.Message(
            self._topic("presence"),
            json.dumps({
                "state": "offline",
                "runtime_type": "pi_gateway",
                "reason": "disconnect",
                "timestamp": _utc_ts(),
            }),
            qos=1,
            retain=True,
        )
        self.client.set_config({"reconnect_retries": -1, "reconnect_delay": 1})
        self.client._will_message = lwt

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

    def _on_connect(self, client, flags, rc, properties):
        log.info("MQTT connected (rc=%d), subscribing to commands", rc)
        client.subscribe(self._topic("commands"), qos=1)
        self._publish("presence", {
            "state": "online", "runtime_type": "pi_gateway",
            "reason": "boot", "timestamp": _utc_ts(),
        }, retain=True)
        self._publish("capabilities", self.build_capabilities(), retain=True)
        self._publish("state/reported", self.get_state(), retain=True)

    def _on_disconnect(self, client, packet, exc=None):
        if exc:
            log.error("MQTT disconnected unexpectedly: %s — gmqtt will reconnect", exc)
        else:
            log.info("MQTT disconnected cleanly")

    def _on_message(self, client, topic, payload, qos, properties):
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 0
        command_id = data.get("command_id")
        command_type = data.get("type")
        if not command_id or not command_type:
            return 0
        log.info("Received command %s type=%s", command_id, command_type)
        asyncio.ensure_future(self._handle_command(data))
        return 0

    async def _handle_command(self, data):
        command_id = data["command_id"]
        command_type = data["type"]
        payload = data.get("payload", {})
        expires_at = data.get("expires_at")

        log.info("Processing command %s type=%s state=%s", command_id, command_type, self.state.state)

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
            await self._handle_dispense(command_id, payload)
        elif command_type == "camera_reset":
            await self._handle_camera_reset(command_id, payload)
        else:
            self._reject(command_id, command_type, "unknown_command")

    async def _handle_dispense(self, command_id: str, payload: dict):
        now = time.time()
        duration = payload.get("duration_ms", DEFAULT_DURATION_MS)
        duration = max(50, min(duration, MAX_DURATION_MS))
        busy_seconds = duration / 1000.0 + COOLDOWN_S

        elapsed = now - self.state.last_dispense_at
        if elapsed < busy_seconds:
            log.info("Rejecting %s: busy (%.1fs elapsed, need %.1fs)", command_id, elapsed, busy_seconds)
            self._reject(command_id, "dispense", "cooldown")
            return
        self.state.last_dispense_at = now
        log.info("Accepting dispense %s (state=%s, elapsed=%.1fs)", command_id, self.state.state, elapsed)

        ack = {"command_id": command_id, "type": "dispense", "status": "accepted", "timestamp": _utc_ts()}
        self._publish("ack", ack)

        self.state.state = "executing"
        self._publish("state/reported", self.get_state(), retain=True)

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.adapter.dispense, duration)
        except Exception as e:
            log.error("Dispense failed: %s", e)
            self.state.last_error = str(e)

        self.state.last_completed_command_id = command_id
        self.state.state = "cooldown"
        result = {"command_id": command_id, "type": "dispense", "status": "completed", "timestamp": _utc_ts()}
        self._publish("result", result)
        self._publish("state/reported", self.get_state(), retain=True)
        self._remember(command_id, ack, result)

        await asyncio.sleep(COOLDOWN_S)
        self.state.state = "idle"
        self._publish("state/reported", self.get_state(), retain=True)

    async def _handle_camera_reset(self, command_id: str, payload: dict):
        ack = {"command_id": command_id, "type": "camera_reset", "status": "accepted", "timestamp": _utc_ts()}
        self._publish("ack", ack)

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.adapter.camera_reset, 3000)
        except Exception as e:
            log.error("Camera reset failed: %s", e)
            self.state.last_error = str(e)

        result = {"command_id": command_id, "type": "camera_reset", "status": "completed", "timestamp": _utc_ts()}
        self._publish("result", result)
        self._remember(command_id, ack, result)

    def _reject(self, command_id: str, command_type: str, reason: str):
        self.state.last_error = reason
        ack = {"command_id": command_id, "type": command_type, "status": "rejected", "reason": reason, "timestamp": _utc_ts()}
        result = {"command_id": command_id, "type": command_type, "status": "rejected", "reason": reason, "timestamp": _utc_ts()}
        self._publish("ack", ack)
        self._publish("result", result)
        self._publish("state/reported", self.get_state(), retain=True)
        self._remember(command_id, ack, result)

    def _remember(self, command_id: str, ack: dict, result: dict):
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

    async def _telemetry_loop(self):
        while not self._shutdown_event.is_set():
            await asyncio.sleep(TELEMETRY_INTERVAL_S)
            try:
                self._publish("telemetry", self.get_telemetry(), qos=0)
                self._publish("presence", {
                    "state": "online", "runtime_type": "pi_gateway",
                    "reason": "heartbeat", "timestamp": _utc_ts(),
                }, retain=True)
            except Exception as e:
                log.error("Telemetry publish failed: %s", e)
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._report_presence_http, "online")
            except Exception as e:
                log.debug("HTTP presence failed: %s", e)

    async def _main(self):
        self._loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            self._loop.add_signal_handler(sig, self._shutdown_event.set)

        log.info("Connecting to MQTT %s:%d as node %d",
                 self.config["mqtt_host"], self.config["mqtt_port"], self.node_id)

        await self.client.connect(
            self.config["mqtt_host"],
            self.config["mqtt_port"],
            keepalive=15,
        )

        telemetry_task = asyncio.ensure_future(self._telemetry_loop())

        await self._shutdown_event.wait()

        log.info("Shutting down")
        self._publish("presence", {
            "state": "offline", "runtime_type": "pi_gateway",
            "reason": "shutdown", "timestamp": _utc_ts(),
        }, retain=True)
        telemetry_task.cancel()
        await self.client.disconnect()
        if self.adapter and hasattr(self.adapter, "cleanup"):
            self.adapter.cleanup()

    def run(self):
        asyncio.run(self._main())


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
