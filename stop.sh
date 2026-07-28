#!/usr/bin/env bash
# stop.sh — stop the ubersdr_voiceskimmer service

set -euo pipefail

INSTALL_DIR="${HOME}/ubersdr/voiceskimmer"

cd "${INSTALL_DIR}"
echo "Stopping ubersdr_voiceskimmer..."
docker compose down
echo "Done."
