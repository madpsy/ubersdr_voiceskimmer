#!/usr/bin/env python3
"""
Tests for the DX cluster spotter: the (callsign, frequency) re-spot cooldown
in SpotThrottle, and DXClusterSpotter's supervised, self-reconnecting
connection.
"""

import argparse
import threading
import time
import unittest

import dxcluster
from scanner import percent
from dxcluster import DXClusterSpotter, SpotThrottle, SupersessionTracker


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestSpotThrottle(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self._real_time = dxcluster.time.time
        dxcluster.time.time = self.clock
        self.addCleanup(setattr, dxcluster.time, "time", self._real_time)

    def test_first_sighting_should_spot(self):
        t = SpotThrottle(cooldown=900)
        self.assertTrue(t.should_spot("MM3NDH", 14260000))

    def test_immediate_repeat_is_throttled(self):
        t = SpotThrottle(cooldown=900)
        t.record("MM3NDH", 14260000)
        self.assertFalse(t.should_spot("MM3NDH", 14260000))

    def test_respots_after_cooldown_elapses(self):
        t = SpotThrottle(cooldown=900)
        t.record("MM3NDH", 14260000)
        self.clock.advance(901)
        self.assertTrue(t.should_spot("MM3NDH", 14260000))

    def test_still_throttled_just_before_cooldown(self):
        t = SpotThrottle(cooldown=900)
        t.record("MM3NDH", 14260000)
        self.clock.advance(899)
        self.assertFalse(t.should_spot("MM3NDH", 14260000))

    def test_frequency_tolerance_collapses_nearby_frequencies(self):
        # Detector estimate wobbles a little between hearings of the same
        # station; within tolerance must still count as the same target.
        t = SpotThrottle(cooldown=900, freq_tolerance_hz=100)
        t.record("MM3NDH", 14260000)
        self.assertFalse(t.should_spot("MM3NDH", 14260050))
        self.assertFalse(t.should_spot("MM3NDH", 14259960))

    def test_frequency_outside_tolerance_is_a_different_target(self):
        t = SpotThrottle(cooldown=900, freq_tolerance_hz=100)
        t.record("MM3NDH", 14260000)
        self.assertTrue(t.should_spot("MM3NDH", 14261000))

    def test_different_callsign_same_frequency_not_throttled(self):
        t = SpotThrottle(cooldown=900)
        t.record("MM3NDH", 14260000)
        self.assertTrue(t.should_spot("G0ABC", 14260000))

    def test_failed_submission_does_not_block_retry(self):
        # record() must only be called after a successful submit — verifying
        # the contract: should_spot alone never marks anything as spotted.
        t = SpotThrottle(cooldown=900)
        t.should_spot("MM3NDH", 14260000)
        t.should_spot("MM3NDH", 14260000)
        self.assertTrue(t.should_spot("MM3NDH", 14260000))

    def test_max_entries_evicts_oldest(self):
        t = SpotThrottle(cooldown=900, max_entries=3)
        for i in range(3):
            t.record(f"CALL{i}", 14260000)
            self.clock.advance(1)
        self.assertEqual(len(t._last_spot), 3)

        t.record("CALL3", 14260000)  # should evict CALL0
        self.assertEqual(len(t._last_spot), 3)
        self.assertTrue(t.should_spot("CALL0", 14260000))  # evicted -> due again
        self.assertFalse(t.should_spot("CALL3", 14260000))  # still resident

    def test_re_recording_moves_entry_to_end_lru(self):
        t = SpotThrottle(cooldown=900, max_entries=2)
        t.record("A", 14260000)
        self.clock.advance(1)
        t.record("B", 14260000)
        self.clock.advance(1)
        # Touch A again — should now be the most-recently-used, not B.
        t.record("A", 14260000)
        self.clock.advance(1)
        t.record("C", 14260000)  # forces an eviction — should evict B, not A

        self.assertTrue(t.should_spot("B", 14260000))  # evicted
        self.assertFalse(t.should_spot("A", 14260000))  # survived
        self.assertFalse(t.should_spot("C", 14260000))




class TestSpotMinHits(unittest.TestCase):
    """
    Requiring the same callsign on the same frequency more than once before
    spotting. The extractor can assemble a plausible-but-wrong callsign from
    one garbled pass, but is unlikely to invent the same wrong one twice on
    the same frequency, so repeat hearings are real corroboration.
    """

    def test_hits_accumulate_per_callsign_and_frequency(self):
        t = SpotThrottle(freq_tolerance_hz=100)
        self.assertEqual(t.record_hit("M0ABC", 14270000), 1)
        self.assertEqual(t.record_hit("M0ABC", 14270000), 2)
        # A different frequency, and a different callsign, each start over.
        self.assertEqual(t.record_hit("M0ABC", 14280000), 1)
        self.assertEqual(t.record_hit("G4XYZ", 14270000), 1)

    def test_drifting_frequency_does_not_restart_the_tally(self):
        # The detector's estimate wobbles between hearings; the same
        # bucketing as the cooldown keeps them counting as one station.
        t = SpotThrottle(freq_tolerance_hz=100)
        t.record_hit("M0ABC", 14270000)
        t.record_hit("M0ABC", 14270050)
        self.assertEqual(t.record_hit("M0ABC", 14269960), 3)

    def test_hits_query_does_not_count(self):
        t = SpotThrottle(freq_tolerance_hz=100)
        t.record_hit("M0ABC", 14270000)
        self.assertEqual(t.hits("M0ABC", 14270000), 1)
        self.assertEqual(t.hits("M0ABC", 14270000), 1)
        self.assertEqual(t.hits("NEVER", 14270000), 0)

    def test_bounded_like_the_cooldown(self):
        t = SpotThrottle(max_entries=5, freq_tolerance_hz=100)
        for i in range(20):
            t.record_hit(f"CALL{i}", 14000000 + i * 10000)
        self.assertLessEqual(len(t._hits), 5)


class TestTransferHits(unittest.TestCase):
    """
    Moving corroboration from a superseded callsign onto the longer one that
    replaced it — see SupersessionTracker. Those hearings were of the same
    station, so the evidence should not be thrown away.
    """

    def test_hits_move_across(self):
        t = SpotThrottle(freq_tolerance_hz=500)
        t.record_hit("ON2GB", 14226000)
        t.record_hit("ON2GBR", 14226000)
        self.assertEqual(t.transfer_hits("ON2GB", "ON2GBR", 14226000), 1)
        self.assertEqual(t.hits("ON2GBR", 14226000), 2)

    def test_source_is_emptied(self):
        t = SpotThrottle(freq_tolerance_hz=500)
        t.record_hit("ON2GB", 14226000)
        t.transfer_hits("ON2GB", "ON2GBR", 14226000)
        self.assertEqual(t.hits("ON2GB", 14226000), 0)

    def test_transfer_can_carry_the_target_over_the_threshold(self):
        # The whole point: a station heard twice, once with a character
        # missing, should reach --spot-min-hits 2 rather than stall at one
        # hit under each spelling.
        t = SpotThrottle(freq_tolerance_hz=500)
        t.record_hit("ON2GB", 14226000)
        t.record_hit("ON2GBR", 14226000)
        t.transfer_hits("ON2GB", "ON2GBR", 14226000)
        self.assertGreaterEqual(t.hits("ON2GBR", 14226000), 2)

    def test_nothing_to_transfer_is_a_no_op(self):
        t = SpotThrottle(freq_tolerance_hz=500)
        t.record_hit("ON2GBR", 14226000)
        self.assertEqual(t.transfer_hits("ON2GB", "ON2GBR", 14226000), 0)
        self.assertEqual(t.hits("ON2GBR", 14226000), 1)

    def test_frequency_scoped(self):
        # Hits are per (callsign, frequency); a transfer must not reach
        # across to a hearing on another band.
        t = SpotThrottle(freq_tolerance_hz=500)
        t.record_hit("ON2GB", 7155000)
        self.assertEqual(t.transfer_hits("ON2GB", "ON2GBR", 14226000), 0)
        self.assertEqual(t.hits("ON2GB", 7155000), 1)


class TestRevisitSpotHistory(unittest.TestCase):
    """
    Backs --revisit-dwell-percent: how long since ANY callsign was spotted on
    a frequency, so a frequency already producing spots gets a shorter dwell.
    """

    def test_none_before_any_spot(self):
        st = SpotThrottle(freq_tolerance_hz=500)
        self.assertIsNone(st.seconds_since_spot_on(14226000))

    def test_records_on_spot(self):
        st = SpotThrottle(freq_tolerance_hz=500)
        st.record("G0VIM", 14226000)
        since = st.seconds_since_spot_on(14226000)
        self.assertIsNotNone(since)
        self.assertLess(since, 1.0)

    def test_keyed_on_frequency_not_callsign(self):
        # The reduction is about the frequency having produced a spot, not
        # about which station it was — a different call asking about the same
        # frequency must see it.
        st = SpotThrottle(freq_tolerance_hz=500)
        st.record("G0VIM", 14226000)
        self.assertIsNotNone(st.seconds_since_spot_on(14226000))

    def test_shares_the_frequency_bucketing(self):
        # Dial drift within tolerance is the same frequency, exactly as for
        # the cooldown and the hit count.
        st = SpotThrottle(freq_tolerance_hz=500)
        st.record("G0VIM", 14226000)
        self.assertIsNotNone(st.seconds_since_spot_on(14226200))
        self.assertIsNone(st.seconds_since_spot_on(14230000))

    def test_other_frequencies_unaffected(self):
        st = SpotThrottle(freq_tolerance_hz=500)
        st.record("G0VIM", 14226000)
        self.assertIsNone(st.seconds_since_spot_on(7155000))

    def test_bounded(self):
        st = SpotThrottle(freq_tolerance_hz=500, max_entries=50)
        for i in range(500):
            st.record("C%d" % i, 7000000 + i * 5000)
        self.assertLessEqual(len(st._last_freq_spot), 50)


class TestSupersessionTracker(unittest.TestCase):
    """
    Retiring a shorter callsign once a longer one containing it is confirmed
    on the same frequency — the ON2GB / ON2GBR case. The string test lives in
    phonetics.supersedes; what is tested here is when the tracker is willing
    to ACT on it, which is where the risk of silencing a real station sits.
    """

    BUCKET = 14226000

    def setUp(self):
        self.clock = FakeClock()
        self._real_time = dxcluster.time.time
        dxcluster.time.time = self.clock
        self.addCleanup(setattr, dxcluster.time, "time", self._real_time)

    def test_longer_callsign_retires_the_shorter(self):
        t = SupersessionTracker(window=900, min_hits=2)
        self.assertEqual(t.record("ON2GB", self.BUCKET).retired, [])
        self.assertEqual(t.record("ON2GBR", self.BUCKET).retired, ["ON2GB"])
        self.assertEqual(t.check("ON2GB", self.BUCKET), "ON2GBR")

    def test_the_longer_callsign_stays_live(self):
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)
        self.assertIsNone(t.check("ON2GBR", self.BUCKET))

    def test_unrelated_callsigns_are_untouched(self):
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("G0VIM", self.BUCKET)
        self.assertEqual(t.record("ON2GBR", self.BUCKET).retired, [])
        self.assertIsNone(t.check("G0VIM", self.BUCKET))

    def test_a_different_frequency_is_a_different_station(self):
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GB", self.BUCKET)
        self.assertEqual(t.record("ON2GBR", 7155000).retired, [])
        self.assertIsNone(t.check("ON2GB", self.BUCKET))

    def test_outside_the_window_is_not_evidence(self):
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GB", self.BUCKET)
        self.clock.advance(901)
        self.assertEqual(t.record("ON2GBR", self.BUCKET).retired, [])
        self.assertIsNone(t.check("ON2GB", self.BUCKET))

    def test_a_corroborated_callsign_is_not_retired(self):
        # Once the shorter callsign has cleared min_hits it has stopped
        # looking like a one-pass garble, and retiring it would be silencing
        # a station that keeps proving itself.
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GB", self.BUCKET)
        self.assertEqual(t.record("ON2GBR", self.BUCKET).retired, [])
        self.assertIsNone(t.check("ON2GB", self.BUCKET))

    def test_a_weaker_longer_callsign_cannot_retire_a_stronger_one(self):
        # The inverse error is real: a following word can be absorbed as a
        # trailing letter ("ON2GB, radio check" -> ON2GBR). One decode must
        # not outweigh several.
        t = SupersessionTracker(window=900, min_hits=5)
        for _ in range(3):
            t.record("ON2GB", self.BUCKET)
        self.assertEqual(t.record("ON2GBR", self.BUCKET).retired, [])
        self.assertIsNone(t.check("ON2GB", self.BUCKET))

    def test_an_equally_heard_longer_callsign_may_retire(self):
        t = SupersessionTracker(window=900, min_hits=5)
        for _ in range(3):
            t.record("ON2GB", self.BUCKET)
        for _ in range(2):
            t.record("ON2GBR", self.BUCKET)
        self.assertEqual(t.record("ON2GBR", self.BUCKET).retired, ["ON2GB"])

    def test_retirement_is_reported_once(self):
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GB", self.BUCKET)
        self.assertEqual(t.record("ON2GBR", self.BUCKET).retired, ["ON2GB"])
        self.assertEqual(t.record("ON2GBR", self.BUCKET).retired, [])

    def test_dormancy_expires_with_the_window(self):
        # A callsign still being heard 15 minutes later is behaving like a
        # real station; the evidence for retiring it has gone stale.
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)
        self.assertEqual(t.check("ON2GB", self.BUCKET), "ON2GBR")
        self.clock.advance(901)
        self.assertIsNone(t.check("ON2GB", self.BUCKET))

    def test_hearings_are_counted_independently_of_spotthrottle(self):
        # SpotThrottle.record_hit is only reached with --spot enabled; this
        # has to behave identically with spotting off.
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GBR", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)
        self.assertEqual(t.hearings("ON2GBR", self.BUCKET), 2)
        self.assertEqual(t.hearings("ON2GBR", 7155000), 0)

    def test_a_shorter_callsign_arriving_second_is_retired_immediately(self):
        # The longer callsign was already on record. Without this the shorter
        # one would stay live purely because it turned up second, and could go
        # on to be spotted.
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GBR", self.BUCKET)
        result = t.record("ON2GB", self.BUCKET)
        self.assertEqual(result.dormant_under, "ON2GBR")
        self.assertEqual(t.check("ON2GB", self.BUCKET), "ON2GBR")

    def test_a_shorter_callsign_never_retires_a_longer_one(self):
        # A missing phonetic word is far likelier than an invented one, so
        # length decides, not arrival order. The longer callsign stays live
        # whichever way round they are heard.
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GBR", self.BUCKET)
        t.record("ON2GB", self.BUCKET)
        self.assertIsNone(t.check("ON2GBR", self.BUCKET))

    def test_self_retirement_carries_hearings_to_the_longer_callsign(self):
        t = SupersessionTracker(window=900, min_hits=3)
        t.record("ON2GBR", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)
        t.record("ON2GB", self.BUCKET)          # retired on arrival
        self.assertEqual(t.hearings("ON2GBR", self.BUCKET), 3)
        self.assertEqual(t.hearings("ON2GB", self.BUCKET), 0)

    def test_self_retirement_respects_the_corroboration_guards(self):
        # The shorter callsign has already proved itself; a single hearing of
        # a longer one must not retire it on arrival either.
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)
        result = t.record("ON2GB", self.BUCKET)
        self.assertIsNone(result.dormant_under)

    def test_a_weaker_longer_callsign_cannot_retire_on_arrival_either(self):
        # Mirror of the inverse-error guard, on the self-retirement path: a
        # single ON2GBR cannot retire an ON2GB that keeps being heard. Needs
        # min_hits high enough that ON2GB is not simply exempted by having
        # corroborated, so it is the weight comparison under test.
        t = SupersessionTracker(window=900, min_hits=10)
        for _ in range(3):
            t.record("ON2GB", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)            # too weak to retire it
        result = t.record("ON2GB", self.BUCKET)
        self.assertIsNone(result.dormant_under)
        self.assertIsNone(t.check("ON2GB", self.BUCKET))

    def test_outcome_does_not_depend_on_arrival_order(self):
        forwards = SupersessionTracker(window=900, min_hits=2)
        forwards.record("ON2GB", self.BUCKET)
        forwards.record("ON2GBR", self.BUCKET)

        backwards = SupersessionTracker(window=900, min_hits=2)
        backwards.record("ON2GBR", self.BUCKET)
        backwards.record("ON2GB", self.BUCKET)

        for t in (forwards, backwards):
            self.assertEqual(t.check("ON2GB", self.BUCKET), "ON2GBR")
            self.assertIsNone(t.check("ON2GBR", self.BUCKET))
            self.assertEqual(t.hearings("ON2GBR", self.BUCKET), 2)

    def test_retired_hearings_are_folded_into_the_longer_callsign(self):
        # Those hearings were of the same station, so the corroboration is
        # carried forward rather than discarded.
        t = SupersessionTracker(window=900, min_hits=3)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)          # retires ON2GB here
        self.assertEqual(t.hearings("ON2GBR", self.BUCKET), 4)

    def test_retired_callsign_loses_its_tally(self):
        # It is dormant; if that expires it should have to earn its way back
        # from nothing rather than resume where it left off.
        t = SupersessionTracker(window=900, min_hits=3)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)
        self.assertEqual(t.hearings("ON2GB", self.BUCKET), 0)

    def test_dormant_listing(self):
        t = SupersessionTracker(window=900, min_hits=2)
        t.record("ON2GB", self.BUCKET)
        t.record("ON2GBR", self.BUCKET)
        self.assertEqual(t.dormant(), {("ON2GB", self.BUCKET): "ON2GBR"})

    def test_bounded(self):
        t = SupersessionTracker(window=900, min_hits=2, max_entries=50)
        for i in range(500):
            t.record("G%dABC" % (i % 10), 7000000 + i * 5000)
        self.assertLessEqual(len(t._heard), 50)


