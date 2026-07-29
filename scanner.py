#!/usr/bin/env python3
"""
UberSDR callsign scanner — proof of concept.

Hops around detected voice activity, feeds each frequency to the Whisper
speech-to-text extension, extracts candidate callsigns from the transcript, and
validates each one against QRZ via /api/lookup.

    python3 scanner.py --host localhost --port 8080

Run it on the SDR host or LAN: those IPs are in server.timeout_bypass_ips by
default, which exempts the session from max_session_time and removes the 10/min
lookup rate limit.

Output is a JSONL log — one record per detection, with the raw transcript that
produced it. That file is the actual deliverable of the PoC: it tells you what
Whisper really produces on live SSB and how much of it survives validation.
"""

import argparse
import json
import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set

from activity import ActivityTracker, Target
from lookup import CallsignValidator, LookupResult
from preflight import run_preflight
from phonetics import (
    Candidate,
    extract_callsigns,
    is_lookupable,
    normalise_callsign,
)
from dxcluster import DXClusterSpotter, SpotThrottle
from timeline import FrequencyTimeline
from ubersdr import Segment, UberSDRSession
from web import WebUI

log = logging.getLogger("scanner")

# Primes Whisper for on-air phonetics. Without this it renders "mike mike three"
# as prose and drifts toward conversational English; with it, spelled-out
# callsigns survive far more often. Kept under the 1024-byte server cap.
DEFAULT_PROMPT = (
    "Amateur radio HF single sideband contact. Operators exchange callsigns "
    "spelled in the NATO phonetic alphabet: alpha bravo charlie delta echo "
    "foxtrot golf hotel india juliett kilo lima mike november oscar papa quebec "
    "romeo sierra tango uniform victor whiskey xray yankee zulu, with digits "
    "zero one two three four five six seven eight nine. Typical speech: "
    "CQ CQ CQ this is mike mike three november delta hotel calling CQ and "
    "standing by. Roger, your signal report is five nine, QTH, QSL, seventy "
    "three, over to you."
)


@dataclass
class Detection:
    """One validated (or rejected) callsign detection, written to the log."""

    time: str
    timestamp: float
    band: str
    frequency: int
    mode: str
    snr: float
    activity_confidence: float
    raw_text: str
    candidate: str
    normalised: str
    source: str
    extract_confidence: float
    strict_tokens: int
    cued: bool
    validated: bool
    lookup_checked: bool
    lookup_summary: str
    name: str = ""
    country: str = ""
    dx_spot: str = ""
    agrees_with_dx_spot: bool = False
    # False when the segment's audio spanned a frequency hop, so the frequency
    # above is a best guess rather than a certainty.
    attribution_certain: bool = True
    straddled_hop: bool = False


class SharedState:
    """
    Everything the scanning workers hold in common.

    One instance per run, handed to every CallsignScanner. Anything here is
    touched from several worker threads at once, so the mutable parts are
    guarded: ActivityTracker, SpotThrottle and DXClusterSpotter carry their
    own locks already, while the counters, the confirmed set and the JSONL
    file did not and are protected here.

    Results deliberately stay shared rather than per worker. A station is a
    station regardless of which session happened to hear it, and splitting
    them would double-count uniques, break the "(repeat)" tag, and let both
    workers spot the same callsign moments apart.
    """

    def __init__(self, args, base_url: str):
        self.tracker = ActivityTracker(
            base_url=base_url,
            bands=args.band,
            min_snr=args.min_snr,
            min_confidence=args.min_confidence,
        )
        self.spot_throttle = SpotThrottle(
            cooldown=args.spot_cooldown,
            max_entries=args.spot_max_entries,
            freq_tolerance_hz=args.spot_freq_tolerance,
        )
        self.spotter: Optional[DXClusterSpotter] = None
        self.lock = threading.Lock()
        self.confirmed: Dict[str, Detection] = {}
        self.stats = {
            "dwells": 0, "segments": 0, "candidates": 0, "malformed": 0,
            "validated": 0, "rejected": 0, "dx_agreements": 0, "straddled": 0,
            "too_short": 0,
        }

        self.log_lock = threading.Lock()
        self.log_file = None

    def bump(self, key: str, amount: int = 1) -> None:
        with self.lock:
            self.stats[key] += amount


