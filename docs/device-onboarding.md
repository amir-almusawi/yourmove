# Device Onboarding Paths

## Goal

Support two very different operator types without splitting the underlying protocol:

- `BYO / developer`
  They are comfortable with MQTT, firmware, serial logs, and custom hardware.
- `Prebuilt / plug-and-play`
  They bought hardware from us and should not need to understand MQTT topics, JSON payloads, or broker credentials.

The rule is:

- same cloud protocol
- different provisioning experience


## Shared Foundation

Both user types should end up with the same platform shape:

- one `yourmove` node
- one linked platform `mqtt_node_id`
- one MQTT topic family
- one command lifecycle
- one operator dashboard

This keeps:

- support simpler
- operator tooling consistent
- protocol drift low


## BYO Path

### User expectation

This user wants control.

They should be able to:

- register a device
- receive MQTT credentials
- see exact topics
- inspect protocol docs
- run a simulator locally
- build ESP32 or Pi code against the live broker

### What we should expose

- raw MQTT topics
- runtime type selection
- device type selection
- capability and state debugging
- command payload examples
- one-shot credential handoff
- simulator tooling

### Current repo support

- dashboard device prep panel
- operator device registration
- live platform node registration and linking
- `device/simulator.py`
- `docs/device-runtime-spec.md`


## Prebuilt Path

### User expectation

This user should not have to know what MQTT is.

The expected experience is:

1. Plug in hardware
2. Join Wi-Fi or ethernet
3. Enter or scan a setup code
4. Claim the device to a node
5. See it appear in the dashboard as online

### What we should hide

- broker hostname
- topic names
- credential rotation details
- firmware flashing details
- raw JSON command envelopes

### What we should expose instead

- claim code or QR onboarding
- simple device status
- camera check
- network check
- calibration wizard
- update available / reboot / reset actions


## Recommended Product Split

### BYO mode

Operator dashboard should show:

- `mqtt_node_id`
- runtime type
- derived MQTT topics
- one-shot credentials after registration
- docs links
- simulator link

### Prebuilt mode

Operator dashboard should show:

- device nickname
- online/offline
- firmware version
- network quality
- camera status
- setup checklist
- reboot / update / reset

The dashboard should not show the raw password by default in prebuilt mode.


## Provisioning Plan

### Phase 1: current state

- manual registration from dashboard
- live MQTT credentials
- manual firmware provisioning

Good enough for:

- internal testing
- BYO developers
- simulator-driven protocol work

### Phase 2: claimable prebuilt hardware

Add a device claim record on the platform side:

- `hardware_serial`
- `claim_code`
- `claimed_by_user_id`
- `claimed_at`
- `factory_profile`

Factory flow:

1. Burn firmware with bootstrap config
2. Reserve a claim code from the node dashboard
3. Device boots into setup AP mode
4. Customer enters Wi-Fi and claim code locally
5. Device calls the provisioning endpoint with `product_slug`, `hardware_serial`, and `claim_code`
6. Platform returns live MQTT credentials for the reserved node
7. Device stores them and reboots into normal runtime

### Phase 3: zero-touch prebuilt onboarding

Add:

- captive portal or setup AP
- Wi-Fi provisioning
- dashboard pairing handshake
- calibration wizard
- OTA flow


## Simulator Purpose

The simulator is for the BYO audience and for internal testing.

It lets us:

- validate command flow without hardware
- test presence/capabilities/state handling
- test command ack/result timing
- exercise dashboard and operator views
- prototype ESP32 and Pi behavior before touching firmware

For prebuilt hardware, the simulator is mostly an internal tool, not a customer-facing one.


## Firmware Strategy By Audience

### BYO

Ship:

- protocol spec
- simulator
- example ESP32 direct client
- example Pi gateway client

### Prebuilt

Ship:

- locked-down production firmware
- setup guide
- pairing flow
- OTA update path
- remote support path

The prebuilt customer should consume a product, not a protocol.


## Recommended Next Steps

1. Add captive portal firmware scaffolding for ESP32 setup mode.
2. Add a reset-button state machine for Wi-Fi reset vs full factory reset.
3. Add a local provisioning UI with QR and claim code instructions.
4. Add a calibration and connectivity checklist to the dashboard.