def wait_until(predicate, timeout: float = 3.0) -> bool:
    """Poll until predicate() is true. Everything under test here happens on a
    background thread, so tests wait on the observable state rather than
    sleeping a guessed interval."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakeSocket:
    """Stand-in for websocket.WebSocketApp, driven by FakeNode's script."""

    # What the node says back on login, per scripted behaviour.
    REPLIES = {
        "auth": ["Spot submission enabled for MM3NDH"],
        "silent": [],
        "bad-call": ["MM3NDH is an invalid callsign"],
        "bad-pass": ["Sorry, invalid password"],
    }

    def __init__(self, node, url, header=None, on_open=None, on_message=None,
                 on_error=None, on_close=None):
        self.node = node
        self.url = url
        self.on_open, self.on_message = on_open, on_message
        self.on_error, self.on_close = on_error, on_close
        self.sent = []
        self.closed = threading.Event()
        self.send_error = None

    def send(self, data):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)

    def close(self):
        self.closed.set()

    def run_forever(self, **_kwargs):
        behaviour = self.node.next_behaviour()
        if behaviour == "refuse":       # node down: never opens
            return
        if behaviour == "stall":        # accepts the socket, never handshakes
            self.closed.wait(5.0)       # only the watchdog gets us out of here
            return
        self.on_open(self)
        for reply in self.REPLIES.get(behaviour, []):
            self.on_message(self, reply)
        if behaviour == "drop":         # opens, then vanishes mid-handshake
            self.closed.set()
        self.closed.wait(5.0)           # until either side closes
        self.on_close(self, 1000, "")


