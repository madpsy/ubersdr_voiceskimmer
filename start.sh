#!/usr/bin/env bash
# start.sh — start the ubersdr_voiceskimmer service

set -euo pipefail

INSTALL_DIR="${HOME}/ubersdr/voiceskimmer"

cd "${INSTALL_DIR}"
echo "Starting ubersdr_voiceskimmer..."
docker compose up -d --remove-orphans
echo "Done."
echo "  View logs : docker compose logs -f"
echo "  Dashboard : http://localhost:6098"