class CallsignScanner:
    """
    One scanning session: its own audio socket, Whisper attach and dwell loop.

    Several can run at once (--parallel), each holding one Whisper slot and
    sharing a SharedState. They never sit on the same frequency — the tracker
    hands out claims, see ActivityTracker.next_target.
    """

    def __init__(
        self,
        args,
        web: Optional[WebUI] = None,
        worker_id: int = 0,
        shared: Optional[SharedState] = None,
    ):
        self.args = args
        self.web = web
        self.worker_id = worker_id
        self.base_url = (
            f"{'https' if args.ssl else 'http'}://{args.host}:{args.port}"
        )
        self.shared = shared or SharedState(args, self.base_url)

        self.segments: "queue.Queue[Segment]" = queue.Queue()
        self.tracker = self.shared.tracker
        self.session: Optional[UberSDRSession] = None
        # Per worker rather than shared: a lookup is authenticated by an
        # active audio session, so each one uses its own. The cost is that a
        # callsign heard by both workers is looked up twice, which is
        # immaterial from a bypassed IP.
        self.validator: Optional[CallsignValidator] = None
        # Recent completed segments for the CURRENT frequency, oldest first,
        # capped at 3 — used to reconstruct callsigns WhisperLive's VAD split
        # mid-utterance (max_speech_duration_s forces a break every 15s with
        # no real pause). Reset to [] on every genuine hop in _dwell().
        self._segment_history: List[Segment] = []

        self.spot_throttle = self.shared.spot_throttle
        self.timeline = FrequencyTimeline(pipeline_latency=args.pipeline_latency)

        # When set, run() dwells on this exact frequency forever instead of
        # pulling from the tracker's rotation — for confirming the pipeline
        # actually catches a specific known signal, e.g. one you're listening
        # to yourself. Not enriched with a DX spot (the exact Hz you give
        # rarely matches the detector's rounded estimate), so
        # agrees_with_dx_spot will always be false here — that's expected.
        self.locked_target: Optional[Target] = None
        if args.lock_freq is not None:
            self.locked_target = Target(
                band="locked",
                dial_freq=args.lock_freq,
                mode=args.lock_mode,
                snr=0.0,
                confidence=1.0,
            )

        self._running = True
        self._last_key: Optional[tuple] = None
        self._worker: Optional[threading.Thread] = None
        self._web_stats_thread: Optional[threading.Thread] = None
        self._extend_lock = threading.Lock()
        self._extend_until = 0.0
        # Set by _segment_worker the instant a VALIDATED callsign is heard on
        # the frequency we are currently dwelling on; _dwell's wait loop polls
        # this and exits immediately rather than lingering.
        self._success_event = threading.Event()
        # Cumulative word count from COMPLETED segments attributed to the
        # current dwell's frequency — used to cut a dwell short when nothing
        # substantive is being heard, rather than sitting out the full
        # --dwell on dead air. Word count, not "did any segment arrive": on
        # a weak/dead frequency Whisper is fed continuous noise and readily
        # hallucinates short stock phrases ("Thanks.", "Thank you.", "Bye.")
        # — exactly the YouTube-sign-off artifacts it's notorious for. Those
        # would otherwise satisfy an any-segment check and disable the
        # silence-timeout for the rest of the dwell despite there being no
        # real signal. Counting only completed segments avoids inflating the
        # total from a single utterance's incomplete segments, which are
        # re-sent repeatedly as Whisper's transcription of it grows.
        self._silence_lock = threading.Lock()
        self._heard_words = 0
        # Peak SNR seen since this dwell started, from the audio frame headers
        # (see UberSDRSession._handle_audio_frame). A direct measurement of
        # whether anything is actually on the frequency, rather than inferring
        # it from what Whisper produced. None until the first frame arrives —
        # if it stays None the server gave us no signal data and the
        # word-count check below is used instead.
        self._peak_snr: Optional[float] = None
        self._last_snr: Optional[float] = None
        self._last_web_signal = 0.0

        # Shared across workers — see SharedState.
        self.stats = self.shared.stats
        self.confirmed = self.shared.confirmed

    # -- Setup --------------------------------------------------------------

    def start(self) -> bool:
        # Shared setup runs once, on whichever worker starts first.
        if self.worker_id == 0:
            if self.args.output:
                self.shared.log_file = open(self.args.output, "a", encoding="utf-8")
                log.info("Logging detections to %s", self.args.output)
            self.tracker.start()

        if self.locked_target is not None:
            first = self.locked_target
            log.info(
                "Locked to %.3f MHz %s — rotation disabled for this run",
                first.dial_freq / 1e6, first.mode.upper(),
            )
        else:
            # Wait briefly for something to point at before opening the
            # session, so the first tune is not an arbitrary frequency.
            deadline = time.time() + 20
            while (
                time.time() < deadline
                and len(self.tracker) == 0
                and self._running
            ):
                time.sleep(1.0)

            first = self.tracker.next_target()
            if first is None:
                log.warning(
                    "No voice activity reported yet — starting on %.3f MHz "
                    "and waiting for the stream", 14200000 / 1e6,
                )

        freq = first.dial_freq if first else 14200000
        mode = first.mode if first else "usb"

        self.session = UberSDRSession(
            host=self.args.host,
            port=self.args.port,
            use_ssl=self.args.ssl,
            password=self.args.password,
            frequency=freq,
            mode=mode,
            on_segment=self.segments.put,
            on_error=self._on_session_error,
            on_signal=self._on_signal,
        )

        if not self.session.start():
            log.error("Could not establish the audio session")
            return False

        self.validator = CallsignValidator(
            base_url=self.base_url,
            session_uuid=self.session.user_session_id,
            min_interval=self.args.lookup_interval,
            prefilter=not self.args.no_prefilter,
        )

        if self.web:
            # Lets the dashboard's listen button relay this session's audio.
            # The session stays muted until someone actually listens.
            self.web.audio[self.worker_id].attach(self.session, self.base_url)

        attach_kwargs = {}
        if not self.args.stock_whisper:
            attach_kwargs = {
                "initial_prompt": self.args.prompt,
                "task": "transcribe",
                "asr_language": self.args.asr_language,
            }

        # Seed the timeline with where we actually opened, so segments arriving
        # before the first hop are attributed correctly.
        if first is not None:
            self.timeline.record(first)

        if not self.session.attach_whisper(**attach_kwargs):
            if self.web:
                self.web.set_worker_status(
                    self.worker_id, False,
                    "Whisper attach failed — the server may be at "
                    "whisper.max_users",
                )
            if attach_kwargs:
                log.error(
                    "Whisper attach failed. If the server reported that per-attach "
                    "recognition parameters are disabled, set "
                    "whisper.allow_client_params: true in config.yaml, or rerun "
                    "with --stock-whisper."
                )
            else:
                log.error("Whisper attach failed (is whisper.enabled set?)")
            return False

        if self.web:
            self.web.set_worker_status(self.worker_id, True, "transcribing")

        # One long-lived consumer for the whole run; the session and the Whisper
        # attach are never rebuilt.
        self._worker = threading.Thread(target=self._segment_worker, daemon=True)
        self._worker.start()

        if self.web:
            # Stats and the band/freq activity table need to stay live
            # regardless of dwell length — --progress-interval (5 min
            # default) is far too coarse for a dashboard, and a single dwell
            # can run for --max-dwell (180s) before the main loop ticks
            # again. A small dedicated thread keeps them fresh on their own
            # short cadence instead.
            self._web_stats_thread = threading.Thread(
                target=self._web_stats_loop, daemon=True
            )
            self._web_stats_thread.start()

        # One cluster login for the whole run, however many workers there are:
        # logging in twice with the same callsign would fight over the same
        # account, and SpotThrottle already dedupes across workers.
        if self.args.spot and self.worker_id == 0:
            spotter = DXClusterSpotter(
                base_url=self.base_url,
                spotter_call=self.args.spotter_call,
                spotter_pass=self.args.spotter_pass,
            )
            if spotter.start():
                self.shared.spotter = spotter
            else:
                # Degrade rather than abort: losing spot submission should
                # not cost the run its actual job of finding and validating
                # callsigns. Leaving shared.spotter as None makes
                # _maybe_spot's early return kick in cleanly instead of
                # retrying (and warning) for every confirmation.
                log.error(
                    "DX cluster spot submission unavailable — continuing "
                    "without it. Confirmed callsigns will still be logged "
                    "and printed, just not spotted."
                )

        return True

    # Frames arrive at ~50 Hz; the dashboard is pushed at most this often.
    _WEB_SIGNAL_INTERVAL = 0.25

    def _on_signal(self, baseband: float, noise: float) -> None:
        """Called ~50x/sec from the audio socket thread for every frame."""
        snr = baseband - noise
        with self._silence_lock:
            self._last_snr = snr
            if self._peak_snr is None or snr > self._peak_snr:
                self._peak_snr = snr
            peak = self._peak_snr
            due = (time.time() - self._last_web_signal) >= self._WEB_SIGNAL_INTERVAL
            if due:
                self._last_web_signal = time.time()

        if due and self.web:
            self.web.update_signal(self.worker_id, snr, peak, self.args.silence_min_snr)

    def _on_session_error(self, message: str) -> None:
        if self.web:
            self.web.set_worker_status(self.worker_id, False, message[:120])
        if "kicked" in message.lower() or "audio session" in message.lower():
            log.error("Session lost: %s", message)
            self._running = False

    # -- Main loop ----------------------------------------------------------

    def run(self) -> None:
        keepalive = time.time()
        last_progress = time.time()

        while self._running:
            if self.locked_target is not None:
                target = self.locked_target
            else:
                target = self.tracker.next_target(
                    exclude=self._last_key, cooldown=self.args.revisit_cooldown
                )
                if target is None:
                    # Either nothing is on the air, or every target is claimed
                    # by another worker. Waiting is right in both cases —
                    # doubling up on a frequency wastes a Whisper slot.
                    log.info("No active voice targets; waiting")
                    if not self._sleep(10.0):
                        break
                    continue

            # Held for the whole dwell so no other worker retunes onto it.
            self.tracker.claim(target)
            try:
                self._dwell(target)
            finally:
                self.tracker.release(target)
            self._last_key = target.key

            if time.time() - keepalive > 30:
                self.session.ping()
                keepalive = time.time()

            if (
                self.args.progress_interval > 0
                and time.time() - last_progress > self.args.progress_interval
            ):
                self._log_progress()
                last_progress = time.time()

    def _log_progress(self) -> None:
        """Periodic heartbeat for long unattended runs — you shouldn't have
        to wait for Ctrl-C to see how the scan is doing so far."""
        log.info(
            "— progress: %d dwells, %d unique confirmed, %d candidates checked "
            "(%d validated, %d rejected), %d segments transcribed —",
            self.stats["dwells"], len(self.confirmed), self.stats["candidates"],
            self.stats["validated"], self.stats["rejected"], self.stats["segments"],
        )

    def _web_stats_loop(self) -> None:
        """Push stats + the band/freq activity table to the dashboard every
        few seconds for the lifetime of the run — see the note in start()."""
        while self._running:
            if self.web:
                self.web.update_stats(
                    self.stats, [asdict(t) for t in self.tracker.snapshot()]
                )
            if not self._sleep(2.0):
                break

    def _dwell(self, target: Target) -> None:
        """
        Point at a target and stay there.

        Segments are NOT collected here — they are consumed continuously by
        _segment_worker and attributed via the timeline, because Whisper's
        output lags its input by seconds and a segment arriving now often
        belongs to the previous frequency.
        """
        # Re-dwelling on the exact frequency we were just on — either
        # --lock-freq, or (rarely) the tracker legitimately had nowhere else
        # to send us. Skip the retune and, critically, the transcript reset:
        # resetting wipes WhisperLive's dedup history, and the tail of the
        # SAME utterance we may have just confirmed is often still in its
        # send buffer. Wiping the history makes that trailing text look "new"
        # on the very next poll, which re-triggers extraction, a cache-hit
        # QRZ re-validation, and an instant success-exit — observed live as a
        # rapid confirm/re-confirm/exit loop with ~0s dwells. Leaving the
        # dedup history intact is exactly what we want here: it's still the
        # same conversation, so suppressing an exact repeat is correct.
        same_as_before = self._last_key == target.key

        if same_as_before:
            log.debug("Re-dwelling on %s without retune/reset", target.band)
        else:
            log.info(
                "→ %s %.3f MHz %s (SNR %.1f dB, conf %.2f)%s",
                target.band, target.dial_freq / 1e6, target.mode.upper(),
                target.snr, target.confidence,
                f" [DX spot: {target.dx_callsign}]" if target.dx_callsign else "",
            )

            if not self.session.tune(target.dial_freq, target.mode):
                log.warning("Tune failed; skipping")
                return

            if self.web:
                self.web.set_current(self.worker_id, asdict(target))

            # Record the hop before resetting, so in-flight audio from the
            # previous frequency is still attributed to it.
            self.timeline.record(target)

            # Clear Whisper's dedup history only — this is a control message,
            # not a teardown. Without it, a repeated phrase on the new
            # frequency would be suppressed as a duplicate of the previous
            # frequency's transcript.
            self.session.reset_transcript()

            # New frequency — the segment-join history belongs to the
            # conversation we just left, not this one.
            self._segment_history = []

        self.shared.bump("dwells")
        with self._extend_lock:
            self._extend_until = 0.0
        self._success_event.clear()

        started = time.time()
        with self._silence_lock:
            self._heard_words = 0
            self._peak_snr = None      # measured fresh for this dwell

        deadline = started + self.args.dwell
        # Each unvalidated candidate pushes the deadline out, so without a
        # hard ceiling a busy net would hold the scanner indefinitely.
        hard_deadline = started + self.args.max_dwell
        silence_deadline = started + self.args.silence_timeout

        # Locked mode has nowhere to "move on" to, so the confirmed/silence
        # early-exits below are meaningless — every validated callsign is
        # already reported live by _segment_worker's _announce() call as it's
        # heard. Here we just idle, waking periodically so run()'s keepalive
        # ping and progress heartbeat still fire, and re-loop with no retune
        # or transcript reset (see the same_as_before branch above).
        locked = self.locked_target is not None

        exit_reason = "timeout"
        while self._running:
            if not locked and self._success_event.is_set():
                exit_reason = "confirmed"
                break

            if not locked and time.time() >= silence_deadline:
                with self._silence_lock:
                    heard_words = self._heard_words
                    peak_snr = self._peak_snr
                # Prefer the direct measurement: a peak above the threshold
                # anywhere in the window means something was genuinely on the
                # frequency, so stay. Only fall back to counting Whisper's
                # words when the server sends no signal data (version 1, or
                # radiod had no channel status), since that count is a proxy
                # for the same question and a poor one — Whisper hallucinates
                # stock phrases on dead air.
                if peak_snr is not None:
                    quiet = peak_snr < self.args.silence_min_snr
                else:
                    quiet = heard_words < self.args.silence_min_words
                if quiet:
                    exit_reason = "silence"
                    break

            with self._extend_lock:
                effective = min(max(deadline, self._extend_until), hard_deadline)
            if time.time() >= effective:
                break
            time.sleep(0.25)

        held = time.time() - started
        if locked:
            pass  # continuous listening — nothing to report about "moving on"
        elif exit_reason == "confirmed":
            log.info("   ✓ callsign confirmed here — moving on (%.0fs)", held)
        elif exit_reason == "silence":
            with self._silence_lock:
                words = self._heard_words
                peak_snr = self._peak_snr
            if peak_snr is not None:
                log.info(
                    "   (peak SNR %.1f dB < %.1f in %.0fs — nothing on this "
                    "frequency, moving on)",
                    peak_snr, self.args.silence_min_snr, held,
                )
            else:
                log.info(
                    "   (only %d word(s) in %.0fs — likely QSY'd, inaudible, or "
                    "hallucinated stock phrases, moving on)",
                    words, held,
                )
        elif held > self.args.dwell + 1:
            log.info("   (held %.0fs)", held)

        self.tracker.mark_visited(target)

    def _segment_worker(self) -> None:
        """
        Continuously drain Whisper output for the lifetime of the run.

        Runs independently of the hop loop so that audio captured before a hop
        is still processed, and credited to the frequency it actually came from.
        """
        while self._running:
            try:
                segment = self.segments.get(timeout=0.5)
            except queue.Empty:
                continue

            self.shared.bump("segments")
            marker = "✓" if segment.completed else "…"
            if self.args.verbose:
                log.info("   %s %s", marker, segment.text)
            if self.web:
                # Best-effort band/freq context: attribution (below) only
                # runs for completed segments, but the dashboard shows the
                # in-progress line too, so tag with wherever we're currently
                # sitting rather than waiting for attribution.
                live = self.timeline.current()
                self.web.push_transcript(
                    self.worker_id,
                    live.band if live else "", live.dial_freq if live else 0,
                    segment.completed, segment.text,
                )

            # Incomplete segments are re-sent repeatedly as Whisper's
            # transcription of the same utterance grows, and would inflate
            # the silence word-count if counted here — only completed
            # segments go on to attribution, counting, and extraction.
            if not segment.completed:
                continue

            duration = max(segment.end - segment.start, 0.0)
            attribution = self.timeline.attribute(segment.received_at, duration)
            target = attribution.target
            if target is None:
                continue

            current = self.timeline.current()
            is_live_target = current is not None and current.key == target.key
            if is_live_target:
                word_count = len(segment.text.split())
                with self._silence_lock:
                    self._heard_words += word_count

            if attribution.straddled:
                self.shared.bump("straddled")
                if self.args.verbose:
                    log.info(
                        "   ~ segment spans a hop; crediting %.3f MHz (%.0f%%)",
                        target.dial_freq / 1e6, attribution.overlap_fraction * 100,
                    )

            detections = self._process(segment, target, attribution)

            # Also try joining with recent completed segments on this same
            # frequency: WhisperLive's VAD forces a segment break every 15s
            # (max_speech_duration_s) even mid-utterance with no real pause,
            # and a spelled callsign can straddle that break — observed live,
            # repeatedly, on operators who spell across what should be one
            # continuous utterance ("Mike India... [forced break] ...number 3
            # Juliet X-ray Golf"). Only joins segments close enough together
            # in the audio's own timeline to plausibly be one utterance; a
            # real conversational pause is left alone. Gated on is_live_target
            # since history is scoped to the current frequency's conversation.
            if is_live_target:
                joined_text = self._joined_history_text(segment)
                if joined_text is not None:
                    joined_segment = Segment(
                        text=joined_text,
                        start=self._segment_history[0].start if self._segment_history else segment.start,
                        end=segment.end,
                        completed=True,
                        received_at=segment.received_at,
                    )
                    detections = detections + self._process(joined_segment, target, attribution)

                self._segment_history.append(segment)
                if len(self._segment_history) > 3:
                    self._segment_history.pop(0)

            validated_count = sum(1 for d in detections if d.validated)
            self.tracker.record_success(target, validated_count)

            # Only act on detections for the frequency we are still sitting
            # on — a late detection attributed to the one we already left
            # must not affect the CURRENT dwell.
            if not is_live_target:
                continue

            if validated_count > 0:
                # A validated callsign is the whole point of visiting this
                # frequency — don't linger for more, move on to the next.
                self._success_event.set()
            elif detections:
                # Something callsign-shaped but not yet confirmed — linger a
                # little in case a repeat lets it validate.
                with self._extend_lock:
                    self._extend_until = max(
                        self._extend_until, time.time() + self.args.dwell_extension
                    )

    def _joined_history_text(self, segment: Segment) -> Optional[str]:
        """
        Build the largest available join of `segment` with immediately
        preceding completed segments on the same frequency, walking backward
        through history while the gap between consecutive segments (measured
        in the audio's own timeline via start/end, not wall-clock — immune to
        transcription-latency jitter) stays within --segment-join-gap. A real
        conversational pause breaks the chain; a forced VAD split does not.

        Returns None if there is nothing to join (no history, or the nearest
        neighbour's gap already exceeds the threshold).
        """
        if self.args.segment_join_gap <= 0 or not self._segment_history:
            return None

        parts = [segment.text]
        boundary = segment.start
        for prev in reversed(self._segment_history[-2:]):
            gap = boundary - prev.end
            if gap < 0 or gap > self.args.segment_join_gap:
                break
            parts.insert(0, prev.text)
            boundary = prev.start

        if len(parts) < 2:
            return None
        return " ".join(parts)

    # -- Extraction and validation -----------------------------------------

    def _process(
        self, segment: Segment, target: Target, attribution
    ) -> List[Detection]:
        candidates = extract_callsigns(segment.text)
        if not candidates:
            return []

        detections: List[Detection] = []
        for cand in candidates[: self.args.max_candidates]:
            if cand.confidence < self.args.min_extract_confidence:
                continue

            self.shared.bump("candidates")
            normalised = normalise_callsign(cand.callsign)

            # Re-check the shape after normalisation — stripping a prefix
            # overlay can leave something that is no longer a callsign, and the
            # server would reject it with a 400 anyway.
            if not is_lookupable(normalised):
                self.shared.bump("malformed")
                if self.args.verbose:
                    log.info("   – %s → %s, not lookupable", cand.callsign, normalised)
                continue

            # Length gate, before spending a lookup. A 3-character candidate
            # is the shortest shape a callsign can take (1-letter prefix +
            # digit + 1-letter suffix — see CALLSIGN_RE), which also makes it
            # the shape most likely to coincidentally match a real but
            # unrelated station. Observed live: "kilo five india" and "kilo
            # five delta" torn out of noisy transcripts both validated
            # against real hams who were never on the air, so QRZ cannot save
            # us here — the only defence is not asking.
            #
            # Literal matches are exempt: a callsign Whisper wrote out
            # verbatim is far stronger evidence than a short run assembled
            # token by token.
            if (cand.source == "phonetic"
                    and len(normalised) < self.args.min_callsign_length):
                self.shared.bump("too_short")
                if self.args.verbose:
                    log.info(
                        "   – %s too short (%d chars, need %d) — not looked up",
                        normalised, len(normalised), self.args.min_callsign_length,
                    )
                continue

            # QRZ is the arbiter. The extractor will invent callsign-shaped
            # strings out of ordinary speech; only a real registry lookup can
            # tell those apart from genuine stations.
            result = self.validator.validate(normalised)
            detection = self._build_detection(
                segment, target, cand, normalised, result, attribution
            )

            if result.valid:
                self.shared.bump("validated")
                if detection.agrees_with_dx_spot:
                    self.shared.bump("dx_agreements")
                is_repeat = normalised in self.confirmed
                self.confirmed[normalised] = detection
                self._announce(detection, is_repeat)
                # Independent of is_repeat: the on-screen tag never resets,
                # but a station still active after the spot cooldown is worth
                # spotting again — see SpotThrottle.
                self._maybe_spot(detection)
            elif result.checked:
                self.shared.bump("rejected")
                if self.args.verbose:
                    log.info("   ✗ %s — not in QRZ", normalised)
            elif self.args.verbose:
                log.info("   ? %s — %s", normalised, result.error)

            detections.append(detection)
            self._write(detection)

        return detections

    def _announce(self, detection: Detection, is_repeat: bool) -> None:
        """
        Print a confirmed callsign in a way that's self-contained and
        unmissable in a long-running, possibly --verbose scroll — includes
        band/frequency/mode on the line itself rather than relying on the
        reader to scroll back to the last hop line, which isn't even reliable
        since a detection can be attributed to a frequency already left.
        Always printed, independent of --verbose.
        """
        marker = "★" if detection.agrees_with_dx_spot else "✓"  # star / check
        who = detection.name or detection.country or "no QRZ bio"
        dx_note = "  [matches DX spot]" if detection.agrees_with_dx_spot else ""
        tag = " (repeat)" if is_repeat else f" [#{len(self.confirmed)} unique]"

        log.info(
            "%s CONFIRMED  %-10s %5s %9.3f MHz %-4s  %s%s%s",
            marker,
            detection.normalised,
            detection.band,
            detection.frequency / 1e6,
            detection.mode.upper(),
            who,
            dx_note,
            tag,
        )
        log.info("      heard: %r", detection.raw_text[:140])

        if self.web:
            self.web.push_confirmed(asdict(detection), is_repeat)

    def _maybe_spot(self, detection: Detection) -> None:
        """
        Submit a DX spot for a confirmed callsign, if --spot is enabled and
        this (callsign, frequency) is due — see SpotThrottle. Comment format
        is "<tag> <QRZ name>" (tag defaults to "[Voice]", see --spot-tag) —
        the tag distinguishes these from manually-submitted or CW-skimmer
        spots in anyone else's cluster view; the name is the same one
        already shown in the CONFIRMED line, so it degrades to the tag alone
        when QRZ has no name on file.
        """
        if self.shared.spotter is None:
            return
        # No length check here — --min-callsign-length is applied before the
        # lookup in _process, so anything too short never became a detection.

        # Corroboration gate. Counted only for candidates that already cleared
        # the quality gates above, so it measures spottable decodes rather
        # than every scrap the extractor produced. The extractor can assemble
        # a plausible-but-wrong callsign from one garbled pass, but it is
        # unlikely to invent the same wrong one twice on the same frequency —
        # so requiring more than one hearing trades a little latency for a
        # markedly lower chance of spotting a station that was never there.
        hits = self.spot_throttle.record_hit(
            detection.normalised, detection.frequency
        )
        if hits < self.args.spot_min_hits:
            log.info(
                "   – %s heard %d/%d times on %.3f MHz — not spotted yet",
                detection.normalised, hits, self.args.spot_min_hits,
                detection.frequency / 1e6,
            )
            return

        if not self.spot_throttle.should_spot(detection.normalised, detection.frequency):
            return

        who = detection.name or detection.country or ""
        comment = f"{self.args.spot_tag} {who}".strip()
        if self.shared.spotter.submit(detection.frequency, detection.normalised, comment):
            self.spot_throttle.record(detection.normalised, detection.frequency)
            if self.web:
                self.web.push_spot(detection.normalised, detection.frequency, comment)

    def _build_detection(
        self, segment: Segment, target: Target,
        cand: Candidate, normalised: str, result: LookupResult, attribution,
    ) -> Detection:
        agrees = bool(
            target.dx_callsign
            and normalise_callsign(target.dx_callsign) == normalised
        )
        return Detection(
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            timestamp=time.time(),
            band=target.band,
            frequency=target.dial_freq,
            mode=target.mode,
            snr=target.snr,
            activity_confidence=target.confidence,
            raw_text=segment.text,
            candidate=cand.callsign,
            normalised=normalised,
            source=cand.source,
            extract_confidence=round(cand.confidence, 3),
            strict_tokens=cand.strict_tokens,
            cued=cand.cued,
            validated=result.valid,
            lookup_checked=result.checked,
            lookup_summary=result.summary,
            name=result.name,
            country=result.country,
            dx_spot=target.dx_callsign,
            agrees_with_dx_spot=agrees,
            attribution_certain=attribution.certain,
            straddled_hop=attribution.straddled,
        )

    def _write(self, detection: Detection) -> None:
        if self.shared.log_file is None:
            return
        # One file, several workers.
        with self.shared.log_lock:
            self.shared.log_file.write(json.dumps(asdict(detection)) + "\n")
            self.shared.log_file.flush()

    # -- Shutdown -----------------------------------------------------------

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns False if we should stop."""
        end = time.time() + seconds
        while time.time() < end:
            if not self._running:
                return False
            time.sleep(0.25)
        return self._running

    def stop(self) -> None:
        self._running = False

    def shutdown(self) -> None:
        log.info("Shutting down worker %d", self.worker_id)
        if self.worker_id == 0:
            self.tracker.stop()

        # Whisper still holds buffered audio. Give it a moment to flush before
        # detaching, or the last segments of the run are lost.
        if self.session is not None and self.session.attached:
            drain = max(self.args.pipeline_latency * 2, 3.0)
            log.info("Draining the transcription pipeline (%.0fs)", drain)
            self._running = True          # keep the worker alive for the drain
            deadline = time.time() + drain
            while time.time() < deadline:
                time.sleep(0.25)
            self._running = False

        if self._worker is not None:
            self._worker.join(timeout=3.0)
        if self._web_stats_thread is not None:
            self._web_stats_thread.join(timeout=3.0)

        if self.session is not None:
            try:
                self.session.detach_whisper()
                time.sleep(0.4)
            except Exception:
                pass
            self.session.stop()

        # Shared resources are torn down once, by the same worker that set
        # them up — another worker may still be draining.
        if self.worker_id == 0:
            if self.shared.spotter is not None:
                self.shared.spotter.stop()
            if self.shared.log_file is not None:
                self.shared.log_file.close()

    def report(self) -> None:
        print("\n" + "=" * 68)
        print("Scan summary")
        print("=" * 68)
        print(f"  Dwells:               {self.stats['dwells']}")
        print(f"  Segments transcribed: {self.stats['segments']}")
        print(f"  Candidates extracted: {self.stats['candidates']}")
        print(f"  Dropped (malformed):  {self.stats['malformed']}")
        print(f"  Dropped (too short):  {self.stats['too_short']}")
        print(f"  Validated by QRZ:     {self.stats['validated']}")
        print(f"  Rejected by QRZ:      {self.stats['rejected']}")
        print(f"  Matched a DX spot:    {self.stats['dx_agreements']}")
        print(f"  Spanned a hop:        {self.stats['straddled']}")

        if self.validator is not None:
            stats = self.validator.stats
            print(
                f"  Lookups: {stats['misses']} sent, {stats['hits']} cached, "
                f"{stats['prefiltered']} prefiltered, {stats['errors']} failed"
            )

        if self.confirmed:
            print(f"\nConfirmed callsigns ({len(self.confirmed)}):")
            for call, det in sorted(self.confirmed.items()):
                flag = " [DX]" if det.agrees_with_dx_spot else ""
                where = f"{det.frequency / 1e6:.3f} MHz {det.band}"
                who = det.name or det.country or ""
                print(f"  {call:<10} {where:<20} {who}{flag}")
        else:
            print("\nNo callsigns confirmed.")
        print()


def parse_band_list(value: str) -> Set[str]:
    """argparse type= for --band: 'a,b,c' -> {'a','b','c'}, lowercased-none
    (bands are matched case-sensitively against server band names like
    "20m", so this deliberately does not alter case)."""
    bands = {b.strip() for b in value.split(",") if b.strip()}
    if not bands:
        raise argparse.ArgumentTypeError("--band requires at least one band")
    return bands


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Scan UberSDR voice activity and extract callsigns via Whisper",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    conn = parser.add_argument_group("connection")
    conn.add_argument("--host", default="localhost", help="UberSDR host")
    conn.add_argument("--port", type=int, default=8080, help="UberSDR port")
    conn.add_argument("--ssl", action="store_true", help="Use https/wss")
    conn.add_argument("--password", help="Bypass password, if the instance needs one")

    scan = parser.add_argument_group("scanning")
    scan.add_argument("--lock-freq", type=int, default=None,
                      help="Stay on this exact frequency (Hz) forever instead "
                           "of hopping around detected voice activity — for "
                           "confirming the pipeline catches one specific known "
                           "signal. Listens continuously and reports every "
                           "validated callsign as it's heard; --silence-timeout "
                           "and the confirmed-callsign early exit do not apply "
                           "since there is nowhere to move on to. Requires "
                           "--lock-mode. Example: --lock-freq 7200000 "
                           "--lock-mode lsb")
    scan.add_argument("--lock-mode", default="usb",
                      choices=["usb", "lsb", "am", "sam", "fm", "nfm", "cwu", "cwl"],
                      help="Mode for --lock-freq")
    scan.add_argument("--band", type=parse_band_list,
                      help="Restrict to one or more bands, comma-separated, "
                           "e.g. --band 20m or --band 20m,40m,80m. Default: "
                           "all bands. The server's SSE stream only supports "
                           "filtering to a single band, so for a list the "
                           "tracker subscribes unfiltered and filters "
                           "client-side instead — functionally identical, "
                           "just slightly more data over the wire.")
    scan.add_argument("--dwell", type=float, default=30.0,
                      help="Seconds to listen on each frequency")
    scan.add_argument("--dwell-extension", type=float, default=30.0,
                      help="Extra seconds when something callsign-shaped is "
                           "heard but not yet QRZ-validated. A validated "
                           "callsign does NOT extend the dwell — the scanner "
                           "moves on immediately instead.")
    scan.add_argument("--max-dwell", type=float, default=180.0,
                      help="Hard ceiling on time spent on one frequency, "
                           "however much is being heard there")
    scan.add_argument("--silence-timeout", type=float, default=10.0,
                      help="Move on early if fewer than --silence-min-words "
                           "have been transcribed within this many seconds of "
                           "tuning in (station moved off, signal faded, dead "
                           "air, etc.). Once enough is heard this no longer "
                           "applies for the rest of the dwell. The frequency "
                           "stays in rotation and is retried on the next sweep.")
    scan.add_argument("--parallel", type=int, default=1,
                      help="Number of scanning sessions to run at once, each "
                           "on its own frequency. Every one holds a Whisper "
                           "slot for the whole run, and whisper.max_users "
                           "defaults to 2 on the server — so 2 here consumes "
                           "every slot and leaves none for web UI users. "
                           "Raise whisper.max_users before going above 1. "
                           "Ignored with --lock-freq, which pins a single "
                           "frequency.")
    scan.add_argument("--silence-min-snr", type=float, default=40.0,
                      help="Peak SNR (dB) that must be seen within "
                           "--silence-timeout for a frequency to count as "
                           "active. Measured directly from the audio frame "
                           "headers, so it does not depend on Whisper "
                           "producing anything. Any single peak above this "
                           "keeps the dwell alive. This is power vs noise "
                           "DENSITY (the server's own min_snr definition), not "
                           "per-channel SNR — subtract ~34.8 dB for a 3 kHz "
                           "SSB channel. Measured live: a quiet frequency "
                           "sits around 33-34 dB and peaks below 38; an "
                           "active one peaks past 40. Used in preference to "
                           "--silence-min-words whenever the server reports "
                           "signal data.")
    scan.add_argument("--silence-min-words", type=int, default=4,
                      help="Words (from completed segments) required within "
                           "--silence-timeout to count as real activity. "
                           "Deliberately not just \"did anything arrive\": "
                           "Whisper readily hallucinates short stock phrases "
                           "(\"Thanks.\", \"Thank you.\", \"Bye.\") on noise/dead "
                           "air, which would otherwise disable the silence "
                           "check despite there being no real signal.")
    scan.add_argument("--revisit-cooldown", type=float, default=120.0,
                      help="Seconds before a frequency may be revisited. "
                           "Ignored when every target is in cooldown.")
    scan.add_argument("--min-snr", type=float, default=8.0,
                      help="Ignore activity below this SNR")
    scan.add_argument("--min-confidence", type=float, default=0.7,
                      help="Ignore activity below this detector confidence")

    extract = parser.add_argument_group("extraction")
    extract.add_argument("--min-callsign-length", type=int, default=4,
                         help="Minimum character length for a "
                              "phonetically-assembled callsign to be looked "
                              "up at all (default 4; literal verbatim matches "
                              "are exempt). Anything shorter is discarded "
                              "before QRZ, so it is never confirmed, logged "
                              "as valid, or spotted. QRZ cannot save us here: "
                              "a bare 3-char candidate is the shortest shape a "
                              "callsign can take and so the most likely to "
                              "coincidentally match a real but unrelated "
                              "station — K5I and K5D, torn out of noisy "
                              "transcripts, both validated against real hams "
                              "who were never on the air.")
    extract.add_argument("--min-extract-confidence", type=float, default=0.4,
                         help="Discard candidates below this heuristic confidence")
    extract.add_argument("--max-candidates", type=int, default=3,
                         help="Most candidates to validate per segment")
    extract.add_argument("--lookup-interval", type=float, default=0.0,
                         help="Min seconds between QRZ lookups (use 6.0 if not "
                              "running from a bypassed IP)")
    extract.add_argument("--no-prefilter", action="store_true",
                         help="Skip the free CTY unallocated-prefix filter and "
                              "send every candidate straight to QRZ")
    extract.add_argument("--segment-join-gap", type=float, default=2.5,
                         help="Seconds of silence (measured in the audio's own "
                              "timeline, between consecutive completed "
                              "segments) below which they're joined and "
                              "extraction is retried on the combined text. "
                              "WhisperLive forces a segment break every 15s "
                              "even mid-utterance with no real pause, which "
                              "otherwise splits a spelled callsign in two. "
                              "0 disables joining.")

    whisper = parser.add_argument_group("whisper")
    whisper.add_argument("--prompt", default=DEFAULT_PROMPT,
                         help="Whisper initial prompt (max 1024 bytes)")
    whisper.add_argument("--asr-language", default="en",
                         help="Recognition language")
    whisper.add_argument("--pipeline-latency", type=float, default=2.0,
                         help="Estimated seconds between audio reaching the "
                              "server and its transcript arriving. Used to "
                              "credit segments to the frequency that produced "
                              "them rather than the current one.")
    whisper.add_argument("--stock-whisper", action="store_true",
                         help="Do not send per-attach recognition parameters. "
                              "Required against a server without "
                              "whisper.allow_client_params enabled.")

    dx = parser.add_argument_group("dx cluster")
    dx.add_argument("--spot", action="store_true",
                    help="Submit a DX spot for each confirmed callsign, via "
                         "this instance's dxcluster addon at "
                         "/addon/dxcluster/. Re-spots the same "
                         "(callsign, frequency) once --spot-cooldown has "
                         "elapsed — see --spot-cooldown. Off by default — "
                         "spots are immediately visible to every connected "
                         "DX cluster client. Requires --spotter-call and "
                         "--spotter-pass, and the addon's spot submission "
                         "must be enabled on this instance.")
    dx.add_argument("--spotter-call",
                    help="Callsign to log in to the DX cluster with "
                         "(required with --spot)")
    dx.add_argument("--spotter-pass",
                    help="DX cluster spot password (required with --spot)")
    dx.add_argument("--spot-cooldown", type=float, default=900.0,
                    help="Seconds before the same (callsign, frequency) may "
                         "be spotted again — a station still active later is "
                         "itself useful information. Default 900s (15 min).")
    dx.add_argument("--spot-freq-tolerance", type=int, default=100,
                    help="Hz tolerance when matching frequency for the "
                         "cooldown, since the detector's dial-frequency "
                         "estimate can wobble slightly between hearings of "
                         "the same station.")
    dx.add_argument("--spot-max-entries", type=int, default=1000,
                    help="Cap on remembered (callsign, frequency) cooldown "
                         "entries, so a long-running scan can't grow this "
                         "unboundedly. Oldest/least-recently-spotted entries "
                         "are evicted first.")
    dx.add_argument("--spot-tag", default="[Voice]",
                    help="Tag prefixed to every spot comment (default "
                         "\"[Voice]\"), e.g. \"[Voice] <QRZ name>\" — "
                         "distinguishes these from manually-submitted or "
                         "CW-skimmer spots in anyone else's cluster view.")
    dx.add_argument("--spot-min-hits", type=int, default=1,
                    help="Times the same callsign must be decoded on the same "
                         "frequency before it is spotted (default 1 — spot on "
                         "the first decode). Above 1 this trades latency for "
                         "confidence: the extractor can assemble a "
                         "plausible-but-wrong callsign from one garbled pass, "
                         "but is unlikely to invent the same wrong one twice "
                         "on the same frequency. Frequency is matched with the "
                         "same tolerance as the cooldown, so a drifting "
                         "estimate does not restart the tally, and the count "
                         "is never reset — a station that has proved itself "
                         "does not have to prove it again after the cooldown.")

    web = parser.add_argument_group("web ui")
    web.add_argument("--web-port", type=int, default=6098,
                     help="Port for the live dashboard (transcript, confirmed "
                          "callsigns, band/freq activity, DX spots). 0 disables "
                          "it entirely.")
    web.add_argument("--web-host", default="0.0.0.0",
                     help="Bind address for the dashboard")

    parser.add_argument("--check", action="store_true",
                        help="Run pre-flight checks against the instance and "
                             "exit without scanning")

    out = parser.add_argument_group("output")
    out.add_argument("--output", default="detections.jsonl",
                     help="JSONL detection log ('' to disable)")
    out.add_argument("-v", "--verbose", action="store_true",
                     help="Log every transcript segment and rejection")
    out.add_argument("--progress-interval", type=float, default=300.0,
                     help="Seconds between periodic progress heartbeats "
                          "(dwell/confirmed/candidate counts), for watching "
                          "a long-running scan without waiting for Ctrl-C. "
                          "0 to disable.")

    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if len(args.prompt.encode("utf-8")) > 1024:
        log.error("--prompt exceeds the server's 1024-byte cap")
        return 2

    if args.spot and not (args.spotter_call and args.spotter_pass):
        log.error("--spot requires both --spotter-call and --spotter-pass")
        return 2

    if args.check:
        base = f"{'https' if args.ssl else 'http'}://{args.host}:{args.port}"
        print(f"\nPre-flight: {base}\n")
        usable, lines = run_preflight(base, args.password or "")
        for line in lines:
            print(f"  {line}")
        print()
        if usable:
            print("Ready to scan.\n")
            return 0
        print("Not usable — fix the FAIL items above.\n")
        return 1

    parallel = max(1, args.parallel)
    if parallel > 1 and args.lock_freq is not None:
        # Every worker would lock onto the same frequency and transcribe
        # identical audio, burning a Whisper slot for nothing.
        log.warning("--lock-freq pins one frequency; forcing --parallel 1")
        parallel = 1

    web_ui = None
    if args.web_port:
        web_ui = WebUI(workers=parallel)
        web_ui.start(args.web_host, args.web_port)

    base_url = f"{'https' if args.ssl else 'http'}://{args.host}:{args.port}"
    shared = SharedState(args, base_url)
    scanners = [
        CallsignScanner(args, web=web_ui, worker_id=wid, shared=shared)
        for wid in range(parallel)
    ]

    def handle_signal(signum, frame):
        log.info("Interrupted")
        for s in scanners:
            s.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threads: List[threading.Thread] = []
    try:
        for s in scanners:
            # Sequential start: worker 0 brings up the shared tracker, log
            # file and cluster login, and each session must register before
            # the next one opens its sockets.
            if not s.start():
                if s.worker_id == 0:
                    return 1      # the finally block handles cleanup
                log.error(
                    "Worker %d failed to start — continuing with %d",
                    s.worker_id, s.worker_id,
                )
                break
            if parallel > 1:
                log.info("Worker %d scanning", s.worker_id)
            t = threading.Thread(target=s.run, name=f"scan-{s.worker_id}")
            t.start()
            threads.append(t)

        for t in threads:
            t.join()
    finally:
        for s in reversed(scanners):     # worker 0 last: it owns the shared bits
            s.stop()
        for t in threads:
            t.join(timeout=5.0)
        for s in reversed(scanners):
            s.shutdown()
        scanners[0].report()
        if web_ui is not None:
            web_ui.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
