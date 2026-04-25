#!/usr/bin/env python3
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
class GatewayState:
    runtime_type: str = "pi_gateway"
    device_type: str = "water_turret"
    protocol_version: int = 1
    firmware_version: str = "pi-gateway-ref-0.1.0"
    session_id: str | None = None
    state: str = "idle"
    pan: float = 0.0
    tilt: float = 0.0
    last_completed_command_id: str | None = None
    last_error: str | None = None
    startup_time: float = field(default_factory=time.time)
    recent_commands: dict[str, dict] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def uptime(self) -> int:
        return int(time.time() - self.startup_time)


class LocalAdapter:
    """Replace this with GPIO or serial-backed controller logic later."""

    def __init__(self, pan_min: float, pan_max: float, tilt_min: float, tilt_max: float):
        self.pan_min = pan_min
        self.pan_max = pan_max
        self.tilt_min = tilt_min
        self.tilt_max = tilt_max

    def set_target(self, pan: float, tilt: float) -> tuple[float, float]:
        pan = max(self.pan_min, min(self.pan_max, pan))
        tilt = max(self.tilt_min, min(self.tilt_max, tilt))
        return pan, tilt

    def fire(self, max_ms: int) -> None:
        time.sleep(min(max_ms / 1000.0, 0.5))


class PiGateway:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.state = GatewayState(
            device_type=args.device_type,
            firmware_version=args.firmware_version,
        )
        self.adapter = LocalAdapter(args.pan_min, args.pan_max, args.tilt_min, args.tilt_max)
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

    def publish_capabilities(self) -> None:
        payload = {
            "node_type": self.state.device_type,
            "runtime_type": self.state.runtime_type,
            "protocol_version": self.state.protocol_version,
            "firmware_version": self.state.firmware_version,
            "commands": {
                "aim": {"class": "target", "replaceable": True},
                "fire": {"class": "action", "replaceable": False},
                "arm": {"class": "mode", "replaceable": False},
                "disarm": {"class": "mode", "replaceable": False},
            },
            "limits": {
                "pan_min": self.args.pan_min,
                "pan_max": self.args.pan_max,
                "tilt_min": self.args.tilt_min,
                "tilt_max": self.args.tilt_max,
                "fire_max_ms": self.args.fire_max_ms,
                "cooldown_ms": self.args.cooldown_ms,
            },
        }
        self.publish("capabilities", payload, retain=True)

    def publish_state(self) -> None:
        with self.state.lock:
            payload = {
                "state": self.state.state,
                "session_id": self.state.session_id,
                "position": {"pan": self.state.pan, "tilt": self.state.tilt},
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
                "free_heap": None,
                "rssi": None,
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
        print(f"[gateway] connected rc={reason_code}", flush=True)
        client.subscribe(self.topic("commands"), qos=1)
        self.publish_presence("online")
        self.publish_capabilities()
        self.publish_state()

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            print(f"[gateway] invalid payload: {exc}", flush=True)
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

        if command_type in {"aim", "set_target"}:
            self.handle_target(command_id, command_payload)
        elif command_type == "arm":
            self.handle_mode(command_id, "armed")
        elif command_type == "disarm":
            self.handle_mode(command_id, "idle")
        elif command_type == "fire":
            self.handle_fire(command_id)
        else:
            self.reject(command_id, f"unsupported command: {command_type}")

    def handle_target(self, command_id: str, payload: dict) -> None:
        ack = self.publish_ack(command_id, "ack")
        with self.state.lock:
            pan = float(payload.get("pan", self.state.pan))
            tilt = float(payload.get("tilt", self.state.tilt))
            self.state.pan, self.state.tilt = self.adapter.set_target(pan, tilt)
            self.state.last_completed_command_id = command_id
            self.state.last_error = None
        self.publish_state()
        result = self.publish_result(command_id, "completed")
        self.remember(command_id, ack, result)

    def handle_mode(self, command_id: str, next_state: str) -> None:
        ack = self.publish_ack(command_id, "ack")
        with self.state.lock:
            self.state.state = next_state
            self.state.last_completed_command_id = command_id
            self.state.last_error = None
        self.publish_state()
        result = self.publish_result(command_id, "completed")
        self.remember(command_id, ack, result)

    def handle_fire(self, command_id: str) -> None:
        with self.state.lock:
            if self.state.state != "armed":
                self.reject(command_id, "node not armed")
                return

        ack = self.publish_ack(command_id, "ack")

        def _run():
            with self.state.lock:
                self.state.state = "executing"
                self.state.last_error = None
            self.publish_state()

            self.adapter.fire(self.args.fire_max_ms)

            with self.state.lock:
                self.state.state = "cooldown"
                self.state.last_completed_command_id = command_id
            self.publish_state()

            result = self.publish_result(command_id, "completed")
            self.remember(command_id, ack, result)

            time.sleep(min(self.args.cooldown_ms / 1000.0, 2.0))
            with self.state.lock:
                if self.state.state == "cooldown":
                    self.state.state = "armed"
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
    parser = argparse.ArgumentParser(description="Run the YourMove Pi gateway reference client.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--node-id", type=int, required=True)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--device-type", default="water_turret")
    parser.add_argument("--firmware-version", default="pi-gateway-ref-0.1.0")
    parser.add_argument("--telemetry-sec", type=int, default=15)
    parser.add_argument("--pan-min", type=float, default=-70.0)
    parser.add_argument("--pan-max", type=float, default=70.0)
    parser.add_argument("--tilt-min", type=float, default=-20.0)
    parser.add_argument("--tilt-max", type=float, default=35.0)
    parser.add_argument("--fire-max-ms", type=int, default=500)
    parser.add_argument("--cooldown-ms", type=int, default=2000)
    return parser


if __name__ == "__main__":
    PiGateway(build_parser().parse_args()).run()
