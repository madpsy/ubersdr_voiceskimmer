"""
Tests for the UberSDR audio protocol version 4 migration (ubersdr.py, pcm_v4.py).

Run with:  .venv/bin/python -m pytest test_ubersdr.py -q
      or:  python3 -m unittest test_ubersdr -v

Two things are checked here, and they fail in different ways.

THE VERSION IN THE URL
    Asking for a version the client cannot read is not a clean failure. Up to
    and including UberSDR 0.1.62 the server silently CLAMPS an unsupported
    version to 1 and serves that; only from 0.1.63 does it refuse. So a wrong
    number here produces frames of the wrong shape and no error to say so.

THE DECODER ITSELF
    pcm_v4.py is vendored from the server repository
    (clients/python/pcm_v4.py), and the version 4 predictor is BACKWARD
    adaptive: the two ends derive their filter taps from samples already coded
    and never exchange a coefficient. Any arithmetic difference from the server
    therefore returns plausible noise rather than an error, and nothing short of
    comparing decoded samples against a stream the server actually produced
    would catch it. testdata/pcmv4_stream.bin is that stream; EXPECTED_SHA is
    the hash the server, the Go client, the C++ client and the reference Python
    client all agree on.

    This addon does not decode audio bodies at all — only headers, for the
    signal-quality pair. The conformance test is here anyway, because the file
    is vendored and a re-vendor that grabbed a broken or truncated copy would
    otherwise be invisible until the header layout happened to change.
"""

import hashlib
import struct
import unittest
from pathlib import Path

import pcm_v4
import ubersdr
from pcm_v4 import (
    PCM_PROTOCOL_VERSION,
    PCMv4Decoder,
    PCMv4Error,
    QUALITY_NO_READING,
    is_v4_packet,
    is_zstd_frame,
    quality_to_float,
)

# The SHA-256 of the samples the server encoded into the fixture, little endian.
# Identical to the constant in the server repository's
# clients/python/test_pcm_v4.py, its Go port and its C++ port; that agreement is
# what makes this a conformance test rather than a regression test.
EXPECTED_SHA = "4875d2185f1ff5a2031386c569cac0c2259e6a827b9e61f813399a19c3b9c903"

# The second fixture: reduced-depth I/Q at a 20 dB margin, crossing between
# profile 2 and profile 0 mid-stream. This addon never asks for that mode — it
# takes demodulated audio — but the vendored decoder implements the profile, and
# an implementation nothing exercises is an implementation nothing checks.
EXPECTED_SCALED_SHA = "7315366ceed3e70552c28d31cde690a14dc66f5244b5a8dc34a5e696f5698ccc"

FIXTURE = Path(__file__).with_name("testdata") / "pcmv4_stream.bin"
SCALED_FIXTURE = Path(__file__).with_name("testdata") / "pcmv4_scaled.bin"


def read_fixture(path=None):
    """Packets from a fixture.

    Layout: "UV4F", a format byte, a uint32 packet count, then each packet as a
    uint32 length and that many bytes.
    """
    raw = (path or FIXTURE).read_bytes()
    if len(raw) < 9 or raw[:4] != b"UV4F":
        raise AssertionError("fixture: bad header")
    count = struct.unpack_from("<I", raw, 5)[0]
    packets = []
    off = 9
    for _ in range(count):
        n = struct.unpack_from("<I", raw, off)[0]
        off += 4
        packets.append(raw[off:off + n])
        off += n
    if off != len(raw):
        raise AssertionError("fixture: %d trailing bytes" % (len(raw) - off))
    return packets


def session(**kw):
    """An UberSDRSession that has opened no sockets.

    __init__ does no I/O — it generates a UUID and builds the decoder state —
    so the URL builders and the frame handler can be driven directly.
    """
    kw.setdefault("host", "sdr.example")
    kw.setdefault("port", 8080)
    return ubersdr.UberSDRSession(**kw)


# ---------------------------------------------------------------------------
# The negotiated version
# ---------------------------------------------------------------------------

