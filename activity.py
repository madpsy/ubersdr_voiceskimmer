"""
Voice activity tracking — decides where the scanner points next.

Sources, in order of usefulness:

  GET /api/voice-activity/stream        SSE push, all bands, max 2 conns per IP
  GET /api/noisefloor/voice-activity/all  snapshot, rate limited to 1 req / 2 s

The SSE feed is the primary source. The server's background scanner runs every
5 s and pushes each detection once on first sight, then re-pushes every 60 s
while it stays active (or immediately if its DX callsign changes). So an entry
that has not been refreshed in a few minutes is stale and gets expired.

The snapshot endpoint is used once at startup so the scanner does not have to
wait up to 60 s for the SSE feed to redescribe everything already on the air.

Each entry carries `dx_callsign` when the DX cluster has spotted that frequency
within the last 30 minutes. That is free ground truth: if Whisper hears a
callsign there and it matches the spot, confidence is very high — and it is also
how you measure whether the whole approach works at all.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import requests

from useragent import USER_AGENT

log = logging.getLogger(__name__)

# Bands the server never reports voice activity for (voice_activity.go).
EXCLUDED_BANDS = {"2200m", "630m", "30m"}


@dataclass
class Target:
    """One active voice signal."""

    band: str
    dial_freq: int
    mode: str
    snr: float
    confidence: float
    bandwidth: int = 0
    dx_callsign: str = ""
    dx_country: str = ""
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_visited: float = 0.0
    visits: int = 0
    callsigns_found: int = 0

    @property
    def key(self) -> Tuple[str, int]:
        return (self.band, self.dial_freq)

    @property
    def age(self) -> float:
        return time.time() - self.last_seen

    def priority(self) -> float:
        """
        Scan priority. Higher is scanned sooner.

        Balances signal quality against starvation: a strong signal is worth
        more, but anything unvisited for a long time climbs regardless, so the
        scanner sweeps rather than camping on the loudest station.
        """
        score = 0.0
        score += min(self.snr, 40.0) / 40.0 * 30.0
        score += self.confidence * 20.0

        if self.last_visited == 0.0:
            score += 40.0  # never visited — strongly prefer
        else:
            idle = time.time() - self.last_visited
            score += min(idle / 60.0, 10.0) * 4.0

        # A DX spot means someone is genuinely working this frequency.
        if self.dx_callsign:
            score += 15.0

        # Somewhere we have already succeeded is worth revisiting.
        if self.callsigns_found:
            score += 10.0

        # Decay stale detections.
        score -= min(self.age / 60.0, 5.0) * 3.0
        return score


class ActivityTracker:
    """Maintains the live set of scan targets."""

    def __init__(
        self,
        base_url: str,
        bands: Optional[Set[str]] = None,
        min_snr: float = 0.0,
        min_confidence: float = 0.0,
        expiry: float = 600.0,
    ):
        self.base_url = base_url.rstrip("/")
        # None/empty = no filter, every band. A set rather than a single
        # string so --band accepts a comma-separated list. The SSE stream's
        # own band= query param only ever supported ONE band server-side
        # (see _sse_loop), so for a list we don't ask the server to filter at
        # all — we subscribe unfiltered and filter every event client-side,
        # the same path the snapshot seed already has to use.
        self.bands = bands or None
        self.min_snr = min_snr
        self.min_confidence = min_confidence
        self.expiry = expiry

        self._targets: Dict[Tuple[str, int], Target] = {}
        # Dial frequencies a worker is sitting on right now — see next_target.
        # Keyed on dial_freq alone, not the full (band, freq) key: a
        # --lock-freq worker's Target uses the synthetic band "locked", which
        # would never match a real target's key and so would fail to exclude
        # the frequency it is actually sitting on.
        self._claimed: Set[int] = set()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._http = requests.Session()
        self._http.headers["User-Agent"] = USER_AGENT

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self.seed_from_snapshot()
        self._thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # -- Ingest -------------------------------------------------------------

    def seed_from_snapshot(self) -> int:
        """Prime the target set so we do not wait on the SSE feed."""
        params = {}
        if self.min_confidence > 0:
            params["min_confidence"] = str(self.min_confidence)

        try:
            resp = self._http.get(
                f"{self.base_url}/api/noisefloor/voice-activity/all",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Voice activity snapshot failed: %s", exc)
            return 0

        count = 0
        for band, activities in (data.get("bands") or {}).items():
            if band in EXCLUDED_BANDS:
                continue
            if self.bands and band not in self.bands:
                continue
            for act in activities or []:
                if self._ingest(
                    band=band,
                    dial_freq=int(act.get("estimated_dial_freq") or 0),
                    mode=act.get("mode", ""),
                    snr=float(act.get("snr") or 0.0),
                    confidence=float(act.get("confidence") or 0.0),
                    bandwidth=int(act.get("bandwidth") or 0),
                    dx_callsign=act.get("dx_callsign", "") or "",
                    dx_country=act.get("dx_country", "") or "",
                ):
                    count += 1

        log.info("Seeded %d targets from snapshot", count)
        return count

    def _sse_loop(self) -> None:
        """
        Consume the SSE feed, reconnecting with backoff.

        Always subscribes unfiltered: the server's own band= query param only
        supports a single band, which can't express a multi-band --band list.
        Filtering happens client-side in the loop below instead, uniformly
        for the single-band, multi-band, and no-filter cases.
        """
        url = f"{self.base_url}/api/voice-activity/stream"
        backoff = 2.0

        while self._running:
            try:
                with self._http.get(
                    url, stream=True, timeout=(10, 90),
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    resp.raise_for_status()
                    log.info("Voice activity stream connected")
                    backoff = 2.0

                    for raw in resp.iter_lines(decode_unicode=True):
                        if not self._running:
                            break
                        if not raw or not raw.startswith("data:"):
                            continue
                        payload = raw[5:].strip()
                        if not payload:
                            continue
                        try:
                            event = json.loads(payload)
                        except ValueError:
                            continue
                        if event.get("type") != "voice_activity":
                            continue  # heartbeats and anything else

                        band = event.get("band", "")
                        if band in EXCLUDED_BANDS:
                            continue
                        if self.bands and band not in self.bands:
                            continue
                        self._ingest(
                            band=band,
                            dial_freq=int(event.get("estimated_dial_freq") or 0),
                            mode=event.get("mode", ""),
                            snr=float(event.get("snr") or 0.0),
                            confidence=float(event.get("confidence") or 0.0),
                            bandwidth=int(event.get("bandwidth") or 0),
                            dx_callsign=event.get("dx_callsign", "") or "",
                            dx_country=event.get("dx_country", "") or "",
                        )
            except requests.RequestException as exc:
                if self._running:
                    log.warning("Voice activity stream dropped (%s); retrying", exc)

            if self._running:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _ingest(
        self, band: str, dial_freq: int, mode: str, snr: float,
        confidence: float, bandwidth: int, dx_callsign: str, dx_country: str,
    ) -> bool:
        if dial_freq <= 0:
            return False
        if snr < self.min_snr or confidence < self.min_confidence:
            return False

        key = (band, dial_freq)
        now = time.time()

        with self._lock:
            existing = self._targets.get(key)
            if existing is not None:
                existing.last_seen = now
                existing.snr = snr
                existing.confidence = confidence
                if dx_callsign:
                    existing.dx_callsign = dx_callsign
                    existing.dx_country = dx_country
                return False

            self._targets[key] = Target(
                band=band,
                dial_freq=dial_freq,
                mode=(mode or "USB").lower(),
                snr=snr,
                confidence=confidence,
                bandwidth=bandwidth,
                dx_callsign=dx_callsign,
                dx_country=dx_country,
            )
            return True

    # -- Query --------------------------------------------------------------

    def _expire(self) -> None:
        cutoff = time.time() - self.expiry
        dead = [k for k, t in self._targets.items() if t.last_seen < cutoff]
        for key in dead:
            del self._targets[key]

    def next_target(
        self,
        exclude: Optional[Tuple[str, int]] = None,
        cooldown: float = 0.0,
    ) -> Optional[Target]:
        """
        Highest-priority target, or None if nothing is active.

        Selection is tiered so that a loud, interesting frequency cannot starve
        the rest of the band. The priority score alone is not enough: a target
        with high SNR, a DX spot and a previous success can out-score an
        unvisited weak one even with its idle bonus at zero, and the scanner
        would then sit on it forever.

          tier 1  everything outside its cooldown, excluding the last frequency
          tier 2  ignore the cooldown, but still refuse an immediate repeat
          tier 3  the last frequency, only when it is genuinely all there is

        A frequency another worker is currently sitting on is excluded from
        ALL of those tiers, including the last-resort one — two workers on the
        same frequency would transcribe identical audio and waste a Whisper
        slot, which is the scarcest thing here. When that leaves nothing, the
        caller gets None and waits rather than doubling up.

        Args:
            exclude:  key of the frequency just visited; never chosen unless it
                      is the only target left
            cooldown: seconds a frequency stays ineligible after a visit
        """
        with self._lock:
            self._expire()
            everything = [
                t for t in self._targets.values()
                if t.dial_freq not in self._claimed
            ]
            if not everything:
                return None

            now = time.time()
            not_repeat = [t for t in everything if exclude is None or t.key != exclude]

            fresh = [
                t for t in not_repeat
                if t.last_visited == 0.0 or (now - t.last_visited) >= cooldown
            ]

            pool = fresh or not_repeat or everything
            return max(pool, key=lambda t: t.priority())

    def claim(self, target: Target) -> None:
        """Mark a frequency as being scanned, so no other worker picks it."""
        with self._lock:
            self._claimed.add(target.dial_freq)

    def release(self, target: Target) -> None:
        with self._lock:
            self._claimed.discard(target.dial_freq)

    def claimed(self) -> Set[int]:
        with self._lock:
            return set(self._claimed)

    def mark_visited(self, target: Target) -> None:
        """Called once per dwell, by the hop loop."""
        with self._lock:
            live = self._targets.get(target.key)
            if live is not None:
                live.last_visited = time.time()
                live.visits += 1

    def record_success(self, target: Target, callsigns_found: int) -> None:
        """
        Credit a confirmed callsign to a frequency.

        Separate from mark_visited because segments are attributed
        asynchronously — a success may land well after the dwell that produced
        it has ended, and must not be counted as another visit.
        """
        if callsigns_found <= 0:
            return
        with self._lock:
            live = self._targets.get(target.key)
            if live is not None:
                live.callsigns_found += callsigns_found

    def snapshot(self) -> List[Target]:
        with self._lock:
            self._expire()
            return sorted(
                self._targets.values(), key=lambda t: t.priority(), reverse=True
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._targets)
