#!/usr/bin/env python3
"""
YourMove treat dispenser reference client.

A minimal, non-turret device that shows how to publish a v2 capability
payload and drive the dashboard's generic Setup controls (no pan/tilt,
no fire, no crosshair calibration — just a single `dispense` command
plus configurable rate / per-user / cooldown limits).

Run it like a normal MQTT node:

  python device/treat_dispenser/dispenser.py \
    --host yourmove.live --port 1883 \
    --node-id 42 \
    --username node_xxx --password secret

The client will:

  * publish presence + v2 capability snapshot on connect
  * handle `dispense` commands by simulating a brief relay pulse
  * honor a cooldown between dispenses locally
  * re-publish capability any time limits shift (handy during tuning)

This is a reference, not a sealed firmware. Anyone building their own
dispenser (or anything else) should feel free to copy and rewire.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt


def utc_ts() -> int:
    return int(time.time())


@dataclass
class DispenserState:
    runtime_type: str = "pi_gateway"
    device_type: str = "treat_dispenser"
    protocol_version: int = 1
    firmware_version: str = "treat-dispenser-ref-0.1.0"
    session_id: str | None = None
    state: str = "idle"
    last_completed_command_id: str | None = None
    last_error: str | None = None
    last_dispense_at: float = 0.0
    startup_time: float = field(default_factory=time.time)
    recent_commands: dict[str, dict] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def uptime(self) -> int:
        return int(time.time() - self.startup_time)


class RelayAdapter:
    """Stand-in for a GPIO / serial relay driver."""

    def pulse(self, duration_ms: int) -> None:
        time.sleep(max(0.0, duration_ms / 1000.0))


class TreatDispenser:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.state = DispenserState(firmware_version=args.firmware_version)
        self.adapter = RelayAdapter()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if args.username:
            self.client.username_pw_set(args.username, args.password or None)
        self.client.enable_logger()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.will_set(
            self.topic("presence"),
            json.dumps({"state": "offline", "reason": "disconnect", "timestamp": utc_ts()}),
            qos=1,
            retain=True,
        )
        self.running = True

    def topic(self, suffix: str) -> str:
        return f"nodes/{self.args.node_id}/{suffix}"

    def publish(self, suffix: str, payload: dict, *, qos: int = 1, retain: bool = False) -> None:
        self.client.publish(self.topic(suffix), json.dumps(payload), qos=qos, retain=retain)

    def publish_presence(self, state: str, reason: str | None = None) -> None:
        payload = {"state": state, "runtime_type": self.state.runtime_type, "timestamp": utc_ts()}
        if reason:
            payload["reason"] = reason
        self.publish("presence", payload, retain=True)

    def build_capability_payload(self) -> dict:
        """The v2 capability snapshot. The platform renders it; it does not police it.

        The firmware is the source of truth for what `dispense` means, what
        durations are safe, and how fast the hardware can actually fire.
        The platform's job is just to surface these knobs on the dashboard.
        """
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
                    "label": "Dispense treat",
                    "params": {
                        "duration_ms": {
                            "type": "int",
                            "default": self.args.default_duration_ms,
                            "min": 50,
                            "max": self.args.max_duration_ms,
                            "step": 10,
                            "unit": "ms",
                            "label": "Dispense duration",
                            "quick_edit": True,
                        },
                    },
                },
            },
            "tuning": {},
            "limits": {
                "rates": {
                    "dispense": {
                        "per_minute": {
                            "default": self.args.rate_per_minute,
                            "configurable": True,
                            "label": "Max dispenses per minute (shared)",
                        },
                        "per_hour": {
                            "default": self.args.rate_per_hour,
                            "configurable": True,
                            "label": "Max dispenses per hour (shared)",
                        },
                    },
                },
                "per_user": {
                    "dispense": {
                        "per_minute": {
                            "default": self.args.per_user_per_minute,
                            "configurable": True,
                            "label": "Max dispenses per minute (per user)",
                        },
                        "per_hour": {
                            "default": self.args.per_user_per_hour,
                            "configurable": True,
                            "label": "Max dispenses per hour (per user)",
                        },
                    },
                },
                "cooldown_ms": {
                    "dispense": {
                        "default": self.args.cooldown_ms,
                        "configurable": True,
                        "label": "Cooldown between dispenses",
                    },
                },
            },
            "telemetry": {
                "uptime": {"unit": "s"},
                "last_error": {"type": "string"},
            },
        }

    def publish_capabilities(self) -> None:
        self.publish("capabilities", self.build_capability_payload(), retain=True)

    def publish_state(self) -> None:
        with self.state.lock:
            payload = {
                "state": self.state.state,
                "session_id": self.state.session_id,
                "last_completed_command_id": self.state.last_completed_command_id,
                "uptime": self.state.uptime(),
                "protocol_version": self.state.protocol_version,
            }
            if self.state.last_error:
                payload["last_error"] = self.state.last_error
        self.publish("state/reported", payload, retain=True)

    def publish_telemetry(self) -> None:
        with self.state.lock:
            payload = {
                "uptime": self.state.uptime(),
                "last_error": self.state.last_error,
            }
        self.publish("telemetry", payload, qos=0, retain=False)

    def remember(self, command_id: str, ack: dict, result: dict) -> None:
        with self.state.lock:
            self.state.recent_commands[command_id] = {"ack": ack, "result": result}
            if len(self.state.recent_commands) > 50:
                oldest = next(iter(self.state.recent_commands))
                self.state.recent_commands.pop(oldest, None)

    def publish_ack(self, command_id: str, status: str, reason: str | None = None) -> dict:
        payload = {"command_id": command_id, "status": status, "timestamp": utc_ts()}
        if reason:
            payload["reason"] = reason
        self.publish("ack", payload)
        return payload

    def publish_result(self, command_id: str, status: str, reason: str | None = None) -> dict:
        payload = {"command_id": command_id, "status": status, "timestamp": utc_ts()}
        if reason:
            payload["reason"] = reason
        self.publish("result", payload)
        return payload

    def reject(self, command_id: str, reason: str) -> None:
        with self.state.lock:
            self.state.last_error = reason
        ack = self.publish_ack(command_id, "rejected", reason)
        result = self.publish_result(command_id, "rejected", reason)
        self.publish_state()
        self.remember(command_id, ack, result)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[dispenser] connected rc={reason_code}", flush=True)
        client.subscribe(self.topic("commands"), qos=1)
        self.publish_presence("online")
        self.publish_capabilities()
        self.publish_state()

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            print(f"[dispenser] invalid payload: {exc}", flush=True)
            return

        command_id = payload.get("command_id")
        command_type = payload.get("type")
        command_payload = payload.get("payload") or {}
        expires_at = payload.get("expires_at")

        if not command_id or not command_type:
            return

        with self.state.lock:
            if command_id in self.state.recent_commands:
                prior = self.state.recent_commands[command_id]
                self.publish("ack", prior["ack"])
                self.publish("result", prior["result"])
                return

        if expires_at and utc_ts() > int(expires_at):
            self.reject(command_id, "expired")
            return

        if command_type == "dispense":
            self.handle_dispense(command_id, command_payload)
        else:
            self.reject(command_id, f"unsupported command: {command_type}")

    def handle_dispense(self, command_id: str, payload: dict) -> None:
        now = time.time()
        cooldown_s = self.args.cooldown_ms / 1000.0
        with self.state.lock:
            since_last = now - self.state.last_dispense_at
            if since_last < cooldown_s:
                wait_ms = int((cooldown_s - since_last) * 1000)
                self.reject(command_id, f"cooldown: wait {wait_ms}ms")
                return

        requested = int(payload.get("duration_ms", self.args.default_duration_ms))
        duration_ms = max(50, min(self.args.max_duration_ms, requested))

        ack = self.publish_ack(command_id, "ack")

        def _run():
            with self.state.lock:
                self.state.state = "executing"
                self.state.last_error = None
            self.publish_state()

            self.adapter.pulse(duration_ms)

            with self.state.lock:
                self.state.state = "cooldown"
                self.state.last_completed_command_id = command_id
                self.state.last_dispense_at = time.time()
            self.publish_state()

            result = self.publish_result(command_id, "completed")
            self.remember(command_id, ack, result)

            time.sleep(min(cooldown_s, 2.0))
            with self.state.lock:
                if self.state.state == "cooldown":
                    self.state.state = "idle"
            self.publish_state()

        threading.Thread(target=_run, daemon=True).start()

    def telemetry_loop(self) -> None:
        while self.running:
            self.publish_telemetry()
            time.sleep(self.args.telemetry_sec)

    def run(self) -> None:
        self.client.connect(self.args.host, self.args.port, 60)
        self.client.loop_start()
        threading.Thread(target=self.telemetry_loop, daemon=True).start()

        def shutdown(*_args):
            self.running = False
            self.publish_presence("offline", "shutdown")
            self.client.loop_stop()
            self.client.disconnect()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        while self.running:
            time.sleep(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the YourMove treat dispenser reference client.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--node-id", type=int, required=True)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--firmware-version", default="treat-dispenser-ref-0.1.0")
    parser.add_argument("--telemetry-sec", type=int, default=15)
    parser.add_argument("--default-duration-ms", type=int, default=300)
    parser.add_argument("--max-duration-ms", type=int, default=2000)
    parser.add_argument("--cooldown-ms", type=int, default=1500)
    parser.add_argument("--rate-per-minute", type=int, default=10)
    parser.add_argument("--rate-per-hour", type=int, default=120)
    parser.add_argument("--per-user-per-minute", type=int, default=2)
    parser.add_argument("--per-user-per-hour", type=int, default=20)
    return parser


if __name__ == "__main__":
    TreatDispenser(build_parser().parse_args()).run()
