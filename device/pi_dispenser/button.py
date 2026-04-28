"""Single-button control for Pi dispenser. GPIO 17, active low, internal pull-up.

Short press  (< 2s)  — restart services (go2rtc, device, edge)
Medium press (5s)    — safe shutdown for unplugging
Long press   (30s)   — factory reset + reboot into setup AP
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from device.pi_dispenser.config import factory_reset, CONFIG_PATH

log = logging.getLogger(__name__)

BUTTON_PIN = 17
RESTART_MAX_S = 2.0
SHUTDOWN_S = 5.0
FACTORY_RESET_S = 30.0


def _restart_services():
    log.info("Restarting services...")
    for svc in ["yourmove-go2rtc", "yourmove-device", "yourmove-edge"]:
        subprocess.run(["systemctl", "restart", svc], check=False)
    log.info("Services restarted")


def _shutdown():
    log.info("Shutting down for safe unplug...")
    subprocess.run(["systemctl", "poweroff"], check=False)


def _factory_reset(config_path: Path):
    log.warning("Factory reset — clearing all config and rebooting into setup")
    factory_reset(config_path)
    subprocess.run(["systemctl", "reboot"], check=False)


def run(config_path: Path = CONFIG_PATH):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [button] %(message)s")

    try:
        from gpiozero import Button
    except ImportError:
        log.error("gpiozero not available — button monitor cannot run")
        return

    button = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
    press_start = [0.0]

    def on_press():
        press_start[0] = time.monotonic()

    def on_release():
        if press_start[0] == 0.0:
            return
        held = time.monotonic() - press_start[0]
        press_start[0] = 0.0

        if held >= FACTORY_RESET_S:
            _factory_reset(config_path)
        elif held >= SHUTDOWN_S:
            _shutdown()
        elif held < RESTART_MAX_S:
            _restart_services()

    button.when_pressed = on_press
    button.when_released = on_release
    log.info(
        "Button monitor started on GPIO %d — "
        "tap: restart, %ds: shutdown, %ds: factory reset",
        BUTTON_PIN, int(SHUTDOWN_S), int(FACTORY_RESET_S),
    )

    try:
        import signal
        signal.pause()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
