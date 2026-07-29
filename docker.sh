#!/usr/bin/env bash
# docker.sh — build the ubersdr_voiceskimmer Docker image
#
# Usage:
#   ./docker.sh [build|push|run|arm64]
#
#   build  — build the image for linux/amd64 (default, local load)
#   arm64  — build the image for linux/arm64 (Raspberry Pi, Apple Silicon, etc.)
#   push   — build multi-platform manifest (amd64 + arm64) via buildx and push
#   run    — run the image locally (set env vars below)
#
# build, arm64 and push all run the test suite first and refuse to build if it
# fails. See run_tests().
#
# Environment variables (build):
#   IMAGE      Docker image name/tag   (default: madpsy/ubersdr_voiceskimmer:latest)
#   PLATFORM   Docker --platform flag  (default: linux/amd64)
#   BUILDER    buildx builder name     (default: ubersdr_voiceskimmer_builder)
#   SKIP_TESTS Set to 1 to build without running the tests

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${IMAGE:-madpsy/ubersdr_voiceskimmer:latest}"
PLATFORM="${PLATFORM:-linux/amd64}"
BUILDER="${BUILDER:-ubersdr_voiceskimmer_builder}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() { echo "error: $*" >&2; exit 1; }

check_deps() {
    command -v docker >/dev/null || die "docker not found in PATH"
}

# Ensure a buildx builder that supports multi-platform builds exists.
# Uses the existing builder if already present; creates one otherwise.
ensure_builder() {
    if ! docker buildx inspect "$BUILDER" &>/dev/null; then
        echo "Creating buildx builder '$BUILDER'..."
        docker buildx create --name "$BUILDER" --driver docker-container --bootstrap
    else
        echo "Using existing buildx builder '$BUILDER'."
    fi
}

# Run the test suite before anything is built or published.
#
# Both suites matter and neither subsumes the other. pytest covers extraction,
# rotation, spotting and the HTTP layer; test_frontend.js loads static/app.js
# in a stubbed DOM, which is the only thing that catches a dashboard that
# parses cleanly and then dies on load — a `node --check` passes such a file
# happily, and one shipped that way (initMap() reading a `let` still in its
# temporal dead zone took the whole page down).
#
# Set SKIP_TESTS=1 to bypass, for when you need an image out and already know.
run_tests() {
    if [[ "${SKIP_TESTS:-0}" == "1" ]]; then
        echo "SKIP_TESTS=1 — skipping tests."
        return
    fi

    local py="$SCRIPT_DIR/.venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3 || true)"
    if [[ -n "$py" ]] && "$py" -c "import pytest" 2>/dev/null; then
        echo "Running Python tests..."
        (cd "$SCRIPT_DIR" && "$py" -m pytest -q) \
            || die "Python tests failed — not building. Set SKIP_TESTS=1 to override."
    else
        echo "warning: pytest unavailable, skipping Python tests" >&2
    fi

    if command -v node >/dev/null; then
        echo "Running frontend tests..."
        (cd "$SCRIPT_DIR" && node test_frontend.js) \
            || die "Frontend tests failed — not building. Set SKIP_TESTS=1 to override."
    else
        echo "warning: node not found, skipping frontend tests" >&2
    fi
}

stage_context() {
    TMPCTX="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap 'rm -rf "$TMPCTX"' EXIT

    echo "Staging build context in $TMPCTX..."
    rsync -a --exclude='.git' \
              --exclude='.venv' \
              --exclude='__pycache__' \
              --exclude='*.pyc' \
              --exclude='*.jsonl' \
              --exclude='*.log' \
              --exclude='voiceskimmer_data' \
              "$SCRIPT_DIR/" "$TMPCTX/"
}

build() {
    check_deps
    run_tests
    stage_context

    echo "Building image $IMAGE (platform=$PLATFORM)..."
    docker build \
        --platform "$PLATFORM" \
        --tag "$IMAGE" \
        "$TMPCTX"

    echo "Built: $IMAGE"
}

