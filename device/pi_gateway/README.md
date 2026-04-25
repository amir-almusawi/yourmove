# Pi Gateway Skeleton

This is the reference starting point for the future `pi_gateway` runtime.

Use it when:

- you want MQTT and orchestration on a Raspberry Pi
- you expect multiple local controllers or richer media/device handling
- you eventually want serial bridging to an attached MCU

What this skeleton includes:

- MQTT connection and topic subscription
- retained presence, capabilities, state, and telemetry
- a local adapter abstraction
- direct action handling for `aim`, `arm`, `disarm`, and `fire`
- a clean seam to replace the in-process adapter with serial or GPIO code later

Run it like:

```bash
python device/pi_gateway/gateway.py \
  --host yourmove.live \
  --node-id 1 \
  --username node_xxx \
  --password secret
```
