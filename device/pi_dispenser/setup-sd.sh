#!/bin/bash
# DEPRECATED — Use image/build.sh to create a custom image instead.
# This script is kept as a manual fallback for development/debugging.
#
# For production: cd device/pi_dispenser/image && bash build.sh
echo "This script is deprecated. Use the custom image builder instead:"
echo "  cd device/pi_dispenser/image && bash build.sh"
echo ""
echo "The custom image includes all packages pre-installed."
echo "Flash it, plug in the Pi, AP comes up. No internet needed."
exit 1