push() {
    check_deps
    run_tests
    ensure_builder
    stage_context

    local platforms="linux/amd64,linux/arm64"
    echo "Building and pushing multi-platform image $IMAGE (platforms=$platforms)..."
    docker buildx build \
        --builder "$BUILDER" \
        --platform "$platforms" \
        --tag "$IMAGE" \
        --push \
        "$TMPCTX"

    echo "Pushed multi-platform manifest: $IMAGE"

    # Refresh the local tag to match what was just pushed. buildx uses the
    # docker-container driver, which exports the manifest straight to the
    # registry and never loads it into the local image store — so without
    # this the local $IMAGE stays at whatever the last plain build produced.
    # Anything run locally afterwards (docker run, docker compose up without
    # a pull) would silently use that stale image. Layers are already cached
    # from the build above, so this costs seconds.
    echo "Loading $PLATFORM into the local image store..."
    docker build \
        --platform "$PLATFORM" \
        --tag "$IMAGE" \
        "$TMPCTX"

    echo "Local image now matches the pushed one."

    # Push whatever is already committed — but never commit on the user's
    # behalf. This previously ran "git add -A" and committed everything with
    # a generic "Release" message, which silently swallowed real commit
    # messages and would sweep any unrelated work in progress (or a stray
    # credentials file) into a public push with no chance to review it.
    if [[ -n "$(git status --porcelain)" ]]; then
        echo
        echo "WARNING: uncommitted changes — the image was built from them," >&2
        echo "         but they are NOT being committed or pushed:" >&2
        git status --short >&2
        echo >&2
        echo "         Commit them yourself, then run: git push" >&2
        exit 1
    fi

    echo "Pushing git repository..."
    git push
}

run_image() {
    local args=()

    [[ -n "${UBERSDR_HOST:-}"      ]] && args+=(-e "UBERSDR_HOST=$UBERSDR_HOST")
    [[ -n "${UBERSDR_PORT:-}"      ]] && args+=(-e "UBERSDR_PORT=$UBERSDR_PORT")
    [[ -n "${UBERSDR_SSL:-}"       ]] && args+=(-e "UBERSDR_SSL=$UBERSDR_SSL")
    [[ -n "${UBERSDR_PASS:-}"      ]] && args+=(-e "UBERSDR_PASS=$UBERSDR_PASS")
    [[ -n "${BAND:-}"              ]] && args+=(-e "BAND=$BAND")
    [[ -n "${SPOT:-}"              ]] && args+=(-e "SPOT=$SPOT")
    [[ -n "${SPOTTER_CALL:-}"      ]] && args+=(-e "SPOTTER_CALL=$SPOTTER_CALL")
    [[ -n "${SPOTTER_PASS:-}"      ]] && args+=(-e "SPOTTER_PASS=$SPOTTER_PASS")
    [[ -n "${WEB_PORT:-}"          ]] && args+=(-e "WEB_PORT=$WEB_PORT")

    docker run --rm -it \
        --platform "$PLATFORM" \
        -p "${WEB_PORT:-6098}:${WEB_PORT:-6098}" \
        -v "$SCRIPT_DIR/voiceskimmer_data:/data" \
        "${args[@]}" \
        "$IMAGE" \
        "$@"
}

# ---------------------------------------------------------------------------
# Environment variable reference (for docker run -e ...)
# ---------------------------------------------------------------------------
#
#   UBERSDR_HOST     UberSDR host (default: ubersdr)
#   UBERSDR_PORT     UberSDR port (default: 8080)
#   UBERSDR_SSL      Set to 1 to use https/wss
#   UBERSDR_PASS     UberSDR bypass password (optional)
#   BAND             Comma-separated band list (default: all bands)
#   SPOT             Set to 1 to submit DX spots
#   SPOTTER_CALL     DX cluster login callsign (required with SPOT)
#   SPOTTER_PASS     DX cluster spot password (required with SPOT)
#   WEB_PORT         Dashboard port (default: 6098)
#
# See entrypoint.sh for the full list of supported environment variables.

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-build}" in
    build) build ;;
    arm64) PLATFORM=linux/arm64 build ;;
    push)  push  ;;
    run)   shift; run_image "$@" ;;
    *)
        echo "Usage: $0 [build|arm64|push|run [args...]]" >&2
        exit 1
        ;;
esac
