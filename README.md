# YourMove On-Platform Runtime Kit

Public runtime components for builders who want to run hardware, cameras, and game logic on the hosted YourMove platform.

This repository is intentionally **not** the YourMove platform itself. It does **not** include:

- the hosted backend
- the public site
- operator dashboard code
- internal deployment or infrastructure

It **does** include the parts people run on their own hardware:

- ESP32 reference firmware
- Raspberry Pi and Python reference runtimes
- the local edge publisher supervisor
- edge CV and game-layer helpers
- protocol and onboarding docs for connecting to `yourmove.live`

## Repo Layout

- [`device/`](device)
  Reference device clients and firmware for nodes that connect to the platform over MQTT.
- [`edge/`](edge)
  Local camera publishing, clip capture, and optional edge CV/game-layer tooling.
- [`docs/`](docs)
  Public docs for the device protocol and hosted-platform onboarding flow.

## Quick Start

### 1. Device runtime

Choose one:

- [`device/esp32_direct`](device/esp32_direct): direct ESP32 firmware for pan/tilt/relay rigs
- [`device/esp32_prebuilt`](device/esp32_prebuilt): setup-mode firmware for prebuilt hardware
- [`device/pi_gateway`](device/pi_gateway): Python gateway skeleton for Raspberry Pi or similar
- [`device/treat_dispenser`](device/treat_dispenser): simple non-turret reference client
- [`device/simulator.py`](device/simulator.py): simulator for MQTT contract testing

### 2. Edge publisher

Use [`edge/`](edge) when you want a local box near the camera to:

- probe camera health
- restart a local publisher process or container
- push clips and health signals back to YourMove
- optionally run local CV/game-layer analysis

### 3. Platform setup

Use the hosted YourMove dashboard to:

- register a device and receive one-time MQTT credentials
- reserve claim codes for prebuilt devices
- generate or store the edge publisher restart command
- inspect state, capabilities, telemetry, and clip/game status

See:

- [`docs/device-protocol.md`](docs/device-protocol.md)
- [`docs/platform-onboarding.md`](docs/platform-onboarding.md)

## Python Requirements

Base Python tools in this repo use [`requirements.txt`](requirements.txt).

Optional CV/game-layer dependencies live in [`edge/requirements-cv.txt`](edge/requirements-cv.txt).

## Publishing Notes

Before pushing this repo to GitHub, choose a license and add it explicitly. This workspace does not assume one on your behalf.
