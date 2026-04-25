# YourMove Device Runtime Spec

## Goal

Define a single cloud-to-device control contract that supports:

- `v1`: direct ESP32 nodes
- `v2`: Raspberry Pi gateway plus attached ESP32 or other controller
- future device types with different actuator models

This spec is written against the current `yourmove` codebase, not as a generic greenfield design.

Current integration points already in the app:

- `Node.mqtt_node_id` in `app/models/models.py`
- public command issuance at `POST /nodes/{slug}/command`
- platform relay in `app/services/platform.py`
- operator status and health APIs in `app/routes/api.py`

The main design rule is:

- cloud semantics stay stable
- node runtime may be `esp32_direct` or `pi_gateway`
- hardware-specific execution lives behind the same MQTT protocol


## Current State In Repo

Today `yourmove` does this:

- validates user/session/queue ownership in the app
- looks up `control_layout` on the node
- sends a command through the DataRook realtime platform with:
  - `node_id`
  - `command_type`
  - `payload`
  - user JWT
- records interaction delivery as `pending`, `sent`, or `failed`

Current gaps:

- no first-class command audit table
- no ack/result lifecycle from device back into `yourmove`
- no retained presence/state model
- no capability/config version tracking
- no runtime distinction between direct MCU and Pi gateway


## Runtime Model

### Logical Node

The cloud always manages one logical node.

Examples:

- a water turret controlled directly by an ESP32
- a water turret controlled by a Pi over MQTT and by an ESP32 over serial
- a treat dispenser on a Pi with local GPIO control

The cloud should not care which runtime is underneath except for diagnostics and capability reporting.

### Runtime Types

Add `runtime_type` as a first-class concept:

- `esp32_direct`
- `pi_gateway`

Optional later values:

- `simulator`
- `custom_gateway`

### Device Types

Add `device_type` as a separate concept:

- `water_turret`
- `treat_dispenser`
- `relay_board`
- `pan_tilt_camera`
- `custom`

Runtime type answers "where does the device protocol run?"

Device type answers "what kind of machine is this?"


## Cloud Responsibilities

The cloud decides:

- who may act
- when they may act
- what session is active
- which command to issue
- command expiry window
- audit history
- public UI state
- operator health and fault visibility

The cloud does not decide:

- exact PWM timing
- motor ramping details
- local actuator cutoffs
- safe retry behavior after disconnect


## Node Responsibilities

The node decides:

- whether the command is still valid
- whether the session matches
- whether the machine is armed
- whether the command violates safety limits
- how to execute safely
- how to stop safely on disconnect or fault

The node must enforce:

- angle and range limits
- actuator max runtime
- action cooldown
- rate limits
- safe stop on disconnect
- stale-command rejection


## MQTT Topic Contract

Use one topic family for both ESP32 and Pi runtimes.

- `nodes/{node_id}/commands`
- `nodes/{node_id}/ack`
- `nodes/{node_id}/result`
- `nodes/{node_id}/state/reported`
- `nodes/{node_id}/state/desired`
- `nodes/{node_id}/telemetry`
- `nodes/{node_id}/presence`
- `nodes/{node_id}/capabilities`
- `nodes/{node_id}/config`
- `nodes/{node_id}/events`

### QoS and Retain Rules

- `commands`: QoS 1, not retained
- `ack`: QoS 1, not retained
- `result`: QoS 1, not retained
- `state/reported`: QoS 1, retained
- `state/desired`: QoS 1, retained
- `telemetry`: QoS 0 or 1, not retained
- `presence`: QoS 1, retained, backed by LWT
- `capabilities`: QoS 1, retained
- `config`: QoS 1, retained
- `events`: QoS 0 or 1, not retained


## Command Envelope

Every command issued by the cloud should use one envelope.

```json
{
  "command_id": "uuid",
  "class": "target | action | mode",
  "type": "set_target | fire | arm | disarm | home | estop",
  "payload": {},
  "session_id": "session_123",
  "actor_id": "user_45",
  "issued_at": 1710000000,
  "expires_at": 1710000002,
  "protocol_version": 1
}
```

### Command Classes

- `target`
  Latest wins. Replaceable. Example: `set_target`.
- `action`
  Exactly-once semantic goal with ack/result tracking. Example: `fire`, `dispense`, `pulse`.
- `mode`
  Changes operating mode. Example: `arm`, `disarm`, `estop`, `set_session`.

