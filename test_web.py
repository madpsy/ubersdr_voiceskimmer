#!/usr/bin/env python3
"""
Tests for the dashboard's HTTP layer.

Focused on the parts that are not just plumbing: the rate limiter and the
client-address derivation behind UberSDR's addon proxy. Both are the kind of
thing that appears to work when it is silently wrong — a limiter that keys
every request to the same address rate-limits the whole world together, and
one that keys on a spoofable header does not limit anyone at all.
"""

import threading
import time
import unittest

from lookup import CallsignValidator
from web import RateLimiter, WebUI, client_ip


class FakeRequest:
    def __init__(self, headers=None, remote_addr="10.0.0.1"):
        self.headers = headers or {}
        self.remote_addr = remote_addr


class TestClientIP(unittest.TestCase):
    """
    Mirrors realClientIPDebug in ubersdr_dxcluster/terminal.go — X-Real-IP,
    then the first X-Forwarded-For entry, then the socket peer.
    """

    def test_prefers_x_real_ip(self):
        req = FakeRequest(
            {"X-Real-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.9"},
            remote_addr="172.18.0.5",
        )
        self.assertEqual(client_ip(req), "203.0.113.7")

    def test_falls_back_to_forwarded_for(self):
        req = FakeRequest({"X-Forwarded-For": "203.0.113.7"}, remote_addr="172.18.0.5")
        self.assertEqual(client_ip(req), "203.0.113.7")

    def test_takes_the_first_forwarded_for_entry(self):
        # The addon proxy sets a single address, but a chain is the general
        # shape of this header and the client is the leftmost entry.
        req = FakeRequest({"X-Forwarded-For": "203.0.113.7, 198.51.100.9, 172.18.0.5"})
        self.assertEqual(client_ip(req), "203.0.113.7")

    def test_falls_back_to_remote_addr(self):
        self.assertEqual(client_ip(FakeRequest(remote_addr="192.0.2.4")), "192.0.2.4")

    def test_whitespace_and_empty_headers(self):
        self.assertEqual(
            client_ip(FakeRequest({"X-Real-IP": "  203.0.113.7  "})), "203.0.113.7"
        )
        # An empty header must not shadow the fallbacks.
        self.assertEqual(
            client_ip(FakeRequest({"X-Real-IP": ""}, remote_addr="192.0.2.4")),
            "192.0.2.4",
        )

    def test_never_returns_none(self):
        # Flask leaves remote_addr None for some unix-socket setups; a None
        # key would collapse every such caller into one bucket.
        self.assertEqual(client_ip(FakeRequest(remote_addr=None)), "unknown")


class TestRateLimiter(unittest.TestCase):
    def test_first_request_allowed_then_blocked(self):
        rl = RateLimiter(interval=1.0)
        allowed, _ = rl.allow("1.2.3.4")
        self.assertTrue(allowed)
        allowed, retry = rl.allow("1.2.3.4")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        self.assertLessEqual(retry, 1.0)

    def test_addresses_are_independent(self):
        rl = RateLimiter(interval=1.0)
        self.assertTrue(rl.allow("1.2.3.4")[0])
        self.assertTrue(rl.allow("5.6.7.8")[0])
        self.assertFalse(rl.allow("1.2.3.4")[0])

    def test_allowed_again_after_the_interval(self):
        rl = RateLimiter(interval=0.05)
        self.assertTrue(rl.allow("1.2.3.4")[0])
        time.sleep(0.06)
        self.assertTrue(rl.allow("1.2.3.4")[0])

    def test_rejections_do_not_extend_the_lockout(self):
        # Only a successful request advances the clock. If a rejected attempt
        # reset it too, a client polling faster than the interval would be
        # locked out for as long as it kept trying — so the property to check
        # is that hammering still yields roughly one success per interval,
        # not that any particular call fails.
        rl = RateLimiter(interval=0.05)
        allowed = 0
        deadline = time.monotonic() + 0.30
        while time.monotonic() < deadline:
            if rl.allow("1.2.3.4")[0]:
                allowed += 1
            time.sleep(0.005)
        # 0.30s at one per 0.05s is ~6; loose bounds keep this off a timing
        # knife-edge on a loaded machine while still failing outright if the
        # limiter locks up (0-1) or stops limiting (~60).
        self.assertGreaterEqual(allowed, 3)
        self.assertLessEqual(allowed, 9)

    def test_map_stays_bounded(self):
        rl = RateLimiter(interval=0.01, max_entries=50)
        for i in range(500):
            rl.allow(f"10.0.0.{i}")
        self.assertLessEqual(len(rl._last), 50 + 1)

    def test_bounded_even_when_nothing_is_stale(self):
        # A flood from many addresses at once leaves nothing to sweep; the
        # oldest half goes anyway rather than growing without limit.
        rl = RateLimiter(interval=3600, max_entries=50)
        for i in range(500):
            rl.allow(f"10.0.0.{i}")
        self.assertLessEqual(len(rl._last), 50 + 1)

    def test_concurrent_callers_get_exactly_one_through(self):
        rl = RateLimiter(interval=10.0)
        results = []
        lock = threading.Lock()

        def hit():
            allowed, _ = rl.allow("1.2.3.4")
            with lock:
                results.append(allowed)

        threads = [threading.Thread(target=hit) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 1)