class TestAudioURL(unittest.TestCase):
    def test_audio_socket_asks_for_version_four(self):
        url = session(frequency=14200000, mode="usb")._audio_url()
        self.assertIn("version=4", url)
        self.assertNotIn("version=2", url)
        self.assertIn("format=pcm-zstd", url)
        self.assertTrue(url.startswith("ws://sdr.example:8080/ws?"), url)

    def test_the_version_comes_from_the_decoder_that_reads_it(self):
        """Hardcoding 4 in two places is how they drift apart."""
        self.assertEqual(PCM_PROTOCOL_VERSION, 4)
        self.assertIn("version=%d" % PCM_PROTOCOL_VERSION, session()._audio_url())

    def test_version_four_is_not_ahead_of_the_server(self):
        """pcmMaxProtocolVersion in the server's pcm_v4_header.go. The audio
        WebSocket refuses anything higher outright from 0.1.63 on, so this must
        never run ahead of what the server implements."""
        self.assertLessEqual(PCM_PROTOCOL_VERSION, 4)

    def test_format_stays_pcm_zstd(self):
        """The query parameter is still the server's name for the lossless
        format (websocket.go's valid-format map); version 4 changed what travels
        under it, not what it is called. Sending anything else is a 400."""
        self.assertEqual(ubersdr.PCM_FORMAT, "pcm-zstd")

    def test_the_password_is_still_quoted(self):
        url = session(password="a b&c=d")._audio_url()
        self.assertIn("password=a%20b%26c%3Dd", url)

    def test_the_dxcluster_socket_carries_no_wire_version(self):
        """It is a control/spot socket with no binary framing of its own, so it
        must not have acquired a version parameter along the way."""
        url = session()._dx_url()
        self.assertNotIn("version", url)
        self.assertNotIn("format", url)
        self.assertIn("/ws/dxcluster?user_session_id=", url)