### Idempotency

The node must keep a rolling cache of recent `command_id` values.

Rules:

- duplicate command returns the same lifecycle outcome
- a completed action is not re-executed
- an expired command is rejected
- a superseded target command may return `superseded`


## Device Responses

### Ack

Published immediately after validation or rejection.

```json
{
  "command_id": "uuid",
  "status": "ack | rejected",
  "reason": null,
  "timestamp": 1710000000
}
```

### Result

Published after execution or terminal failure.

```json
{
  "command_id": "uuid",
  "status": "completed | rejected | error | superseded",
  "reason": null,
  "timestamp": 1710000001
}
```

### Reported State

```json
{
  "state": "idle | armed | executing | cooldown | fault | estopped | offline",
  "session_id": "session_123",
  "position": {
    "pan": 12.5,
    "tilt": -3.0
  },
  "active_command_id": "uuid-or-null",
  "last_completed_command_id": "uuid-or-null",
  "uptime": 12345,
  "protocol_version": 1
}
```

### Presence

Use retained online message plus retained LWT offline message.

```json
{
  "state": "online",
  "runtime_type": "esp32_direct",
  "timestamp": 1710000000
}
```

On disconnect the broker should publish:

```json
{
  "state": "offline",
  "reason": "disconnect",
  "timestamp": 1710000010
}
```


## Capability Model

Capabilities should describe supported commands and limits, not just booleans.

```json
{
  "node_type": "water_turret",
  "runtime_type": "pi_gateway",
  "protocol_version": 1,
  "firmware_version": "1.0.3",
  "gateway_version": "0.4.0",
  "commands": {
    "set_target": {
      "class": "target",
      "replaceable": true,
      "params": ["pan", "tilt"]
    },
    "fire": {
      "class": "action",
      "replaceable": false,
      "params": ["duration_ms"]
    },
    "arm": {
      "class": "mode",
      "replaceable": false,
      "params": []
    }
  },
  "limits": {
    "pan_min": -70,
    "pan_max": 70,
    "tilt_min": -20,
    "tilt_max": 35,
    "fire_max_ms": 500,
    "cooldown_ms": 2000
  }
}
```

This should be the canonical source for:

- public developer docs
- operator dashboards
- cloud-side command validation
- future device profile generation


## Config Model

Config must be versioned from day one.

```json
{
  "config_version": 12,
  "device_profile": "water_turret_v1",
  "runtime_type": "esp32_direct",
  "transport": {
    "heartbeat_sec": 15,
    "telemetry_sec": 30
  },
  "limits": {
    "pan_min": -70,
    "pan_max": 70,
    "tilt_min": -20,
    "tilt_max": 35,
    "fire_max_ms": 500,
    "cooldown_ms": 2000
  },
  "hardware": {
    "servo_driver": "ledc",
    "pump_mode": "mosfet"
  }
}
```

Pi-backed nodes should use the same shape with an added local-controller section:

```json
{
  "runtime_type": "pi_gateway",
  "child_controller": {
    "transport": "serial",
    "port": "/dev/ttyUSB0",
    "baud": 115200
  }
}
```


## Database Changes For YourMove

The current `Node` table should remain the product-facing node record.

Add new schema objects in `yourmove` rather than overloading `interactions`.

### 1. Extend `yourmove.nodes`

Add columns:

- `runtime_type text not null default 'esp32_direct'`
- `device_type text not null default 'water_turret'`
- `protocol_version integer not null default 1`
- `firmware_version text null`
- `gateway_version text null`
- `last_presence_at timestamptz null`
- `last_telemetry_at timestamptz null`
- `last_fault_code text null`
- `last_fault_at timestamptz null`
- `desired_state jsonb not null default '{}'::jsonb`
- `reported_state jsonb not null default '{}'::jsonb`
- `capability_snapshot jsonb not null default '{}'::jsonb`
- `config_snapshot jsonb not null default '{}'::jsonb`

Do not remove:

- `mqtt_node_id`
- `turret_pan`
- `turret_tilt`
- `last_heartbeat`

Those fields can be backfilled later from `reported_state`.

### 2. Add `device_commands`

Purpose:

- one cloud-issued row per command
- authoritative join point for ack/result
- replay and operator audit

Suggested shape:

- `id bigserial primary key`
- `node_id int not null references yourmove.nodes(id)`
- `interaction_id int null references yourmove.interactions(id)`
- `command_id uuid not null unique`
- `session_id text null`
- `actor_user_id int null references public.users(id)`
- `command_class text not null`
- `command_type text not null`
- `payload jsonb not null default '{}'::jsonb`
- `status text not null default 'issued'`
- `issued_at timestamptz not null`
- `expires_at timestamptz null`
- `acked_at timestamptz null`
- `completed_at timestamptz null`
- `failed_at timestamptz null`
- `failure_reason text null`
- `created_at timestamptz not null default now()`

Indexes:

- `(node_id, created_at desc)`
- `(node_id, status)`
- `(actor_user_id, created_at desc)`

### 3. Add `device_command_events`

Append-only lifecycle log.

- `id bigserial primary key`
- `device_command_id bigint not null references yourmove.device_commands(id)`
- `event_type text not null`
- `payload jsonb not null default '{}'::jsonb`
- `created_at timestamptz not null default now()`

Useful event types:

- `issued`
- `ack`
- `rejected`
- `completed`
- `error`
- `superseded`
- `timed_out`

### 4. Add `device_telemetry`

Store either sampled or recent telemetry. Keep retention bounded later.

- `id bigserial primary key`
- `node_id int not null references yourmove.nodes(id)`
- `payload jsonb not null`
- `created_at timestamptz not null default now()`

Indexes:

- `(node_id, created_at desc)`

### 5. Add `device_faults`

- `id bigserial primary key`
- `node_id int not null references yourmove.nodes(id)`
- `fault_code text not null`
- `severity text not null`
- `detail jsonb not null default '{}'::jsonb`
- `opened_at timestamptz not null default now()`
- `cleared_at timestamptz null`

### 6. Optional Later: `device_config_applies`

Track config rollout and acknowledgment if remote config becomes active.


## Service Changes In YourMove

### 1. Replace bare command relay with a device-control service

Current command path:

- route validates queue/session
- route calls `platform.send_command(...)`
- route treats successful HTTP relay as delivery success

Target command path:

1. validate user, queue, and node state
2. create `device_commands` row with `status = issued`
3. create `device_command_events` row `issued`
4. publish MQTT command through platform
5. return a command receipt to caller
6. update final state only when ack/result arrives

This means `sent to platform` and `completed on device` become separate states.

### 2. Add a dedicated device service module

Add a new service, for example:

- `app/services/device_service.py`

Responsibilities:

- build command envelope
- persist command rows
- publish through realtime platform
- consume ack/result/presence/telemetry events
- update node snapshots
- expose query helpers for operator UI and public state

`app/services/platform.py` should remain a transport client, not the device domain layer.

### 3. Update interaction semantics

Current interaction delivery fields should stay, but their meaning should tighten:

- `pending`: command row created, not yet acked
- `sent`: device acked or accepted command
- `failed`: relay, rejection, timeout, or execution error

Longer term, `interactions` should not be the only place command delivery truth lives. It should point to `device_commands`.

Recommended later addition:

- `interactions.device_command_id bigint null references yourmove.device_commands(id)`

### 4. Add inbound device-event handlers

The product needs a way to receive forwarded MQTT-derived events from the platform.

Suggested new product endpoints:

- `POST /internal/device/ack`
- `POST /internal/device/result`
- `POST /internal/device/presence`
- `POST /internal/device/telemetry`
- `POST /internal/device/capabilities`
- `POST /internal/device/state`

These should be private, platform-to-product routes, similar in spirit to the forwarded Stripe webhook.

If the DataRook platform already owns MQTT consumption centrally, keep it that way and forward normalized events into `yourmove`.

### 5. Operator API additions

Extend operator-facing APIs to expose:

- runtime type
- protocol version
- firmware and gateway versions
- online/offline state
- latest fault
- latest command failures
- recent command timeline
- current capability snapshot
- config version applied


## DataRook Platform Changes

The platform already sits between `yourmove` and remote infrastructure for realtime commands and Stripe webhooks. Keep that pattern.

### The platform should own:

- MQTT broker credentials and ACLs
- TLS MQTT termination
- node registration and node identity
- publish path to `nodes/{node_id}/commands`
- consume path from node topics
- product routing by node ownership or product slug
- internal forwarding to `yourmove`

### The platform should not own:

- queue eligibility
- user turn logic
- node economics
- node-specific command semantics beyond transport normalization

### Recommended platform behavior

When `yourmove` issues a command:

1. platform receives normalized command publish request
2. platform publishes to MQTT
3. node ack/result arrives on MQTT
4. platform validates source topic and node identity
5. platform forwards normalized event to `yourmove`

This mirrors the current Stripe ownership model and keeps product services private.


## Raspberry Pi Gateway Plan

The Pi should be treated as a runtime implementation, not as a different product protocol.

### Pi responsibilities

- MQTT client with TLS
- local event spool if internet drops
- process supervision
- local logging
- camera and media coordination
- serial or GPIO bridge to attached controller
- OTA package updates
- richer telemetry collection

### Pi should expose the same cloud contract

The cloud still sees:

- one logical node
- one node topic family
- one capability declaration
- one command lifecycle

Internally the Pi may:

- drive GPIO directly
- proxy to ESP32 over serial
- split one logical node into local subcontrollers

That remains an implementation detail under `runtime_type = pi_gateway`.

### Local Pi-to-controller contract

Do not make this part of the cloud protocol yet.

It can evolve separately and may later use:

- newline-delimited JSON over serial
- protobuf over serial
- local MQTT
- unix socket IPC

The important constraint is that the Pi must translate local execution back into the same cloud ack/result/state model.


## Rollout Plan

### Phase 1: Schema and service foundation

- add `runtime_type`, `device_type`, and snapshot columns to `nodes`
- add `device_commands`
- add `device_command_events`
- add `device_telemetry`
- add `device_faults`
- add `device_command_id` to `interactions` later if desired

### Phase 2: Command issuance path

- add `device_service`
- change `POST /nodes/{slug}/command` to create command rows
- preserve existing public API response shape for compatibility
- stop treating transport acceptance as final execution truth

### Phase 3: Platform-forwarded device events

- add internal event ingestion routes
- update command status from ack/result
- update node presence, snapshots, and telemetry
- surface command history and state in operator APIs

### Phase 4: Developer and operator visibility

- update developer docs page with protocol v1
- show runtime type, firmware, capabilities, and fault state in dashboard
- add recent command timeline and health counters

### Phase 5: ESP32 direct reference runtime

- implement protocol v1 on direct ESP32
- validate command lifecycle, disconnect safety, and telemetry

### Phase 6: Raspberry Pi gateway runtime

- implement gateway agent on Pi
- preserve protocol v1 externally
- optionally bridge to attached ESP32 over serial


## Initial Migration Strategy

This should be introduced with explicit SQL migrations in `migrations/`, following the migration discipline already added to the repo.

Recommended first migration set:

1. extend `yourmove.nodes`
2. create `yourmove.device_commands`
3. create `yourmove.device_command_events`
4. create `yourmove.device_telemetry`
5. create `yourmove.device_faults`

Backfill strategy:

- set `runtime_type = 'esp32_direct'` for existing rows
- set `device_type = 'water_turret'` for existing rows unless overridden
- backfill `reported_state.position` from `turret_pan` and `turret_tilt`
- leave capability and config snapshots empty until first real report


## Open Decisions

These are the main decisions still worth settling before implementation:

1. Does the DataRook platform already persist MQTT ack/result, or should `yourmove` become the source of truth?
2. Should target commands like `set_target` receive both ack and result, or only result on a throttled cadence?
3. What timeout marks a command as failed for each device type?
4. Should Pi gateways be allowed to acknowledge before attached MCU execution finishes, or only after local delegation succeeds?
5. How much telemetry history belongs in `yourmove` versus the platform?


## Recommended Immediate Next Step

Implement Phase 1 and Phase 2 first:

- add the schema
- add `device_service`
- convert `/nodes/{slug}/command` to issue command records with `command_id`

That gives `yourmove` a real device-control backbone without waiting for the full MQTT return path.


---

# Capability Schema v2 — Dashboard-Rendering Contract

The v1 capability model above defines the *minimum* a device must publish for the command path to work. Schema v2 is a strict superset that adds the metadata the operator dashboard needs to render a device-agnostic UI.

**Core principle:** the platform renders what the device declares. It does not enforce safety ceilings, value maxes, device-type allowlists, or schema-level governance. Sensible defaults and safety bounds are firmware author responsibility. The platform's job is to expose the *what* (capabilities) and give creators low-friction UI to tune the *how*.

## Versioning and fallback

