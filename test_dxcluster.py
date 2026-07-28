#!/usr/bin/env python3
"""Tests for SpotThrottle — the (callsign, frequency) re-spot cooldown."""

import unittest

import dxcluster
from dxcluster import SpotThrottle


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
        self.assertEqual(len(t), 3)

        t.record("CALL3", 14260000)  # should evict CALL0
        self.assertEqual(len(t), 3)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
