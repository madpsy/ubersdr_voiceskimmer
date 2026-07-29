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
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, Response, jsonify, request
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
        self, workers: int = 1, transcript_maxlen: int = 300, spots_maxlen: int = 100
    ):
        self._lock = threading.Lock()
        self._start_time = time.time()

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
        # Keyed by normalised callsign — latest sighting wins, but first_seen/
        # hit_count accumulate across repeats, mirroring the on-screen
        # "(repeat)" tracking in scanner.py's own self.confirmed dict.
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

    def push_confirmed(self, detection: Dict[str, Any], is_repeat: bool) -> None:
        """`detection` is dataclasses.asdict(Detection)."""
        call = detection["normalised"]
        with self._lock:
            existing = self._confirmed.get(call)
            entry = dict(detection)
            entry["first_seen"] = existing["timestamp"] if existing else detection["timestamp"]
            entry["hit_count"] = (existing["hit_count"] + 1) if existing else 1
            entry["is_repeat"] = is_repeat
            entry["spotted_at"] = existing.get("spotted_at") if existing else None
            self._confirmed[call] = entry
        self._broadcast("confirmed", entry)

    def push_spot(self, callsign: str, freq: int, comment: str) -> None:
        entry = {
            "time": time.time(), "callsign": callsign,
            "freq": freq, "comment": comment,
        }
        with self._lock:
            self._spots.append(entry)
            if callsign in self._confirmed:
                self._confirmed[callsign]["spotted_at"] = entry["time"]
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
