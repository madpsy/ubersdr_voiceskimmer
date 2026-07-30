"""
Live dashboard for the voice callsign skimmer.

Runs a small Flask app in a background thread inside the same process as
scanner.py — no separate process or IPC, just another daemon thread alongside
the existing segment-worker thread. All state here is in-memory and bounded
(deques with a maxlen); nothing is persisted — the --output JSONL file remains
the durable record of what was found.

Every push_*()/set_current()/update_stats() call comes from scanner.py's
existing threads (the dwell loop and the segment worker), so all state
mutation goes through a single lock. Callers pass plain, already-JSON-safe
dicts (usually dataclasses.asdict() of an existing scanner.py/activity.py
dataclass) rather than scanner.py's own classes, so this module never needs to
import scanner.py — it stays a leaf module.

HTTP surface (mirrors the snapshot+SSE pattern used by the other ubersdr_*
addons' web UIs, e.g. ubersdr_lightning's /api/status + /api/events):

    GET  /             index.html, with __BASE_PATH__ substituted from the
                        X-Forwarded-Prefix header set by UberSDR's addon proxy
                        (empty string when accessed directly, not via the proxy)
    GET  /static/*     app.js and anything else under static/
    GET  /api/state     full JSON snapshot — used for the initial page load
    GET  /api/events    SSE stream of incremental events: hop, transcript,
                        confirmed, spot, stats. A fresh "state" event is sent
                        first so a client that connects mid-run isn't blank.
"""

import json
import logging
import math
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

from phonetics import explain


def client_ip(req) -> str:
    """
    The real client address behind UberSDR's addon proxy.

    Mirrors realClientIPDebug in ubersdr_dxcluster/terminal.go, which is the
    established convention for addons: X-Real-IP first (the proxy sets it to
    a single authoritative address), then the first entry of
    X-Forwarded-For, then the socket peer for a direct connection.

    Trusting those headers is safe here for the same reason it is there. The
    addon proxy DELETES any client-supplied X-Real-IP and X-Forwarded-For
    before setting its own (addon_proxy.go, "Strip client-supplied proxy
    headers before we set our own authoritative values"), and the container
    publishes its port only on the internal sdr-network — so nothing that
    can reach us is in a position to forge them. If this were ever exposed
    directly to the internet, these headers would become attacker-controlled
    and this function would need a trusted-proxy check like the main
    server's getClientIP.
    """
    xri = (req.headers.get("X-Real-IP") or "").strip()
    if xri:
        return xri
    xff = req.headers.get("X-Forwarded-For") or ""
    if xff:
        return xff.split(",")[0].strip()
    return req.remote_addr or "unknown"


class QueryError(ValueError):
    """A bad query parameter. Carries the message shown to the caller."""


_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw]?)$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: str) -> float:
    """
    "5m" / "30s" / "2h" / "1d" / "90" (bare number is seconds) -> seconds.

    Rejects anything else rather than silently treating it as zero, which
    would turn a typo like "5min" into "everything since the epoch" — the
    opposite of what was asked for.
    """
    match = _DURATION_RE.match((value or "").strip())
    if not match:
        raise QueryError(
            f"invalid duration {value!r} — use e.g. 30s, 5m, 2h, 1d "
            "(a bare number is seconds)"
        )
    amount, unit = match.groups()
    return float(amount) * _DURATION_UNITS[(unit or "s").lower()]


def parse_bool(value: str, name: str) -> bool:
    lowered = (value or "").strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise QueryError(f"invalid boolean for {name}: {value!r} — use true or false")


