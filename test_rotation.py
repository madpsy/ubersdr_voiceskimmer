#!/usr/bin/env python3
"""
Tests for target selection.

The failure this guards against is camping: a loud frequency with a DX spot and
a previous success scores highly enough to beat unvisited targets even with its
idle bonus at zero, so a pure priority sort will keep picking it and the rest of
the band never gets scanned.
"""

import time
import unittest

from activity import Target
from lookup import LookupResult
from scanner import CallsignScanner, SharedState, parse_args
from ubersdr import Segment
from unittest import mock

import activity
import scanner
from activity import ActivityTracker, Target


def tracker_with(*targets) -> ActivityTracker:
    tracker = ActivityTracker(base_url="http://localhost")
    for target in targets:
        tracker._targets[target.key] = target
    return tracker


def make(freq, snr=10.0, conf=0.8, band="20m", **kw) -> Target:
    return Target(
        band=band, dial_freq=freq, mode="usb", snr=snr, confidence=conf, **kw
    )


class TestRotation(unittest.TestCase):
    def test_does_not_repeat_last_frequency(self):
        # A is overwhelmingly attractive and was just visited.
        a = make(14200000, snr=40.0, conf=0.95, dx_callsign="MM3NDH")
        a.last_visited = time.time()
        a.callsigns_found = 3
        b = make(14250000, snr=8.0, conf=0.7)

        tracker = tracker_with(a, b)
        chosen = tracker.next_target(exclude=a.key, cooldown=120.0)
        self.assertEqual(chosen.key, b.key, "camped on the just-visited target")

    def test_priority_alone_would_have_camped(self):
        """Documents why the exclusion tier is needed at all."""
        a = make(14200000, snr=40.0, conf=0.95, dx_callsign="MM3NDH")
        a.last_visited = time.time()
        a.callsigns_found = 3
        b = make(14250000, snr=8.0, conf=0.7)
        self.assertGreater(a.priority(), b.priority())

    def test_cooldown_blocks_recent_visit(self):
        a = make(14200000, snr=30.0)
        a.last_visited = time.time() - 10
        b = make(14250000, snr=5.0)

        tracker = tracker_with(a, b)
        self.assertEqual(
            tracker.next_target(cooldown=120.0).key, b.key
        )

    def test_cooldown_expires(self):
        # Both visited, so neither gets the unvisited bonus: the only thing
        # separating them is whether their cooldown has elapsed.
        now = time.time()
        a = make(14200000, snr=30.0)
        a.last_visited = now - 300          # cooldown long expired
        b = make(14250000, snr=5.0)
        b.last_visited = now - 10           # still in cooldown

        tracker = tracker_with(a, b)
        self.assertEqual(tracker.next_target(cooldown=120.0).key, a.key)

    def test_unvisited_beats_a_stronger_stale_target(self):
        # Deliberate: coverage is worth more than signal strength. A target
        # visited five minutes ago loses to one never heard, even at 6x the SNR.
        a = make(14200000, snr=30.0)
        a.last_visited = time.time() - 300
        b = make(14250000, snr=5.0)

        tracker = tracker_with(a, b)
        self.assertEqual(tracker.next_target(cooldown=120.0).key, b.key)

    def test_single_target_still_returned(self):
        # With nothing else on the air, revisiting is correct.
        a = make(14200000)
        a.last_visited = time.time()
        tracker = tracker_with(a)
        self.assertEqual(tracker.next_target(exclude=a.key, cooldown=120.0).key, a.key)

    def test_all_in_cooldown_falls_through(self):
        now = time.time()
        a = make(14200000, snr=30.0)
        b = make(14250000, snr=5.0)
        a.last_visited = now - 5
        b.last_visited = now - 5

        tracker = tracker_with(a, b)
        chosen = tracker.next_target(exclude=a.key, cooldown=120.0)
        self.assertEqual(chosen.key, b.key, "should still rotate, not stall")

    def test_unvisited_preferred_over_visited(self):
        a = make(14200000, snr=25.0)
        a.last_visited = time.time() - 200
        b = make(14250000, snr=10.0)  # never visited

        tracker = tracker_with(a, b)
        self.assertEqual(tracker.next_target(cooldown=120.0).key, b.key)

    def test_full_sweep_covers_every_target(self):
        """Ten dwells across four targets must touch all four."""
        targets = [make(14200000 + i * 5000, snr=30.0 - i) for i in range(4)]
        tracker = tracker_with(*targets)

        seen = set()
        last = None
        for _ in range(10):
            chosen = tracker.next_target(exclude=last, cooldown=120.0)
            seen.add(chosen.key)
            tracker.mark_visited(chosen)
            last = chosen.key

        self.assertEqual(len(seen), 4, f"only reached {len(seen)} of 4 targets")

    def test_no_targets(self):
        self.assertIsNone(tracker_with().next_target())

    def test_long_run_never_starves_a_target(self):
        """
        Over a realistic run the strong target should be favoured but must not
        starve the weak ones, and must never be picked twice in a row.
        """
        import activity as activity_module

        clock = [1_000_000.0]
        real_time = activity_module.time.time
        activity_module.time.time = lambda: clock[0]
        try:
            targets = []
            for i in range(6):
                t = make(14200000 + i * 5000, snr=30.0 - i * 4, conf=0.9 - i * 0.03)
                t.first_seen = t.last_seen = clock[0]
                if i == 0:
                    t.dx_callsign = "MM3NDH"
                targets.append(t)
            tracker = tracker_with(*targets)

            order = []
            last = None
            for _ in range(24):
                for t in tracker._targets.values():
                    t.last_seen = clock[0]      # keep them from expiring
                chosen = tracker.next_target(exclude=last, cooldown=120.0)
                order.append(chosen.dial_freq)
                tracker.mark_visited(chosen)
                if chosen.dial_freq == 14200000:
                    tracker.record_success(chosen, 1)
                last = chosen.key
                clock[0] += 45.0
        finally:
            activity_module.time.time = real_time

        repeats = sum(1 for i in range(1, len(order)) if order[i] == order[i - 1])
        self.assertEqual(repeats, 0, "picked the same frequency twice in a row")
        self.assertEqual(len(set(order)), 6, "starved at least one target")

        # The best target should be favoured, but not to the point of dominance.
        share = order.count(14200000) / len(order)
        self.assertGreater(share, 1 / 6, "strong target not favoured at all")
        self.assertLess(share, 0.5, "strong target monopolised the scan")


