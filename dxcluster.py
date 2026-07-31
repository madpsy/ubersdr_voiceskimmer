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

The connection is supervised rather than one-shot: a scan runs for hours or
days, and cluster nodes restart, drop idle sessions and go behind flaky
links. A DXClusterSpotter keeps a background thread that reconnects and
re-logs-in for as long as the scan lasts, so a cluster that is down at
startup — or that disappears halfway through the night — costs the run a few
spots rather than all of them. The one exception is a rejected callsign or
password: that will never succeed however long we retry, so it stops.
"""

import logging
import random
import threading
import time
from collections import OrderedDict
from typing import Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

from phonetics import supersedes

import websocket

from useragent import USER_AGENT

log = logging.getLogger(__name__)

# Server-enforced cap (spotMaxCommentLen in ubersdr_dxcluster/commands.go).
MAX_COMMENT_LEN = 50


class DXClusterSpotter:
    """
    Supervised connection to the DX cluster terminal for spot submission.

    A single background thread owns the socket for the life of the object: it
    connects, logs in, runs the WebSocket until it closes for any reason, then
    backs off and does the whole thing again. Callers never see the churn —
    submit() simply reports whether the connection happened to be authenticated
    at that moment, and the next hearing of the same station will try again
    (SpotThrottle only records successes, so a spot lost to an outage is not
    throttled out).
    """

    # Reconnect backoff. Starts short so a node restart or a momentary blip
    # costs almost nothing, and caps low enough that a multi-hour outage is
    # picked up within a minute of the cluster coming back.
    BACKOFF_START = 5.0
    BACKOFF_MAX = 60.0

    def __init__(
        self, base_url: str, spotter_call: str, spotter_pass: str,
        timeout: float = 15.0,
        backoff_start: Optional[float] = None,
        backoff_max: Optional[float] = None,
    ):
        parsed = urlsplit(base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        self.ws_url = f"{scheme}://{parsed.netloc}/addon/dxcluster/api/terminal"
        self.spotter_call = spotter_call.upper().strip()
        self.spotter_pass = spotter_pass
        self.timeout = timeout
        self.backoff_start = self.BACKOFF_START if backoff_start is None else backoff_start
        self.backoff_max = self.BACKOFF_MAX if backoff_max is None else backoff_max

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        # Set only while a live socket is logged in and cleared the moment it
        # closes — this is the single source of truth for "can I spot now?".
        self._can_spot = threading.Event()
        # Terminal: bad callsign or password. Retrying cannot fix it.
        self._rejected = threading.Event()
        # Set by stop(); also doubles as the interruptible sleep for backoff.
        self._stopping = threading.Event()
        self._watchdog_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        # Per-attempt bookkeeping, for the backoff decision and the reconnect
        # log line. _session_ok is sticky for the attempt where _can_spot is
        # not: _can_spot is cleared the moment the socket closes, so it cannot
        # answer "did this session ever work?" once the session is over.
        self._last_error: Optional[BaseException] = None
        self._opened = False
        self._session_ok = False

        # Diagnostics, read by tests and the shutdown summary.
        self.attempts = 0
        self.sessions = 0     # attempts that got as far as authenticated

    # -- lifecycle ---------------------------------------------------------

    def start(self, wait: bool = True) -> bool:
        """
        Start the supervisor thread and, by default, wait up to `timeout` for
        the first login to be confirmed.

        Returns True if spot submission is authenticated by the time it
        returns. False means "not yet" — not "never": unless failed_permanently()
        is also true, the supervisor is still running and will keep retrying,
        so the caller should hold on to the object either way.
        """
        if self._thread is not None:
            raise RuntimeError("DXClusterSpotter.start() called twice")
        self._thread = threading.Thread(
            target=self._supervise, name="dxcluster-spotter", daemon=True
        )
        self._thread.start()
        if not wait:
            return False
        return self.wait_ready(self.timeout)

    def wait_ready(self, timeout: float) -> bool:
        """Wait up to `timeout` for an authenticated connection. Returns early
        if the credentials are rejected or stop() is called — neither will ever
        become ready, so there is nothing left to wait for."""
        deadline = time.monotonic() + timeout
        while True:
            if self._can_spot.is_set():
                return True
            if self._rejected.is_set() or self._stopping.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._can_spot.is_set()
            self._can_spot.wait(min(0.25, remaining))

    def _supervise(self) -> None:
        """Connect / login / run / reconnect, until stop() or a rejection."""
        backoff = self.backoff_start
        while not self._stopping.is_set():
            self.attempts += 1
            authenticated = self._run_session()

            if self._stopping.is_set() or self._rejected.is_set():
                break

            if authenticated:
                # A session that actually worked earns a fresh short backoff:
                # a node bouncing us every few hours should reconnect fast
                # each time, not inherit the delay from a past outage.
                backoff = self.backoff_start

            delay = backoff + random.uniform(0, backoff * 0.25)
            # Carry the underlying error into our own line: during a long
            # outage this is the only thing in the log every minute, and
            # "connection refused" and "timed out" want different fixes.
            if authenticated:
                what = "disconnected"
            elif self._last_error is not None:
                what = f"connection failed ({self._last_error})"
            else:
                what = "connection failed"
            log.warning(
                "DX cluster %s — retrying in %.0fs (attempt %d)",
                what, delay, self.attempts + 1,
            )
            self._stopping.wait(delay)
            backoff = min(backoff * 2, self.backoff_max)

        self._can_spot.clear()
        log.info(
            "DX cluster supervisor stopped after %d attempt(s), %d session(s)",
            self.attempts, self.sessions,
        )

    def _run_session(self) -> bool:
        """
        One connect-and-run cycle. Blocks until the socket closes, and returns
        whether this session ever reached the authenticated state.
        """
        self._last_error = None
        self._opened = False
        self._session_ok = False
        ws = websocket.WebSocketApp(
            self.ws_url,
            header={"User-Agent": USER_AGENT},
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        with self._lock:
            self._ws = ws

        # Nothing below has a timeout of its own: websocket-client leaves the
        # socket at the global default (none), so a node that accepts the TCP
        # connection and then never finishes the handshake — or finishes it
        # and never answers the login — would block this thread forever. One
        # watchdog covers both: if the session is not authenticated within a
        # connect-plus-login budget, tear the socket down and let the
        # supervisor back off and try again.
        self._arm_watchdog(ws)

        try:
            # ping/pong is what catches a half-open socket: without it a
            # silently dead link looks connected until the next spot, and the
            # spot is the thing we cannot afford to lose. run_forever returns
            # when the connection ends, for any reason.
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as exc:
            # DNS failures and the like can escape run_forever; the supervisor
            # treats them exactly like a close and backs off.
            log.debug("DX cluster connection attempt failed: %s", exc)

        self._can_spot.clear()
        self._cancel_watchdog()
        with self._lock:
            self._ws = None
        return self._session_ok

    def stop(self) -> None:
        """Stop the supervisor and close the connection. Idempotent."""
        self._stopping.set()
        self._can_spot.clear()
        self._cancel_watchdog()
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)

    # -- connection state --------------------------------------------------

    def is_connected(self) -> bool:
        """True if a spot submitted right now would reach the cluster."""
        return self._can_spot.is_set()

    def failed_permanently(self) -> bool:
        """True if the callsign or password was rejected. No amount of
        retrying will help, so callers should give up on spotting."""
        return self._rejected.is_set()

    # -- callbacks ---------------------------------------------------------

    def _on_open(self, ws) -> None:
        self._opened = True
        log.info("DX cluster terminal connected — logging in as %s", self.spotter_call)
        # DX Spider one-line shortcut: "CALLSIGN PASSWORD" authenticates for
        # spot submission in a single round-trip.
        try:
            ws.send(f"{self.spotter_call} {self.spotter_pass}\n")
        except Exception as exc:
            log.warning("DX cluster login send failed: %s", exc)
            self._close(ws)

    def _on_message(self, ws, message) -> None:
        if not isinstance(message, str):
            return
        if "Spot submission enabled" in message:
            self._cancel_watchdog()
            if not self._session_ok:      # a node may repeat the banner
                self._session_ok = True
                self.sessions += 1
            self._can_spot.set()
            log.info("DX cluster spot submission authenticated")
        elif "is an invalid callsign" in message:
            log.error(
                "DX cluster rejected callsign %r — not retrying", self.spotter_call
            )
            self._reject(ws)
        elif "Sorry" in message and "invalid" in message.lower():
            log.error(
                "DX cluster login rejected: %s — not retrying", message.strip()
            )
            self._reject(ws)

    def _on_error(self, ws, err) -> None:
        self._last_error = err
        log.debug("DX cluster WS error: %s", err)

    def _on_close(self, ws, code, msg) -> None:
        # Clearing here as well as in _run_session so a spot submitted between
        # the close and run_forever returning is refused rather than written
        # into a dead socket.
        self._can_spot.clear()
        self._cancel_watchdog()
        if self._stopping.is_set():
            log.info("DX cluster connection closed (%s)", code)
        elif self._session_ok:
            log.warning("DX cluster connection closed (%s)", code)
        elif self._opened:
            log.info("DX cluster connection closed before login (%s)", code)
        # A close for a socket that never opened is just the failed connect
        # attempt; the supervisor reports that itself, with the reason.

    # -- helpers -----------------------------------------------------------

    def _reject(self, ws) -> None:
        self._rejected.set()
        self._can_spot.clear()
        self._cancel_watchdog()
        self._close(ws)

    def _close(self, ws) -> None:
        try:
            ws.close()
        except Exception:
            pass

    def _arm_watchdog(self, ws) -> None:
        # Budget: one timeout to connect, one to get the login answered.
        self._cancel_watchdog()
        timer = threading.Timer(self.timeout * 2, self._watchdog_fired, [ws])
        timer.daemon = True
        self._watchdog_timer = timer
        timer.start()

    def _watchdog_fired(self, ws) -> None:
        if self._can_spot.is_set() or self._stopping.is_set():
            return
        if self._opened:
            log.error(
                "DX cluster login sent but spot submission was never confirmed "
                "(wrong password, or spot submission not enabled on this "
                "instance) — will retry"
            )
        else:
            log.warning(
                "DX cluster connection stalled before login — will retry"
            )
        self._close(ws)

    def _cancel_watchdog(self) -> None:
        timer, self._watchdog_timer = self._watchdog_timer, None
        if timer is not None:
            timer.cancel()

    # -- submission --------------------------------------------------------

    def submit(self, freq_hz: int, callsign: str, comment: str) -> bool:
        """
        Submit a DX spot. Best-effort: any failure is logged and returns
        False rather than raising.

        A False return is not fatal to the spot — the caller only records the
        cooldown on success, so the next hearing of the same station tries
        again, by which time the supervisor has usually reconnected.
        """
        freq_khz = freq_hz / 1000.0
        comment = " ".join(comment.split())[:MAX_COMMENT_LEN]
        cmd = f"DX {freq_khz:.1f} {callsign} {comment}\n"

        with self._lock:
            ws = self._ws
            if ws is None or not self._can_spot.is_set():
                log.warning(
                    "DX cluster not connected%s; skipping spot for %s "
                    "(reconnecting in the background)",
                    " (credentials rejected)" if self._rejected.is_set() else "",
                    callsign,
                )
                return False
            try:
                ws.send(cmd)
            except Exception as exc:
                log.warning("DX spot submission failed for %s: %s", callsign, exc)
                # The socket is no longer trustworthy. Drop it now so the
                # supervisor reconnects immediately instead of waiting for
                # ping/pong to notice, and so nothing else is written to it.
                self._can_spot.clear()
                self._close(ws)
                return False

        log.info(
            "DX SPOT SUBMITTED  %-10s %.3f MHz  %r", callsign, freq_hz / 1e6, comment
        )
        return True


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
        # Last spot per frequency bucket, whatever the callsign — see
        # seconds_since_spot_on. Bounded the same way as the rest.
        self._last_freq_spot: "OrderedDict[int, float]" = OrderedDict()
        # Validated decodes per (callsign, frequency) — see record_hit.
        # Bounded the same way, so a long run cannot grow it unboundedly.
        self._hits: "OrderedDict[Tuple[str, int], int]" = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, callsign: str, freq_hz: int) -> Tuple[str, int]:
        return (callsign, self.bucket_freq(freq_hz))

    def bucket_freq(self, freq_hz: int) -> int:
        """
        Round a frequency to the same bucket used for hit-counting and the
        spot cooldown. Exposed so callers outside this class (scanner.py's
        confirmed-callsigns tracking) can group by "the same station on the
        same frequency" using the exact same boundary spot_min_hits gates
        on — otherwise a UI-level hit count could disagree with the number
        that actually decides when a spot goes out.
        """
        return round(freq_hz / self._bucket_hz) * self._bucket_hz

    def record_hit(self, callsign: str, freq_hz: int) -> int:
        """
        Count one validated decode of this callsign on this frequency, and
        return the running total.

        Hearing the same callsign on the same frequency repeatedly is real
        corroboration: the extractor can assemble a plausible-but-wrong
        callsign from one garbled pass, but it is unlikely to invent the same
        wrong one twice on the same frequency. Callers use this to hold a spot
        back until it has been decoded --spot-min-hits times.

        Shares the frequency bucketing with the cooldown, so the detector's
        estimate drifting a few Hz between hearings still counts as the same
        station rather than restarting the tally.

        The count is never reset. Once a station has proved itself it stays
        proved, so a re-spot after the cooldown does not have to earn its
        confidence again.
        """
        key = self._key(callsign, freq_hz)
        with self._lock:
            count = self._hits.pop(key, 0) + 1   # drop-then-add keeps it fresh
            self._hits[key] = count
            while len(self._hits) > self.max_entries:
                self._hits.popitem(last=False)
            return count

    def hits(self, callsign: str, freq_hz: int) -> int:
        with self._lock:
            return self._hits.get(self._key(callsign, freq_hz), 0)

    def transfer_hits(self, from_call: str, to_call: str, freq_hz: int) -> int:
        """
        Move `from_call`'s corroboration on this frequency onto `to_call`, and
        return how much moved.

        Used when a longer callsign is found to supersede a shorter one (see
        SupersessionTracker): those hearings were of the same station, so the
        corroboration they represent belongs to the longer callsign. Without
        this the evidence is simply lost, and a station heard three times —
        twice with a character missing — could sit below --spot-min-hits
        indefinitely despite having been heard plenty.

        Moved rather than copied. The shorter callsign is dormant; leaving it
        a tally would let it walk straight back over the threshold if its
        dormancy later expires, when what we want is for it to start again
        from nothing.
        """
        src = self._key(from_call, freq_hz)
        dst = self._key(to_call, freq_hz)
        with self._lock:
            moved = self._hits.pop(src, 0)
            if not moved:
                return 0
            count = self._hits.pop(dst, 0) + moved
            self._hits[dst] = count       # drop-then-add keeps it fresh
            while len(self._hits) > self.max_entries:
                self._hits.popitem(last=False)
            return moved

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
        bucket = self.bucket_freq(freq_hz)
        now = time.time()
        with self._lock:
            self._last_spot.pop(key, None)  # drop-then-add moves it to the end
            self._last_spot[key] = now
            while len(self._last_spot) > self.max_entries:
                self._last_spot.popitem(last=False)  # evict the oldest

            # Also by frequency alone, ignoring which callsign it was. The
            # scanner uses this to shorten a revisit to a frequency it has
            # already produced a spot from — see --revisit-dwell-percent. A
            # separate map rather than scanning _last_spot for a matching
            # bucket: this is read once per dwell and the scan would be
            # O(max_entries) every time.
            self._last_freq_spot.pop(bucket, None)
            self._last_freq_spot[bucket] = now
            while len(self._last_freq_spot) > self.max_entries:
                self._last_freq_spot.popitem(last=False)

    def seconds_since_spot_on(self, freq_hz: int) -> Optional[float]:
        """
        How long since ANY callsign was spotted on this frequency, or None if
        none ever was. Uses the same frequency bucketing as everything else
        here, so the detector's estimate drifting a few Hz still counts as the
        same frequency.
        """
        bucket = self.bucket_freq(freq_hz)
        with self._lock:
            last = self._last_freq_spot.get(bucket)
        return None if last is None else max(0.0, time.time() - last)


class Supersession(NamedTuple):
    """
    What one call to SupersessionTracker.record changed.

    Both fields can be populated at once, if the callsign just heard sits in
    the middle of a chain (it retires ON2B while ON2GBR retires it). The
    caller transfers hits in the order given — inbound first — so the
    corroboration ends up on the callsign left standing.
    """

    retired: List[str]                  # shorter callsigns this one retired
    dormant_under: Optional[str] = None  # a longer one that retired THIS one


class SupersessionTracker:
    """
    Retires a callsign once a longer one that subsumes it turns up on the same
    frequency — see phonetics.supersedes for the shape test.

    The problem: a dropped phonetic word yields a SHORTER callsign that is
    itself real and passes QRZ, so validation cannot tell it from a genuine
    station. Observed live as ON2GB, with ON2GBR arriving on the same
    frequency a couple of minutes later.

    The shape test alone is not enough to act on. ON4AB and ON4KAB are both
    plausible real callsigns that could share a frequency during a QSO, and
    suppressing a real station is a worse outcome than the stray spot this
    exists to prevent — a spot can be seen and dismissed, a station that never
    appears cannot. So two corroboration guards sit in front of the string
    match, both of them about weight of evidence rather than spelling:

      * the longer callsign must have been heard at least as often as the
        shorter one. This is what blocks the INVERSE error, which is real:
        a word following the callsign can be absorbed as a trailing letter
        ("ON2GB, radio check" -> ON2GBR, since "radio" maps to R). A one-off
        decode cannot retire a station heard four times.
      * a shorter callsign that has already cleared `min_hits` is left alone.
        Corroboration is exactly what min_hits measures; something that has
        earned it has stopped looking like a one-pass garble.

    Hearings are counted here rather than read from SpotThrottle because
    SpotThrottle.record_hit is only reached when --spot is enabled, and this
    has to behave identically with spotting off.

    Dormancy expires with the window rather than lasting the whole run. If the
    shorter callsign is still being heard an hour later it is behaving like a
    real station, and the evidence for retiring it has gone stale.

    Bounded to `max_entries` and LRU-evicted, the same as SpotThrottle, so a
    scan running for days cannot grow this without limit.
    """

    def __init__(
        self, window: float = 900.0, min_hits: int = 2, max_entries: int = 1000,
    ):
        self.window = window
        self.min_hits = min_hits
        self.max_entries = max_entries
        # (callsign, freq_bucket) -> [last_heard, hearings]. Counted from
        # confirmed detections only, so it measures corroborated hearings on
        # the same footing as --spot-min-hits.
        self._heard: "OrderedDict[Tuple[str, int], List[float]]" = OrderedDict()
        # (callsign, freq_bucket) -> (superseding callsign, when it happened)
        self._dormant: "OrderedDict[Tuple[str, int], Tuple[str, float]]" = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Drop everything older than the window. Caller holds the lock."""
        cutoff = now - self.window
        for key in [k for k, v in self._heard.items() if v[0] < cutoff]:
            del self._heard[key]
        for key in [k for k, v in self._dormant.items() if v[1] < cutoff]:
            del self._dormant[key]

    def check(self, callsign: str, freq_bucket: int) -> Optional[str]:
        """
        The longer callsign that has retired this one on this frequency, or
        None if it is still live. Read-only — call before crediting a
        detection, so a dormant callsign never reaches the hit count or a
        spot.
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            entry = self._dormant.get((callsign, freq_bucket))
            return entry[0] if entry else None

    def record(self, callsign: str, freq_bucket: int) -> Supersession:
        """
        Count one confirmed hearing of `callsign` on this frequency, and
        report what that changed.

        A retired callsign's hearings are folded into whichever callsign
        survives — they were hearings of that station, decoded with a
        character missing — so the corroboration is carried forward rather
        than discarded.

        BOTH directions are examined on every call, because arrival order is
        not guaranteed and the outcome must not depend on it. The longer
        callsign usually turns up second, so the common case is retiring
        shorter ones already on record; but a garble can equally follow a
        clean decode, and then the callsign just heard is the one that has to
        go dormant. Only ever the shorter of the pair is retired either way —
        a missing phonetic word is far likelier than an invented one, so
        length is the tie-break, not arrival order.
        """
        now = time.time()
        retired: List[str] = []
        dormant_under: Optional[str] = None
        key = (callsign, freq_bucket)

        with self._lock:
            self._prune(now)

            entry = self._heard.pop(key, None)  # drop-then-add keeps it fresh
            if entry is None:
                entry = [now, 0]
            entry[0] = now
            entry[1] += 1
            self._heard[key] = entry

            for (other, bucket), other_entry in list(self._heard.items()):
                if bucket != freq_bucket or other == callsign:
                    continue
                if (other, bucket) in self._dormant:
                    continue

                # Corroboration guards — see the class docstring. Deliberately
                # applied after the cheap shape test, which rejects almost
                # everything.
                if supersedes(callsign, other):
                    if other_entry[1] >= self.min_hits:
                        continue
                    # entry[1] rather than a value captured before the loop: a
                    # retirement earlier in this pass folded its hearings in,
                    # and those count towards outweighing the next candidate.
                    if entry[1] < other_entry[1]:
                        continue
                    self._dormant[(other, bucket)] = (callsign, now)
                    retired.append(other)
                    # Those hearings were of this station, so its corroboration
                    # comes along. The shorter one's tally is removed rather
                    # than left behind: it is dormant, and if that later
                    # expires it should have to earn its way back from nothing.
                    # (See SpotThrottle.transfer_hits, which does the same for
                    # the count that actually gates spotting.)
                    entry[1] += other_entry[1]
                    del self._heard[(other, bucket)]

                elif dormant_under is None and supersedes(other, callsign):
                    # The longer callsign was already on record when this
                    # shorter one arrived. Without this branch the shorter one
                    # would stay live purely because it turned up second, and
                    # could go on to be spotted — the very outcome the rule
                    # exists to prevent.
                    if entry[1] >= self.min_hits:
                        continue
                    if other_entry[1] < entry[1]:
                        continue
                    self._dormant[key] = (other, now)
                    dormant_under = other
                    other_entry[1] += entry[1]
                    del self._heard[key]

            while len(self._heard) > self.max_entries:
                self._heard.popitem(last=False)
            while len(self._dormant) > self.max_entries:
                self._dormant.popitem(last=False)

        return Supersession(retired, dormant_under)

    def hearings(self, callsign: str, freq_bucket: int) -> int:
        """Confirmed hearings of this pair inside the current window."""
        with self._lock:
            entry = self._heard.get((callsign, freq_bucket))
            return entry[1] if entry else 0

    def dormant(self) -> Dict[Tuple[str, int], str]:
        """Every currently-dormant (callsign, bucket) and what retired it."""
        now = time.time()
        with self._lock:
            self._prune(now)
            return {k: v[0] for k, v in self._dormant.items()}

    def __len__(self) -> int:
        with self._lock:
            return len(self._last_spot)
