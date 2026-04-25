# Prebuilt Provisioning Spec

## Goal

Make prebuilt YourMove hardware follow a plug-and-play path:

1. power on
2. join setup Wi-Fi
3. enter home Wi-Fi and claim code
4. device provisions itself
5. node appears online

This should work without exposing MQTT topics or broker credentials to the customer.


## First-boot Device States

- `factory_unprovisioned`
  No saved Wi-Fi or MQTT credentials.
- `setup_mode`
  Device is broadcasting its own setup AP and hosting a local config page.
- `wifi_configured`
  Home Wi-Fi has been saved but cloud provisioning has not completed.
- `provisioned`
  Device has received MQTT credentials and stored them locally.
- `online`
  Device is connected and running its normal command loop.
- `fault`
  Device cannot complete setup or safe runtime.


## Stored Device Config

Persist in NVS or Preferences:

- `wifi_ssid`
- `wifi_password`
- `hardware_serial`
- `claim_code`
- `mqtt_host`
- `mqtt_port`
- `mqtt_node_id`
- `mqtt_username`
- `mqtt_password`
- `runtime_type`
- `device_type`
- `provisioned_at`
- `firmware_version`

Do not hardcode this into firmware for prebuilt units.


## Button Behavior

- tap: optional status LED action
- hold 5 seconds: clear Wi-Fi only, keep claim/device identity
- hold 12 seconds: full factory reset, wipe Wi-Fi and MQTT credentials, return to `setup_mode`


## Setup AP

Suggested SSID:

- `YourMove-Setup-AB12`

Suggested local portal screens:

1. welcome
2. select home Wi-Fi
3. enter home Wi-Fi password
4. enter claim code
5. provisioning progress
6. success / retry


## Provisioning Request

The device should POST to the platform:

- `POST /platform/devices/provision`

Payload:

```json
{
  "product_slug": "yourmove",
  "hardware_serial": "YM-HW-ABCD1234",
  "claim_code": "YM-ABCD1234",
  "firmware_version": "esp32-c3-pan-tilt-relay-0.1.0",
  "runtime_type": "esp32_direct"
}
```

Success response:

```json
{
  "ok": true,
  "product_slug": "yourmove",
  "hardware_serial": "YM-HW-ABCD1234",
  "claim_code": "YM-ABCD1234",
  "runtime_type": "esp32_direct",
  "device_type": "water_turret",
  "status": "provisioned",
  "platform_node_id": 42,
  "node_name": "Chicken Blaster",
  "node_slug": "chicken-blaster",
  "mqtt_username": "node_xxx",
  "mqtt_password": "secret",
  "mqtt_topics": {
    "commands": "nodes/42/commands",
    "presence": "nodes/42/presence",
    "ack": "nodes/42/ack",
    "result": "nodes/42/result",
    "telemetry": "nodes/42/telemetry",
    "capabilities": "nodes/42/capabilities",
    "state_reported": "nodes/42/state/reported"
  }
}
```


## Reservation Flow

The operator-side dashboard should reserve a prebuilt slot against a real node before the device ever boots.

Current rule:

- reserving a prebuilt slot auto-registers a platform node if one does not exist yet
- the reserved `claim_code` is anchored to that `platform_node_id`

That makes the provisioning response deterministic and reproducible.


## Reprovisioning Rule

If the device is factory-reset later, it should be able to repeat the setup flow with the same:

- `hardware_serial`
- `claim_code`

The platform should rotate MQTT credentials on each reprovision instead of trying to reveal an old password.


## Operator UX

The node dashboard should surface:

- reserved claim code
- hardware serial
- runtime type
- current provisioning status
- last provisioned timestamp
- firmware version when known


## Firmware Next Step

The next ESP32 firmware pass should add:

- captive portal setup mode
- NVS-backed config store
- provisioning POST call
- reset-button handling
- LED status patterns

The existing direct-MQTT firmware stays useful for BYO development, but prebuilt hardware should boot through this provisioning path instead of `config.h`.