class TestVisitAccounting(unittest.TestCase):
    def test_success_does_not_count_as_a_visit(self):
        # Successes arrive asynchronously, after the dwell has ended.
        a = make(14200000)
        tracker = tracker_with(a)

        tracker.mark_visited(a)
        tracker.record_success(a, 2)
        tracker.record_success(a, 1)

        live = tracker._targets[a.key]
        self.assertEqual(live.visits, 1)
        self.assertEqual(live.callsigns_found, 3)

    def test_zero_successes_ignored(self):
        a = make(14200000)
        tracker = tracker_with(a)
        tracker.record_success(a, 0)
        self.assertEqual(tracker._targets[a.key].callsigns_found, 0)


class TestBandFilter(unittest.TestCase):
    """
    --band accepts a comma-separated list. The server's SSE stream only
    supports filtering to a single band, so a multi-band list means
    subscribing unfiltered and filtering client-side instead — this covers
    that client-side filter through the real seed_from_snapshot() path
    (mocking only the HTTP layer), not by poking _ingest directly, since the
    filter itself lives in the caller.
    """

    @staticmethod
    def _mock_snapshot_response(bands_payload):
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {"bands": bands_payload}
        return response

    def _activity(self, band, freq, snr=10.0, conf=0.8, mode="usb"):
        return {
            "estimated_dial_freq": freq,
            "mode": mode,
            "snr": snr,
            "confidence": conf,
            "bandwidth": 2500,
        }

    def test_no_filter_accepts_every_band(self):
        tracker = activity.ActivityTracker(base_url="http://x")
        payload = {
            "20m": [self._activity("20m", 14200000)],
            "40m": [self._activity("40m", 7200000)],
            "80m": [self._activity("80m", 3700000)],
        }
        with mock.patch.object(
            tracker._http, "get",
            return_value=self._mock_snapshot_response(payload),
        ):
            tracker.seed_from_snapshot()

        self.assertEqual(
            {k[0] for k in tracker._targets}, {"20m", "40m", "80m"}
        )

    def test_single_band_string_still_works(self):
        tracker = activity.ActivityTracker(base_url="http://x", bands={"20m"})
        payload = {
            "20m": [self._activity("20m", 14200000)],
            "40m": [self._activity("40m", 7200000)],
        }
        with mock.patch.object(
            tracker._http, "get",
            return_value=self._mock_snapshot_response(payload),
        ):
            tracker.seed_from_snapshot()

        self.assertEqual({k[0] for k in tracker._targets}, {"20m"})

    def test_multi_band_list_filters_correctly(self):
        tracker = activity.ActivityTracker(
            base_url="http://x", bands={"20m", "80m"}
        )
        payload = {
            "20m": [self._activity("20m", 14200000)],
            "40m": [self._activity("40m", 7200000)],
            "80m": [self._activity("80m", 3700000)],
            "15m": [self._activity("15m", 21200000)],
        }
        with mock.patch.object(
            tracker._http, "get",
            return_value=self._mock_snapshot_response(payload),
        ):
            tracker.seed_from_snapshot()

        self.assertEqual({k[0] for k in tracker._targets}, {"20m", "80m"})

    def test_excluded_bands_still_dropped_even_when_listed(self):
        # 2200m/630m/30m are never valid targets regardless of --band.
        tracker = activity.ActivityTracker(
            base_url="http://x", bands={"20m", "30m"}
        )
        payload = {
            "20m": [self._activity("20m", 14200000)],
            "30m": [self._activity("30m", 10120000)],
        }
        with mock.patch.object(
            tracker._http, "get",
            return_value=self._mock_snapshot_response(payload),
        ):
            tracker.seed_from_snapshot()

        self.assertEqual({k[0] for k in tracker._targets}, {"20m"})

    def test_cli_parses_comma_separated_list(self):
        args = scanner.parse_args(["--host", "x", "--band", "20m,40m,80m"])
        self.assertEqual(args.band, {"20m", "40m", "80m"})

    def test_cli_single_band(self):
        args = scanner.parse_args(["--host", "x", "--band", "20m"])
        self.assertEqual(args.band, {"20m"})

    def test_cli_unset_is_none(self):
        args = scanner.parse_args(["--host", "x"])
        self.assertIsNone(args.band)

    def test_cli_rejects_empty_band(self):
        with self.assertRaises(SystemExit):
            scanner.parse_args(["--host", "x", "--band", " , ,"])


