#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "${SCRIPT_DIR}/ssh-key.pub" ]; then
    install -m 644 "${SCRIPT_DIR}/ssh-key.pub" "${ROOTFS_DIR}/tmp/yourmove-ssh-key.pub"
fi