class FakeNode:
    """A scripted cluster node. Each connection attempt consumes the next
    behaviour in the list; the last one repeats for every attempt after."""

    def __init__(self, *behaviours):
        self.behaviours = list(behaviours)
        self.sockets = []
        self.attempts = 0
        self._lock = threading.Lock()

    def factory(self, url, header=None, **callbacks):
        sock = FakeSocket(self, url, header=header, **callbacks)
        with self._lock:
            self.sockets.append(sock)
        return sock

    def next_behaviour(self) -> str:
        with self._lock:
            behaviour = self.behaviours[min(self.attempts, len(self.behaviours) - 1)]
            self.attempts += 1
            return behaviour

    def live(self):
        """The most recent socket, once one exists."""
        with self._lock:
            return self.sockets[-1] if self.sockets else None


class SpotterTestCase(unittest.TestCase):
    """Wires DXClusterSpotter to a FakeNode with timings squashed so a
    reconnect cycle takes milliseconds rather than the production minute."""

    def spotter(self, *behaviours, timeout=0.2, backoff=0.01) -> DXClusterSpotter:
        node = FakeNode(*behaviours)
        self.node = node
        self._real_ws_app = dxcluster.websocket.WebSocketApp
        dxcluster.websocket.WebSocketApp = node.factory
        self.addCleanup(
            setattr, dxcluster.websocket, "WebSocketApp", self._real_ws_app
        )
        spotter = DXClusterSpotter(
            "https://sdr.example/", "MM3NDH", "secret",
            timeout=timeout, backoff_start=backoff, backoff_max=backoff,
        )
        self.addCleanup(spotter.stop)
        return spotter