class TestExplainEndpoint(unittest.TestCase):
    def setUp(self):
        self.ui = WebUI(
            workers=1,
            gates={"min_extract_confidence": 0.4, "min_callsign_length": 4,
                   "spot_min_hits": 2},
        )
        self.client = self.ui.app.test_client()

    def post(self, text, ip="203.0.113.1"):
        return self.client.post(
            "/api/explain", json={"text": text}, headers={"X-Real-IP": ip}
        )

    def test_analyses_a_line(self):
        r = self.post("This is Mike Zero Alpha Bravo Charlie calling CQ")
        self.assertEqual(r.status_code, 200)
        self.assertIn("M0ABC", [c["normalised"] for c in r.get_json()["candidates"]])

    def test_second_request_is_rate_limited(self):
        self.assertEqual(self.post("hello").status_code, 200)
        r = self.post("hello")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers["Retry-After"], "1")
        self.assertIn("retry_after", r.get_json())

    def test_limit_is_per_address(self):
        self.assertEqual(self.post("hello", ip="203.0.113.1").status_code, 200)
        self.assertEqual(self.post("hello", ip="203.0.113.2").status_code, 200)
        self.assertEqual(self.post("hello", ip="203.0.113.1").status_code, 429)

    def test_rejects_non_string_and_oversized_text(self):
        self.assertEqual(
            self.client.post("/api/explain", json={"text": 123},
                             headers={"X-Real-IP": "203.0.113.5"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/explain", json={"text": "x" * 3000},
                             headers={"X-Real-IP": "203.0.113.6"}).status_code,
            413,
        )

    def test_other_endpoints_are_not_limited(self):
        # Only /api/explain does work on caller-supplied input; the dashboard
        # polls /api/state on load and must not be throttled with it.
        for _ in range(5):
            self.assertEqual(self.client.get("/api/state").status_code, 200)


class TestCountryCode(unittest.TestCase):
    """
    The dashboard's flags key on the ISO code from the lookup response's CTY
    block, never on the country NAME. DXCC entity names are not ISO names and
    the stations this hears most are the worst cases — England, Scotland and
    Wales are three DXCC entities and one ISO country (GB), and Japan's CTY
    country_code is JP while its amateur prefix is JA.
    """

    def test_extracts_the_iso_code(self):
        r = CallsignValidator._parse("G0VIM", {
            "callsign": "G0VIM", "fname": "Malcolm",
            "cty": {"country": "England", "country_code": "GB",
                    "continent": "EU", "latitude": 52.77, "longitude": -1.47},
        })
        self.assertEqual(r.country_code, "GB")
        self.assertEqual(r.country, "England")

    def test_normalises_case(self):
        r = CallsignValidator._parse("X", {"cty": {"country_code": "de"}})
        self.assertEqual(r.country_code, "DE")

    def test_absent_cty_block_is_not_an_error(self):
        # QRZ can answer without a CTY augmentation; the flag is simply
        # omitted rather than the lookup failing.
        r = CallsignValidator._parse("X", {"callsign": "X"})
        self.assertEqual(r.country_code, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