def parse_number(value: str, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise QueryError(f"invalid number for {name}: {value!r}")


def csv_set(value: str) -> Set[str]:
    """Comma-separated list -> lowercased set, blanks dropped."""
    return {part.strip().lower() for part in (value or "").split(",") if part.strip()}


class RateLimiter:
    """
    At most one request per `interval` per key.

    Only a SUCCESSFUL request advances the clock, so a client hammering the
    endpoint still gets exactly one through per interval rather than being
    pushed further out by its own rejected attempts.

    Bounded: entries older than the interval are dead weight, so they are
    swept once the map grows past `max_entries`. If a genuine flood from
    many distinct addresses leaves nothing to sweep, the oldest half goes
    anyway — losing a little rate-limit state under attack is much better
    than growing the map without limit in a long-running container.
    """

    def __init__(self, interval: float = 1.0, max_entries: int = 10000):
        self.interval = interval
        self.max_entries = max_entries
        # monotonic, not wall clock: this measures an elapsed interval, and
        # an NTP step must not hand out a free request or a long lockout.
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> Tuple[bool, float]:
        """Returns (allowed, seconds until the next request is allowed)."""
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.interval:
                return False, self.interval - (now - last)
            self._last[key] = now
            if len(self._last) > self.max_entries:
                self._prune(now)
            return True, 0.0

    def _prune(self, now: float) -> None:
        """Caller holds the lock."""
        for key in [k for k, t in self._last.items() if now - t >= self.interval]:
            del self._last[key]
        if len(self._last) > self.max_entries:
            oldest = sorted(self._last, key=lambda k: self._last[k])
            for key in oldest[: len(oldest) // 2]:
                del self._last[key]

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class AudioRelay:
    """
    Relays the scanner's own audio to dashboard listeners.

    UberSDR exposes GET /audio/stream?session=<uuid> (audio_http_stream.go),
    which serves the session's audio as WebM/Opus that a plain <audio> element
    can play. Rather than pointing the browser straight at it, we proxy it:
    that URL needs the session UUID, and the dashboard is typically reachable
    by anyone (the addon proxy defaults to allowed_ips 0.0.0.0/0 with
    require_admin false). Handing that UUID to every visitor would let them
    retune the scanner's session or spend its QRZ lookup quota. Relaying keeps
    it inside the container, and keeps the audio same-origin with the
    dashboard so it works unchanged behind the addon proxy.

    The scanner's session is muted (see UberSDRSession), and muting
    substitutes silence rather than skipping packets, so a listener would
    otherwise hear nothing. We unmute for exactly as long as at least one
    listener is connected, then re-mute. Transcription is unaffected either
    way — the Whisper tap is upstream of the mute check.

    UberSDR supports only one HTTP audio consumer per session, so a single
    upstream connection is fanned out to every listener rather than opening
    one per browser tab.
    """

    CHUNK = 4096

    def __init__(self):
        self._lock = threading.Lock()
        self._listeners: List["queue.Queue[Optional[bytes]]"] = []
        self._session = None
        self._base_url = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def attach(self, session, base_url: str) -> None:
        """Wire in the live UberSDR session once the scanner has opened it."""
        with self._lock:
            self._session = session
            self._base_url = base_url.rstrip("/")

    @property
    def available(self) -> bool:
        with self._lock:
            return self._session is not None

    def subscribe(self) -> "queue.Queue[Optional[bytes]]":
        q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=64)
        with self._lock:
            self._listeners.append(q)
            first = len(self._listeners) == 1
            session = self._session
        if first and session is not None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()
            # Unmute only after the upstream tap exists, so audio never takes
            # the WebSocket path to this client (which would just discard it).
            session.set_mute(False)
            log.info("Audio preview started — session unmuted")
        return q

    def unsubscribe(self, q: "queue.Queue[Optional[bytes]]") -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)
            last = not self._listeners
            session = self._session
        if last:
            self._stop.set()
            if session is not None:
                session.set_mute(True)
            log.info("Audio preview stopped — session re-muted")

    def _pump(self) -> None:
        """Single upstream connection, fanned out to every listener."""
        with self._lock:
            session, base = self._session, self._base_url
        if session is None:
            return
        url = f"{base}/audio/stream"
        try:
            with requests.get(
                url, params={"session": session.user_session_id},
                stream=True, timeout=(10, 30),
            ) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(self.CHUNK):
                    if self._stop.is_set():
                        break
                    if not chunk:
                        continue
                    with self._lock:
                        subs = list(self._listeners)
                    for q in subs:
                        try:
                            q.put_nowait(chunk)
                        except queue.Full:
                            # A stalled listener must not hold up the others.
                            pass
        except requests.RequestException as exc:
            log.warning("Audio relay upstream failed: %s", exc)
        finally:
            with self._lock:
                subs = list(self._listeners)
            for q in subs:
                try:
                    q.put_nowait(None)   # sentinel: end of stream
                except queue.Full:
                    pass