class TestSpotterConnection(SpotterTestCase):
    def test_authenticates_and_sends_login(self):
        s = self.spotter("auth")
        self.assertTrue(s.start())
        self.assertTrue(s.is_connected())
        self.assertEqual(self.node.live().sent, ["MM3NDH secret\n"])

    def test_ws_url_derived_from_base(self):
        s = self.spotter("auth")
        self.assertEqual(
            s.ws_url, "wss://sdr.example/addon/dxcluster/api/terminal"
        )

    def test_submit_formats_the_dx_command_in_khz(self):
        s = self.spotter("auth")
        s.start()
        self.assertTrue(s.submit(14260000, "G0ABC", "[Voice] Bob"))
        self.assertEqual(self.node.live().sent[-1], "DX 14260.0 G0ABC [Voice] Bob\n")

    def test_comment_is_truncated_to_the_server_cap(self):
        s = self.spotter("auth")
        s.start()
        s.submit(14260000, "G0ABC", "x" * 200)
        comment = self.node.live().sent[-1].split("G0ABC ", 1)[1].rstrip("\n")
        self.assertEqual(len(comment), dxcluster.MAX_COMMENT_LEN)


class TestSpotterReconnects(SpotterTestCase):
    """The point of the supervisor: a scan runs for hours, and the node will
    restart, drop idle sessions, or be unreachable at startup."""

    def test_retries_until_the_node_comes_back(self):
        # Down for the first two attempts, then up. The caller does not wait
        # around for it — the supervisor gets there on its own.
        s = self.spotter("refuse", "refuse", "auth")
        self.assertFalse(s.start(wait=False))
        self.assertFalse(s.failed_permanently())
        self.assertTrue(wait_until(s.is_connected))
        self.assertGreaterEqual(self.node.attempts, 3)

    def test_reconnects_after_the_node_drops_the_session(self):
        s = self.spotter("auth")
        self.assertTrue(s.start())
        first = self.node.live()

        first.close()                                   # node hangs up
        self.assertTrue(wait_until(lambda: self.node.live() is not first))
        self.assertTrue(wait_until(s.is_connected))
        # Re-logged in on the new socket, not just reconnected.
        self.assertEqual(self.node.live().sent, ["MM3NDH secret\n"])

    def test_a_working_session_is_counted_as_one(self):
        # The backoff resets after a session that actually worked, so the
        # supervisor has to distinguish "was connected, got dropped" from
        # "never got in" — and it cannot ask _can_spot, which the close has
        # already cleared by the time the session ends.
        s = self.spotter("auth")
        s.start()
        self.node.live().close()
        self.assertTrue(wait_until(lambda: s.sessions >= 1))

    def test_failed_attempts_are_not_counted_as_sessions(self):
        s = self.spotter("refuse")
        s.start(wait=False)
        self.assertTrue(wait_until(lambda: self.node.attempts >= 3))
        self.assertEqual(s.sessions, 0)

    def test_spots_are_refused_while_disconnected(self):
        # The old bug: _can_spot stayed set after a close, so every spot was
        # written into a dead socket and silently lost.
        s = self.spotter("auth", "refuse")
        s.start()
        self.node.live().close()
        self.assertTrue(wait_until(lambda: not s.is_connected()))
        self.assertFalse(s.submit(14260000, "G0ABC", "test"))

    def test_spotting_resumes_after_a_reconnect(self):
        s = self.spotter("auth")
        s.start()
        self.node.live().close()
        self.assertTrue(wait_until(s.is_connected))
        self.assertTrue(s.submit(14260000, "G0ABC", "after reconnect"))

    def test_failed_send_drops_the_socket_and_reconnects(self):
        s = self.spotter("auth")
        s.start()
        broken = self.node.live()
        broken.send_error = OSError("broken pipe")

        self.assertFalse(s.submit(14260000, "G0ABC", "test"))
        self.assertTrue(wait_until(lambda: self.node.live() is not broken))
        self.assertTrue(wait_until(s.is_connected))

    def test_login_that_is_never_confirmed_is_retried(self):
        # A node that accepts the socket and then says nothing — what an
        # instance with spot submission disabled looks like. The session must
        # be torn down rather than sitting there looking healthy.
        s = self.spotter("silent", "auth")
        self.assertFalse(s.start())
        self.assertTrue(wait_until(s.is_connected))

    def test_stalled_handshake_is_torn_down_and_retried(self):
        # websocket-client leaves the socket on the global default timeout
        # (none), so a node that accepts the connection and then never
        # completes the handshake would block the supervisor forever.
        s = self.spotter("stall", "auth")
        self.assertFalse(s.start())
        self.assertTrue(wait_until(s.is_connected))

    def test_immediate_drop_is_retried(self):
        s = self.spotter("drop", "auth")
        s.start()
        self.assertTrue(wait_until(s.is_connected))