- Payloads include `"schema_version": 2`. Missing or `1` is treated as v1 (render minimal fallback UI, dev can inject config manually).
- v2 is non-breaking: every v1 field keeps its v1 meaning.
- Dashboard never rejects a snapshot for being "invalid" — it renders whatever fields it recognizes and ignores the rest.

## Full v2 example

```jsonc
{
  "schema_version": 2,
  "node_type": "treat_dispenser",
  "device_type": "treat_dispenser",         // informational only, not enforced
  "runtime_type": "pi_gateway",
  "protocol_version": 1,
  "firmware_version": "0.1.0",
  "gateway_version": "0.4.0",

  "commands": {
    "dispense": {
      "class": "action",
      "label": "Dispense treat",
      "replaceable": false,
      "params": {
        "duration_ms": {
          "type": "int",
          "min": 50,
          "max": 5000,
          "step": 50,
          "default": 400,
          "unit": "ms",
          "label": "Dispense duration",
          "quick_edit": true
        }
      }
    },
    "arm":    { "class": "mode", "label": "Arm",    "params": {} },
    "disarm": { "class": "mode", "label": "Disarm", "params": {} },
    "estop":  { "class": "mode", "label": "E-Stop", "params": {} }
  },

  "tuning": {
    // Non-command config that affects behavior — calibration, inversions, offsets.
    // Editable on Developer tab. Anything with `quick_edit: true` also appears on Setup.
    "relay_idle_state": {
      "type": "enum",
      "options": ["open", "closed"],
      "default": "open",
      "label": "Relay idle state",
      "dev_only": true
    }
  },

  "limits": {
    // Generic governance. Present on any device type, not just dispensers.
    // Platform persists edits to `config_snapshot.limits`; firmware enforces them.
    "rates": {
      "dispense": {
        "max_per_minute": { "default": 6,  "configurable": true, "label": "Max per minute" },
        "max_per_hour":   { "default": 30, "configurable": true, "label": "Max per hour"   }
      }
    },
    "per_user": {
      "dispense": {
        "max_per_session": { "default": 3,  "configurable": true, "label": "Max per session" },
        "max_per_day":     { "default": 10, "configurable": true, "label": "Max per day"     }
      }
    },
    "cooldown_ms": {
      "dispense": { "default": 2000, "configurable": true, "label": "Cooldown" }
    }
  },

  "video": {
    "streams": [
      { "id": "primary", "label": "Primary camera", "kind": "whep", "hint": "/media/primary" }
    ]
  },

  "telemetry": {
    // Fields the device publishes on nodes/{id}/telemetry. Dashboard uses these
    // descriptors to render readouts on the Operator Readout card.
    "battery_pct":   { "label": "Battery",     "unit": "%",  "kind": "gauge" },
    "feeder_level":  { "label": "Feeder fill", "unit": "%",  "kind": "gauge" }
  }
}
```

## Param schema

A command `params` entry (or a `tuning` entry) has this shape:

| Field         | Required | Meaning |
|---------------|----------|---------|
| `type`        | yes      | `"int"`, `"float"`, `"bool"`, `"string"`, `"enum"` |
| `default`     | yes      | Value used when neither `config_snapshot` nor legacy columns override |
| `label`       | no       | Human-readable name for the UI |
| `min`/`max`   | no       | UI slider/input hints. Not enforced platform-side |
| `step`        | no       | UI slider increment |
| `unit`        | no       | Display suffix (ms, %, °) |
| `options`     | enum-only | List of `{value,label}` or raw values |
| `quick_edit`  | no       | If `true`, param surfaces on Setup tab for all audiences |
| `dev_only`    | no       | If `true`, param is hidden from Setup even when `quick_edit` is set |
| `description` | no       | Tooltip/help text |

`quick_edit` and `dev_only` are not mutually exclusive — `dev_only` wins. Default visibility is Developer-only.

## Limits schema

`limits` is a top-level section with three sub-shapes:

- **`rates.<command>.<window>`** — hard rate caps across all users (`max_per_minute`, `max_per_hour`, `max_per_day`).
- **`per_user.<command>.<window>`** — caps scoped to a single user identity (`max_per_session`, `max_per_day`, `max_per_week`).
- **`cooldown_ms.<command>`** — minimum time between successive invocations, regardless of user.

Each leaf entry is an object `{ default, configurable, label }`. `configurable: true` means the dashboard renders an editor; `false` means it's shown read-only as a firmware-declared fact.