class TestSegmentJoinDoesNotDoubleCount(unittest.TestCase):
    """
    The joined pass re-reads the raw segment's text, so a callsign that
    fits inside one segment was extracted twice: announced twice, written
    twice, and credited two corroboration hits for a single hearing —
    which silently defeated --spot-min-hits, spotting on the first
    hearing however high it was set.
    """

    def _scanner(self, **overrides):
        argv = ["--host", "x"] + [a for k, v in overrides.items()
                                  for a in (f"--{k.replace('_','-')}", str(v))]
        args = parse_args(argv)
        shared = SharedState(args, "http://x")
        sc = CallsignScanner(args, shared=shared)
        self.announced, self.spotted = [], []
        outer = self
        class V:
            def validate(self, c):
                return LookupResult(c, valid=True, checked=True, name="N")
        class S:
            def submit(self, f, c, cm):
                outer.spotted.append(c); return True
        sc.validator = V()
        sc._announce = lambda d, r: outer.announced.append(d.normalised)
        sc._write = lambda d: None
        shared.spotter = S()
        sc.web = None
        return sc, shared

    def _feed(self, sc, history, raw):
        t = Target(band="20m", dial_freq=14270000, mode="usb", snr=20, confidence=0.8)
        class A:
            certain, straddled, overlap_fraction = True, False, 1.0
        sc._segment_history = [Segment(text=history, start=0.0, end=2.0,
                                       completed=True, received_at=0)]
        seg = Segment(text=raw, start=2.5, end=5.0, completed=True, received_at=1)
        seen = set()
        sc._process(seg, t, A(), seen)
        joined = sc._joined_history_text(seg)
        if joined:
            sc._process(Segment(text=joined, start=0.0, end=5.0, completed=True,
                                received_at=1), t, A(), seen)

    def test_one_hearing_is_one_hit(self):
        sc, shared = self._scanner(spot_min_hits=2)
        self._feed(sc, "good morning to you",
                   "this is mike zero alpha bravo charlie")
        self.assertEqual(self.announced, ["M0ABC"])
        self.assertEqual(shared.spot_throttle.hits("M0ABC", 14270000), 1)
        self.assertEqual(self.spotted, [], "spotted on a single hearing")

    def test_split_across_the_vad_break_still_recovered(self):
        # The whole reason the join exists — this callsign appears in
        # neither segment alone.
        sc, shared = self._scanner()
        self._feed(sc, "this is golf mike 6", "zulu alpha kilo")
        self.assertIn("GM6ZAK", self.announced)


if __name__ == "__main__":
    unittest.main(verbosity=2)