class TestZstandardIsGone(unittest.TestCase):
    """zstandard was needed only to unwrap version 1-3 frames. Version 4 does
    not use zstd at all, so the dependency should not have survived."""

    def test_module_does_not_import_zstandard(self):
        self.assertFalse(hasattr(ubersdr, "zstandard"))
        self.assertFalse(hasattr(ubersdr, "_zstd"))

    def test_requirements_does_not_list_zstandard(self):
        req = Path(__file__).with_name("requirements.txt").read_text()
        active = [ln.strip() for ln in req.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
        self.assertFalse([ln for ln in active if ln.lower().startswith("zstandard")],
                         "zstandard is still a declared dependency: %r" % active)


# ---------------------------------------------------------------------------
# The frame handler
# ---------------------------------------------------------------------------

class TestSignalFromHeaders(unittest.TestCase):
    """_handle_audio_frame reads the header and nothing else.

    The body is skipped deliberately: the header decoder is self-contained, so
    never running the predictive codec cannot desynchronise anything, and the
    scanner's session is muted — its packets carry the silent flag and have no
    body at all.
    """

    def drive(self, packets, **kw):
        seen = []
        s = session(on_signal=lambda p, n: seen.append((p, n)), **kw)
        for pkt in packets:
            s._handle_audio_frame(pkt)
        return s, seen

    def test_every_packet_yields_a_reading_once_one_has_been_sent(self):
        """Version 4 transmits the quality pair only when it CHANGES — radiod
        moves it at about 10 Hz against ~50 packets a second — and the header
        decoder carries the last value forward. Without that carry-forward the
        silence check would see a reading on a minority of frames and nothing on
        the rest, and its peak-hold would under-read.
        """
        packets = read_fixture()
        _, seen = self.drive(packets)

        # Count how many packets actually carried the pair on the wire.
        transmitted = sum(1 for p in packets if len(p) > 4 and (p[4] & (1 << 6)))
        self.assertGreater(transmitted, 0, "the fixture never sends signal quality")
        self.assertGreater(len(seen), transmitted,
                           "readings only appeared on the packets that carried them: "
                           "%d readings for %d transmissions" % (len(seen), transmitted))
        self.assertGreater(len(seen), len(packets) // 2)

        # And the values themselves are the decoded centidecibel pairs, not
        # whatever four bytes happened to sit at a fixed offset. The fixture
        # encodes one pair, -12.5 and -70.25 dB, on every packet that carries a
        # reading; reading them as anything but signed centidecibels lands
        # somewhere else entirely.
        self.assertEqual(set(seen), {(-12.5, -70.25)},
                         "the pair is not the one the server encoded")

    def test_no_reading_is_reported_as_no_reading(self):
        """radiod having nothing to say arrives as the QUALITY_NO_READING
        codepoint, which becomes -999.0 — and must be dropped rather than fed to
        the silence check as a -999 dB signal."""
        seen = []
        s = session(on_signal=lambda p, n: seen.append((p, n)))
        pkt = self._resync_packet(power=QUALITY_NO_READING, noise=QUALITY_NO_READING)
        s._handle_audio_frame(pkt)
        self.assertEqual(seen, [])

        # One half missing is still no reading.
        seen.clear()
        s2 = session(on_signal=lambda p, n: seen.append((p, n)))
        s2._handle_audio_frame(self._resync_packet(power=-1234, noise=QUALITY_NO_READING))
        self.assertEqual(seen, [])

        # Both present is a reading.
        seen.clear()
        s3 = session(on_signal=lambda p, n: seen.append((p, n)))
        s3._handle_audio_frame(self._resync_packet(power=-1234, noise=-5678))
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0][0], -12.34, places=5)
        self.assertAlmostEqual(seen[0][1], -56.78, places=5)

    @staticmethod
    def _resync_packet(power, noise, ts=1_700_000_000_000_000_000, rate=12000,
                       ch=1, count=240):
        """A version 4 resynchronisation packet: magic, flags, absolute
        timestamp, sample count, metadata, quality. Built from the layout in the
        server's pcm_v4_header.go rather than from pcm_v4.py's parser."""
        flags = (1 << 5) | (1 << 4) | (1 << 3) | (1 << 6)   # metadata|silent|count|quality
        out = bytearray(struct.pack("<I", pcm_v4.PCMV4_MAGIC))
        out.append(flags)
        out += struct.pack("<Q", ts)
        out += _uvarint(count)
        out += _uvarint(rate)
        out.append(ch)
        out += struct.pack("<hh", power, noise)
        return bytes(out)

    def test_a_legacy_zstd_frame_is_reported_once_and_does_not_raise(self):
        """A server older than 0.1.63 clamps the version it cannot serve and
        answers with zstd-wrapped version 1 frames. That has to become one
        message, not an exception and not a warning per frame at ~50 Hz."""
        seen = []
        s = session(on_signal=lambda p, n: seen.append((p, n)))
        zstd = b"\x28\xB5\x2F\xFD" + b"\x00" * 40
        self.assertTrue(is_zstd_frame(zstd))
        for _ in range(10):
            s._handle_audio_frame(zstd)
        self.assertEqual(seen, [])
        self.assertTrue(s._legacy_warned)

    def test_junk_frames_are_ignored(self):
        s = session(on_signal=lambda p, n: self.fail("callback fired on junk"))
        for junk in (b"", b"\x50", b"\x01\x02\x03\x04\x05", b"\xff" * 64):
            s._handle_audio_frame(junk)

    def test_a_truncated_v4_packet_does_not_raise(self):
        """PCMv4Error is caught: a packet arriving before the first
        resynchronisation point looks exactly like this, and the server's
        five-second resync ends that state on its own."""
        s = session(on_signal=lambda p, n: self.fail("callback fired on a bad header"))
        magic = struct.pack("<I", pcm_v4.PCMV4_MAGIC)
        s._handle_audio_frame(magic + b"\x00")          # delta before any resync
        s._handle_audio_frame(magic + b"\x20")          # metadata bit, no timestamp

    def test_no_callback_means_no_work(self):
        s = session()                                   # on_signal is None
        for pkt in read_fixture()[:5]:
            s._handle_audio_frame(pkt)

    def test_each_session_gets_its_own_header_decoder(self):
        """The decoder IS the stream: it carries the sample rate, channel count
        and quality pair the encoder chose not to repeat. A shared one would
        apply a reconnected session's deltas to the previous session's baseline,
        and a delta packet must be refused until the new stream resynchronises.
        """
        packets = read_fixture()
        a = session(on_signal=lambda p, n: None)
        for pkt in packets[:20]:
            a._handle_audio_frame(pkt)
        b = session(on_signal=lambda p, n: None)
        self.assertIsNot(a._header, b._header)
        delta = next(p for p in packets if not (p[4] & (1 << 5)))
        with self.assertRaises(PCMv4Error):
            b._header.decode(delta)


def _uvarint(v):
    out = bytearray()
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


# ---------------------------------------------------------------------------
# Conformance: the vendored decoder against a stream the server produced
# ---------------------------------------------------------------------------

