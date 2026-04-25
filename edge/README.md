# Edge Publisher

Local runtime for publishing camera video from your network into the hosted YourMove platform.

This layer is meant to run on a machine near the camera, not inside the hosted platform.

## What It Does

- probes the local RTSP camera
- verifies the local publisher/container is still healthy
- restarts the publisher when needed
- reports edge health back to YourMove
- optionally records clip segments for upload
- optionally runs local CV/game-layer analysis

## Core Files

- [`edge_supervisor.py`](edge_supervisor.py)
  Main supervisor loop for health checks, restart policy, heartbeats, and clip handling.
- [`run_edge_supervisor.sh`](run_edge_supervisor.sh)
  Wrapper script for running the supervisor with a config file.
- [`install_user_service.sh`](install_user_service.sh)
  Installs a per-user systemd service that points at your local checkout.
- [`chicken-blaster.sample.json`](chicken-blaster.sample.json)
  Safe example config. Replace placeholders with node-specific values from the dashboard.
- [`run_edge_cv.sh`](run_edge_cv.sh)
  Starts the optional CV runtime.
- [`setup_cv.sh`](setup_cv.sh)
  Installs CV dependencies, enables CV in config, and restarts the supervisor.

## Config

The supervisor expects a JSON config with values like:

- `node_slug`
- `base_url`
- `auth_token`
- `rtsp_url`
- `container_name`
- `restart_command`

Use the dashboard-generated values for the node-specific auth token and restart command. Do not hardcode production credentials into git.
