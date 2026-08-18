"""
Tests for mqtt.py — publishing confirmed callsigns and DX spots to UberSDR.

These run against a real HTTP server standing in for UberSDR's ingest port, so
they exercise the whole client path (probe, declare, publish) rather than
mocking requests.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import mqtt
from mqtt import MQTTPublisher, NullPublisher, build_publisher, ingest_base_url


# ---------------------------------------------------------------------------
# Endpoint derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("ubersdr", "http://ubersdr:6926"),
        ("sdr.example.com", "http://sdr.example.com:6926"),
        ("192.168.1.10", "http://192.168.1.10:6926"),
        ("", "http://ubersdr:6926"),
        ("  ubersdr  ", "http://ubersdr:6926"),
        ("fd00::1", "http://[fd00::1]:6926"),
        ("[fd00::1]", "http://[fd00::1]:6926"),
    ],
)
def test_ingest_base_url(host, expected, monkeypatch):
    """Derivation from the UberSDR host is why no compose changes are needed."""
    monkeypatch.delenv("UBERSDR_INGEST_URL", raising=False)
    assert ingest_base_url(host) == expected


def test_ingest_base_url_override(monkeypatch):
    monkeypatch.setenv("UBERSDR_INGEST_URL", "http://elsewhere:7000/")
    assert ingest_base_url("ubersdr") == "http://elsewhere:7000"


def test_ingest_base_url_ignores_blank_override(monkeypatch):
    monkeypatch.setenv("UBERSDR_INGEST_URL", "   ")
    assert ingest_base_url("ubersdr") == "http://ubersdr:6926"


# ---------------------------------------------------------------------------
# Declarations must satisfy the server's validator
# ---------------------------------------------------------------------------

ENTITY_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SUB_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")
ICON_RE = re.compile(r"^mdi:[a-z0-9-]{1,40}$")

COMPONENTS = {"sensor", "binary_sensor"}
STATE_CLASSES = {"measurement", "total", "total_increasing"}
ENTITY_CATEGORIES = {"diagnostic"}
# The subset of UberSDR's device-class allowlist this addon uses.
DEVICE_CLASSES = {"timestamp", "connectivity", "running", "problem", "frequency"}


def test_declarations_match_server_rules():
    """
    A declaration that violates UberSDR's rules is rejected, and the entity then
    silently never appears in Home Assistant — worth catching here rather than
    on a live receiver.
    """
    entities = mqtt._ha_entities()
    assert entities, "no entities declared"

    seen = set()
    for e in entities:
        key = e.get("entity_key") or e["sub_topic"]

        assert SUB_TOPIC_RE.match(e["sub_topic"]), f"{key}: bad sub_topic"
        assert e["sub_topic"] != "status", f"{key}: 'status' is reserved by UberSDR"
        if "entity_key" in e:
            assert ENTITY_KEY_RE.match(e["entity_key"]), f"{key}: bad entity_key"
        assert e["component"] in COMPONENTS, f"{key}: bad component"
        assert 0 < len(e["name"]) <= 64, f"{key}: name length"

        for field in ("value_template", "json_attributes_template"):
            assert len(e.get(field, "")) <= 256, f"{key}: {field} too long"
        assert len(e.get("unit_of_measurement", "")) <= 16, f"{key}: unit too long"

        if "icon" in e:
            assert ICON_RE.match(e["icon"]), f"{key}: bad icon {e['icon']!r}"
        if "entity_category" in e:
            assert e["entity_category"] in ENTITY_CATEGORIES, f"{key}: bad entity_category"
        if "device_class" in e:
            assert e["device_class"] in DEVICE_CLASSES, f"{key}: bad device_class"
        if "state_class" in e:
            assert e["state_class"] in STATE_CLASSES, f"{key}: bad state_class"
            assert e["component"] == "sensor", f"{key}: state_class needs a sensor"
        if e["component"] == "sensor":
            assert "payload_on" not in e and "payload_off" not in e, (
                f"{key}: payload_on/off are binary_sensor only"
            )

        # object_id derives from entity_key (or sub_topic); a duplicate would be
        # rejected by the server as a collision.
        assert key not in seen, f"duplicate entity identity {key!r}"
        seen.add(key)


def test_no_control_characters_in_declarations():
    """UberSDR rejects control characters in every text field."""
    for e in mqtt._ha_entities():
        for k, v in e.items():
            if isinstance(v, str):
                assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in v), f"{k} has control chars"


# ---------------------------------------------------------------------------
# Fake ingest server
# ---------------------------------------------------------------------------


class FakeIngest:
    """Stands in for UberSDR's ingest port."""

    def __init__(self, health_status=200, ha_discovery=True):
        self.lock = threading.Lock()
        self.declarations = []
        self.published = {}
        self.retained = set()
        self.health_status = health_status
        self.ha_discovery = ha_discovery
        self.publish_status = 200

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # keep pytest output clean

            def _json(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                with outer.lock:
                    status, ha = outer.health_status, outer.ha_discovery
                if self.path != "/health":
                    self._json(404, {})
                    return
                if status != 200:
                    self._json(status, {})
                    return
                self._json(200, {
                    "addon": "voiceskimmer",
                    "mqtt_connected": True,
                    "ha_discovery": ha,
                    "rate_limit": 120,
                    "offline_after_sec": 300,
                })

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")

                if self.path == "/discovery":
                    with outer.lock:
                        outer.declarations.append(body)
                    self._json(200, {"status": "declared"})
                    return

                if self.path.startswith("/publish/"):
                    path, _, query = self.path.partition("?")
                    sub = path[len("/publish/"):]
                    with outer.lock:
                        status = outer.publish_status
                        outer.published.setdefault(sub, []).append(body)
                        if "retain=true" in query:
                            outer.retained.add(sub)
                    self._json(status, {"status": "published"})
                    return

                self._json(404, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def count(self, sub):
        with self.lock:
            return len(self.published.get(sub, []))

    def last(self, sub):
        with self.lock:
            msgs = self.published.get(sub, [])
            return msgs[-1] if msgs else None

    def wait_for(self, sub, n=1, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.count(sub) >= n:
                return True
            time.sleep(0.01)
        return False


class FakeShared:
    """Minimal stand-in for scanner.SharedState."""

    def __init__(self, spotter=None):
        self.confirmed = {}
        self.stats = {
            "dwells": 0, "candidates": 0, "validated": 0,
            "rejected": 0, "qrz_lookups": 0,
        }
        self.spotter = spotter


@pytest.fixture
def ingest(monkeypatch):
    fake = FakeIngest()
    monkeypatch.setenv("UBERSDR_INGEST_URL", fake.url)
    yield fake
    fake.close()


def make_detection(**overrides):
    d = {
        "time": "2026-08-18T14:00:00Z",
        "timestamp": time.time(),
        "band": "20m",
        "frequency": 14205000,
        "mode": "usb",
        "snr": 24.5,
        "normalised": "G4ABC",
        "name": "Alice Example",
        "country": "England",
        "country_code": "GB",
        "grid": "IO91",
        "latitude": 51.5,
        "longitude": -0.1,
        "raw_text": "this is golf four alpha bravo charlie calling cq " * 10,
        "agrees_with_dx_spot": False,
        "dx_corrected": False,
    }
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_declares_entities_on_start(ingest):
    p = MQTTPublisher("ubersdr", FakeShared())
    p.start()
    try:
        assert p._available
        with ingest.lock:
            assert len(ingest.declarations) == len(mqtt._ha_entities())
            for d in ingest.declarations:
                assert d["addon_version"] == mqtt.ADDON_VERSION
                assert d["addon_model"] == mqtt.ADDON_MODEL
    finally:
        p.stop()


def test_publishes_confirmed_callsign(ingest):
    p = MQTTPublisher("ubersdr", FakeShared())
    p.start()
    try:
        p.publish_confirmed(make_detection(), is_repeat=False)
        assert ingest.wait_for("confirmed"), "confirmed callsign was never published"

        msg = ingest.last("confirmed")
        assert msg["callsign"] == "G4ABC"
        assert msg["band"] == "20m"
        assert msg["frequency"] == 14205000
        assert msg["country_code"] == "GB"
        assert msg["is_repeat"] is False
        # The transcript is trimmed — it can run long and is on the dashboard.
        assert len(msg["heard"]) <= 200

        # Events must not be retained: replaying a stale callsign is wrong.
        with ingest.lock:
            assert "confirmed" not in ingest.retained
    finally:
        p.stop()


def test_publishes_spot(ingest):
    p = MQTTPublisher("ubersdr", FakeShared())
    p.start()
    try:
        p.publish_spot("G4ABC", 14205000, "[Voice] Alice", "20m", "GB", "England", "usb")
        assert ingest.wait_for("spots"), "spot was never published"

        msg = ingest.last("spots")
        assert msg["callsign"] == "G4ABC"
        assert msg["comment"] == "[Voice] Alice"
        assert msg["band"] == "20m"
        assert msg["country"] == "England"
        with ingest.lock:
            assert "spots" not in ingest.retained
    finally:
        p.stop()


def test_summary_is_retained_and_counts(ingest):
    shared = FakeShared(spotter=object())
    p = MQTTPublisher("ubersdr", shared)
    p.start()
    try:
        assert ingest.wait_for("summary"), "no initial summary published"

        p.publish_confirmed(make_detection(), is_repeat=False)
        p.publish_confirmed(make_detection(normalised="M0XYZ"), is_repeat=False)
        p.publish_spot("G4ABC", 14205000, "[Voice]", "20m", "GB", "England", "usb")
        assert ingest.wait_for("confirmed", 2)
        assert ingest.wait_for("spots", 1)

        shared.confirmed = {("G4ABC", 14205): 1, ("M0XYZ", 14205): 1}
        shared.stats["dwells"] = 7
        p._publish_summary()

        with ingest.lock:
            assert "summary" in ingest.retained

        s = ingest.last("summary")
        assert s["confirmed_total"] == 2
        assert s["confirmed_last_hour"] == 2
        assert s["spots_total"] == 1
        assert s["unique_callsigns"] == 2
        assert s["last_callsign"] == "M0XYZ"
        assert s["last_spot_callsign"] == "G4ABC"
        assert s["last_callsign_detail"]["band"] == "20m"
        assert s["spotting_enabled"] is True
    finally:
        p.stop()


def test_summary_promotes_frequency_band_and_country(ingest):
    """
    Frequency, band and country each get their own Home Assistant entity, so
    they must appear as top-level summary fields rather than only inside
    another entity's attributes.
    """
    p = MQTTPublisher("ubersdr", FakeShared())
    assert p._probe()

    p.publish_confirmed(make_detection(), is_repeat=False)
    p.publish_spot("M0XYZ", 7150000, "[Voice]", "40m", "GB", "Wales", "lsb")
    p._publish_summary()

    s = ingest.last("summary")

    assert s["last_band"] == "20m"
    assert s["last_mode"] == "usb"
    assert s["last_country"] == "England"
    assert s["last_country_code"] == "GB"
    assert s["last_frequency_hz"] == 14205000
    assert s["last_frequency_mhz"] == 14.205

    assert s["last_spot_band"] == "40m"
    assert s["last_spot_country"] == "Wales"
    assert s["last_spot_frequency_hz"] == 7150000
    assert s["last_spot_frequency_mhz"] == 7.15

    # Every entity's value_template must resolve against the payload it reads.
    for entity in mqtt._ha_entities():
        for field in re.findall(r"value_json\.(\w+)", entity["value_template"]):
            assert field in s, f"{entity['entity_key']}: summary has no {field!r}"


def test_to_mhz_handles_bad_input():
    """A malformed frequency must yield unknown, not a template error."""
    assert mqtt._to_mhz(14205000) == 14.205
    assert mqtt._to_mhz("14205000") == 14.205
    assert mqtt._to_mhz(None) is None
    assert mqtt._to_mhz("not a number") is None
    assert json.dumps({"f": mqtt._to_mhz(None)})  # must stay encodable


def test_scanning_reflects_dwell_progress(ingest):
    """
    "Scanning" must mean the scanner is making progress, not merely that the
    process is alive — the addon status topic already reports the latter.

    Driven synchronously (no start()) so the periodic summary thread cannot
    consume a dwell delta between the assertions below.
    """
    shared = FakeShared()
    p = MQTTPublisher("ubersdr", shared)
    assert p._probe()

    # First summary establishes a baseline; it cannot yet know if we moved.
    shared.stats["dwells"] = 3
    p._publish_summary()
    assert ingest.last("summary")["scanning"] is False

    # Dwell counter advanced → scanning.
    shared.stats["dwells"] = 5
    p._publish_summary()
    assert ingest.last("summary")["scanning"] is True

    # Counter stuck → not scanning, even though the process is fine.
    p._publish_summary()
    assert ingest.last("summary")["scanning"] is False


def test_spotting_disabled_reported(ingest):
    p = MQTTPublisher("ubersdr", FakeShared(spotter=None))
    assert p._probe()
    p._publish_summary()
    assert ingest.last("summary")["spotting_enabled"] is False


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_dormant_when_not_recognised(monkeypatch):
    fake = FakeIngest(health_status=403)
    monkeypatch.setenv("UBERSDR_INGEST_URL", fake.url)
    try:
        p = MQTTPublisher("ubersdr", FakeShared())
        p.start()
        try:
            assert not p._available
            p.publish_confirmed(make_detection(), is_repeat=False)
            time.sleep(0.2)
            assert fake.count("confirmed") == 0
        finally:
            p.stop()
    finally:
        fake.close()


def test_recovers_when_receiver_comes_up(monkeypatch):
    """
    The receiver may be restarted, or have MQTT enabled, after this container
    started — the next summary must re-probe and re-declare.

    Driven synchronously (no start()) so the count below reflects only the
    summary this test triggered.
    """
    fake = FakeIngest(health_status=503)
    monkeypatch.setenv("UBERSDR_INGEST_URL", fake.url)
    try:
        p = MQTTPublisher("ubersdr", FakeShared())
        assert not p._probe()

        with fake.lock:
            fake.health_status = 200
        p._publish_summary()

        assert p._available
        with fake.lock:
            assert len(fake.declarations) == len(mqtt._ha_entities())
        assert fake.count("summary") == 1
    finally:
        fake.close()


def test_unreachable_receiver_does_not_raise(monkeypatch):
    """A closed port must degrade silently, not take the scanner down."""
    monkeypatch.setenv("UBERSDR_INGEST_URL", "http://127.0.0.1:1")
    p = MQTTPublisher("ubersdr", FakeShared())
    p.start()
    try:
        assert not p._available
        p.publish_confirmed(make_detection(), is_repeat=False)
        p.publish_spot("G4ABC", 14205000, "x")
        p._publish_summary()
    finally:
        p.stop()


def test_ha_discovery_disabled_still_publishes(monkeypatch):
    fake = FakeIngest(ha_discovery=False)
    monkeypatch.setenv("UBERSDR_INGEST_URL", fake.url)
    try:
        p = MQTTPublisher("ubersdr", FakeShared())
        p.start()
        try:
            assert p._available
            with fake.lock:
                assert fake.declarations == [], "must not declare when HA discovery is off"
            p.publish_confirmed(make_detection(), is_repeat=False)
            assert fake.wait_for("confirmed"), "data publishing must still work"
        finally:
            p.stop()
    finally:
        fake.close()


def test_queue_drops_rather_than_blocking(ingest):
    """
    The scanner's audio thread must never wait on telemetry. When the queue is
    full, events are dropped and counted.
    """
    p = MQTTPublisher("ubersdr", FakeShared())
    p._available = True  # skip probe; do not start the draining thread

    for _ in range(mqtt.QUEUE_MAX + 50):
        p.publish_confirmed(make_detection(), is_repeat=False)

    assert p._dropped >= 50, "queue should have shed events once full"
    assert p._queue.qsize() <= mqtt.QUEUE_MAX


def test_null_publisher_is_a_noop():
    p = NullPublisher()
    p.start()
    p.publish_confirmed({}, False)
    p.publish_spot("G4ABC", 1, "x")
    p.stop()


def test_build_publisher_never_raises(monkeypatch):
    """Whatever goes wrong, the scanner must still start."""
    def boom(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(mqtt, "MQTTPublisher", boom)
    p = build_publisher("ubersdr", FakeShared())
    assert isinstance(p, NullPublisher)
