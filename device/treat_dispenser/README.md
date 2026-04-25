# Treat Dispenser Reference Client

A minimal non-turret device that publishes a v2 capability snapshot and
exercises the dashboard's generic Setup controls. Use it as a starting
point for any "one relay, one action" device: treat dispensers, bubble
machines, party-button buzzers, etc.

## What it does

On connect, the client publishes a retained capability payload shaped
against the v2 schema (see [docs/device-runtime-spec.md](../../docs/device-runtime-spec.md#capability-schema-v2--dashboard-rendering-contract)):

- `commands.dispense` — a single action with a `duration_ms` param
  flagged `quick_edit`, so the platform dashboard surfaces it on the
  Setup tab for everyone (not just developers).
- `limits.rates.dispense.*` — shared caps the creator can tune.
- `limits.per_user.dispense.*` — per-viewer caps.
- `limits.cooldown_ms.dispense` — minimum gap between dispenses.

The platform renders those entries. It does not police them. The
firmware here is the one that enforces cooldowns and clamps durations
against what the hardware can actually do.

## What the dashboard should show

With this capability payload and `device_type = "treat_dispenser"`:

- Turret sections (Controls/Targeting/Motion, Feed Tools with sweep and
  fire buttons, Crosshair Calibration, Fire tuning) are hidden.
- Setup tab shows a generic "Device Commands" card with a
  `Dispense duration` number input.
- Setup tab shows a "Limits & Governance" card with four rate inputs
  (shared per-minute/per-hour, per-user per-minute/per-hour) plus a
  cooldown input.
- Developer tab shows the raw capability JSON in the Capability
  Inspector, and the full `config_snapshot` in the Raw Config editor.

## Running

```
python device/treat_dispenser/dispenser.py \
  --host yourmove.live --port 1883 \
  --node-id 42 \
  --username node_xxx --password secret
```

All flags are optional except `--node-id`. Defaults mirror the
capability payload the dashboard will render.

## Adapting to real hardware

Replace `RelayAdapter.pulse()` with your GPIO / serial driver. Every
other detail — the command name, the param list, the limits — is yours
to redefine in `build_capability_payload()`. If you rename `dispense`
to `squirt` or add a `flavor` param, the dashboard follows.
