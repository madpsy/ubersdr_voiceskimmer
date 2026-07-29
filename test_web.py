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
from web import QueryError, RateLimiter, WebUI, client_ip, parse_duration


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


class TestParseDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("5m"), 300)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertEqual(parse_duration("90"), 90)      # bare number is seconds

    def test_rejects_junk_rather_than_defaulting(self):
        # A typo silently becoming 0 would widen the window to everything --
        # the opposite of what the caller asked for.
        for bad in ["5min", "", "abc", "-5m", "5 5m", None]:
            with self.assertRaises(QueryError, msg=repr(bad)):
                parse_duration(bad)


class TestSpotQuery(unittest.TestCase):
    """
    /api/spots is a documented contract, so these pin the filters and the
    error behaviour rather than the internals behind them.
    """

    def setUp(self):
        self.ui = WebUI(workers=1)
        self.client = self.ui.app.test_client()
        self.now = time.time()
        rows = [
            # call, band, hz, country, cc, age, submitted, dx_agree, hits, snr
            ("G0VIM", "20m", 14226000, "England", "GB", 30, True, True, 4, 42.1),
            ("DL2BHM", "20m", 14188000, "Germany", "DE", 120, True, False, 3, 39.5),
            ("JA1ABC", "15m", 21255000, "Japan", "JP", 600, False, False, 1, 35.0),
            ("EA5XX", "40m", 7155000, "Spain", "ES", 3600, False, False, 2, 44.2),
        ]
        for call, band, hz, ctry, cc, age, sub, dxa, hits, snr in rows:
            for _ in range(hits):
                self.ui.push_confirmed({
                    "normalised": call, "band": band, "frequency": hz, "mode": "usb",
                    "name": "N", "country": ctry, "country_code": cc,
                    "timestamp": self.now - age, "snr": snr,
                    "agrees_with_dx_spot": dxa, "raw_text": f"this is {call}",
                    "extract_confidence": 0.75,
                }, False, hz)
            if sub:
                self.ui.push_spot(call, hz, "[Voice] N", hz, band, cc, ctry)

    def get(self, query=""):
        # A distinct address per call: the endpoint allows one request per
        # second per address and these tests fire many in a row.
        self._n = getattr(self, "_n", 0) + 1
        r = self.client.get(
            "/api/spots?" + query,
            headers={"X-Real-IP": f"198.51.100.{self._n % 250}"},
        )
        return r.status_code, r.get_json()

    def calls(self, query=""):
        status, body = self.get(query)
        self.assertEqual(status, 200, body)
        return [s["callsign"] for s in body["spots"]]

    def test_returns_everything_by_default(self):
        self.assertEqual(sorted(self.calls()), ["DL2BHM", "EA5XX", "G0VIM", "JA1ABC"])

    def test_submitted_filter(self):
        self.assertEqual(sorted(self.calls("submitted=true")), ["DL2BHM", "G0VIM"])
        self.assertEqual(sorted(self.calls("submitted=false")), ["EA5XX", "JA1ABC"])

    def test_relative_time_window(self):
        self.assertEqual(sorted(self.calls("last=5m")), ["DL2BHM", "G0VIM"])
        self.assertEqual(sorted(self.calls("last=1m")), ["G0VIM"])
        self.assertEqual(len(self.calls("last=2h")), 4)

    def test_filters_combine(self):
        self.assertEqual(self.calls("last=5m&submitted=true&band=20m"),
                         ["G0VIM", "DL2BHM"])

    def test_band_and_country_lists(self):
        self.assertEqual(sorted(self.calls("band=15m,40m")), ["EA5XX", "JA1ABC"])
        self.assertEqual(sorted(self.calls("country_code=gb,de")), ["DL2BHM", "G0VIM"])

    def test_numeric_filters(self):
        self.assertEqual(sorted(self.calls("min_hits=3")), ["DL2BHM", "G0VIM"])
        self.assertEqual(sorted(self.calls("min_snr=42")), ["EA5XX", "G0VIM"])
        self.assertEqual(
            sorted(self.calls("min_freq=14000000&max_freq=14350000")),
            ["DL2BHM", "G0VIM"],
        )

    def test_dx_agreement(self):
        self.assertEqual(self.calls("dx_agree=true"), ["G0VIM"])

    def test_free_text_searches_heard_text_too(self):
        self.assertEqual(self.calls("q=JA1ABC"), ["JA1ABC"])
        self.assertEqual(len(self.calls("q=this is")), 4)

    def test_sort_and_order(self):
        self.assertEqual(self.calls("sort=callsign&order=asc"),
                         ["DL2BHM", "EA5XX", "G0VIM", "JA1ABC"])
        self.assertEqual(self.calls("sort=snr&order=desc")[0], "EA5XX")

    def test_pagination_reports_the_unpaged_total(self):
        status, body = self.get("limit=2")
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["matched"], 4)
        self.assertEqual(body["total"], 4)
        first = self.calls("limit=2")
        second = self.calls("limit=2&offset=2")
        self.assertEqual(len(set(first) & set(second)), 0)

    def test_limit_is_capped_server_side(self):
        status, body = self.get("limit=999999")
        self.assertEqual(status, 200)
        self.assertLessEqual(body["limit"], 5000)

    def test_field_projection(self):
        status, body = self.get("fields=callsign,band")
        self.assertEqual(status, 200)
        self.assertEqual(set(body["spots"][0]), {"callsign", "band"})

    def test_submitted_window_uses_the_submission_time(self):
        # DL2BHM was last heard 120s ago but spotted just now, so a
        # submitted_at window of 1m includes it while a last_heard one does not.
        self.assertIn("DL2BHM", self.calls("time_field=submitted_at&last=1m"))
        self.assertNotIn("DL2BHM", self.calls("last=1m"))

    def test_record_carries_the_full_detail(self):
        status, body = self.get("callsign=G0VIM")
        rec = body["spots"][0]
        for field in ("callsign", "band", "frequency", "frequency_mhz", "mode",
                      "first_heard_iso", "last_heard_iso", "hits", "submitted",
                      "submitted_at_iso", "spot_comment", "country_code",
                      "snr", "source", "extract_confidence", "heard_text",
                      "agrees_with_dx_spot"):
            self.assertIn(field, rec)
        self.assertTrue(rec["submitted"])
        self.assertEqual(rec["spot_comment"], "[Voice] N")

    def test_rate_limited_per_address(self):
        c = self.client
        self.assertEqual(
            c.get("/api/spots", headers={"X-Real-IP": "203.0.113.90"}).status_code, 200)
        r = c.get("/api/spots", headers={"X-Real-IP": "203.0.113.90"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers["Retry-After"], "1")
        self.assertEqual(
            c.get("/api/spots", headers={"X-Real-IP": "203.0.113.91"}).status_code, 200)

    def test_its_budget_is_separate_from_explain(self):
        # Spending the spots budget must not lock the caller out of an
        # explanation.
        c, ip = self.client, {"X-Real-IP": "203.0.113.92"}
        self.assertEqual(c.get("/api/spots", headers=ip).status_code, 200)
        self.assertEqual(
            c.post("/api/explain", json={"text": "hi"}, headers=ip).status_code, 200)
        self.assertEqual(c.get("/api/spots", headers=ip).status_code, 429)

    def test_bad_parameters_are_400_not_an_empty_list(self):
        # Answering a typo with [] reads as "nothing matched", which is the
        # most misleading thing a filter API can do.
        for bad in ["last=5min", "submitted=maybe", "sort=bogus", "order=sideways",
                    "fields=nope", "min_hits=abc", "time_field=whenever"]:
            status, body = self.get(bad)
            self.assertEqual(status, 400, f"{bad} -> {status}")
            self.assertIn("error", body)


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
