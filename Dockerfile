# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# ubersdr_voiceskimmer — hops UberSDR voice activity, feeds audio to Whisper,
# extracts phonetic callsigns, validates via QRZ, and optionally spots to the
# DX cluster. Pure Python, no compiled binary — single-stage image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -s /bin/false voiceskimmer

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ ./static/

# Copy entrypoint script (translates env vars to scanner.py flags)
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Create the default data directory (for --output detections.jsonl) and
# ensure the voiceskimmer user owns it.
# Note: no VOLUME declaration — the docker-compose.yml bind mount handles
# persistence. A VOLUME declaration would cause Docker to create a
# root-owned anonymous volume that overwrites the chown, preventing the
# voiceskimmer user from writing to /data.
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /data \
    && chown voiceskimmer:voiceskimmer /data \
    && chmod 755 /data

USER voiceskimmer

# Expose the dashboard port (default; override with WEB_PORT env var)
EXPOSE 6098

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/usr/bin/wget", "-q", "-O", "/dev/null", "http://localhost:6098/"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
