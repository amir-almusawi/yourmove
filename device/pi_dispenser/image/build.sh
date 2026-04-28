#!/bin/bash
# Build a custom YourMove Dispenser Raspberry Pi image using pi-gen.
# Runs in Docker — no ARM cross-compilation setup needed on host.
#
# Usage:
#   cd device/pi_dispenser/image
#   bash build.sh [--ssh-key ~/.ssh/id_rsa.pub]
#
# Output: deploy/ directory with the .img.xz file ready to flash.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PIGEN_DIR="${SCRIPT_DIR}/pi-gen"
SSH_KEY=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

echo "=== YourMove Dispenser Image Build ==="
echo "Repo: ${REPO_DIR}"

# ── 1. Clone pi-gen if needed ──

if [ ! -d "${PIGEN_DIR}" ]; then
    echo "Cloning pi-gen..."
    git clone --depth 1 https://github.com/RPi-Distro/pi-gen.git "${PIGEN_DIR}"
else
    echo "pi-gen already cloned"
fi

# ── 2. Copy config ──

cp "${SCRIPT_DIR}/config" "${PIGEN_DIR}/config"

# ── 3. Skip stages 3-5 (desktop, full desktop, bloat) ──

for stage in stage3 stage4 stage5; do
    mkdir -p "${PIGEN_DIR}/${stage}"
    touch "${PIGEN_DIR}/${stage}/SKIP"
    rm -f "${PIGEN_DIR}/${stage}/EXPORT_IMAGE" "${PIGEN_DIR}/${stage}/EXPORT_NOOBS"
done

# ── 4. Copy our custom stage into pi-gen (not symlink — Docker can't follow host paths) ──

rm -rf "${PIGEN_DIR}/stage-yourmove"
cp -a "${SCRIPT_DIR}/stage-yourmove" "${PIGEN_DIR}/stage-yourmove"

# ── 5. Stage repo subset for 01-app (copy via tmp to avoid cp "into itself" error) ──

rm -rf "${PIGEN_DIR}/stage-yourmove/01-app/repo"
TMPSTAGE="$(mktemp -d)"
mkdir -p "${TMPSTAGE}/device" "${TMPSTAGE}/edge"
rsync -a --exclude='pi_dispenser/image/pi-gen' "${REPO_DIR}/device/" "${TMPSTAGE}/device/"
cp -a "${REPO_DIR}/edge/." "${TMPSTAGE}/edge/"
mv "${TMPSTAGE}" "${PIGEN_DIR}/stage-yourmove/01-app/repo"

# ── 6. Copy SSH public key if provided ──

if [ -n "$SSH_KEY" ] && [ -f "$SSH_KEY" ]; then
    echo "Including SSH key: ${SSH_KEY}"
    cp "$SSH_KEY" "${PIGEN_DIR}/stage-yourmove/02-config/ssh-key.pub"
else
    # Try default key locations
    for keyfile in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub; do
        if [ -f "$keyfile" ]; then
            echo "Found SSH key: ${keyfile}"
            cp "$keyfile" "${PIGEN_DIR}/stage-yourmove/02-config/ssh-key.pub"
            break
        fi
    done
fi

# ── 7. Copy SSH key into image if we have one ──
# The chroot script looks for /tmp/yourmove-ssh-key.pub. We stage it
# so pi-gen's file injection picks it up.

if [ -f "${PIGEN_DIR}/stage-yourmove/02-config/ssh-key.pub" ]; then
    # Create a run.sh (host-side) that copies the key into rootfs
    cat > "${PIGEN_DIR}/stage-yourmove/02-config/00-run.sh" <<'HOSTSCRIPT'
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "${SCRIPT_DIR}/ssh-key.pub" ]; then
    install -m 644 "${SCRIPT_DIR}/ssh-key.pub" "${ROOTFS_DIR}/tmp/yourmove-ssh-key.pub"
fi
HOSTSCRIPT
    chmod +x "${PIGEN_DIR}/stage-yourmove/02-config/00-run.sh"
fi

# ── 8. Make all scripts executable ──

find "${PIGEN_DIR}/stage-yourmove" -name "*.sh" -exec chmod +x {} \;

# ── 9. Build with Docker ──

echo ""
echo "Starting pi-gen Docker build (this takes 20-40 minutes)..."
echo "Image will be output to: ${PIGEN_DIR}/deploy/"
echo ""

cd "${PIGEN_DIR}"
./build-docker.sh

# ── 10. Copy output ──

echo ""
if ls "${PIGEN_DIR}/deploy/"*.img.xz 1>/dev/null 2>&1; then
    mkdir -p "${SCRIPT_DIR}/deploy"
    cp "${PIGEN_DIR}/deploy/"*.img.xz "${SCRIPT_DIR}/deploy/"
    cp "${PIGEN_DIR}/deploy/"*.info "${SCRIPT_DIR}/deploy/" 2>/dev/null || true
    echo "=== Build complete! ==="
    echo "Image: $(ls ${SCRIPT_DIR}/deploy/*.img.xz)"
    echo ""
    echo "Flash with:"
    echo "  xzcat deploy/*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress"
    echo "  OR use Raspberry Pi Imager -> 'Use custom' and select the .img.xz file"
else
    echo "Build may have failed — check ${PIGEN_DIR}/deploy/ and build logs"
    exit 1
fi
