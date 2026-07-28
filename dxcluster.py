"""
DX spot submission via the UberSDR dxcluster addon's WebSocket terminal.

The addon tunnels the standard AK1A/DX Spider telnet protocol over a
WebSocket at:

    ws(s)://<base>/addon/dxcluster/api/terminal

Login is "<CALLSIGN> <PASSWORD>" on one line — a DX Spider convention the
addon supports specifically so scripts can authenticate in a single step
(confirmed live: the banner replies "Spot submission enabled" immediately,
no separate SET/SPOTPASS round-trip needed). Submission is the standard

    DX <freq_kHz> <callsign> [comment]

Frequency must be in kHz, not Hz. The comment is capped at 50 characters
server-side (spotMaxCommentLen in commands.go) — anything longer is silently
truncated there, so we truncate client-side too to avoid surprises.

Spot submission is a real, externally-visible action: every spot appears to
every connected DX cluster client, immediately. It is opt-in (--spot) and
every failure is logged and swallowed rather than raised, so a DX cluster
outage never interrupts the scan itself.
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple
from urllib.parse import urlsplit

import websocket

from useragent import USER_AGENT

log = logging.getLogger(__name__)

# Server-enforced cap (spotMaxCommentLen in ubersdr_dxcluster/commands.go).
MAX_COMMENT_LEN = 50


class DXClusterSpotter:
    """Persistent connection to the DX cluster terminal for spot submission."""

    def __init__(
        self, base_url: str, spotter_call: str, spotter_pass: str,
        timeout: float = 15.0,
    ):
        parsed = urlsplit(base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        self.ws_url = f"{scheme}://{parsed.netloc}/addon/dxcluster/api/terminal"
        self.spotter_call = spotter_call.upper().strip()
        self.spotter_pass = spotter_pass
        self.timeout = timeout

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._can_spot = threading.Event()
        self._rejected = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Connect and log in. Returns True once spot submission is confirmed
        authenticated, False on any failure (connection, bad callsign, wrong
        password)."""
        self._ws = websocket.WebSocketApp(
            self.ws_url,
            header={"User-Agent": USER_AGENT},
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=lambda ws, err: log.debug("DX cluster WS error: %s", err),
            on_close=lambda ws, code, msg: log.info(
                "DX cluster connection closed (%s)", code
            ),
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(self.timeout):
            log.error("Timed out connecting to the DX cluster terminal")
            return False

        # Login is sent on open; wait for the server's confirmation text.
        if self._can_spot.wait(self.timeout):
            return True
        if self._rejected.is_set():
            return False
        log.error(
            "DX cluster login sent but spot submission was never confirmed "
            "(wrong password, or spot submission not enabled on this instance)"
        )
        return False

    def _on_open(self, ws) -> None:
        log.info("DX cluster terminal connected — logging in as %s", self.spotter_call)
        # DX Spider one-line shortcut: "CALLSIGN PASSWORD" authenticates for
        # spot submission in a single round-trip.
        ws.send(f"{self.spotter_call} {self.spotter_pass}\n")
        self._ready.set()

    def _on_message(self, ws, message) -> None:
        if not isinstance(message, str):
            return
        if "Spot submission enabled" in message:
            self._can_spot.set()
            log.info("DX cluster spot submission authenticated")
        elif "is an invalid callsign" in message:
            log.error("DX cluster rejected callsign %r", self.spotter_call)
            self._rejected.set()
        elif "Sorry" in message and "invalid" in message.lower():
            log.error("DX cluster login rejected: %s", message.strip())
            self._rejected.set()

    def submit(self, freq_hz: int, callsign: str, comment: str) -> bool:
        """
        Submit a DX spot. Best-effort: any failure is logged and returns
        False rather than raising.
        """
        if self._ws is None or not self._can_spot.is_set():
            log.warning(
                "DX cluster not authenticated for spot submission; skipping %s",
                callsign,
            )
            return False

        freq_khz = freq_hz / 1000.0
        comment = " ".join(comment.split())[:MAX_COMMENT_LEN]
        cmd = f"DX {freq_khz:.1f} {callsign} {comment}\n"

        with self._lock:
            try:
                self._ws.send(cmd)
            except Exception as exc:
                log.warning("DX spot submission failed for %s: %s", callsign, exc)
                return False

        log.info(
            "DX SPOT SUBMITTED  %-10s %.3f MHz  %r", callsign, freq_hz / 1e6, comment
        )
        return True

    def stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass


class SpotThrottle:
    """
    Decides whether a (callsign, frequency) pair is due for another spot.

    Independent from the on-screen "(repeat)" tracking in scanner.py, which
    marks a callsign as seen-before for the whole run and never resets — that
    is about not cluttering the display, not about cluster etiquette. This is
    the reverse: a station still active 20 minutes after its first spot is
    itself useful information for anyone watching the cluster, so it SHOULD
    be spotted again once the cooldown has elapsed, on the same or a
    different frequency.

    Bounded to `max_entries` so a long-running scan can't grow this
    unboundedly. Eviction is LRU by way of Python's OrderedDict: re-inserting
    an existing key moves it to the end, so entries that keep getting
    resubmitted stay resident and only genuinely stale ones get evicted —
    not simply whichever was inserted first.
    """

    def __init__(
        self, cooldown: float = 900.0, max_entries: int = 1000,
        freq_tolerance_hz: int = 100,
    ):
        self.cooldown = cooldown
        self.max_entries = max_entries
        # The voice-activity detector's dial-frequency estimate can wobble a
        # little between hearings of the same station — without tolerance
        # that would look like two different targets and defeat the
        # cooldown. Bucket width is 2x tolerance so two frequencies within
        # freq_tolerance_hz of each other very likely land in the same
        # bucket. (Edge case: two frequencies exactly at a bucket boundary
        # can still land in adjacent buckets — acceptable for a cooldown that
        # only needs to be approximately right.)
        self._bucket_hz = max(1, freq_tolerance_hz * 2)
        self._last_spot: "OrderedDict[Tuple[str, int], float]" = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, callsign: str, freq_hz: int) -> Tuple[str, int]:
        bucket = round(freq_hz / self._bucket_hz) * self._bucket_hz
        return (callsign, bucket)

    def should_spot(self, callsign: str, freq_hz: int) -> bool:
        """True if this pair has never been spotted, or the cooldown since
        its last spot has elapsed. Does not record anything — call record()
        only after a successful submission, so a failed attempt can still be
        retried immediately rather than being throttled by its own failure."""
        key = self._key(callsign, freq_hz)
        with self._lock:
            last = self._last_spot.get(key)
        return last is None or (time.time() - last) >= self.cooldown

    def record(self, callsign: str, freq_hz: int) -> None:
        key = self._key(callsign, freq_hz)
        with self._lock:
            self._last_spot.pop(key, None)  # drop-then-add moves it to the end
            self._last_spot[key] = time.time()
            while len(self._last_spot) > self.max_entries:
                self._last_spot.popitem(last=False)  # evict the oldest

    def __len__(self) -> int:
        with self._lock:
            return len(self._last_spot)
