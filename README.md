# ubersdr_voiceskimmer

Hops around detected voice activity, feeds each frequency to the Whisper
speech-to-text extension, extracts candidate callsigns from the transcript, and
validates every one against QRZ. Optionally submits confirmed callsigns as real
DX spots. A live dashboard (transcript, confirmed callsigns, band/freq
activity, DX spots) is built in — see [Docker / Deployment](#8-docker--deployment)
to run it as a container alongside UberSDR, or `--web-port` when running from
source.

The real output is a JSONL log — one record per candidate, with the raw
transcript that produced it. That file is what tells you whether the approach
works on your bands and conditions.

## What the UberSDR instance must have

This is a client. It does none of the signal processing itself, so the instance
it points at has to provide all of it:

| Needed | For | Without it |
|---|---|---|
| **Whisper enabled** (`whisper.enabled: true`, with a reachable `whisper.server_url`) | Transcription — the entire input to this tool | Nothing works at all |
| **Lookup services enabled** (`lookup_services.enabled: true`) | Validating extracted callsigns against QRZ | Every candidate stays unvalidated, so nothing is ever confirmed |
| **Noise floor monitoring** (`noisefloor.enabled: true`) | The voice activity feed this hops around | No targets, nothing to scan |
| **The `dxcluster` addon installed** | Submitting DX spots, via `/addon/dxcluster/api/terminal` | The scan still runs and confirms callsigns, but `--spot` cannot submit anything |

The first three are mandatory. The DX cluster addon is only needed if you want
spots submitted — which for most people is the point, so it is listed here
rather than buried in the spotting section. Note the revisit settings depend on
it too: they trigger on a spot this scanner submitted, so with no cluster they
never fire (see [Revisits](#revisits)).

Run `scanner.py --check` against the instance to have all of this verified for
you before you start — see [Check the instance first](#2-check-the-instance-first).

---

## 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Three dependencies: `requests`, `websocket-client`, and `flask` (for the
dashboard). No audio codecs — the client never decodes audio (see
[Why muting is free](#why-muting-is-free)).

## 2. Check the instance first

```bash
.venv/bin/python scanner.py --check --host 44.31.241.7 --port 8080
```

```
Pre-flight: http://44.31.241.7:8080

  OK    Instance reachable — UberSDR 0.1.58
  OK    Speech-to-text enabled
  OK    Lookup service enabled
  OK    Noise floor monitoring enabled
  OK    10 voice signals active across 3 band(s)
  OK    Bypassed IP — no lookup rate limit, no session time cap
  OK    CTY available — free unallocated-prefix filter active
  WARN  Cannot detect the whisper parameter policy remotely...

Ready to scan.
```

`--check` exits without scanning. Any `FAIL` must be fixed first; `WARN` items
still let a scan run, and the output tells you which flags to add.

### Server requirements

| Setting | Needed for |
|---|---|
| `whisper.enabled: true` | Transcription at all. Also needs a reachable `whisper.server_url` |
| `lookup_services.enabled: true` | QRZ validation — without it there is nothing to check against |
| `noisefloor.enabled: true` | Voice activity detection, i.e. somewhere to hop |
| `whisper.allow_client_params: true` | The tuned recognition parameters. Not needed when the server trusts this container — see [Trusted container](#trusted-container) |
| The `dxcluster` addon installed | Submitting DX spots. Only needed with `--spot`; the scan itself runs without it |

## 3. Run it

```bash
.venv/bin/python scanner.py --host 44.31.241.7 --port 8080 --verbose
```

A live dashboard is served on `--web-port` (default `6098`, `0` disables it) —
open `http://localhost:6098/` while it runs for the transcript, confirmed
callsigns, band/freq activity, and DX spots submitted, updating in real time.
Rows and transcript lines are tinted by band, and the confirmed and spots
tables each carry a free-text filter over callsign, frequency and name.

Both tables show a **rolling 24 hours** — the same window as the charts below
them, so nothing on the page can disagree about what the last day looked like.
A confirmed row ages on when the station was *last* heard, not when it was
first, so one worked all day stays put rather than vanishing while it is still
talking. The window applies to the history a page load is handed as well as to
an already-open dashboard, which expires its own rows as they fall out.

The dashboard's **🔈 Listen** button plays the audio the scanner is currently
hearing, following it as it hops. See
[Audio preview](#how-the-audio-preview-works) for what it does server-side.

**Click any completed transcript line** to see what the extractor made of it:
what each word mapped to, which runs were assembled, the evidence each scored
against the gate, and which gate a candidate died at. This is the fastest way
to work out why an obvious callsign produced nothing. It is served by
`POST /api/explain`, which makes no QRZ lookup and is rate limited to one
request per second per address.

The **activity chart** under the map shows the last 24 hours per band, in the
same colours as the tables, as two stacks per hour — confirmed and spotted.
Spotted is necessarily a subset of confirmed, so side-by-side stacks make the
gap between "heard" and "actually submitted" visible where one combined stack
would hide it. Both count *distinct callsigns* per hour rather than events: a
station heard fifty times is one station, and a re-spot after the cooldown is
the same station again, so the chart reads as "stations active" rather than
being dominated by whichever operator talked most. The window is rolling and
always a full 24 hours, so a quiet night reads as quiet rather than as a
missing axis.

Under it, **top callsigns** shows the ten busiest stations over the same rolling
24 hours, two bars each: confirmed hits and DX submissions. Hits are summed
across every frequency a station was heard on — two rows in the confirmed table
are one bar here. The bars sit side by side rather than stacked, because a
submission is a subset of the hits and adding them would mean nothing.

Both charts are summed from the same hourly buckets, so they age out together
and cannot disagree about what happened. Note the two read those buckets
differently, which is deliberate: the per-band chart counts *distinct
callsigns* per hour, so one talkative station cannot tower over a band full of
quieter ones, while this one counts *total hits*, which is the question it is
asking.

The **map** plots stations against the receiver, using the coordinates QRZ
returns (falling back to the DXCC entity centre from CTY). Confirmed and
spotted stations are separate layers, both on by default — a spotted callsign
is necessarily a confirmed one, so they partition rather than overlap, and
turning off *Confirmed* answers "what did I actually submit". One marker per
callsign however many bands it was heard on; hover it for the bands,
frequencies and whether each was spotted. Stations QRZ has no coordinates for
are counted under the map rather than silently dropped. Map tiles are fetched
from CARTO, so the map is blank without internet access — nothing else on the
dashboard depends on it.

Stop with Ctrl-C; it drains the transcription pipeline and prints a summary.

**Don't pipe through `tail`** — it buffers everything until exit and you will
see nothing while it runs. Use `tee` if you want a copy:

```bash
.venv/bin/python scanner.py --host 44.31.241.7 --verbose 2>&1 | tee run.log
```

### Trusted container

UberSDR 0.1.59 added `whisper.trusted_containers`, which lists `voiceskimmer` by
default. When the scanner runs as that container on the server's own Docker
network — the [compose deployment](#8-docker--deployment) — the server
recognises it and:

* accepts the recognition parameters whatever `whisper.allow_client_params` is
  set to, so no server change is needed for the tuned path;
* exempts its sessions from `whisper.max_users`, so they neither consume a slot
  nor get turned away when web UI users have taken them all.

Trust is decided on the **raw TCP peer IP** of the audio WebSocket, matched
against the container name via Docker DNS. So it applies only to a direct
connection on the internal network: running from source, from another host, or
through Caddy is an ordinary client, however the container is named. If you
rename the container, add the new name to `whisper.trusted_containers` on the
server. It grants nothing beyond those two whisper privileges.

### If the attach is rejected

Against anything else — an UberSDR older than 0.1.59, `trusted_containers: []`,
or a scanner not running as that container — `whisper.allow_client_params`
governs, and it defaults to `false`. There is no way to detect it remotely. If
the attach fails with a message about per-attach recognition parameters being
disabled, either set it on the server or run:

```bash
.venv/bin/python scanner.py --host <host> --stock-whisper
```

`--stock-whisper` sends no recognition parameters, so the server's own config
applies: `task: translate` and auto-detect language. Expect noticeably worse
results — auto-detect misfires on noisy narrowband audio and translate-mode then
invents fluent prose from noise. The tuned path uses `transcribe`, a pinned
language, and a NATO-phonetics prompt.

### If you are not on a bypassed IP

`--check` tells you. Bypassed IPs (localhost and RFC1918 by default, per
`server.timeout_bypass_ips`) get unlimited lookups and no session time cap.
Otherwise:

```bash
.venv/bin/python scanner.py --host <host> --lookup-interval 6.0
```

That keeps you inside the default 10 lookups/minute. Without it you will see
`429` warnings and candidates will go unvalidated.

## 4. Read the results

```bash
.venv/bin/python analyse.py detections.jsonl
```

Reports precision overall and broken down by extraction path, by whether a cue
phrase was present, and by strict-token count — so you can see where the errors
concentrate and re-tune the gates. It also lists what the extractor invented,
which is the most useful thing in the file.

Raw records look like:

```json
{"time":"2026-07-28T14:22:31Z","band":"20m","frequency":14225000,"mode":"usb",
 "snr":14.2,"raw_text":"CQ CQ this is mike mike three november delta hotel",
 "candidate":"MM3NDH","normalised":"MM3NDH","source":"phonetic",
 "extract_confidence":0.8,"strict_tokens":4,"cued":true,
 "validated":true,"lookup_summary":"Nathan — Scotland",
 "dx_spot":"MM3NDH","agrees_with_dx_spot":true,
 "attribution_certain":true,"straddled_hop":false}
```

`agrees_with_dx_spot` is the metric to watch. The voice activity feed carries
`dx_callsign` when the DX cluster has spotted that frequency within 30 minutes,
so it is free ground truth — agreement means the whole chain worked end to end.
It only fires when the DX cluster is enabled *and* has a recent spot on that
exact frequency, so on a quiet instance expect zeroes.

## 5. Useful flags

| Flag | Default | Why change it |
|---|---|---|
| `--band 20m,40m` | all | Restrict to one or more bands (comma-separated) |
| `--parallel` | 1 | Scanning sessions to run at once, each on its own frequency. As a [trusted container](#trusted-container) these are exempt from `whisper.max_users`; otherwise every one holds a slot and the default is **2**, so 2 here uses every slot and leaves none for web UI users. Either way each session is a concurrent transcription on the WhisperLive server |
| `--dwell` | 30 s | Base listen time per frequency (~2 VAD segments) |
| `--max-dwell` | 60 s | Ceiling, so a busy net cannot hold the scanner. The default allows `--dwell` plus one `--dwell-extension` |
| `--silence-timeout` | 10 s | Move on early if nothing is heard at all — dead air, not a real dwell |
| `--dwell-extension` | 30 s | Extra time when something callsign-shaped is heard but not yet validated |
| `--revisit-cooldown` | 120 s | How long before a frequency may be revisited at all |
| `--revisit-dwell-period` | 900 s | A frequency this scanner has already submitted a DX spot from within this long counts as a *revisit*. No effect without `--spot` |
| `--revisit-dwell-percent` | 0.50 | Fraction of the normal dwell times to spend on such a revisit. `1.0` treats it like anything else |
| `--min-snr` | 20.0 | Raise to skip marginal signals. A **per-channel** SNR, filtering the server's activity feed — not the same scale as `--silence-min-snr` |
| `--min-extract-confidence` | 0.4 | Raise for precision, lower for recall |
| `--min-callsign-length` | 4 | Minimum callsign length to be looked up at all — shorter ones never reach QRZ, so are never confirmed or spotted. Applies to literal verbatim matches too |
| `--pipeline-latency` | 2.0 s | Raise if your WhisperLive is slow (affects frequency attribution) |
| `--verbose` | off | Log every transcript segment and rejection — use this while testing |
| `--no-prefilter` | off | Send every candidate to QRZ, skipping the free CTY filter |

A good first test, focused and chatty:

```bash
.venv/bin/python scanner.py --host <host> --band 40m --dwell 60 --verbose
```

### DX cluster spot submission

Every confirmed callsign can be submitted as a real DX spot via the
instance's [dxcluster addon](https://github.com/madpsy/ubersdr_dxcluster),
mounted at `/addon/dxcluster/`. Off by default — spots are immediately
visible to every connected DX cluster client. Requires the addon's spot
submission to be enabled on that instance (`SPOT_PASSWORD` set), plus a
callsign and password to log in with.

| Flag | Default | Meaning |
|---|---|---|
| `--spot` | off | Enable spot submission |
| `--spotter-call` | — | Callsign to log in with (required with `--spot`) |
| `--spotter-pass` | — | DX cluster spot password (required with `--spot`) |
| `--spot-cooldown` | 900 s | Re-spot the same (callsign, frequency) once this elapses — a station still active later is itself useful information |
| `--spot-freq-tolerance` | 500 Hz | How far apart two hearings can be and still count as the same station. Applies to both the hit count and the cooldown — too tight and one station splits into several, so the hit tally never accumulates |
| `--spot-max-entries` | 1000 | Cap on remembered cooldown entries, oldest/least-recent evicted first |
| `--spot-min-hits` | 2 | Decodes of the same callsign on the same frequency required before spotting. Trades latency for confidence — a wrong callsign assembled from one garbled pass is unlikely to be assembled identically again. Set to 1 to spot on the first decode |
| `--spot-tag` | `[Voice]` | Tag prefixed to every spot comment |

Comments are tagged `<tag> <QRZ name>` (default tag `[Voice]`, truncated to
the server's 50-character cap) so they're distinguishable from
manually-submitted or CW-skimmer spots in anyone else's cluster view.

**The connection looks after itself.** A scan runs for hours or days, and
cluster nodes restart, drop idle sessions and sit behind flaky links, so the
spotter keeps a background thread that reconnects and re-logs-in for as long
as the scan lasts — backing off from 5 s to a maximum of 60 s between
attempts. A node that is down when the scan starts, or that disappears
halfway through the night, costs you the spots during the outage and nothing
more: spotting resumes on its own within a minute of it coming back, without
restarting the scan. Spots attempted while disconnected are logged as skipped
and are not counted against the cooldown, so the next hearing of that station
spots it normally.

The one thing it does not retry is a rejected callsign or password — that
will never start working, so it is reported once and the run continues
without spotting.

A full roaming scan across every band, with spot submission on:

```bash
.venv/bin/python scanner.py --host m9psy.tunnel.ubersdr.org --port 443 --ssl \
  --stock-whisper --spot --spotter-call MM3NDH --spotter-pass xxxx \
  --progress-interval 180 --output full-scan.jsonl
```

## 6. HTTP API

The dashboard's own endpoints are documented here too, but `GET /api/spots` is
the one meant for other programs: every confirmed sighting in the last 24
hours, with everything known about it, filterable. It reads the same table the
dashboard does, so it holds the same rolling window — `--output` is the durable
record of anything older.

A record is one **callsign on one frequency**. The same station heard on two
bands is two records — that is also how corroboration and the re-spot cooldown
are counted, so `hits` always means "on this frequency".

```bash
# everything
curl 'http://localhost:6098/api/spots'

# only what was actually submitted to the cluster, in the last 5 minutes
curl 'http://localhost:6098/api/spots?submitted=true&last=5m'

# 20m and 40m, heard at least twice, strongest first
curl 'http://localhost:6098/api/spots?band=20m,40m&min_hits=2&sort=snr'

# just the fields a logger needs
curl 'http://localhost:6098/api/spots?fields=callsign,frequency,band,submitted_at_iso'
```

### Filters

All optional; combining them narrows (AND). An unknown or malformed value is
a **400 with a reason**, never an empty list — a filter API that answers a
typo with `[]` reads as "nothing matched", which is the most misleading thing
it could do.

| Parameter | Meaning |
| --- | --- |
| `last` | Relative window: `30s`, `5m`, `2h`, `1d`, `1w`. A bare number is seconds. |
| `since`, `until` | Absolute window, unix seconds. |
| `time_field` | Which timestamp the window applies to: `last_heard` (default), `first_heard`, `submitted_at`. |
| `submitted` | `true` = only what went to the DX cluster, `false` = only what did not. |
| `band` | Comma list, e.g. `20m,40m`. |
| `mode` | Comma list, e.g. `usb,lsb`. |
| `callsign` | Comma list, exact match. |
| `country`, `country_code` | Comma list. `country_code` is ISO 3166-1 alpha-2. |
| `min_freq`, `max_freq` | Hz. |
| `min_hits` | Times heard on that frequency. |
| `min_snr` | dB at the time it was heard. |
| `min_confidence` | Extractor confidence, 0–1. |
| `dx_agree` | `true` = only those matching a DX cluster spot on the same frequency. |
| `q` | Free text over callsign, name, country, band, grid, spot comment and the transcript it came from. |
| `sort` | `last_heard` (default), `first_heard`, `submitted_at`, `callsign`, `band`, `frequency`, `hits`, `snr`, `country`, `confidence`. |
| `order` | `desc` (default) or `asc`. |
| `limit`, `offset` | Default 500, capped at 5000. |
| `fields` | Comma list to trim the response to just those fields. |

Rate limited to **four requests per second per address**, with its own budget
separate from `/api/explain`. It is a token bucket, so a caller that has been
idle can spend all four at once and then refills one every 250 ms. Over the
limit is a `429` with `Retry-After` (whole seconds, per RFC 9110) and a
precise `retry_after` in the JSON body.

### Response

`total` is everything held — i.e. the last 24 hours — `matched` is how many
passed the filters, `count` is how many are in this page.

```json
{
  "generated_at": 1785332908.677, "generated_at_iso": "2026-07-29T13:48:28Z",
  "total": 4, "matched": 1, "count": 1, "offset": 0, "limit": 500,
  "spots": [{
    "callsign": "G0VIM", "key": "G0VIM|14226000",
    "band": "20m", "frequency": 14226000, "frequency_mhz": 14.226, "mode": "USB",
    "first_heard": 1785332850.5, "first_heard_iso": "2026-07-29T13:47:30Z",
    "last_heard": 1785332850.5,  "last_heard_iso": "2026-07-29T13:47:30Z",
    "hits": 4,
    "submitted": true, "submitted_at": 1785332880.5,
    "submitted_at_iso": "2026-07-29T13:48:00Z", "spot_comment": "[Voice] Malcolm",
    "name": "Malcolm", "country": "England", "country_code": "GB",
    "grid": "IO92", "latitude": 52.2, "longitude": -0.9,
    "lookup_summary": "Malcolm - England",
    "snr": 42.1, "activity_confidence": 0.8,
    "source": "phonetic", "extract_confidence": 0.75, "strict_tokens": 3,
    "cued": true, "candidate": "G0VIM",
    "heard_text": "this is G0VIM calling CQ",
    "attribution_certain": true, "straddled_hop": false,
    "dx_spot": "G0VIM", "agrees_with_dx_spot": true
  }]
}
```

`heard_text` is the transcript line the callsign came out of, and
`source`/`extract_confidence`/`strict_tokens`/`cued` are how it was extracted —
together they are the audit trail for a callsign you doubt. `attribution_certain`
is false when the audio spanned a frequency hop, so the frequency is a best
guess (see [Frequency attribution](#frequency-attribution)).

State is in memory only: it starts empty on restart, and `--output` remains the
durable record. Nothing here is authenticated — see the note on exposure in
[Docker / Deployment](#8-docker--deployment).

### Other endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/state` | Full dashboard snapshot (workers, transcript tail, confirmed, spots, targets, stats). Confirmed rows and submitted spots are the last 24 hours, matching the tables and charts they draw. |
| `GET /api/history` | Per-band activity over a rolling 24 hours, one bucket per hour, plus `top_callsigns` — both of the dashboard's charts from one poll. Always 24 buckets, zero-filled. The per-hour series count **distinct callsigns** per bucket, not events; `top_callsigns` counts total hits per station over the same window. |
| `GET /api/events` | SSE stream of incremental updates. |
| `POST /api/explain` | Why a transcript line did or did not yield a callsign. Body `{"text": "..."}`. Rate limited 1/s per address. |
| `GET /api/audio/<worker>` | Live audio from that worker, WebM/Opus. |
| `GET /api/settings` | The scan's runtime settings, grouped and human-readable — backs the "?" button next to the stats bar. Excludes the UberSDR host/port and any credential (`--password`, `--spotter-pass`). |

## 7. Tests

```bash
.venv/bin/python -m pytest -q     # or: python -m unittest discover -p 'test_*.py'
node test_frontend.js             # dashboard
```

Both run automatically on `./docker.sh build`, `arm64` and `push`, and a
failure stops the build before an image is produced. `SKIP_TESTS=1` overrides
it.

The false-positive tests in `test_phonetics.py` matter more than the positive
ones — a recall improvement that lets ordinary conversation through is a bad
trade. `test_rotation.py` guards against camping on one frequency.
`test_web.py` covers the rate limiter and the client-address derivation
behind the addon proxy.

`test_frontend.js` loads `static/app.js` in a stubbed DOM. That is the only
check that catches a dashboard which parses cleanly and then dies on load:
`node --check` passes such a file happily, and one shipped that way once — a
call placed above a `let` declaration hit its temporal dead zone and took the
whole page down with `can't access lexical declaration 'map' before
initialization`.

## 8. Docker / Deployment

Packaged the same way as the other [ubersdr addons](https://ubersdr.org) —
a container that runs alongside UberSDR, joined to its `sdr-network`, and
registered via the Admin → Addon Proxies interface.

**Install** (on the same host as UberSDR):

```bash
curl -fsSL https://raw.githubusercontent.com/madpsy/ubersdr_voiceskimmer/main/install.sh | bash
```

This fetches `docker-compose.yml` and the helper scripts into
`~/ubersdr/voiceskimmer/`, starts the container, and prints the addon-proxy
config to add in UberSDR's Admin UI (`Host: voiceskimmer`, `Port: 6098`,
`Strip prefix: true`). Edit `~/ubersdr/voiceskimmer/docker-compose.yml` to set
`UBERSDR_HOST`, `BAND`, `SPOT`/`SPOTTER_CALL`/`SPOTTER_PASS`, etc., then
`./restart.sh` to apply.

| Script | Does |
|---|---|
| `./start.sh` | Start the container |
| `./stop.sh` | Stop the container |
| `./restart.sh` | Restart (after editing `docker-compose.yml`) |
| `./update.sh` | Pull the latest image and restart |

### Environment variables

Every variable `entrypoint.sh` accepts. All are optional and all are commented
out in `docker-compose.yml` — an unset variable means the flag is not passed and
`scanner.py`'s own default applies.


**Connection**

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `UBERSDR_HOST` | `--host` | `ubersdr` | set in docker-compose.yml |
| `UBERSDR_PORT` | `--port` | `8080` |  |
| `UBERSDR_SSL` | `--ssl` | off | set to `1` for https/wss |
| `UBERSDR_PASS` | `--password` | — |  |

**What to scan**

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `BAND` | `--band` | — |  |
| `PARALLEL` | `--parallel` | `1` | each worker holds a Whisper slot; server default is 2 |
| `LOCK_FREQ` | `--lock-freq` | — | Hz; pins worker 0 to one frequency. With `PARALLEL` > 1 the rest keep hopping normally |
| `LOCK_MODE` | `--lock-mode` | `usb` |  |

**Dwell timing — seconds**

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `DWELL` | `--dwell` | `30.0` |  |
| `MAX_DWELL` | `--max-dwell` | `60.0` | ceiling; below `DWELL` truncates the base dwell |
| `DWELL_EXTENSION` | `--dwell-extension` | `30.0` |  |
| `REVISIT_COOLDOWN` | `--revisit-cooldown` | `120.0` |  |
| `REVISIT_DWELL_PERIOD` | `--revisit-dwell-period` | `900.0` | needs `SPOT` enabled to have any effect |
| `REVISIT_DWELL_PERCENT` | `--revisit-dwell-percent` | `0.5` | fraction, **not** seconds; 0 < x ≤ 1 |

**Which signals to bother with**

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `MIN_SNR` | `--min-snr` | `20.0` | per-channel dB, from the activity feed |
| `MIN_CONFIDENCE` | `--min-confidence` | `0.7` |  |
| `SILENCE_MIN_SNR` | `--silence-min-snr` | `40.0` | power vs noise **density** — a different scale to `MIN_SNR` |
| `SILENCE_TIMEOUT` | `--silence-timeout` | `10.0` |  |
| `SILENCE_MIN_WORDS` | `--silence-min-words` | `4` | fallback when the server sends no signal data |

**Transcription**

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `STOCK_WHISPER` | `--stock-whisper` | off | set to `1` to send no recognition parameters — only needed against a server that rejects them, see [Trusted container](#trusted-container) |
| `ASR_LANGUAGE` | `--asr-language` | `en` |  |
| `PROMPT` | `--prompt` | built-in phonetics prompt | custom Whisper initial prompt |

**Extraction and lookup**

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `MIN_EXTRACT_CONFIDENCE` | `--min-extract-confidence` | `0.4` | 0–1; raise for precision, lower for recall |
| `MIN_CALLSIGN_LENGTH` | `--min-callsign-length` | `4` | characters; gates the QRZ lookup, not just spotting |
| `LOOKUP_INTERVAL` | `--lookup-interval` | `0.0` | set to `6` if not on a bypassed IP |

**DX cluster spotting — needs the `dxcluster` addon**

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `SPOT` | `--spot` | off | set to `1` to submit spots |
| `SPOTTER_CALL` | `--spotter-call` | — | required with `SPOT` |
| `SPOTTER_PASS` | `--spotter-pass` | — | required with `SPOT` |
| `SPOT_TAG` | `--spot-tag` | `[Voice]` |  |
| `SPOT_COOLDOWN` | `--spot-cooldown` | — |  |
| `SPOT_FREQ_TOLERANCE` | `--spot-freq-tolerance` | `500` | Hz; governs hit matching and the re-spot cooldown |
| `SPOT_MIN_HITS` | `--spot-min-hits` | `2` |  |
| `SPOT_MIN_LENGTH` | `--min-callsign-length` | — | former name for `MIN_CALLSIGN_LENGTH`; still accepted |

**Callsign supersession** — see [Supersession](#supersession)

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `SUPERSEDE` | `--no-supersede` | on | set to `0` to disable retiring a shorter callsign when a longer one containing it is confirmed on the same frequency |
| `SUPERSEDE_WINDOW` | `--supersede-window` | `900` | seconds; how close the two hearings must be, and how long the retirement lasts |
| `SUPERSEDE_OBSERVE_ONLY` | `--supersede-observe-only` | off | set to `1` to record supersession without suppressing anything |

**Output**

| Variable | CLI flag | Default | Notes |
|---|---|---|---|
| `WEB_PORT` | `--web-port` | `6098` | `0` disables the dashboard |
| `OUTPUT` | `--output` | `/data/detections.jsonl` | persisted via the bind mount |
| `PROGRESS_INTERVAL` | `--progress-interval` | `300.0` | seconds between progress lines |
| `VERBOSE` | `--verbose` | off | set to `1` for per-segment logging |
| `EXTRA_ARGS` | (appended verbatim) | — | extra scanner.py args, appended verbatim |

A handful of internals have no variable of their own — `--max-candidates`,
`--no-prefilter`, `--pipeline-latency`, `--segment-join-gap`,
`--spot-max-entries` and `--web-host`. Pass them through `EXTRA_ARGS` if you
ever need them.

### Building the image yourself

```bash
./docker.sh build          # linux/amd64, loaded locally
./docker.sh arm64          # linux/arm64
./docker.sh push           # multi-platform buildx push
./docker.sh run [args...]  # run locally with env vars from your shell
```

---

## Troubleshooting

**"Invalid session. Please refresh the page and try again."**
The UUID was never registered. The client calls `POST /connection` before
opening any socket; if that call fails the audio handler rejects the UUID
because it has no recorded User-Agent (`websocket.go:563`). Check the
`/connection` response in the log.

**"Session IP mismatch"**
`server.enforce_session_ip_match` is on and your outbound IP changed between the
`/connection` call and the WebSocket. Common behind round-robin proxies or VPNs.

**Whisper attaches but no segments ever arrive**
Check the server can reach its WhisperLive (`whisper.server_url`). If the attach
itself is refused with "maximum users reached", `whisper.max_users` is
exhausted — which only applies when the server is not treating this client as a
[trusted container](#trusted-container), in which case the scanner holds a slot
for its whole run.

**"Server does not support reset_transcript"**
An older server. The scanner degrades automatically and warns once. It keeps
working, but Whisper's duplicate-suppression history now persists across
frequencies, so a repeated phrase on a new frequency may be dropped.

**Lots of `429` in the log**
Not a bypassed IP. Add `--lookup-interval 6.0`.

**Zero candidates after a long run**
Normal on quiet bands. Run with `--verbose` and look at the raw segments — if
Whisper is producing plausible English but no callsigns are being spoken, the
scanner is working correctly and the band is just not giving you anything.

---

## How it works

```
/api/voice-activity/stream ──► ActivityTracker ──► pick target
                                                        │
                                                     tune (in place)
                                                        │
  /ws (muted) ──► Whisper extension ──► /ws/dxcluster ──► segments
                                                        │
                                             timeline attribution
                                                        │
                                       extract ──► QRZ ──► detections.jsonl
```

### One session, never rebuilt

`POST /connection` registers the UUID, then two WebSockets are opened once and
held for the whole run:

- `/ws` — creates the audio session, immediately muted
- `/ws/dxcluster` — extension control and transcript results

Hopping uses the `tune` control message, which mutates the existing radiod
channel in place and leaves the Whisper tap attached. The audio session and the
Whisper attach are **never** torn down between frequencies — only at shutdown.

### Why muting is free

The extension tap is fed in the RTP receive path (`audio.go:286`), upstream of
the mute check in `streamAudio` (`websocket.go:1618`). So Whisper still receives
full-rate audio server-side while this client receives no audio bytes at all —
no Opus, no zstd, no decoding.

### How the audio preview works

The scanner's session is muted, and UberSDR's mute *substitutes silence*
rather than skipping packets (`websocket.go`, `streamAudio`), so a listener
attached to it would otherwise hear nothing. The dashboard's Listen button
unmutes for exactly as long as someone is listening, then re-mutes.

Transcription is unaffected either way: the Whisper tap is fed in the RTP
receive path (`audio.go`, `SendAudioToExtension`), upstream of the mute check.
Verified by measurement — the same endpoint yields ~9 kbps muted (Opus
encoding pure silence) against ~47 kbps unmuted.

Audio comes from UberSDR's `GET /audio/stream?session=<uuid>`
(`audio_http_stream.go`), which serves WebM/Opus that a plain `<audio>`
element can play. The backend **relays** it rather than pointing the browser
at it directly: that URL needs the session UUID, and the dashboard is
typically reachable by anyone (the addon proxy defaults to
`allowed_ips: 0.0.0.0/0`, `require_admin: false`). Handing that UUID to every
visitor would let them retune the scanner's session or spend its QRZ lookup
quota. Relaying keeps it inside the container and keeps the audio same-origin,
so it works unchanged behind the addon proxy.

UberSDR allows one HTTP audio consumer per session, so a single upstream
connection is fanned out to all listeners rather than one per browser tab.

### Frequency attribution

Whisper's output lags its input: VAD accumulates up to 15 s of speech
(`max_speech_duration_s`) and inference adds more. A segment arriving two seconds
after a hop is usually audio from the *previous* frequency.

`timeline.py` maps each segment back onto whichever frequency was tuned while its
audio was captured:

```
audio_end   ≈ received_at − pipeline_latency
audio_start ≈ audio_end − segment_duration
```

Segments entirely inside one tune window are attributed with certainty. Segments
spanning a hop go to whichever frequency covered more audio and are flagged
`straddled_hop` / `attribution_certain: false`.

On every hop the scanner sends a `reset_transcript` control message, clearing
Whisper's duplicate-suppression history — without it a genuine new "this is …"
on the new frequency is dropped as a duplicate of the previous one. It is a
control message, not a teardown.

### The two SNR thresholds

`--min-snr` (8) and `--silence-min-snr` (40) both read "SNR in dB" but are
**different measurements on different scales**, which is why the defaults look
so far apart:

| | Source | Scale |
|---|---|---|
| `--min-snr` | the server's voice activity feed | Per-channel SNR: the detected region's average power minus the median noise floor (`voice_activity.go`). |
| `--silence-min-snr` | the audio frame headers, measured here | `basebandPower − noiseDensity` — power vs noise **density**, the server's own `min_snr` definition. Runs about 34.8 dB higher for the same signal (10·log10 of a 3 kHz channel). |

So 40 on the silence scale is roughly 5 dB of channel SNR. The two numbers look
wildly different only because the scales are.

The feed in practice reports only signals well clear of the noise, so its values
run far above the 6–10 dB its detector nominally looks for. Sampled live across
three bands: 16 signals, 20.3 dB lowest, 25.7 median, 32.2 highest — which is
why the default is 20 and not something lower. At 8 the threshold discarded
nothing at all.

**Do not copy 40 into `--min-snr`.** It would demand a 40 dB per-channel SNR
from the activity feed, nothing would clear it, and the scanner would sit with
nothing to scan.

### Revisits

**These do nothing unless `--spot` is enabled** and the DX cluster login
succeeded. The trigger is a spot this scanner actually submitted — the history
is only written after a successful submission — so with spotting off, or if the
cluster rejected the login and the scanner degraded to not spotting, no
frequency ever counts as a revisit and every dwell runs at full length.

A frequency this scanner has already submitted a DX spot from is worth another
look — a net or a pile-up keeps producing callsigns — but the likeliest outcome
is hearing the station already spotted. So a revisit within
`--revisit-dwell-period` (15 minutes) gets `--revisit-dwell-percent` of the
normal time: 50% by default, which on the defaults means a 15 s base dwell, a
30 s ceiling and a 5 s silence timeout instead of 30/60/10.

Every timing scales together, including the extension. Shortening only the base
dwell would leave the ceiling and the silence timeout to undo it — a frequency
producing unvalidated candidates would extend right back up to the full ceiling.

Set `--revisit-dwell-percent 1.0` to treat revisits like anything else. Values
of 0 or above 1 are rejected rather than clamped: 0 means never listening to a
revisit at all, and above 1 makes a "reduced" dwell longer than a normal one,
so either is far more likely to be a typo than an intention.

This is **not** `--revisit-cooldown`, which decides whether a frequency may be
visited *at all*. This decides how long, once it is. The spot history is keyed
on frequency rather than callsign — what matters is that the frequency has been
productive, not who was on it — and shares the same frequency bucketing as the
spot cooldown, so normal dial drift still counts as the same frequency.

### Rotation

Selection is tiered rather than a plain priority sort, because priority alone
camps: a loud frequency with a DX spot and a previous success out-scores an
unvisited weak one even with its idle bonus at zero. So selection prefers
targets outside their cooldown, never repeats the immediately-previous
frequency, and only falls back to a repeat when it is genuinely the only target
on the air. Over a 24-dwell run across 6 targets this gives the best frequency
about a third of the dwells while still reaching every one.

A dwell can end four ways:

- **Timeout** — `--dwell` elapses with nothing worth extending for.
- **Extension** — a callsign-shaped but unvalidated candidate was heard; the
  deadline is pushed out by `--dwell-extension` in case a repeat lets it
  validate, up to the `--max-dwell` ceiling.
- **Silence** — nothing at all is heard within `--silence-timeout` of tuning
  in. The station may have moved off, be inaudible, or the detector's estimate
  was off. Rather than sitting out the full dwell on dead air, the scanner
  moves on immediately. Once *anything* is heard the silence check stops
  applying for the rest of that dwell — a pause between overs mid-QSO won't
  trigger it.
- **Confirmed** — a candidate is validated by QRZ. This does **not** extend the
  dwell; the scanner moves on straight away rather than lingering, since the
  goal for that frequency has already been met.

Every exit path still calls the normal visit/cooldown bookkeeping, so a
frequency that was skipped for silence or confirmed early comes back around on
the next sweep exactly like any other — nothing gets permanently skipped.

### Extraction

Three problems make naive matching useless:

1. Operators routinely ignore NATO. "Germany Four Radio Sugar" is an ordinary
   way to send G4RS.
2. Many phonetic words are ordinary English — "for" is 4, "to" is 2, "king" is
   K — so plain conversation yields callsign-shaped token runs.
3. Whisper frequently renders part of a callsign as literal digits or bare
   letters instead of words, and splits it across a filler word — a live run
   against a real instance produced "Golf Mike 6 and Z.A.K." for what is
   almost certainly GM6ZAK.

Mappings are split into STRICT (unambiguous on-air words: `foxtrot`, `niner`,
`zulu`) and LOOSE (ambiguous English and geographic phonetics), plus bare
numerals (`96`) and bare single letters (`z`, `a`, `k`), which the word maps
alone can't see. A single connector word (`and`, `uh`, `um`, …) is bridged
without breaking the run when a mappable token follows immediately, so "Golf
Mike 6 and Z A K" assembles into one run instead of two useless fragments.

A run is promoted using a weighted evidence score (2 points per strict
character, 2/3 per loose character, +2 for a cue phrase such as `this is`,
`cq`, `de`, `qrz`, …), needing 4 points to pass — deliberately permissive on
long all-loose runs, since the mandatory embedded-digit shape check
(`CALLSIGN_RE`) already rejects the vast majority of coincidental English word
sequences; getting a long run to *also* alternate letters and digits correctly
is rare. Verified against a false-positive corpus of plain conversation,
counting phrases, filler words and letter/digit clusters that must never
extract anything (`test_phonetics.py`).

One word is deliberately excluded despite mapping cleanly: "roger", the
near-universal on-air acknowledgment, sits directly after callsigns constantly
and was getting absorbed as a trailing R, corrupting the very callsign it
followed (`GM6ZAKR` instead of `GM6ZAK`). Leaving it unmapped makes the run end
cleanly before it.

Callsigns Whisper spelled out literally are matched separately and scored
higher.

### Validation

**QRZ is the only validator.** A 200 from `/api/lookup` means the station
exists; a 404 means it does not.

Before a lookup is spent, a candidate must pass, in order:

1. ITU structural regex at extraction time
2. The strict-token / cue evidence gate
3. `--min-extract-confidence`
4. Re-check of the structure **after** normalisation — stripping a prefix
   overlay can leave something that is no longer a callsign
5. A free negative CTY filter: a 404 from `/api/cty/lookup` means the prefix
   belongs to no DXCC entity, so the callsign cannot be real
6. The local cache — each callsign is looked up at most once, negatives included

CTY is used **only** as a negative filter. It resolves by longest-prefix match,
so it returns "United Kingdom" for `G4ZZZZ` whether or not that station is
licensed — a CTY hit proves nothing, only a miss is informative.

### Supersession

QRZ cannot catch every mis-decode, because some mis-decodes are themselves
real callsigns. One ITU phonetic word maps to exactly one character, so a word
Whisper never transcribed costs exactly one character — and what is left is
often a licensed station that QRZ happily confirms. Observed live as `ON2GB`,
with the real station `ON2GBR` arriving on the same frequency a couple of
minutes later.

So when a **longer** callsign is confirmed on the same frequency inside
`--supersede-window` (15 minutes by default), and the shorter one is that
callsign with up to two characters missing, the shorter is retired: no further
hits, no announcement, no spot. Its accumulated hits move onto the longer
callsign, since those hearings were of the same station — a station heard
three times, twice with a character missing, still reaches `--spot-min-hits`.

**Only ever the shorter of the pair is retired**, whichever order they are
heard in. A missing phonetic word is far likelier than an invented one, so
length decides and arrival order does not: if the longer callsign was already
on record, a shorter one is retired the moment it arrives rather than waiting
to be heard again.

The shape test is deliberately narrow. The shorter callsign must be a
**subsequence** of the longer — characters only ever dropped, never
substituted — and the prefix through the last digit must be identical, since
dropping a character there changes the country outright (`ON2GBR` → `N2GBR`
turns a Belgian station into an American one).

| Heard first | then | Result |
|---|---|---|
| `ON2GB` | `ON2GBR` | retired — trailing character lost |
| `ON2BR` | `ON2GBR` | retired — interior character lost |
| `ON2DBR` | `ON2GBR` | untouched — a character *differs*, not missing |
| `N2GBR` | `ON2GBR` | untouched — different prefix, different country |

Even so, `ON4AB` and `ON4KAB` are both plausible real callsigns that could
share a frequency during a QSO, so the string test is not allowed to act
alone. Two corroboration guards sit in front of it:

- the longer callsign must have been heard **at least as often** as the
  shorter. This blocks the inverse error, which is real: a word following the
  callsign can be absorbed as a trailing letter — *"ON2GB, radio check"* can
  yield `ON2GBR`, since "radio" maps to `R`. One decode cannot retire a
  station heard four times.
- a shorter callsign that has already reached `--spot-min-hits` is left alone.
  Something that corroborated has stopped looking like a one-pass garble.

Retirement expires with the window rather than lasting the run: a callsign
still being heard 15 minutes later is behaving like a real station.

Every supersession is recorded as `superseded_by` in the JSONL and marked on
the dashboard (`!` on the retired callsign, `+` on the one that replaced it,
both with an explanation on hover) — a wrongly-retired station would otherwise
be invisible by construction. `--supersede-observe-only` records all of that
without suppressing anything, which is worth running for a few days before
trusting the rule on your own bands.

**Note on timing.** With the default `--spot-min-hits 2`, a shorter callsign
often reaches two hearings and is spotted within its dwell, minutes before the
longer version is ever heard. Supersession cannot retract a spot already sent
— it blocks the re-spot after `--spot-cooldown`, cleans up the confirmed list,
and carries the corroboration across. Preventing the first spot outright would
need spots held back for a settling period, which is not implemented.

---

## Known limitations

- One Whisper instance per session, held for the whole run. As a
  [trusted container](#trusted-container) that costs no `whisper.max_users`
  slot, but it is still a concurrent transcription on the WhisperLive server;
  elsewhere it occupies one of the two default slots.
- Scanning is serial. With 20–40 active signals a full cycle takes many minutes,
  and callsigns are given at the start and end of overs, so the hit rate per
  dwell is inherently low. Parallel scanning would need multiple sessions.
- WhisperLive exposes no per-segment confidence, so ASR certainty cannot be
  thresholded — only the extractor's own heuristic is available.
- `--pipeline-latency` is a fixed estimate, not measured. Segments spanning a
  hop are flagged rather than resolved precisely.
