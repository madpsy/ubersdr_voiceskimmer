"""
mqtt.py — publish confirmed callsigns and submitted DX spots to UberSDR's MQTT
feed, and expose them in Home Assistant.

UberSDR exposes an ingest port on the sdr-network that lets addons publish
through the receiver's own MQTT connection, and declare Home Assistant entities,
without holding any broker credentials. See addon_mqtt.md in the ka9q_ubersdr
repository for the full API.

This is not optional and needs no configuration. The ingest endpoint is derived
from --host (UBERSDR_HOST), so the existing docker-compose.yml is sufficient
as-is. When the receiver has MQTT disabled, or this container is not a
recognised addon, publishing is skipped and the scanner runs exactly as before.

Three topics are published:

    confirmed  — one message per confirmed callsign (not retained)
    spots      — one message per submitted DX spot  (not retained)
    summary    — rolling counters and last-seen state, retained, every 30 s

Home Assistant entities all read from the retained summary, so they carry a
value the moment Home Assistant subscribes rather than waiting for the next
callsign to come through.

Publishing NEVER blocks the scanner. Every event goes onto a bounded queue
drained by a single background thread; if the receiver is slow or unreachable
the queue fills and events are dropped, because a real-time audio pipeline must
not wait on telemetry.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.parse
from collections import deque
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("voiceskimmer")

# UberSDR's mqtt.addon_ingest.port default. An operator who changes it can point
# us at the new one with UBERSDR_INGEST_URL.
DEFAULT_INGEST_PORT = 6926

# Populates this addon's Home Assistant device card.
ADDON_VERSION = "1.1.0"
ADDON_MODEL = "Voice callsign skimmer"

# How often the retained summary is refreshed. Must stay comfortably below the
# receiver's offline_after_sec (default 300) or it would mark us offline between
# updates and Home Assistant would flap every entity.
SUMMARY_INTERVAL = 30.0

# Trailing window for the "last hour" counters.
SUMMARY_WINDOW = 3600.0

# Bounded so a stalled receiver can never grow memory without limit. Sized for
# a burst of activity, not a backlog: if it fills, dropping is correct.
QUEUE_MAX = 256

REQUEST_TIMEOUT = 5.0


def ingest_base_url(host: str) -> str:
    """
    Work out where UberSDR's addon ingest port is.

    Derived from the UberSDR host this scanner is already talking to, so a stock
    docker-compose.yml needs no new variables. UBERSDR_INGEST_URL overrides it
    outright for the rare case where the operator has moved the port.

    Always plain http and always the container host: the ingest port is
    reachable only from the sdr-network, so the https/wss setting that applies
    to the public interface is irrelevant here.
    """
    override = os.environ.get("UBERSDR_INGEST_URL", "").strip().rstrip("/")
    if override:
        return override

    host = (host or "").strip()
    if not host:
        return f"http://ubersdr:{DEFAULT_INGEST_PORT}"

    # An IPv6 literal has to stay bracketed or the URL is malformed.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    return f"http://{host}:{DEFAULT_INGEST_PORT}"


def _to_mhz(freq_hz: Any) -> Optional[float]:
    """
    Hz → MHz for display, rounded to 1 Hz resolution.

    Done here rather than in a Jinja template so the entity's value_template
    stays a plain field read, and so a missing or malformed frequency yields
    None (rendered as an empty state, i.e. "unknown") instead of a template
    error on every summary.
    """
    try:
        return round(float(freq_hz) / 1_000_000.0, 6)
    except (TypeError, ValueError):
        return None


def _ha_entities() -> List[Dict[str, Any]]:
    """
    The Home Assistant entities this addon exposes.

    They all read from the single retained "summary" topic and are told apart by
    entity_key, so one publish updates every entity rather than needing a topic
    per value.

    Templates that reference a field only present once something has been heard
    render an empty string until then: Home Assistant skips a state update on an
    empty render, so the entity stays "unknown" instead of showing a fabricated
    zero or raising a template error.
    """
    return [
        {
            "sub_topic": "summary",
            "entity_key": "confirmed_total",
            "component": "sensor",
            "name": "Callsigns Confirmed",
            "value_template": "{{ value_json.confirmed_total }}",
            "unit_of_measurement": "callsigns",
            "state_class": "total_increasing",
            "icon": "mdi:account-check",
        },
        {
            "sub_topic": "summary",
            "entity_key": "confirmed_hour",
            "component": "sensor",
            "name": "Callsigns (Last Hour)",
            "value_template": "{{ value_json.confirmed_last_hour }}",
            "unit_of_measurement": "callsigns",
            "state_class": "measurement",
            "icon": "mdi:account-clock",
        },
        {
            "sub_topic": "summary",
            "entity_key": "unique_total",
            "component": "sensor",
            "name": "Unique Callsigns",
            "value_template": "{{ value_json.unique_callsigns }}",
            "unit_of_measurement": "callsigns",
            "state_class": "measurement",
            "icon": "mdi:account-multiple",
        },
        {
            "sub_topic": "summary",
            "entity_key": "spots_total",
            "component": "sensor",
            "name": "DX Spots Submitted",
            "value_template": "{{ value_json.spots_total }}",
            "unit_of_measurement": "spots",
            "state_class": "total_increasing",
            "icon": "mdi:radio-tower",
        },
        {
            "sub_topic": "summary",
            "entity_key": "last_callsign",
            "component": "sensor",
            "name": "Last Callsign",
            "value_template": "{{ value_json.last_callsign | default('', true) }}",
            # Band, frequency and country ride along as attributes so a
            # dashboard can show the context without a second entity each.
            "json_attributes_template": (
                "{{ value_json.last_callsign_detail | default({}, true) | tojson }}"
            ),
            "icon": "mdi:account-voice",
        },
        {
            "sub_topic": "summary",
            "entity_key": "last_frequency",
            "component": "sensor",
            "name": "Last Frequency",
            "value_template": "{{ value_json.last_frequency_mhz | default('', true) }}",
            "unit_of_measurement": "MHz",
            "device_class": "frequency",
            # No state_class: a hopping frequency is a current reading, not a
            # measurement worth accumulating long-term statistics over.
            "icon": "mdi:sine-wave",
        },
        {
            "sub_topic": "summary",
            "entity_key": "last_band",
            "component": "sensor",
            "name": "Last Band",
            "value_template": "{{ value_json.last_band | default('', true) }}",
            "icon": "mdi:radio-handheld",
        },
        {
            "sub_topic": "summary",
            "entity_key": "last_country",
            "component": "sensor",
            "name": "Last Country",
            "value_template": "{{ value_json.last_country | default('', true) }}",
            # The ISO code rides along so a dashboard can draw a flag without
            # having to map a DXCC entity name onto one — England, Scotland and
            # Wales are three entities and one ISO country, so that mapping
            # would be wrong for exactly the stations this hears most.
            "json_attributes_template": (
                "{{ {'country_code': value_json.last_country_code | default('', true)} | tojson }}"
            ),
            "icon": "mdi:flag",
        },
        {
            "sub_topic": "summary",
            "entity_key": "last_confirmed",
            "component": "sensor",
            "name": "Last Confirmation",
            "value_template": "{{ value_json.last_confirmed_utc | default('', true) }}",
            "device_class": "timestamp",
            "icon": "mdi:clock-outline",
        },
        {
            "sub_topic": "summary",
            "entity_key": "last_spot",
            "component": "sensor",
            "name": "Last Spot",
            "value_template": "{{ value_json.last_spot_callsign | default('', true) }}",
            "json_attributes_template": (
                "{{ value_json.last_spot_detail | default({}, true) | tojson }}"
            ),
            "icon": "mdi:map-marker-radius",
        },
        {
            "sub_topic": "summary",
            "entity_key": "last_spot_frequency",
            "component": "sensor",
            "name": "Last Spot Frequency",
            "value_template": "{{ value_json.last_spot_frequency_mhz | default('', true) }}",
            "unit_of_measurement": "MHz",
            "device_class": "frequency",
            "icon": "mdi:sine-wave",
        },
        {
            "sub_topic": "summary",
            "entity_key": "scanning",
            "component": "binary_sensor",
            "name": "Scanning",
            "value_template": "{{ 'ON' if value_json.scanning else 'OFF' }}",
            "device_class": "running",
        },
        {
            "sub_topic": "summary",
            "entity_key": "spotting",
            "component": "binary_sensor",
            "name": "DX Spotting Enabled",
            "value_template": "{{ 'ON' if value_json.spotting_enabled else 'OFF' }}",
            "entity_category": "diagnostic",
            "icon": "mdi:upload-network",
        },
    ]


class MQTTPublisher:
    """
    Publishes scanner activity to UberSDR's MQTT ingest port.

    Safe to construct unconditionally: if the ingest port is unreachable or this
    container is not recognised, the publisher stays dormant and every method is
    a cheap no-op. Nothing here raises into the caller.
    """

    def __init__(self, host: str, shared: Any) -> None:
        self.base = ingest_base_url(host)
        self.shared = shared
        self.session = requests.Session()

        self._available = False
        self._warned = False
        self._ha_ready = False

        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=QUEUE_MAX)
        self._dropped = 0
        self._dropped_logged = 0.0

        self._lock = threading.Lock()
        self._confirmed_total = 0
        self._confirmed_times: deque = deque()
        self._spots_total = 0
        self._last_confirmed: Optional[Dict[str, Any]] = None
        self._last_spot: Optional[Dict[str, Any]] = None
        self._last_dwells = -1
        self._scanning = False

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

        log.info("[mqtt] ingest endpoint: %s", self.base)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Probe the receiver and start the publisher and summary threads."""
        self._probe()

        for target, name in (
            (self._run_publisher, "mqtt-publisher"),
            (self._run_summary, "mqtt-summary"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def _probe(self) -> bool:
        """
        Check whether the ingest port is reachable and we are recognised.

        Declares Home Assistant entities on the transition to available, which
        covers both first start and the receiver enabling MQTT later.
        """
        try:
            resp = self.session.get(f"{self.base}/health", timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            self._unavailable("ingest port unreachable (%s) — continuing without MQTT", exc)
            return False

        if resp.status_code == 403:
            self._unavailable(
                "this container is not a recognised UberSDR addon — continuing without MQTT"
            )
            return False
        if resp.status_code != 200:
            self._unavailable(
                "ingest health returned %s — continuing without MQTT", resp.status_code
            )
            return False

        try:
            health = resp.json()
        except ValueError as exc:
            self._unavailable("could not parse ingest health (%s) — continuing without MQTT", exc)
            return False

        already = self._available
        self._available = True
        self._warned = False
        if already:
            return True

        log.info(
            "[mqtt] connected as addon %r (broker=%s, ha_discovery=%s, rate_limit=%s/min)",
            health.get("addon"),
            health.get("mqtt_connected"),
            health.get("ha_discovery"),
            health.get("rate_limit"),
        )

        offline_after = health.get("offline_after_sec") or 0
        if offline_after and offline_after < SUMMARY_INTERVAL * 2:
            log.warning(
                "[mqtt] receiver marks addons offline after %ss but we publish every %.0fs",
                offline_after, SUMMARY_INTERVAL,
            )

        if health.get("ha_discovery"):
            self._declare_entities()
        else:
            log.info(
                "[mqtt] Home Assistant discovery is disabled on the receiver — "
                "publishing data only"
            )
        return True

    def _unavailable(self, fmt: str, *args: Any) -> None:
        """Mark publishing off, logging only on a state change."""
        self._available = False
        self._ha_ready = False
        if not self._warned:
            self._warned = True
            log.info("[mqtt] " + fmt, *args)

    # ------------------------------------------------------------------
    # Home Assistant
    # ------------------------------------------------------------------

    def _declare_entities(self) -> None:
        """
        Register every Home Assistant entity. Idempotent — UberSDR treats a
        repeat declaration as an in-place update, so re-running this on each
        reconnection costs nothing and changes nothing.
        """
        entities = _ha_entities()
        declared = 0

        for entity in entities:
            body = dict(entity)
            body["addon_version"] = ADDON_VERSION
            body["addon_model"] = ADDON_MODEL
            try:
                resp = self.session.post(
                    f"{self.base}/discovery", json=body, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                log.warning("[mqtt] declare %s: %s", entity["entity_key"], exc)
                continue

            if resp.status_code >= 400:
                log.warning(
                    "[mqtt] declare %s rejected (%s): %s",
                    entity["entity_key"], resp.status_code, resp.text.strip()[:200],
                )
                continue
            declared += 1

        self._ha_ready = declared > 0
        log.info("[mqtt] declared %d/%d Home Assistant entities", declared, len(entities))

    # ------------------------------------------------------------------
    # Public API — called from the scanner's real-time threads
    # ------------------------------------------------------------------

    def publish_confirmed(self, detection: Dict[str, Any], is_repeat: bool) -> None:
        """
        Queue a confirmed callsign. `detection` is dataclasses.asdict(Detection),
        the same value handed to the web dashboard.
        """
        now = time.time()
        payload = {
            "time": detection.get("time"),
            "timestamp": detection.get("timestamp"),
            "callsign": detection.get("normalised"),
            "band": detection.get("band"),
            "frequency": detection.get("frequency"),
            "mode": detection.get("mode"),
            "snr": detection.get("snr"),
            "name": detection.get("name") or "",
            "country": detection.get("country") or "",
            "country_code": detection.get("country_code") or "",
            "grid": detection.get("grid") or "",
            "latitude": detection.get("latitude"),
            "longitude": detection.get("longitude"),
            "is_repeat": is_repeat,
            "agrees_with_dx_spot": detection.get("agrees_with_dx_spot", False),
            "dx_corrected": detection.get("dx_corrected", False),
            # Trimmed: the full transcript can run long and is available from
            # the dashboard and the JSONL log.
            "heard": (detection.get("raw_text") or "")[:200],
        }

        with self._lock:
            self._confirmed_total += 1
            self._confirmed_times.append(now)
            self._last_confirmed = {
                "callsign": payload["callsign"],
                "band": payload["band"],
                "frequency": payload["frequency"],
                "mode": payload["mode"],
                "country": payload["country"],
                "country_code": payload["country_code"],
                "name": payload["name"],
                "utc": payload["time"],
            }

        self._enqueue("confirmed", payload, retain=False)

    def publish_spot(
        self,
        callsign: str,
        freq: int,
        comment: str,
        band: str = "",
        country_code: str = "",
        country: str = "",
        mode: str = "",
    ) -> None:
        """Queue a DX spot this scanner submitted. Mirrors WebUI.push_spot."""
        payload = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "timestamp": time.time(),
            "callsign": callsign,
            "frequency": freq,
            "band": band,
            "mode": mode,
            "comment": comment,
            "country": country,
            "country_code": country_code,
        }

        with self._lock:
            self._spots_total += 1
            self._last_spot = {
                "callsign": callsign,
                "frequency": freq,
                "band": band,
                "mode": mode,
                "country": country,
                "country_code": country_code,
                "utc": payload["time"],
            }

        self._enqueue("spots", payload, retain=False)

    # ------------------------------------------------------------------
    # Queue and worker threads
    # ------------------------------------------------------------------

    def _enqueue(self, sub_topic: str, payload: Dict[str, Any], retain: bool) -> None:
        """
        Hand an event to the publisher thread. Never blocks: this is called from
        the scanner's audio path, where waiting on HTTP would stall detection.
        """
        if not self._available:
            return
        try:
            self._queue.put_nowait((sub_topic, payload, retain))
        except queue.Full:
            self._note_drop()

    def _note_drop(self) -> None:
        """Count a dropped event, logging at most once a minute."""
        with self._lock:
            self._dropped += 1
            now = time.time()
            if now - self._dropped_logged < 60.0:
                return
            self._dropped_logged = now
            dropped = self._dropped
        log.warning("[mqtt] publish queue full — %d event(s) dropped so far", dropped)

    def _run_publisher(self) -> None:
        """Drain the queue, one HTTP request at a time."""
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                return
            sub_topic, payload, retain = item
            self._post(sub_topic, payload, retain)

    def _run_summary(self) -> None:
        """Refresh the retained summary on a fixed interval."""
        # Publish once straight away so Home Assistant has values immediately
        # rather than after the first interval.
        self._publish_summary()

        while not self._stop.wait(SUMMARY_INTERVAL):
            self._publish_summary()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _build_summary(self) -> Dict[str, Any]:
        cutoff = time.time() - SUMMARY_WINDOW

        with self._lock:
            while self._confirmed_times and self._confirmed_times[0] < cutoff:
                self._confirmed_times.popleft()

            summary: Dict[str, Any] = {
                "confirmed_total": self._confirmed_total,
                "confirmed_last_hour": len(self._confirmed_times),
                "spots_total": self._spots_total,
                "dropped_events": self._dropped,
                "scanning": self._scanning,
            }
            # Frequency, band and country are promoted to top-level fields as
            # well as riding in the detail dict: an entity's value_template
            # reads a top-level key far more legibly than one reaching into a
            # nested object, and each of these is worth its own Home Assistant
            # entity rather than being buried in another entity's attributes.
            if self._last_confirmed:
                last = self._last_confirmed
                summary["last_callsign"] = last["callsign"]
                summary["last_callsign_detail"] = last
                summary["last_confirmed_utc"] = last["utc"]
                summary["last_band"] = last["band"]
                summary["last_mode"] = last["mode"]
                summary["last_country"] = last["country"]
                summary["last_country_code"] = last["country_code"]
                summary["last_frequency_hz"] = last["frequency"]
                summary["last_frequency_mhz"] = _to_mhz(last["frequency"])
            if self._last_spot:
                spot = self._last_spot
                summary["last_spot_callsign"] = spot["callsign"]
                summary["last_spot_detail"] = spot
                summary["last_spot_band"] = spot["band"]
                summary["last_spot_country"] = spot["country"]
                summary["last_spot_frequency_hz"] = spot["frequency"]
                summary["last_spot_frequency_mhz"] = _to_mhz(spot["frequency"])

        # Shared scanner state. Guarded because a change to SharedState should
        # degrade the summary, never take the scanner down with it.
        try:
            summary["unique_callsigns"] = len(self.shared.confirmed)
            stats = dict(self.shared.stats)
            summary["dwells"] = stats.get("dwells", 0)
            summary["candidates"] = stats.get("candidates", 0)
            summary["validated"] = stats.get("validated", 0)
            summary["rejected"] = stats.get("rejected", 0)
            summary["qrz_lookups"] = stats.get("qrz_lookups", 0)
            summary["spotting_enabled"] = self.shared.spotter is not None
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("[mqtt] could not read shared state: %s", exc)

        return summary

    def _publish_summary(self) -> None:
        # Re-probe while dormant so the addon recovers on its own if the
        # receiver was restarted, or had MQTT enabled, after this container
        # started.
        if not self._available and not self._probe():
            return

        summary = self._build_summary()

        # "Scanning" means the dwell counter moved since the last summary —
        # a direct measure of the scanner making progress, rather than of this
        # process merely being alive, which the addon status topic already says.
        dwells = summary.get("dwells", 0)
        with self._lock:
            self._scanning = self._last_dwells >= 0 and dwells > self._last_dwells
            self._last_dwells = dwells
            summary["scanning"] = self._scanning

        self._post("summary", summary, retain=True)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _post(self, sub_topic: str, payload: Dict[str, Any], retain: bool) -> None:
        """Send one payload. Errors are logged, never raised."""
        url = f"{self.base}/publish/{urllib.parse.quote(sub_topic)}"
        if retain:
            url += "?retain=true"

        try:
            resp = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            # The receiver may be restarting. Drop to dormant so the next
            # summary re-probes (and re-declares) instead of logging per event.
            self._unavailable("publish %s failed (%s) — will retry", sub_topic, exc)
            return

        if resp.status_code < 300:
            return
        if resp.status_code == 503:
            # Receiver is up but its broker is down. Transient; stay available.
            return
        if resp.status_code == 429:
            log.warning("[mqtt] rate limited publishing %s", sub_topic)
            return
        if resp.status_code == 403:
            self._unavailable("no longer a recognised addon — pausing MQTT")
            return
        log.warning(
            "[mqtt] publish %s: %s: %s", sub_topic, resp.status_code, resp.text.strip()[:200]
        )


class NullPublisher:
    """
    Stand-in used when the publisher could not be constructed at all, so callers
    never have to branch. Every method is a no-op.
    """

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def publish_confirmed(self, detection: Dict[str, Any], is_repeat: bool) -> None:
        pass

    def publish_spot(self, *args: Any, **kwargs: Any) -> None:
        pass


def build_publisher(host: str, shared: Any) -> Any:
    """
    Construct and start a publisher, falling back to a no-op on any failure.

    MQTT is telemetry: nothing it can do should stop the scanner from starting.
    """
    try:
        pub = MQTTPublisher(host, shared)
        pub.start()
        return pub
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("[mqtt] could not start MQTT publishing: %s", exc)
        return NullPublisher()


# Re-exported for tests that assert the payload shape without a live receiver.
__all__ = ["MQTTPublisher", "NullPublisher", "build_publisher", "ingest_base_url", "json"]