class TestVendoredDecoderConformance(unittest.TestCase):
    """Modelled on clients/python/test_pcm_v4.py in the server repository."""

    def test_decodes_server_stream_bit_exactly(self):
        """The samples must match what the server encoded, byte for byte.

        The stream covers what the format can do: ordinary mono audio, silent
        packets carrying no body, an escape to verbatim samples on
        incompressible noise, a sample-rate change, and interleaved I/Q —
        including the varying packet length that makes the header's sample count
        necessary, across the five-second periodic resynchronisation.
        """
        packets = read_fixture()
        dec = PCMv4Decoder()
        digest = hashlib.sha256()

        # Every distinct (rate, channels) the fixture passes through, in order.
        # A decoder that lost the carried-forward metadata could still hash
        # correctly while mislabelling the stream.
        want_params = [(12000, 1), (24000, 1), (384000, 2)]
        got_params = []

        for i, pkt in enumerate(packets):
            self.assertTrue(is_v4_packet(pkt), "packet %d not recognised as version 4" % i)
            pcm_le, header = dec.decode_packet_le(pkt)
            self.assertEqual(len(pcm_le) % (2 * header.channels), 0,
                             "packet %d is not whole frames" % i)
            param = (header.sample_rate, header.channels)
            if not got_params or got_params[-1] != param:
                got_params.append(param)
            digest.update(pcm_le)

        self.assertEqual(digest.hexdigest(), EXPECTED_SHA,
                         "decoded samples differ from what the server encoded")
        self.assertEqual(got_params, want_params)

    def test_both_profiles_and_both_special_bodies_are_exercised(self):
        """A pass that only ever ran one predictor proves half the codec, and
        the run above only means anything if the silent and escape bodies are in
        the stream: a silent packet carries no body at all, an escape carries
        verbatim samples, and each still advances the predictor."""
        dec = PCMv4Decoder()
        profiles = set()
        silent = escape = 0
        for pkt in read_fixture():
            header, samples = dec.decode_packet(pkt)
            profiles.add(header.profile)
            if header.silent:
                silent += 1
                self.assertEqual(set(samples), {0})
            if header.escape:
                escape += 1
        self.assertEqual(profiles, {pcm_v4.PROFILE_IQ, pcm_v4.PROFILE_AUDIO})
        self.assertGreater(silent, 0, "the fixture carried no silent packets")
        self.assertGreater(escape, 0, "the fixture carried no escape packets")

    def test_decodes_reduced_depth_iq_bit_exactly(self):
        """Profile 2: the same predictor as profile 0 over requantised samples,
        with the scale in one byte ahead of the body and undone only on the way
        out. The fixture crosses back to profile 0 mid-stream, so it also covers
        rebuilding the codec when the declared profile changes."""
        dec = PCMv4Decoder()
        digest = hashlib.sha256()
        profiles = set()
        for pkt in read_fixture(SCALED_FIXTURE):
            pcm_le, header = dec.decode_packet_le(pkt)
            profiles.add(header.profile)
            digest.update(pcm_le)
        self.assertEqual(digest.hexdigest(), EXPECTED_SCALED_SHA,
                         "decoded samples differ from what the server encoded")
        self.assertEqual(profiles, {pcm_v4.PROFILE_IQ, pcm_v4.PROFILE_IQ_SCALED})

    def test_timestamps_advance_monotonically(self):
        """A full timestamp at each resynchronisation point, a signed varint
        delta in between. Losing the carry-forward would still return samples,
        with every timestamp wrong."""
        dec = PCMv4Decoder()
        last = 0
        for i, pkt in enumerate(read_fixture()):
            header, _ = dec.decode_packet(pkt)
            self.assertGreater(header.timestamp_nanos, 0, "packet %d carried no timestamp" % i)
            if last:
                self.assertGreaterEqual(header.timestamp_nanos, last,
                                        "packet %d went backwards in time" % i)
            last = header.timestamp_nanos

    def test_quality_codepoints(self):
        self.assertEqual(quality_to_float(QUALITY_NO_READING), -999.0)
        self.assertAlmostEqual(quality_to_float(-1234), -12.34)
        self.assertAlmostEqual(quality_to_float(0), 0.0)

    def test_frame_classification(self):
        zstd = b"\x28\xB5\x2F\xFD\x00"
        self.assertTrue(is_zstd_frame(zstd))
        self.assertFalse(is_v4_packet(zstd))
        for pkt in read_fixture():
            self.assertFalse(is_zstd_frame(pkt), "a version 4 packet read as zstd")
        for short in (b"", b"\x50", b"\x50\x43\x4D"):
            self.assertFalse(is_v4_packet(short))
            self.assertFalse(is_zstd_frame(short))

    def test_the_vendored_file_is_unmodified_upstream(self):
        """pcm_v4.py is vendored on purpose. The predictor is backward adaptive,
        so an arithmetic 'improvement' returns plausible noise rather than an
        error — the header decoder this addon actually uses is a private name in
        it, and reaching for that is the only liberty taken. The one addition to
        upstream is profile 2, which the reference Python client does not yet
        carry; the two fixture hashes above are what say it was added correctly.
        """
        self.assertTrue(hasattr(pcm_v4, "PROFILE_IQ_SCALED"),
                        "profile 2 was dropped by a re-vendor from upstream")
        self.assertTrue(hasattr(pcm_v4, "_HeaderDecoder"),
                        "the header decoder ubersdr.py uses has been renamed")
        for gone in ("choose_protocol_version", "DEFAULT_PROTOCOL_VERSION",
                     "FALLBACK_PROTOCOL_VERSION"):
            self.assertFalse(hasattr(pcm_v4, gone),
                             "%s survived the removal of versions 1-3" % gone)


