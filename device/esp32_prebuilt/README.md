# ESP32 Prebuilt Setup-Mode Firmware

This is the prebuilt-hardware path for an ESP32-C3 Super Mini turret:

- pan servo on `GPIO4`
- tilt servo on `GPIO5`
- relay on `GPIO6`
- setup AP for first-run onboarding
- local provisioning form for Wi-Fi and claim code
- NVS-backed saved config
- runtime MQTT control after provisioning

Use this for hardware that should feel like a product, not a developer kit.

## Customer Flow

1. Plug in the device
2. Join the setup Wi-Fi
3. Open the setup page
4. Enter home Wi-Fi and claim code
5. Device provisions itself from `yourmove.live`
6. Device reboots into normal runtime

## Factory Flow

1. Copy [config.example.h](./config.example.h) to `config.h`
2. Set a factory serial in `YM_FACTORY_HARDWARE_SERIAL`
3. Flash the firmware
4. Reserve a matching claim code for that serial in the dashboard
5. Ship the device with the claim code or QR code

For development, you can leave the factory serial blank and the firmware will derive one from the chip id.

## Libraries

Install:

- `PubSubClient`
- `ArduinoJson`
- `ESP32Servo`

The ESP32 core also provides:

- `Preferences`
- `WebServer`
- `DNSServer`
- `HTTPClient`

## Setup AP

If no stored Wi-Fi/MQTT config exists, the device starts an AP like:

- `YourMove-Setup-AB12`

Open `http://192.168.4.1/` after joining it.

## Reset Behavior

If you wire a button later and set `YM_RESET_BUTTON_PIN`:

- hold 5s: clear Wi-Fi only
- hold 12s: full factory reset

## Important Note

This firmware is designed so the device only needs:

- factory serial
- product provisioning URL

It does not need customer Wi-Fi or MQTT credentials hardcoded before shipping.
