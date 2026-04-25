# ESP32 Direct Pan/Tilt/Relay Build

This is the first real `esp32_direct` firmware reference for a YourMove node:

- ESP32-C3 Super Mini
- pan servo on `GPIO4`
- tilt servo on `GPIO5`
- relay input on `GPIO6`

It speaks the live YourMove MQTT contract and is meant for a simple first turret:

- Wi-Fi
- MQTT subscribe to `nodes/{node_id}/commands`
- retained `presence`, `capabilities`, and `state/reported`
- `ack` and `result` publishing
- local pan/tilt limit enforcement
- relay runtime and cooldown enforcement
- duplicate command suppression

## Wiring

Signal pins:

- `GPIO4` -> pan servo signal
- `GPIO5` -> tilt servo signal
- `GPIO6` -> relay `IN`

Important power notes:

- do not power either servo from the ESP32 3.3V pin
- use a separate regulated 5V rail for the servos
- use a separate relay supply as required by the module
- tie all grounds together:
  ESP32 GND, servo power GND, relay GND

Recommended minimum hookup:

- pan servo: `5V`, `GND`, signal to `GPIO4`
- tilt servo: `5V`, `GND`, signal to `GPIO5`
- relay module: `VCC`, `GND`, `IN` to `GPIO6`

## What The Firmware Expects

The sketch consumes these command types:

- `set_target`
- `aim`
- `arm`
- `disarm`
- `home`
- `fire`

It publishes:

- `presence`
- `capabilities`
- `state/reported`
- `telemetry`
- `heartbeat`
- `ack`
- `result`

## Install Tools

Use Arduino IDE 2.x or PlatformIO.

In Arduino IDE:

1. Install board package `esp32` by Espressif Systems.
2. Select board `ESP32C3 Dev Module`.
3. Install these libraries:
   - `PubSubClient`
   - `ArduinoJson`
   - `ESP32Servo`

## Get Device Credentials From YourMove

From the node owner dashboard:

1. Open the rig dashboard.
2. Use `Register Device` if this rig is not already linked.
3. Save the returned values immediately:
   - `mqtt_node_id`
   - `mqtt_username`
   - `mqtt_password`

Use:

- `mqtt_node_id` for `YM_NODE_ID`
- `mqtt_username` for `YM_MQTT_USERNAME`
- `mqtt_password` for `YM_MQTT_PASSWORD`

Current broker settings for the live system:

- host: `yourmove.live`
- port: `1883`

## Configure The Firmware

Copy [config.example.h](/home/amir/websites/yourmove/device/esp32_direct/config.example.h) to `config.h`.

Then set:

- your Wi-Fi SSID and password
- `YM_NODE_ID`
- `YM_MQTT_USERNAME`
- `YM_MQTT_PASSWORD`

You will probably also tune:

- `YM_PAN_CENTER_DEG`
- `YM_TILT_CENTER_DEG`
- `YM_PAN_INVERT`
- `YM_TILT_INVERT`
- `YM_FIRE_MAX_MS`

If your relay module triggers when idle, flip:

- `YM_RELAY_ACTIVE_HIGH`

## Flash It

1. Open [yourmove_esp32_direct.ino](/home/amir/websites/yourmove/device/esp32_direct/yourmove_esp32_direct.ino).
2. Make sure `config.h` is in the same folder.
3. Plug in the ESP32-C3 Super Mini over USB.
4. Select the correct serial port.
5. Click `Upload`.

If the board does not auto-enter bootloader:

1. Hold `BOOT`.
2. Tap `RESET`.
3. Release `BOOT` after upload starts.

Open Serial Monitor at `115200`.

On a healthy boot you should see the node come online in the dashboard and begin publishing:

- retained `presence`
- retained `capabilities`
- retained `state/reported`
- periodic `telemetry`

## Initial Calibration

Before you let the relay fire anything:

1. Power up with the relay output physically disconnected from the final load.
2. Confirm both servos move to home cleanly.
3. Test `home`, `arm`, and `disarm`.
4. Send small pan/tilt commands and verify direction.
5. Fix `YM_PAN_INVERT` or `YM_TILT_INVERT` if movement is reversed.
6. Adjust `YM_PAN_CENTER_DEG` and `YM_TILT_CENTER_DEG` until home is correct.
7. Lower `YM_FIRE_MAX_MS` aggressively for the first live tests.

## Current Limitations

This is a strong MVP firmware, not final production firmware.

Still missing:

- TLS MQTT client
- NTP-backed wall-clock timestamps
- OTA
- non-blocking fire/cooldown state machine
- persisted command cache across reboots
- Wi-Fi provisioning UX

For your first turret, this is enough to get a real pan/tilt/relay node onto the live system and controlled by the existing YourMove device path.
