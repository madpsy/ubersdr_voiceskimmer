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
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, Response, jsonify, request

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
from werkzeug.serving import make_server

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
        self.explain_limiter = RateLimiter(interval=1.0)

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
            allowed, retry_after = self.explain_limiter.allow(client_ip(request))
            if not allowed:
                resp = jsonify({
                    "error": "rate limited — one analysis per second",
                    "retry_after": round(retry_after, 2),
                })
                resp.status_code = 429
                # Whole seconds: RFC 9110 delay-seconds is an integer, and
                # rounding up rather than down avoids sending the client
                # straight back into another rejection.
                resp.headers["Retry-After"] = str(math.ceil(retry_after)) or "1"
                return resp

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
        self._broadcast("confirmed", entry)

    def push_spot(
        self, callsign: str, freq: int, comment: str, freq_bucket: int,
        band: str = "", country_code: str = "", country: str = "",
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
            "country_code": country_code, "country": country,
            "freq": freq, "comment": comment, "key": key,
        }
        with self._lock:
            self._spots.append(entry)
            if key in self._confirmed:
                self._confirmed[key]["spotted_at"] = entry["time"]
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
