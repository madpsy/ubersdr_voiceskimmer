#!/usr/bin/env bash
# restart.sh — restart the ubersdr_voiceskimmer service

set -euo pipefail

INSTALL_DIR="${HOME}/ubersdr/voiceskimmer"

cd "${INSTALL_DIR}"
echo "Stopping ubersdr_voiceskimmer..."
docker compose down
echo "Starting ubersdr_voiceskimmer..."
docker compose up -d --remove-orphans
echo "Done."
echo "  View logs : docker compose logs -f"
echo "  Dashboard : http://localhost:6098"
