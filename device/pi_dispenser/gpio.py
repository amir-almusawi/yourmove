"""GPIO relay adapter for Pi dispenser. Requires gpiozero (standard on Pi OS)."""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

DISPENSER_PIN = 24
CAMERA_RESET_PIN = 25

try:
    from gpiozero import OutputDevice
    _MOCK = False
except ImportError:
    _MOCK = True
    log.warning("gpiozero not available — using mock GPIO (dev mode)")


class RelayAdapter:
    def __init__(self):
        if _MOCK:
            self._dispenser = None
            self._camera_reset = None
        else:
            self._dispenser = OutputDevice(DISPENSER_PIN, active_high=False, initial_value=False)
            self._camera_reset = OutputDevice(CAMERA_RESET_PIN, active_high=False, initial_value=False)

    def dispense(self, duration_ms: int) -> None:
        duration_s = max(0.0, duration_ms / 1000.0)
        log.info("dispense: GPIO %d HIGH for %dms", DISPENSER_PIN, duration_ms)
        if self._dispenser:
            self._dispenser.on()
            time.sleep(duration_s)
            self._dispenser.off()
        else:
            time.sleep(duration_s)

    def camera_reset(self, duration_ms: int = 3000) -> None:
        duration_s = max(0.0, duration_ms / 1000.0)
        log.info("camera_reset: GPIO %d HIGH for %dms", CAMERA_RESET_PIN, duration_ms)
        if self._camera_reset:
            self._camera_reset.on()
            time.sleep(duration_s)
            self._camera_reset.off()
        else:
            time.sleep(duration_s)

    def cleanup(self) -> None:
        if self._dispenser:
            self._dispenser.off()
            self._dispenser.close()
        if self._camera_reset:
            self._camera_reset.off()
            self._camera_reset.close()