class TestSpotterTerminalFailures(SpotterTestCase):
    """Bad credentials will never start working, so retrying is pointless —
    the supervisor must stop rather than reconnect forever."""

    def test_invalid_callsign_stops_retrying(self):
        s = self.spotter("bad-call")
        self.assertFalse(s.start())
        self.assertTrue(s.failed_permanently())
        self.assertTrue(wait_until(lambda: not s._thread.is_alive()))
        self.assertEqual(self.node.attempts, 1)

    def test_invalid_password_stops_retrying(self):
        s = self.spotter("bad-pass")
        self.assertFalse(s.start())
        self.assertTrue(s.failed_permanently())
        self.assertTrue(wait_until(lambda: not s._thread.is_alive()))
        self.assertEqual(self.node.attempts, 1)

    def test_unreachable_node_is_not_a_permanent_failure(self):
        # The distinction scanner.py acts on: keep the spotter for one,
        # discard it for the other.
        s = self.spotter("refuse")
        self.assertFalse(s.start())
        self.assertFalse(s.failed_permanently())

    def test_submit_is_safe_before_and_after_stop(self):
        s = self.spotter("auth")
        s.start()
        s.stop()
        self.assertFalse(s.submit(14260000, "G0ABC", "test"))
        s.stop()  # idempotent

    def test_stop_ends_the_supervisor(self):
        s = self.spotter("refuse")
        s.start()
        s.stop()
        self.assertFalse(s._thread.is_alive())


class TestRevisitPercentFlag(unittest.TestCase):
    def test_accepts_a_fraction_and_one(self):
        self.assertEqual(percent("0.5"), 0.5)
        self.assertEqual(percent("1.0"), 1.0)
        self.assertEqual(percent("1"), 1.0)

    def test_rejects_zero_and_above_one(self):
        # 0 would mean never listening to a revisit at all, and >1 would make
        # a "reduced" dwell longer than a normal one — both far likelier to be
        # typos than intentions.
        for bad in ["0", "0.0", "1.01", "2", "-0.5", "abc", ""]:
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                percent(bad)


if __name__ == "__main__":
    unittest.main()
