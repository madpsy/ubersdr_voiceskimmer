#!/bin/sh
# entrypoint.sh — translate environment variables into scanner.py flags
#
# Environment variables:
#   UBERSDR_HOST          UberSDR host                          (default: ubersdr)
#   UBERSDR_PORT          UberSDR port                          (default: 8080)
#   UBERSDR_SSL           Set to 1 to use https/wss
#   UBERSDR_PASS          Bypass password, if the instance needs one
#   PARALLEL              Scanning sessions to run at once      (default: 1)
#                         Each holds a Whisper slot for the whole run, and the
#                         server's whisper.max_users defaults to 2 — so 2 here
#                         uses every slot and leaves none for web UI users.
#   BAND                  Comma-separated band list             e.g. 20m,40m
#                         (default: all bands)
#   DWELL                 Seconds per frequency                 (default: 30)
#   MAX_DWELL             Hard ceiling per frequency             (default: 180)
#   MIN_SNR               Ignore activity below this SNR         (default: 8)
#   MIN_CONFIDENCE        Ignore activity below this confidence   (default: 0.7)
#   SILENCE_MIN_SNR       Peak SNR (dB) within SILENCE_TIMEOUT for a frequency
#                         to count as active (default: 40). Measured from the
#                         audio frame headers, independent of transcription.
#   SILENCE_TIMEOUT       Seconds to wait for that peak before moving on (default: 10)
#   LOCK_FREQ             Stick to one frequency (Hz) instead of hopping
#   LOCK_MODE             Mode for LOCK_FREQ                     (default: usb)
#   PROMPT                Custom Whisper initial prompt
#   ASR_LANGUAGE          Whisper recognition language           (default: en)
#   STOCK_WHISPER         Set to 1 to skip per-attach Whisper params
#                         (required if the server lacks whisper.allow_client_params)
#   SPOT                  Set to 1 to submit DX spots for confirmed callsigns
#   SPOTTER_CALL          Callsign to log in to the DX cluster with (required with SPOT)
#   SPOTTER_PASS          DX cluster spot password                (required with SPOT)
#   SPOT_TAG              Tag prefixed to every spot comment      (default: [Voice])
#   SPOT_COOLDOWN         Seconds before re-spotting the same station (default: 900)
#   SPOT_FREQ_TOLERANCE   Hz tolerance for treating two hearings as the same
#                         station — applies to both the hit count and the
#                         cooldown (default: 500)
#   MIN_CALLSIGN_LENGTH   Minimum length for a phonetically-assembled callsign
#                         to be looked up at all (default: 4)
#   SPOT_MIN_HITS         Decodes of the same callsign on the same frequency
#                         required before spotting (default: 2)
#   WEB_PORT              Dashboard port                          (default: 6098, 0 disables)
#   OUTPUT                JSONL detection log path                (default: /data/detections.jsonl)
#   VERBOSE               Set to 1 for verbose logging
#   EXTRA_ARGS            Extra scanner.py args appended verbatim

set -e

args=""

[ -n "$UBERSDR_HOST"        ] && args="$args --host $UBERSDR_HOST"
[ -n "$UBERSDR_PORT"        ] && args="$args --port $UBERSDR_PORT"
[ "$UBERSDR_SSL" = "1"      ] && args="$args --ssl"
[ -n "$UBERSDR_PASS"        ] && args="$args --password $UBERSDR_PASS"

[ -n "$PARALLEL"            ] && args="$args --parallel $PARALLEL"
[ -n "$BAND"                ] && args="$args --band $BAND"
[ -n "$DWELL"                ] && args="$args --dwell $DWELL"
[ -n "$MAX_DWELL"            ] && args="$args --max-dwell $MAX_DWELL"
[ -n "$MIN_SNR"              ] && args="$args --min-snr $MIN_SNR"
[ -n "$MIN_CONFIDENCE"       ] && args="$args --min-confidence $MIN_CONFIDENCE"
[ -n "$SILENCE_MIN_SNR"      ] && args="$args --silence-min-snr $SILENCE_MIN_SNR"
[ -n "$SILENCE_TIMEOUT"      ] && args="$args --silence-timeout $SILENCE_TIMEOUT"
[ -n "$LOCK_FREQ"            ] && args="$args --lock-freq $LOCK_FREQ"
[ -n "$LOCK_MODE"            ] && args="$args --lock-mode $LOCK_MODE"

[ -n "$ASR_LANGUAGE"         ] && args="$args --asr-language $ASR_LANGUAGE"
[ "$STOCK_WHISPER" = "1"     ] && args="$args --stock-whisper"

[ "$SPOT" = "1"              ] && args="$args --spot"
[ -n "$SPOTTER_CALL"         ] && args="$args --spotter-call $SPOTTER_CALL"
[ -n "$SPOTTER_PASS"         ] && args="$args --spotter-pass $SPOTTER_PASS"
[ -n "$SPOT_TAG"             ] && args="$args --spot-tag $SPOT_TAG"
[ -n "$SPOT_COOLDOWN"        ] && args="$args --spot-cooldown $SPOT_COOLDOWN"
[ -n "$SPOT_FREQ_TOLERANCE"  ] && args="$args --spot-freq-tolerance $SPOT_FREQ_TOLERANCE"
# SPOT_MIN_LENGTH is the former name; it now gates the lookup, not just the spot.
[ -n "$SPOT_MIN_LENGTH"      ] && args="$args --min-callsign-length $SPOT_MIN_LENGTH"
[ -n "$MIN_CALLSIGN_LENGTH"  ] && args="$args --min-callsign-length $MIN_CALLSIGN_LENGTH"
[ -n "$SPOT_MIN_HITS"        ] && args="$args --spot-min-hits $SPOT_MIN_HITS"

[ -n "$WEB_PORT"             ] && args="$args --web-port $WEB_PORT"

# PROMPT and OUTPUT can contain spaces, so pass them through their own
# argument pair instead of the space-joined $args string below.
set -- "$@"
if [ -n "$PROMPT" ]; then
    set -- "$@" --prompt "$PROMPT"
fi

# OUTPUT defaults to /data so detections.jsonl survives container restarts
# via the docker-compose bind mount at /data.
OUTPUT="${OUTPUT:-/data/detections.jsonl}"
set -- "$@" --output "$OUTPUT"

[ "$VERBOSE" = "1" ] && set -- "$@" -v

# shellcheck disable=SC2086
exec python3 scanner.py $args "$@" $EXTRA_ARGS
