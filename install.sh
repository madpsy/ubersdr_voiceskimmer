#!/usr/bin/env bash
# install.sh — fetch the docker-compose.yml from the ubersdr_voiceskimmer repo and start the service
#
# Requires UberSDR to be installed and running first: https://ubersdr.org
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/madpsy/ubersdr_voiceskimmer/main/install.sh | bash
#   — or —
#   ./install.sh [--force-update]
#
# Options:
#   --force-update   Overwrite an existing docker-compose.yml (default: skip if present)
#
# When piping through bash, pass the flag via env var instead:
#   curl -fsSL ... | FORCE_UPDATE=1 bash

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/madpsy/ubersdr_voiceskimmer/main"
INSTALL_DIR="${HOME}/ubersdr/voiceskimmer"
COMPOSE_FILE="docker-compose.yml"
FORCE_UPDATE="${FORCE_UPDATE:-0}"

# Parse flags when run directly (not piped)
for arg in "$@"; do
    case "$arg" in
        --force-update) FORCE_UPDATE=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

command -v docker >/dev/null || die "docker not found in PATH — please install Docker first"
docker compose version >/dev/null 2>&1 || die "docker compose plugin not found — please install Docker Compose v2"

# ---------------------------------------------------------------------------
# Prepare install directory
# ---------------------------------------------------------------------------

mkdir -p "${INSTALL_DIR}"
cd "${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Fetch compose file
# ---------------------------------------------------------------------------

if [[ -f "${COMPOSE_FILE}" && "${FORCE_UPDATE}" != "1" ]]; then
    echo "${COMPOSE_FILE} already exists — skipping download (use --force-update to overwrite)"
else
    echo "Fetching ${COMPOSE_FILE} from GitHub..."
    curl -fsSL "${REPO_RAW}/${COMPOSE_FILE}" -o "${COMPOSE_FILE}"
    echo "Saved ${COMPOSE_FILE}"
fi

# ---------------------------------------------------------------------------
# Fetch helper scripts
# ---------------------------------------------------------------------------

for script in update.sh start.sh stop.sh restart.sh; do
    echo "Fetching ${script}..."
    curl -fsSL "${REPO_RAW}/${script}" -o "${script}"
    chmod +x "${script}"
    echo "Saved ${script}"
done

# ---------------------------------------------------------------------------
# Create data directory on the host
# ---------------------------------------------------------------------------

DATA_DIR="voiceskimmer_data"
mkdir -p "${INSTALL_DIR}/${DATA_DIR}"
# Make writable by the container's voiceskimmer user
chmod 777 "${INSTALL_DIR}/${DATA_DIR}"
echo "Data directory ready: ${INSTALL_DIR}/${DATA_DIR}"

# ---------------------------------------------------------------------------
# Pull image and start service
# ---------------------------------------------------------------------------

echo "Pulling latest Docker image..."
docker compose pull

echo "Starting ubersdr_voiceskimmer..."
docker compose up -d --remove-orphans --force-recreate

echo ""
echo "Done. ubersdr_voiceskimmer is running."
echo "  View logs  : docker compose logs -f"
echo "  Stop       : ./stop.sh"
echo "  Start      : ./start.sh"
echo "  Restart    : ./restart.sh"
echo "  Update     : ./update.sh"
echo ""
echo "Edit ${INSTALL_DIR}/${COMPOSE_FILE} to set UBERSDR_HOST, BAND, SPOT,"
echo "SPOTTER_CALL/SPOTTER_PASS, etc., then run ./restart.sh to apply."
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  UBERSDR PROXY CONFIGURATION"
echo ""
echo "  Add this addon via the UberSDR Admin → Addon Proxies interface:"
echo ""
echo "    Name         : voiceskimmer"
echo "    Host         : voiceskimmer"
echo "    Port         : 6098"
echo "    Enabled      : true"
echo "    Strip prefix : true"
echo "    Rate Limit   : 100"
echo ""
echo "  Then access the dashboard at: http://your-ubersdr-host/addon/voiceskimmer/"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
