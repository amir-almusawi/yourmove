# Device Reference Clients

This folder is for reference device implementations, not polished production firmware.

It serves two audiences:

- `BYO / developer`
  Start from these examples, inspect the live protocol, and adapt them to your own hardware.
- `Internal / prebuilt development`
  Use these as scaffolding while we design the future plug-and-play hardware experience.

Included here:

- [`simulator.py`](/home/amir/websites/yourmove/device/simulator.py)
  Runnable MQTT simulator for protocol and dashboard testing.
- [`esp32_direct/yourmove_esp32_direct.ino`](/home/amir/websites/yourmove/device/esp32_direct/yourmove_esp32_direct.ino)
  Reference Arduino-style ESP32 direct client for a pan/tilt/relay turret.
- [`esp32_prebuilt/esp32_prebuilt.ino`](/home/amir/websites/yourmove/device/esp32_prebuilt/esp32_prebuilt.ino)
  Setup-mode ESP32 firmware for prebuilt hardware with claim-code provisioning.
- [`pi_gateway/gateway.py`](/home/amir/websites/yourmove/device/pi_gateway/gateway.py)
  Runnable Raspberry Pi gateway skeleton with adapter hooks.
- [`treat_dispenser/dispenser.py`](/home/amir/websites/yourmove/device/treat_dispenser/dispenser.py)
  Non-turret starter that publishes a v2 capability snapshot and drives the dashboard's generic Setup controls.

Design rule:

- same cloud protocol
- different local runtime implementation

So:

- `esp32_direct` runs MQTT and hardware logic directly on the MCU
- `pi_gateway` runs MQTT and orchestration on the Pi, with room to delegate local control to an attached MCU later