# ---------------------------------------------------------------------------
# The threshold the protocol change forced
# ---------------------------------------------------------------------------

class TestSilenceThreshold(unittest.TestCase):
    """--silence-min-snr had to be re-derived at the version 4 migration.

    Two unrelated things moved at once: the header's second figure changed from
    noise DENSITY to passband noise POWER (about 33.7 dB lower on this client's
    passband), and the server stopped clamping its noise readings at around -30.
    The old default of 40 was therefore fitted to data that was both on a
    different scale AND compressed, so it could not be converted — 8.0 was
    measured against the live instance instead. See the README.

    None of that is testable here. What is testable is that the number does not
    drift apart across the four places a deployment can read it from.
    """

    EXPECTED = 8.0

    def test_the_argparse_default_is_the_measured_value(self):
        """Asked of a real parse rather than by inspecting the parser: this is
        the value the scanner will actually compare a peak against."""
        import scanner
        self.assertEqual(scanner.parse_args([]).silence_min_snr, self.EXPECTED)

    def test_an_explicit_value_still_wins(self):
        import scanner
        self.assertEqual(scanner.parse_args(["--silence-min-snr", "12.5"])
                         .silence_min_snr, 12.5)

    def test_help_text_renders(self):
        """argparse %-expands every help string, so a bare '%' in one is a
        TypeError at --help and nowhere else: it passes import, passes every
        other test, and breaks only for the person reading the documentation.
        Writing '100% of speech' into this flag's help did exactly that."""
        import scanner
        with self.assertRaises(SystemExit):
            scanner.parse_args(["--help"])

    def test_every_file_quotes_the_same_number(self):
        """scanner.py, entrypoint.sh, docker-compose.yml and the README all
        state this default. A change applied to some of them is worse than one
        applied to none, because the disagreement is invisible until someone
        follows the wrong file."""
        here = Path(__file__).parent
        want = "%g" % self.EXPECTED          # "8"
        for name, pattern in (
            ("entrypoint.sh", "default: %s)" % want),
            ("docker-compose.yml", '# SILENCE_MIN_SNR: "%s"' % want),
            ("README.md", "| `SILENCE_MIN_SNR` | `--silence-min-snr` | `%.1f` |"
                          % self.EXPECTED),
        ):
            text = (here / name).read_text()
            self.assertIn(pattern, text,
                          "%s does not quote the current default %s" % (name, want))

    def test_no_file_still_quotes_the_pre_migration_default_as_current(self):
        """40 may be mentioned as history; it must not be presented as the
        default anyone should set."""
        here = Path(__file__).parent
        for name, stale in (
            ("entrypoint.sh", "default: 40"),
            ("docker-compose.yml", '# SILENCE_MIN_SNR: "40"'),
        ):
            self.assertNotIn(stale, (here / name).read_text(),
                             "%s still offers the pre-migration default" % name)


if __name__ == "__main__":
    unittest.main()