The platform does not enforce limits at the command-issue path — it persists edits to `config_snapshot.limits` and publishes them to the device via `nodes/{id}/state/desired`. The firmware is responsible for actually rejecting or throttling commands that exceed the configured limits.

## Effective config resolution

When the dashboard or command path needs a live value:

```
effective_config(node) =
    capability_snapshot.defaults       ← from param.default
  ← node.config_snapshot               ← operator edits persist here
  ← legacy typed columns               ← compat shim, last wins for back-compat
```

The legacy-column wins is intentional: it lets chicken-blaster keep booting without a capability snapshot being published yet. As firmwares start publishing v2 capabilities and `config_snapshot` takes over, the typed columns become unused and can be dropped.

No validator sits in the middle. If `config_snapshot.commands.fire.duration_ms` is `999999`, the dashboard shows `999999` and the device publish carries `999999`. The firmware clamps or rejects downstream; the operator sees the result in the command event log.

## Rendering rules

### Setup tab (both audiences)

1. **Video Source / Feeds** — always rendered, universal.
2. **Command Quick Controls** — for each entry in `commands`, if any of its `params` has `quick_edit: true` and `!dev_only`, render a card: command label, editable quick params, and a "Test" button.
3. **Limits & Governance** — if `limits` is non-empty and any leaf has `configurable: true`, render grouped editors (Rates / Per-User / Cooldowns).
4. **Conditional Rig Behavior** — turret-style sections render only when their prerequisites exist in `commands`:
   - *Motion Limits* requires `commands.aim` (or equivalent `target`-class command with `pan`/`tilt` params).
   - *Targeting Tuning* requires both aim and fire commands.
5. **Operator Readout** — driven by `telemetry` descriptors.

### Developer tab

1. Device Registration Details (MQTT topics, bootstrap credentials, BYO checklist) — existing card.
2. Device Lab — existing mock/scenario card.
3. **Capability Inspector** — pretty-print `capability_snapshot` verbatim. The escape hatch for diagnosing weird firmware.
4. **Raw Config Editor** — JSON editor for `config_snapshot`. Writes bypass the generic renderer entirely.
5. **Control Layout Editor** — existing full-JSON layout editor.
6. All `tuning` params (including `dev_only: true`).
7. All command params that aren't `quick_edit`.
8. Edge Publisher, Video Advanced — existing cards.

### Fallback when `capability_snapshot` is empty

Render a "Awaiting device handshake" banner on Setup, plus a Developer-tab tool to manually inject a starter capability payload for bootstrapping new hardware.

## Migration: turret-specific Node columns

Today the `Node` table carries typed columns that only make sense for water turrets:

- `motion_pan_min`, `motion_pan_max`, `motion_tilt_min`, `motion_tilt_max`
- `fire_duration_ms`
- `ui_pan_inverted`, `ui_tilt_inverted`
- `targeting_offset_x`, `targeting_offset_y`, …
- `ptz_*`

These move into `config_snapshot` under a namespaced structure:

```jsonc
{
  "commands": {
    "aim":  { "params": { "pan_min": -70, "pan_max": 70, "tilt_min": -20, "tilt_max": 35 } },
    "fire": { "params": { "duration_ms": 300 } }
  },
  "tuning": {
    "ui_pan_inverted": false,
    "ui_tilt_inverted": false,
    "targeting_offset_x": 0,
    "targeting_offset_y": 0
  }
}
```

Migration path:

1. Add `config_snapshot` JSONB column (already in Phase 1).
2. Backfill existing turret rows by reading typed columns → writing the namespaced JSON.
3. Dashboard reads via `effective_config(node)` (merge pipeline above). Legacy columns still win during the compat window so nothing breaks.
4. Once turret firmware starts publishing v2 capabilities and writing `config_snapshot` on edit, the dashboard stops reading typed columns.
5. In a later migration, drop the typed columns.

## Starter firmwares

Reference implementations live in `device/` as opt-in starting points:

- `device/esp32_direct/` — water turret, `esp32_direct` runtime
- `device/pi_gateway/` — water turret on Pi, `pi_gateway` runtime
- `device/treat_dispenser/` — single-relay dispenser, `pi_gateway` runtime
- more added as new prebuilt products ship

Each starter publishes a v2 capability snapshot with sensible defaults and documents which params are expected to be `quick_edit` vs `dev_only`. Nothing about those starters is enforced platform-side — a BYO dev can fork them, modify the capability payload, or write from scratch. The dashboard will render whatever ships up.