class WebUI:
    """In-memory live state + Flask/SSE server for the dashboard."""

    def __init__(
        # transcript_maxlen is PER WORKER — each keeps its own scrollback, so
        # --parallel 2 holds 2x this. Kept equal to the frontend's own cap so
        # a reload shows the same history the live view was holding.
        self, workers: int = 1, transcript_maxlen: int = 200, spots_maxlen: int = 100,
        gates: Optional[Dict[str, Any]] = None,
    ):
        self._lock = threading.Lock()
        self._start_time = time.time()
        # The scanner-side thresholds a candidate must clear after extraction.
        # /api/explain reports them alongside its verdict so the dashboard can
        # say "extracted but dropped for scoring 0.30" rather than leaving the
        # user to guess which gate a callsign died at.
        self.gates: Dict[str, Any] = gates or {}
        # /api/explain is the one endpoint that does real work on
        # caller-supplied input, so it is the one worth pacing. The rest
        # serve state the scanner has already computed.
        # One budget per endpoint rather than one shared across both: they
        # answer different questions, and a dashboard fetching an explanation
        # should not lock the caller out of the spot query for a second.
        self.explain_limiter = RateLimiter(interval=1.0)
        self.spots_limiter = RateLimiter(interval=1.0)

        # Everything a single scanning session owns is keyed by worker id.
        # Confirmed callsigns, spots and stats stay shared — they are results,
        # not per-session state, and it would be actively unhelpful to split a
        # station's history by whichever worker happened to hear it.
        self.worker_ids = list(range(workers))
        self._current: Dict[int, Optional[Dict[str, Any]]] = {w: None for w in self.worker_ids}
        self._live: Dict[int, Optional[Dict[str, Any]]] = {w: None for w in self.worker_ids}
        self._signal: Dict[int, Optional[Dict[str, Any]]] = {w: None for w in self.worker_ids}
        # Whether each worker's Whisper attach is live. Starts unknown so the
        # dashboard shows "connecting" rather than claiming a failure before
        # the session has had a chance to come up.
        self._status: Dict[int, Dict[str, Any]] = {
            w: {"connected": None, "detail": "connecting"} for w in self.worker_ids
        }
        self._transcripts: Dict[int, "deque[Dict[str, Any]]"] = {
            w: deque(maxlen=transcript_maxlen) for w in self.worker_ids
        }
        # One relay per worker: UberSDR allows a single HTTP audio consumer
        # per session, and each worker is its own session.
        self.audio: Dict[int, AudioRelay] = {w: AudioRelay() for w in self.worker_ids}
        # Keyed "callsign|freq_bucket" — the same (callsign, frequency-bucket)
        # identity scanner.py's own self.confirmed dict uses, so a station
        # that moves bands gets its own row and its own hit_count instead of
        # silently overwriting the previous frequency's entry. Latest
        # sighting on a given bucket wins, but first_seen/hit_count
        # accumulate across repeats on that bucket, mirroring the on-screen
        # "(repeat)" tracking.
        self._confirmed: Dict[str, Dict[str, Any]] = {}
        self._spots: "deque[Dict[str, Any]]" = deque(maxlen=spots_maxlen)
        # Rolling per-band activity for the dashboard's 24h chart, keyed
        # (hour bucket, band) -> {"confirmed": {calls}, "spotted": {calls}}.
        #
        # Accumulated here rather than derived from _confirmed, which cannot
        # answer it: that dict holds ONE timestamp per (callsign, frequency)
        # and overwrites it on every re-hearing, so a station active for six
        # hours leaves a single point at the end.
        #
        # Distinct callsigns per bucket rather than raw events, for both
        # series. push_confirmed fires on every validated decode, so counting
        # events would let one chatty station tower over a band full of
        # quieter ones; and a re-spot after the cooldown is the same station
        # again, not a new one. So both series read as "stations active".
        self._history: Dict[Tuple[int, str], Dict[str, Set[str]]] = {}
        # Submissions per callsign, all frequencies. The only part of the
        # top-callsigns chart that cannot be derived: _confirmed carries
        # hit_count so confirmed hits add up across a station's frequencies,
        # but it records only the LAST spot time, and _spots is a capped
        # deque so counting from it would silently undercount a long run.
        self._spot_counts: Dict[str, int] = {}
        self._targets: List[Dict[str, Any]] = []
        self._stats: Dict[str, Any] = {}

        self._subscribers: List["queue.Queue[str]"] = []
        self._subscribers_lock = threading.Lock()

        self._server = None
        self._thread: Optional[threading.Thread] = None
        self.app = self._build_app()

    # -- Flask app ------------------------------------------------------

    def _build_app(self) -> Flask:
        app = Flask(
            __name__,
            static_folder=str(STATIC_DIR),
            static_url_path="/static",
        )
        # Silence Flask's own request logging — scanner.py already configures
        # logging for the whole process; a hit on /api/events every few
        # seconds from an open dashboard tab would otherwise spam it.
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

        @app.route("/")
        def index():
            base_path = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            html = html.replace("__BASE_PATH__", base_path)
            return Response(html, mimetype="text/html")

        @app.route("/api/state")
        def api_state():
            return jsonify(self.snapshot())

        @app.route("/api/events")
        def api_events():
            def stream():
                q: "queue.Queue[str]" = queue.Queue(maxsize=1000)
                with self._subscribers_lock:
                    self._subscribers.append(q)
                try:
                    yield self._sse("state", self.snapshot())
                    while True:
                        try:
                            yield q.get(timeout=15)
                        except queue.Empty:
                            yield ": keepalive\n\n"
                finally:
                    with self._subscribers_lock:
                        if q in self._subscribers:
                            self._subscribers.remove(q)

            return Response(
                stream(),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.route("/api/history")
        def api_history():
            """
            Per-band activity over the last 24 hours, one bucket per hour.

            Serves state already accumulated, so it is not rate limited —
            same as /api/state.
            """
            return jsonify(self.history())

        @app.route("/api/spots")
        def api_spots():
            """
            Every confirmed sighting, filtered. Documented in README.md.

            Rate limited to one request per second per address, like
            /api/explain: it accepts caller-supplied filters and can return a
            large response, so it is not in the same class as the dashboard's
            own /api/state poll. `limit` is capped server-side too, so a
            single request cannot ask for an unbounded response.
            """
            limited = self._too_many(self.spots_limiter)
            if limited is not None:
                return limited
            try:
                return jsonify(self.query_spots(request.args))
            except QueryError as exc:
                # 400 with the reason: a filter API that answers a typo with
                # an empty list is worse than useless, because the caller
                # reads it as "nothing matched".
                return jsonify({"error": str(exc)}), 400

        @app.route("/api/explain", methods=["POST"])
        def api_explain():
            """
            Why a transcript line did or did not yield a callsign.

            Read-only and side-effect free — extraction is pure string work
            and no QRZ lookup is made, so this cannot be used to burn the
            instance's lookup quota. POST rather than GET because a
            transcript line is arbitrary user-visible text that has no
            business in a URL or an access log.
            """
            limited = self._too_many(self.explain_limiter)
            if limited is not None:
                return limited

            payload = request.get_json(silent=True) or {}
            text = payload.get("text", "")
            if not isinstance(text, str):
                return jsonify({"error": "text must be a string"}), 400
            # Long enough for any real segment; a cap keeps a pathological
            # request from spending real CPU in the trim loop.
            if len(text) > 2000:
                return jsonify({"error": "text too long"}), 413

            result = explain(text)
            result["gates"] = dict(self.gates)
            for cand in result["candidates"]:
                cand["verdict"] = self._verdict(cand)
            result["summary"] = self._summarise(result)
            return jsonify(result)

        @app.route("/api/audio")
        @app.route("/api/audio/<int:worker>")
        def api_audio(worker: int = 0):
            """
            Live audio from whatever that worker is currently tuned to.

            Follows it as it hops — the same session retuned in place, not a
            separate receiver. Each worker is its own session and so its own
            relay; UberSDR allows one HTTP audio consumer per session.
            """
            relay = self.audio.get(worker)
            if relay is None:
                return jsonify({"error": f"no such worker {worker}"}), 404
            if not relay.available:
                return jsonify({"error": "audio session not ready"}), 503

            def stream():
                q = relay.subscribe()
                try:
                    while True:
                        try:
                            chunk = q.get(timeout=30)
                        except queue.Empty:
                            break            # upstream went quiet; let the client retry
                        if chunk is None:
                            break            # end-of-stream sentinel from the pump
                        yield chunk
                finally:
                    # Runs when the browser stops/closes the <audio> element,
                    # which is what re-mutes that worker's session.
                    relay.unsubscribe(q)

            return Response(
                stream(),
                mimetype="audio/webm",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        return app

    # -- Activity history -------------------------------------------------

    HISTORY_BUCKET_SECONDS = 3600
    HISTORY_BUCKETS = 24

    def _record_history(self, kind: str, band: str, callsign: str) -> None:
        """Note one station active on a band. Caller holds the lock."""
        if not band or not callsign:
            return
        bucket = int(time.time() // self.HISTORY_BUCKET_SECONDS)
        entry = self._history.get((bucket, band))
        if entry is None:
            entry = {"confirmed": set(), "spotted": set()}
            self._history[(bucket, band)] = entry
        entry[kind].add(callsign)

        # Drop anything that has fallen out of the window. Cheap because the
        # map only ever holds a day's worth of (bucket, band) pairs.
        oldest = bucket - self.HISTORY_BUCKETS
        for key in [k for k in self._history if k[0] < oldest]:
            del self._history[key]

    def top_callsigns(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        The busiest callsigns, aggregated across every frequency they were
        heard on, most confirmed hits first.

        Confirmed hits are summed from _confirmed's own hit_count rather than
        counted separately, so the chart and the confirmed table can never
        disagree — a station on two bands is two rows there and one entry
        here. Submissions come from _spot_counts, which exists because that
        total is the one thing not already recorded.
        """
        with self._lock:
            agg: Dict[str, Dict[str, Any]] = {}
            for entry in self._confirmed.values():
                call = entry.get("normalised") or ""
                if not call:
                    continue
                row = agg.setdefault(call, {
                    "confirmed": 0, "bands": set(), "country_code": "",
                    "country": "", "last_heard": 0.0,
                })
                row["confirmed"] += entry.get("hit_count", 1) or 1
                if entry.get("band"):
                    row["bands"].add(entry["band"])
                if entry.get("country_code") and not row["country_code"]:
                    row["country_code"] = entry["country_code"]
                    row["country"] = entry.get("country", "")
                row["last_heard"] = max(row["last_heard"], entry.get("timestamp") or 0)
            spots = dict(self._spot_counts)

        rows = [
            {
                "callsign": call,
                "confirmed": row["confirmed"],
                "spotted": spots.get(call, 0),
                "bands": sorted(row["bands"]),
                "country_code": row["country_code"],
                "country": row["country"],
                "last_heard": row["last_heard"],
            }
            for call, row in agg.items()
        ]
        # Most hits first; callsign as the tiebreak so equal counts do not
        # shuffle between refreshes and make the chart look alive.
        rows.sort(key=lambda r: (-r["confirmed"], r["callsign"]))
        return rows[:limit]

    def history(self) -> Dict[str, Any]:
        """
        The last 24 hours of per-band activity, one bucket per hour.

        Always returns exactly HISTORY_BUCKETS buckets ending at the current
        hour, zero-filled where nothing was heard — the chart shows a full
        rolling day whether or not the scanner was running for it, so a gap
        reads as "quiet" rather than as missing axis.
        """
        now = time.time()
        current = int(now // self.HISTORY_BUCKET_SECONDS)
        first = current - self.HISTORY_BUCKETS + 1

        with self._lock:
            snapshot = {
                key: {k: len(v) for k, v in val.items()}
                for key, val in self._history.items()
            }

        bands = sorted({band for (bucket, band) in snapshot if bucket >= first})
        buckets = []
        for bucket in range(first, current + 1):
            start = bucket * self.HISTORY_BUCKET_SECONDS
            confirmed, spotted = {}, {}
            for band in bands:
                counts = snapshot.get((bucket, band))
                if not counts:
                    continue
                if counts.get("confirmed"):
                    confirmed[band] = counts["confirmed"]
                if counts.get("spotted"):
                    spotted[band] = counts["spotted"]
            buckets.append({
                "start": start,
                "start_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                "confirmed": confirmed,
                "spotted": spotted,
            })

        return {
            "generated_at": now,
            "bucket_seconds": self.HISTORY_BUCKET_SECONDS,
            "buckets": buckets,
            "bands": bands,
            # Both series count distinct callsigns per bucket — see _history.
            "counts": "distinct callsigns per bucket",
            # Served here rather than from an endpoint of its own: the
            # dashboard draws both charts from one poll.
            "top_callsigns": self.top_callsigns(),
        }

    # -- Rate limiting ----------------------------------------------------

    @staticmethod
    def _too_many(limiter: RateLimiter) -> Optional[Response]:
        """
        A 429 if this caller is over its budget, else None.

        Shared by the endpoints that take caller-supplied queries. The
        address comes from client_ip(), which trusts the addon proxy's
        headers — see the note there about direct exposure.
        """
        allowed, retry_after = limiter.allow(client_ip(request))
        if allowed:
            return None
        resp = jsonify({
            "error": "rate limited — one request per second",
            "retry_after": round(retry_after, 2),
        })
        resp.status_code = 429
        # RFC 9110 delay-seconds is an integer, and rounding up avoids
        # sending the client straight back into another rejection. Floored
        # at 1 so a sub-second wait never advertises itself as "0".
        resp.headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
        return resp

    # -- Spot query -------------------------------------------------------

    # Sortable keys, mapped to the record field they read. Restricted to a
    # known set so `sort` cannot be used to probe the record shape.
    _SORT_KEYS = {
        "last_heard": "last_heard", "time": "last_heard",
        "first_heard": "first_heard",
        "submitted_at": "submitted_at",
        "callsign": "callsign",
        "band": "band",
        "frequency": "frequency", "freq": "frequency",
        "hits": "hits",
        "snr": "snr",
        "country": "country",
        "confidence": "extract_confidence",
    }

    @staticmethod
    def _record(entry: Dict[str, Any], comment: str) -> Dict[str, Any]:
        """
        One confirmed sighting, as the API presents it.

        Built field by field rather than returned raw so the response is a
        contract rather than a window onto whatever the scanner's Detection
        dataclass happens to hold this week. Timestamps are given as both
        unix seconds and ISO-8601 UTC — the first is what you filter and
        sort on, the second is what you read.
        """
        def iso(ts):
            if not ts:
                return None
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

        freq = entry.get("frequency") or 0
        spotted_at = entry.get("spotted_at")
        return {
            # identity
            "callsign": entry.get("normalised", ""),
            "key": entry.get("key", ""),
            # where
            "band": entry.get("band", ""),
            "frequency": freq,
            "frequency_mhz": round(freq / 1e6, 6) if freq else 0,
            "mode": (entry.get("mode") or "").upper(),
            # when
            "first_heard": entry.get("first_seen"),
            "first_heard_iso": iso(entry.get("first_seen")),
            "last_heard": entry.get("timestamp"),
            "last_heard_iso": iso(entry.get("timestamp")),
            "hits": entry.get("hit_count", 1),
            # submission
            "submitted": bool(spotted_at),
            "submitted_at": spotted_at,
            "submitted_at_iso": iso(spotted_at),
            "spot_comment": comment,
            # who — from the QRZ/CTY lookup
            "name": entry.get("name", ""),
            "country": entry.get("country", ""),
            "country_code": entry.get("country_code", ""),
            "grid": entry.get("grid", ""),
            "latitude": entry.get("latitude"),
            "longitude": entry.get("longitude"),
            "lookup_summary": entry.get("lookup_summary", ""),
            # signal at the time it was heard
            "snr": entry.get("snr"),
            "activity_confidence": entry.get("activity_confidence"),
            # how it was extracted — the audit trail for a doubtful callsign
            "source": entry.get("source", ""),
            "extract_confidence": entry.get("extract_confidence"),
            "strict_tokens": entry.get("strict_tokens"),
            "cued": entry.get("cued"),
            "candidate": entry.get("candidate", ""),
            "heard_text": entry.get("raw_text", ""),
            "attribution_certain": entry.get("attribution_certain", True),
            "straddled_hop": entry.get("straddled_hop", False),
            # corroboration against the DX cluster's own spots
            "dx_spot": entry.get("dx_spot", ""),
            "agrees_with_dx_spot": entry.get("agrees_with_dx_spot", False),
        }

    def query_spots(self, args) -> Dict[str, Any]:
        """
        Filtered view of every confirmed sighting. See README for the
        parameter reference. Raises QueryError on a bad parameter.
        """
        now = time.time()
        with self._lock:
            entries = list(self._confirmed.values())
            comments = {s.get("key", ""): s.get("comment", "") for s in self._spots}

        records = [self._record(e, comments.get(e.get("key", ""), "")) for e in entries]
        total = len(records)

        get = args.get

        # -- time window --
        since = None
        if get("last") is not None:
            since = now - parse_duration(get("last"))
        if get("since") is not None:
            since = parse_number(get("since"), "since")
        until = parse_number(get("until"), "until") if get("until") is not None else None

        # Which timestamp the window applies to. Filtering submissions by when
        # the station was last heard would be surprising — a station heard
        # again after being spotted would drift in and out of a submitted
        # query — so the window follows whichever event the caller is asking
        # about.
        time_field = get("time_field", "last_heard")
        if time_field not in ("last_heard", "first_heard", "submitted_at"):
            raise QueryError(
                f"invalid time_field {time_field!r} — use last_heard, "
                "first_heard or submitted_at"
            )

        def in_window(r):
            ts = r.get(time_field)
            if ts is None:
                return since is None and until is None
            if since is not None and ts < since:
                return False
            if until is not None and ts > until:
                return False
            return True

        records = [r for r in records if in_window(r)]

        # -- attribute filters --
        if get("submitted") is not None:
            want = parse_bool(get("submitted"), "submitted")
            records = [r for r in records if r["submitted"] is want]

        if get("dx_agree") is not None:
            want = parse_bool(get("dx_agree"), "dx_agree")
            records = [r for r in records if r["agrees_with_dx_spot"] is want]

        for param, field in (
            ("band", "band"), ("mode", "mode"), ("callsign", "callsign"),
            ("country", "country"), ("country_code", "country_code"),
        ):
            if get(param) is not None:
                wanted = csv_set(get(param))
                if wanted:
                    records = [
                        r for r in records if str(r[field] or "").lower() in wanted
                    ]

        if get("min_freq") is not None:
            lo = parse_number(get("min_freq"), "min_freq")
            records = [r for r in records if r["frequency"] >= lo]
        if get("max_freq") is not None:
            hi = parse_number(get("max_freq"), "max_freq")
            records = [r for r in records if r["frequency"] <= hi]
        if get("min_hits") is not None:
            need = parse_number(get("min_hits"), "min_hits")
            records = [r for r in records if (r["hits"] or 0) >= need]
        if get("min_snr") is not None:
            need = parse_number(get("min_snr"), "min_snr")
            records = [r for r in records if (r["snr"] or 0) >= need]
        if get("min_confidence") is not None:
            need = parse_number(get("min_confidence"), "min_confidence")
            records = [
                r for r in records if (r["extract_confidence"] or 0) >= need
            ]

        # Free text over the fields a person would actually search by.
        if get("q"):
            needle = get("q").strip().lower()
            records = [
                r for r in records
                if needle in " ".join(
                    str(r[f] or "").lower()
                    for f in ("callsign", "name", "country", "band", "grid",
                              "spot_comment", "heard_text")
                )
            ]

        # -- sort --
        sort = get("sort", "last_heard")
        if sort not in self._SORT_KEYS:
            raise QueryError(
                f"invalid sort {sort!r} — one of: "
                + ", ".join(sorted(self._SORT_KEYS))
            )
        order = get("order", "desc").lower()
        if order not in ("asc", "desc"):
            raise QueryError(f"invalid order {order!r} — use asc or desc")
        field = self._SORT_KEYS[sort]

        def sort_key(r):
            v = r.get(field)
            # None sorts last in either direction rather than raising on a
            # str/None comparison — a station with no coordinates or no
            # submission still has to appear somewhere.
            if v is None:
                return (1, "")
            return (0, v.lower() if isinstance(v, str) else v)

        records.sort(key=sort_key, reverse=(order == "desc"))
        matched = len(records)

        # -- paginate --
        offset = int(parse_number(get("offset", "0"), "offset"))
        limit_raw = get("limit")
        limit = int(parse_number(limit_raw, "limit")) if limit_raw is not None else 500
        if offset < 0 or limit < 0:
            raise QueryError("offset and limit must not be negative")
        limit = min(limit, 5000)          # a bound the caller cannot lift
        page = records[offset:offset + limit]

        # -- field projection --
        if get("fields"):
            wanted = csv_set(get("fields"))
            known = set(self._record({}, "").keys())
            unknown = wanted - known
            if unknown:
                raise QueryError(
                    "unknown field(s): " + ", ".join(sorted(unknown))
                )
            page = [{k: v for k, v in r.items() if k.lower() in wanted} for r in page]

        return {
            "generated_at": now,
            "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "total": total,        # every confirmed sighting held
            "matched": matched,    # how many passed the filters
            "count": len(page),    # how many are in this response
            "offset": offset,
            "limit": limit,
            "spots": page,
        }

    # -- Explanation ------------------------------------------------------

    def _verdict(self, cand: Dict[str, Any]) -> Dict[str, Any]:
        """
        What the scanner would do with this candidate, in the order
        _process() actually applies its gates. Stops at the first failure,
        because that is the one that matters — reporting every gate a dead
        candidate would also have failed just buries the real reason.
        """
        min_conf = self.gates.get("min_extract_confidence")
        min_len = self.gates.get("min_callsign_length")

        if min_conf is not None and cand["confidence"] < min_conf:
            return {
                "reached_qrz": False, "gate": "min_extract_confidence",
                "detail": (
                    f"scored {cand['confidence']:.2f}, needs "
                    f"{min_conf:.2f} — extracted, then dropped"
                ),
            }
        if min_len is not None and len(cand["normalised"]) < min_len:
            return {
                "reached_qrz": False, "gate": "min_callsign_length",
                "detail": (
                    f"{len(cand['normalised'])} characters, needs {min_len}"
                ),
            }
        if not cand["lookupable"]:
            return {
                "reached_qrz": False, "gate": "shape",
                "detail": "not a legal callsign once normalised",
            }
        return {
            "reached_qrz": True, "gate": None,
            "detail": "sent to QRZ — existence decides it from here",
        }

    @staticmethod
    def _summarise(result: Dict[str, Any]) -> str:
        """One plain sentence for the top of the modal."""
        looked_up = [
            c for c in result["candidates"] if c["verdict"]["reached_qrz"]
        ]
        if looked_up:
            return "Looked up: " + ", ".join(c["normalised"] for c in looked_up)

        if result["candidates"]:
            first = result["candidates"][0]
            return (
                f"Found {first['callsign']} but did not look it up — "
                f"{first['verdict']['detail']}"
            )

        unmapped = [t for t in result["tokens"] if t["maps_to"] is None]
        if not result["runs"]:
            if unmapped and len(unmapped) == len(result["tokens"]):
                return "No word here maps to a letter or digit"
            return "Nothing callsign-like in this line"

        best = max(result["runs"], key=lambda r: len(r["text"]))
        if best["outcome"] == "below_evidence":
            return (
                f"Assembled {best['text']} but it scored "
                f"{best['evidence']} against a threshold of {best['threshold']}"
            )
        return (
            f"Assembled {best['text']}, which is not a legal callsign shape"
        )

    @staticmethod
    def _sse(event_type: str, data: Any) -> str:
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    def _broadcast(self, event_type: str, data: Any) -> None:
        payload = self._sse(event_type, data)
        with self._subscribers_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # A stalled client's queue filling up must not block the
                # scanner's own threads — just drop the event for them.
                pass

    # -- Mutators (called from scanner.py's threads) ---------------------

    def set_current(self, worker: int, target: Dict[str, Any]) -> None:
        """A real hop happened — `target` is dataclasses.asdict(Target)."""
        entry = {**target, "started_at": time.time(), "worker": worker}
        with self._lock:
            self._current[worker] = entry
        self._broadcast("hop", entry)

    def push_transcript(
        self, worker: int, band: str, freq: int, completed: bool, text: str
    ) -> None:
        """
        Record a transcript segment.

        Mirrors how the server and the stock UberSDR whisper extension model
        this (decoder.go processSegments / static/extensions/whisper/main.js):
        a *completed* segment is final and is appended to the transcript,
        whereas an *incomplete* one is the current utterance still being
        refined — WhisperLive re-sends it repeatedly as it grows, and only
        ever one is in flight. So it replaces the previous live line rather
        than appending, otherwise one utterance renders as a column of
        near-identical lines ("… The" / "… The" / "… The …").
        """
        entry = {
            "time": time.time(), "band": band, "freq": freq,
            "completed": completed, "text": text, "worker": worker,
        }
        with self._lock:
            if completed:
                self._transcripts[worker].append(entry)
                self._live[worker] = None
            else:
                self._live[worker] = entry
        self._broadcast("transcript" if completed else "live", entry)

    def push_confirmed(
        self, detection: Dict[str, Any], is_repeat: bool, freq_bucket: int
    ) -> None:
        """`detection` is dataclasses.asdict(Detection)."""
        call = detection["normalised"]
        key = f"{call}|{freq_bucket}"
        with self._lock:
            existing = self._confirmed.get(key)
            entry = dict(detection)
            entry["key"] = key
            entry["first_seen"] = existing["timestamp"] if existing else detection["timestamp"]
            entry["hit_count"] = (existing["hit_count"] + 1) if existing else 1
            entry["is_repeat"] = is_repeat
            entry["spotted_at"] = existing.get("spotted_at") if existing else None
            self._confirmed[key] = entry
            self._record_history("confirmed", entry.get("band", ""), call)
        self._broadcast("confirmed", entry)

    def push_spot(
        self, callsign: str, freq: int, comment: str, freq_bucket: int,
        band: str = "", country_code: str = "", country: str = "",
        mode: str = "",
    ) -> None:
        # Band and country travel with the spot rather than being worked out
        # in the browser. The server already resolved both, and the frontend's
        # job is to render what it is given: re-deriving the band from the
        # frequency risks a second band plan that disagrees with every other
        # table, and mapping a country NAME to a flag would be wrong for
        # exactly the entities this hears most (England, Scotland and Wales
        # are three DXCC entities and one ISO country).
        key = f"{callsign}|{freq_bucket}"
        entry = {
            "time": time.time(), "callsign": callsign, "band": band,
            "country_code": country_code, "country": country, "mode": mode,
            "freq": freq, "comment": comment, "key": key,
        }
        with self._lock:
            self._spots.append(entry)
            if key in self._confirmed:
                self._confirmed[key]["spotted_at"] = entry["time"]
            self._record_history("spotted", band, callsign)
            self._spot_counts[callsign] = self._spot_counts.get(callsign, 0) + 1
        self._broadcast("spot", entry)

    def set_worker_status(self, worker: int, connected: bool, detail: str = "") -> None:
        """
        Whether this worker is attached to Whisper and transcribing.

        Worth surfacing because the failure is silent otherwise: whisper
        max_users defaults to 2 on the server, so a second worker is routinely
        refused with "maximum users reached" while the first carries on
        happily. Without this the dashboard just shows an empty panel with no
        indication of why.
        """
        entry = {"worker": worker, "connected": connected, "detail": detail}
        with self._lock:
            self._status[worker] = entry
        self._broadcast("status", entry)

    def update_signal(
        self, worker: int, snr: float, peak: Optional[float], threshold: float
    ) -> None:
        """
        Live signal reading for the frequency currently tuned.

        Called from the scanner's own rate limiter, not per audio frame —
        frames arrive at ~50 Hz, which would swamp the SSE stream and every
        connected browser for no visible benefit.
        """
        entry = {
            "snr": snr, "peak": peak, "threshold": threshold,
            "time": time.time(), "worker": worker,
        }
        with self._lock:
            self._signal[worker] = entry
        self._broadcast("signal", entry)

    def update_stats(self, stats: Dict[str, Any], targets: List[Dict[str, Any]]) -> None:
        """`targets` is [dataclasses.asdict(t) for t in tracker.snapshot()]."""
        with self._lock:
            self._stats = dict(stats)
            self._targets = targets
            payload = self._stats_payload_locked()
        self._broadcast("stats", payload)
        self._broadcast("targets", targets)

    # -- Snapshot ---------------------------------------------------------

    def _stats_payload_locked(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "unique_confirmed": len(self._confirmed),
            "uptime": time.time() - self._start_time,
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "workers": [
                    {
                        "id": w,
                        "current": self._current[w],
                        "live": self._live[w],
                        "signal": self._signal[w],
                        "transcript": list(self._transcripts[w]),
                        "audio_available": self.audio[w].available,
                        "status": self._status[w],
                    }
                    for w in self.worker_ids
                ],
                "confirmed": list(self._confirmed.values()),
                "spots": list(self._spots),
                "targets": self._targets,
                "stats": self._stats_payload_locked(),
            }

    # -- Lifecycle ----------------------------------------------------------

    def start(self, host: str = "0.0.0.0", port: int = 6098) -> None:
        self._server = make_server(host, port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("Web UI listening on http://%s:%d/", host, port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
