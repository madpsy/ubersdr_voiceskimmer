#!/usr/bin/env node
//
// Wire tests for static/minimal-radio.js: the version 2 spectrum frame decoder,
// and the two WebSocket URLs that select the protocol versions this addon
// speaks.
//
// Run with:  node --test test_spectrum_v2.js
//
// WHY THIS EXISTS
// ---------------
// static/minimal-radio.js is a vendored copy of the UberSDR server's own file
// (see README, "Vendored from the main UberSDR instance"). Its spectrum decoder
// is a hand-written parse of a binary format defined on the server, so nothing
// in this repository catches it drifting: a misread offset does not throw, it
// renders a plausible-looking spectrum with the wrong numbers in it.
//
// The frames below are therefore built from the SERVER's definition rather than
// from the decoder's, so a decoder that misreads the format fails here instead
// of agreeing with itself:
//
//   ka9q_ubersdr/user_spectrum_v2.go:47-70          the layout, in prose
//   ka9q_ubersdr/user_spectrum_v2_wire_test.go      spectrumV2EncodeRef(), the
//                                                   reference ENCODER, verbatim
//                                                   pre-optimisation Go
//   ka9q_ubersdr/clients/python/test_spectrum_v2.py an independent third
//                                                   implementation of the same
//                                                   builders, whose literal
//                                                   vectors are reused below
//
// makeFrame/fullBody/deltaBody are a transcription of spectrumV2EncodeRef's
// packet assembly:
//
//   copy(packet[0:4], []byte{'S','P','E','C'})
//   packet[4] = SpectrumV2Version                       // 2
//   packet[5] = SpectrumV2FlagFull | SpectrumV2FlagDelta// 0x05 / 0x06
//   binary.LittleEndian.PutUint16(packet[6:8],  sequence)
//   binary.LittleEndian.PutUint64(packet[8:16], timestampNanos)
//   binary.LittleEndian.PutUint64(packet[16:24], centreFreq)
//   full : [refCentiDB int16 LE][stepCentiDB uint8][code uint8 × n]
//   delta: [mask ⌈n/8⌉ bytes][value uint8 per set bit]
//   mask[i>>3] |= 1 << (uint(i) & 7)                    // LSB-first per byte
//   values in increasing bin order
//
// and the decoded value is dB = (refCentiDB + code × stepCentiDB) / 100.

const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");

// ---------------------------------------------------------------------------
// Load MinimalRadio
// ---------------------------------------------------------------------------
//
// minimal-radio.js is a plain browser script with no module wrapper: it just
// declares top-level names. Evaluating it inside a function and returning the
// class is how a browser's global scope is imitated without a DOM.

const SRC = path.join(__dirname, "static", "minimal-radio.js");
const source = fs.readFileSync(SRC, "utf8");
// The file ends with `window.MinimalRadio = MinimalRadio`, so a window must
// exist before it is evaluated. The URL tests replace it with one carrying a
// location.
global.window = global.window || {};
const MinimalRadio = new Function(`${source}\n;return MinimalRadio;`)();

// The constructor logs; keep the test output readable, and let a test opt in to
// seeing what was logged.
function quiet(fn) {
  const saved = { log: console.log, error: console.error, warn: console.warn };
  const seen = [];
  console.log = console.error = console.warn = (...a) => seen.push(a.join(" "));
  try {
    return { value: fn(seen), seen };
  } finally {
    Object.assign(console, saved);
  }
}

function radio() {
  return quiet(() => new MinimalRadio("11111111-2222-4333-8444-555555555555")).value;
}

// ---------------------------------------------------------------------------
// Frame builders — see the header comment for their provenance
// ---------------------------------------------------------------------------

const SPEC_V2 = 2;
const FLAG_V2_FULL = 0x05;
const FLAG_V2_DELTA = 0x06;
const FLAG_V1_FULL = 0x03;
const FLAG_V1_DELTA = 0x04;
const V2_HEADER = 24;
const V1_HEADER = 22;

function makeFrame(flags, body, opts = {}) {
  const {
    seq = 0,
    ts = 1234n,
    freq = 7100000n,
    version = SPEC_V2,
  } = opts;
  const headerLen = version === SPEC_V2 ? V2_HEADER : V1_HEADER;
  const buf = new ArrayBuffer(headerLen + body.length);
  const u8 = new Uint8Array(buf);
  const dv = new DataView(buf);
  u8[0] = 0x53; u8[1] = 0x50; u8[2] = 0x45; u8[3] = 0x43; // "SPEC"
  u8[4] = version;
  u8[5] = flags;
  let off = 6;
  if (version === SPEC_V2) {
    dv.setUint16(off, seq, true);
    off += 2;
  }
  dv.setBigUint64(off, BigInt(ts), true);
  dv.setBigUint64(off + 8, BigInt(freq), true);
  u8.set(body, headerLen);
  assert.strictEqual(off + 16, headerLen, "header builder disagrees with itself");
  return new DataView(buf);
}

function fullBody(refCentiDB, stepCentiDB, codes) {
  const out = new Uint8Array(3 + codes.length);
  new DataView(out.buffer).setInt16(0, refCentiDB, true);
  out[2] = stepCentiDB;
  out.set(codes, 3);
  return out;
}

// changes: { binIndex: code }. Mask LSB-first per byte, values in bin order.
function deltaBody(n, changes) {
  const maskLen = (n + 7) >> 3;
  const idx = Object.keys(changes).map(Number).sort((a, b) => a - b);
  const out = new Uint8Array(maskLen + idx.length);
  for (const i of idx) out[i >> 3] |= 1 << (i & 7);
  idx.forEach((i, k) => { out[maskLen + k] = changes[i]; });
  return out;
}

// Version 1's delta: [count u16 LE] then [index u16 LE, value u8] triples.
function v1DeltaBody(changes) {
  const idx = Object.keys(changes).map(Number).sort((a, b) => a - b);
  const out = new Uint8Array(2 + idx.length * 3);
  new DataView(out.buffer).setUint16(0, idx.length, true);
  idx.forEach((i, k) => {
    new DataView(out.buffer).setUint16(2 + k * 3, i, true);
    out[2 + k * 3 + 2] = changes[i];
  });
  return out;
}

// The decoder returns a Float32Array, so a value such as -327.68 comes back as
// -327.67999267578125. The tolerance is relative and is still four orders of
// magnitude tighter than the format's finest quantisation step (0.25 dB), so it
// cannot hide a scale or offset error.
const near = (a, b, msg) =>
  assert.ok(Math.abs(a - b) <= 1e-5 * Math.max(1, Math.abs(b)), `${msg}: ${a} vs ${b}`);

// ---------------------------------------------------------------------------
// Builder self-check
// ---------------------------------------------------------------------------

test("the frame builder puts each field where the server writes it", () => {
  const f = makeFrame(FLAG_V2_FULL, fullBody(-12000, 50, [7]), {
    seq: 0xbeef, ts: 0x0102030405060708n, freq: 14200000n,
  });
  const u8 = new Uint8Array(f.buffer);
  assert.deepStrictEqual([...u8.subarray(0, 4)], [0x53, 0x50, 0x45, 0x43], "magic");
  assert.strictEqual(u8[4], 2, "version byte");
  assert.strictEqual(u8[5], FLAG_V2_FULL, "flags byte");
  assert.strictEqual(f.getUint16(6, true), 0xbeef, "sequence at 6");
  assert.strictEqual(f.getBigUint64(8, true), 0x0102030405060708n, "timestamp at 8");
  assert.strictEqual(f.getBigUint64(16, true), 14200000n, "centre frequency at 16");
  // Version 2's body starts two bytes later than version 1's, which is the
  // whole difference between the two headers.
  assert.strictEqual(u8.length, V2_HEADER + 3 + 1);
  assert.strictEqual(V2_HEADER - V1_HEADER, 2);
});

test("the delta mask is LSB-first within a byte, as the Go encoder writes it", () => {
  // mask[i>>3] |= 1 << (i & 7)  — bin 0 is bit 0 of byte 0, bin 8 is bit 0 of
  // byte 1. The same assertion the Python client makes in
  // test_spectrum_v2.py::test_mask_is_lsb_first.
  assert.deepStrictEqual([...deltaBody(16, { 0: 9 })], [0x01, 0x00, 9]);
  assert.deepStrictEqual([...deltaBody(16, { 7: 9 })], [0x80, 0x00, 9]);
  assert.deepStrictEqual([...deltaBody(16, { 8: 9 })], [0x00, 0x01, 9]);
  assert.deepStrictEqual([...deltaBody(16, { 1: 4, 9: 5 })], [0x02, 0x02, 4, 5]);
});

// ---------------------------------------------------------------------------
// Version 2 decode
// ---------------------------------------------------------------------------

test("a full frame (0x05) applies the scale it carries", () => {
  // The literal vector from clients/python/test_spectrum_v2.py:
  //   full_body(-12000, 50, [0, 2, 200]) -> -120.0, -119.0, -20.0 dB
  const r = radio();
  const out = r.parseBinarySpectrum(
    makeFrame(FLAG_V2_FULL, fullBody(-12000, 50, [0, 2, 200]), { seq: 1, freq: 7100000n }));
  assert.ok(out, "full frame was refused");
  assert.strictEqual(out.type, "spectrum");
  assert.strictEqual(out.data.length, 3);
  near(out.data[0], -120.0, "bin 0");
  near(out.data[1], -119.0, "bin 1");
  near(out.data[2], -20.0, "bin 2");
  assert.strictEqual(out.frequency, 7100000);
  assert.deepStrictEqual(r.spectrumScale, { ref: -12000, step: 50 });
});

test("a full frame's reference is signed", () => {
  // refCentiDB is an int16 on the wire. Read unsigned, -12000 becomes 53536 and
  // every bin lands 655 dB high — visibly wrong, but only if something looks.
  const r = radio();
  const out = r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(-32768, 100, [0])));
  near(out.data[0], -327.68, "most negative representable reference");
  const pos = radio().parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(32767, 100, [0])));
  near(pos.data[0], 327.67, "most positive representable reference");
});

test("a delta (0x06) patches only the masked bins and keeps the scale", () => {
  const r = radio();
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 100, new Array(20).fill(10)), { seq: 1 }));
  const out = r.parseBinarySpectrum(
    makeFrame(FLAG_V2_DELTA, deltaBody(20, { 3: 40, 17: 200 }), { seq: 2 }));
  assert.ok(out, "delta was refused");
  assert.strictEqual(out.data.length, 20, "a delta must not change the bin count");
  for (let i = 0; i < 20; i++) {
    const want = i === 3 ? 40 : i === 17 ? 200 : 10;
    near(out.data[i], want, `bin ${i}`);
  }
  assert.deepStrictEqual(r.spectrumScale, { ref: 0, step: 100 },
    "a delta carries no scale and must leave the full frame's alone");
});

test("a delta spanning several mask bytes lands the values in bin order", () => {
  // The value bytes are positionally addressed: the k-th set bit, counting bins
  // upward, takes the k-th value. Nothing on the wire pairs them, so an
  // implementation that walked the mask high-bit-first, or byte-wise in the
  // wrong direction, would still consume exactly the right number of bytes and
  // put every one of them on the wrong bin.
  const r = radio();
  const n = 24;
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(-5000, 25, new Array(n).fill(0)), { seq: 1 }));
  const changes = { 0: 1, 7: 2, 8: 3, 15: 4, 16: 5, 23: 6 };
  const out = r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, deltaBody(n, changes), { seq: 2 }));
  for (let i = 0; i < n; i++) {
    const code = changes[i] === undefined ? 0 : changes[i];
    near(out.data[i], (-5000 + code * 25) / 100, `bin ${i}`);
  }
});

test("a partial trailing mask byte is handled, and its stray bits are ignored", () => {
  // 20 bins is 3 mask bytes with 4 bits spare in the last. The server never
  // sets them; the decoder counts them when sizing the body and then must not
  // write past the end of the accumulator.
  const r = radio();
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 50, new Array(20).fill(0)), { seq: 1 }));
  const out = r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, deltaBody(20, { 19: 99 }), { seq: 2 }));
  assert.ok(out, "delta touching the last bin was refused");
  assert.strictEqual(out.data.length, 20);
  near(out.data[19], 49.5, "bin 19");
});

test("a scale change on a new full frame replaces the old one", () => {
  const r = radio();
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(-12000, 50, [100, 100]), { seq: 1 }));
  const first = r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, deltaBody(2, { 0: 120 }), { seq: 2 }));
  near(first.data[0], -60.0, "under the first scale");

  // A new full frame, same codes, different scale: every bin must move.
  const out = r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(-9000, 25, [120, 100]), { seq: 3 }));
  assert.deepStrictEqual(r.spectrumScale, { ref: -9000, step: 25 });
  near(out.data[0], -60.0, "bin 0 under the second scale");
  near(out.data[1], -65.0, "bin 1 under the second scale");

  // And the delta that follows must be read against the NEW scale, not the one
  // the codes were first quantised with.
  const after = r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, deltaBody(2, { 1: 200 }), { seq: 4 }));
  near(after.data[1], -40.0, "bin 1 after the scale change");
});

test("a full frame with a different bin count resizes the accumulator", () => {
  const r = radio();
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 50, new Array(20).fill(1)), { seq: 1 }));
  assert.strictEqual(r.binarySpectrumData8.length, 20);
  const out = r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 50, new Array(9).fill(2)), { seq: 2 }));
  assert.strictEqual(out.data.length, 9);
  assert.strictEqual(r.binarySpectrumData8.length, 9);
});

// ---------------------------------------------------------------------------
// Frames that must NOT be applied to stale bins
// ---------------------------------------------------------------------------

test("a delta before any full frame is refused rather than applied", () => {
  // Joining a stream mid-flight looks exactly like this. There is nothing to
  // patch, and inventing a baseline would render bins that were never sent.
  // The server's keyframe is at most 50 frames (~5 s) away.
  const r = radio();
  const { value: out } = quiet(() =>
    r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, deltaBody(8, { 0: 9 }), { seq: 7 })));
  assert.strictEqual(out, null);
  assert.strictEqual(r.binarySpectrumData8, null, "no accumulator was invented");
  assert.strictEqual(r.spectrumScale, null, "no scale was invented");
});

test("a delta whose length disagrees with its mask is refused WHOLE", () => {
  // Applied part way, the frame would leave some bins updated and some not,
  // with nothing to say which — and they would stay that way until the next
  // keyframe. This is the case that matters most: dropping the frame is
  // recoverable, half-applying it is silent corruption.
  const r = radio();
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 50, [1, 2, 3, 4]), { seq: 1 }));
  const before = [...r.binarySpectrumData8];

  // Mask claims bins 0 and 2 changed; only one value byte follows.
  const short = new Uint8Array([0b0101, 77]);
  const { value: a } = quiet(() => r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, short, { seq: 2 })));
  assert.strictEqual(a, null, "a short delta was accepted");
  assert.deepStrictEqual([...r.binarySpectrumData8], before, "bins were touched anyway");

  // Too many values for the mask is equally malformed.
  const long = new Uint8Array([0b0001, 77, 88]);
  const { value: b } = quiet(() => r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, long, { seq: 3 })));
  assert.strictEqual(b, null, "an overlong delta was accepted");
  assert.deepStrictEqual([...r.binarySpectrumData8], before, "bins were touched anyway");

  // A body with no mask at all.
  const { value: c } = quiet(() =>
    r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, new Uint8Array(0), { seq: 4 })));
  assert.strictEqual(c, null, "an empty delta was accepted");
  assert.deepStrictEqual([...r.binarySpectrumData8], before, "bins were touched anyway");
});

test("a delta sized for the previous bin count is refused after a resize", () => {
  // A bin-count change always arrives as a full frame, so this cannot happen on
  // a well-behaved stream. It is what a dropped full frame followed by a delta
  // from the NEW stream looks like, and it is exactly the shape that would
  // scribble one frame's values across another frame's bins.
  const r = radio();
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 50, new Array(32).fill(1)), { seq: 1 }));
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 50, new Array(8).fill(1)), { seq: 2 }));
  const before = [...r.binarySpectrumData8];
  // A delta built for 32 bins: 4 mask bytes where the decoder now expects 1.
  const { value: out } = quiet(() =>
    r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, deltaBody(32, { 0: 9, 31: 9 }), { seq: 3 })));
  assert.strictEqual(out, null);
  assert.deepStrictEqual([...r.binarySpectrumData8], before);
});

test("a sequence gap: the delta still applies, and the next keyframe repairs", () => {
  // WHAT THE SERVER SPECIFIES, which is not what it might look like it should.
  //
  // Version 2 added the sequence number so a dropped frame can be SEEN
  // (user_spectrum_v2.go's "WHAT WAS WRONG WITH VERSION 1", point 6). It did
  // not add a rule that a delta after a gap be discarded, and no reference
  // client does that: clients/python/spectrum_display.py:514-521 counts the gap
  // and applies the frame anyway —
  //
  //     "A gap means frames were dropped for a slow reader. Nothing to repair
  //      here -- the next keyframe restates everything, which is what they are
  //      for -- but worth counting rather than silently rendering stale bins
  //      until then."
  //
  // Refusing the delta instead would freeze the display for up to a keyframe
  // interval (50 frames, ~5 s) rather than showing bins that are mostly right,
  // so this asserts the specified behaviour rather than the tempting one. The
  // guarantee that actually protects stale bins is the keyframe, checked below.
  const r = radio();
  r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 100, [10, 10, 10, 10]), { seq: 1 }));

  // seq 2 is dropped in flight. seq 3 arrives, changing bin 0 only.
  const gapped = r.parseBinarySpectrum(makeFrame(FLAG_V2_DELTA, deltaBody(4, { 0: 50 }), { seq: 3 }));
  assert.ok(gapped, "the delta after the gap was refused");
  near(gapped.data[0], 50.0, "the bin the surviving delta carried");
  // Bins 1..3 are STALE: whatever seq 2 said about them is lost. That is the
  // known cost, and it is bounded.
  for (let i = 1; i < 4; i++) near(gapped.data[i], 10.0, `stale bin ${i}`);

  // The keyframe is the repair, and it must overwrite every bin — including the
  // ones no delta has mentioned since.
  const key = r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(0, 100, [77, 77, 77, 77]), { seq: 4 }));
  for (let i = 0; i < 4; i++) near(key.data[i], 77.0, `repaired bin ${i}`);
});

// ---------------------------------------------------------------------------
// Version 1 still decodes
// ---------------------------------------------------------------------------

test("version 1 frames still decode, on the fixed -256 dB scale", () => {
  // A server too old to know the version parameter keeps sending version 1
  // whatever was asked for, so both shapes have to work.
  const r = radio();
  const full = r.parseBinarySpectrum(
    makeFrame(FLAG_V1_FULL, new Uint8Array([0, 100, 255]), { version: 1 }));
  assert.ok(full, "v1 full frame refused");
  near(full.data[0], -256, "v1 bin 0");
  near(full.data[1], -156, "v1 bin 1");
  near(full.data[2], -1, "v1 bin 2");
  assert.strictEqual(r.spectrumScale, null, "v1 must not leave a scale behind");

  const delta = r.parseBinarySpectrum(
    makeFrame(FLAG_V1_DELTA, v1DeltaBody({ 1: 200 }), { version: 1 }));
  near(delta.data[1], -56, "v1 delta bin 1");
  near(delta.data[0], -256, "v1 delta left bin 0 alone");
});

test("a version 2 full frame clears a version 1 stream's fixed scale, and back", () => {
  // Mixing the two is what a reconnect to a different server looks like. A
  // version 1 frame decoded with a leftover version 2 scale, or the reverse,
  // is off by tens of decibels with nothing to indicate it.
  const r = radio();
  r.parseBinarySpectrum(makeFrame(FLAG_V1_FULL, new Uint8Array([100]), { version: 1 }));
  assert.strictEqual(r.spectrumScale, null);
  const v2 = r.parseBinarySpectrum(makeFrame(FLAG_V2_FULL, fullBody(-9000, 25, [100])));
  near(v2.data[0], -65.0, "v2 after v1");
  const back = r.parseBinarySpectrum(makeFrame(FLAG_V1_FULL, new Uint8Array([100]), { version: 1 }));
  near(back.data[0], -156, "v1 after v2");
  assert.strictEqual(r.spectrumScale, null, "the v2 scale survived a v1 frame");
});

test("unusable frames are refused", () => {
  const r = radio();
  const refused = (frame) => quiet(() => r.parseBinarySpectrum(frame)).value;
  // An unknown protocol version.
  assert.strictEqual(refused(makeFrame(FLAG_V2_FULL, fullBody(0, 50, [1]), { version: 3 })), null);
  // An unknown frame type.
  assert.strictEqual(refused(makeFrame(0x09, new Uint8Array([1, 2]))), null);
  // A zero quantisation step would make every bin read as the reference.
  assert.strictEqual(refused(makeFrame(FLAG_V2_FULL, fullBody(0, 0, [1, 2]))), null);
  // A frame too short to hold even a header.
  assert.strictEqual(refused(new DataView(new Uint8Array([0x53, 0x50, 0x45, 0x43]).buffer)), null);
  // Version 2 needs 24 header bytes; 22 is a version 1 header.
  const truncated = new Uint8Array(makeFrame(FLAG_V2_FULL, fullBody(0, 50, [1])).buffer).subarray(0, 23);
  assert.strictEqual(refused(new DataView(truncated.slice().buffer)), null);
  // A full frame with no scale in it.
  assert.strictEqual(refused(makeFrame(FLAG_V2_FULL, new Uint8Array([0, 0]))), null);
});

// ---------------------------------------------------------------------------
// The negotiated versions in the URLs
// ---------------------------------------------------------------------------

class FakeWebSocket {
  constructor(url) {
    FakeWebSocket.urls.push(url);
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
  }
  send() {}
  close() {}
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.urls = [];

async function capturedUrl(fn) {
  const savedWs = global.WebSocket;
  const savedWindow = global.window;
  FakeWebSocket.urls = [];
  global.WebSocket = FakeWebSocket;
  global.window = {
    location: { protocol: "https:", host: "sdr.example:8080" },
  };
  try {
    const r = radio();
    // Skip the POST /connection preflight: it is not what these assert, and it
    // is the only thing on the path that needs fetch().
    r.connectionValidated = true;
    let seen;
    await quiet(async () => { seen = fn(r); }).value;
    await seen;
    assert.strictEqual(FakeWebSocket.urls.length, 1, "expected exactly one socket");
    return FakeWebSocket.urls[0];
  } finally {
    global.WebSocket = savedWs;
    global.window = savedWindow;
  }
}

test("the spectrum socket asks for binary8 AND version 2", () => {
  // The server serves version 2 only when BOTH are present:
  //   useV2 := spectrumVersion >= 2 && useBinary8   (user_spectrum_websocket.go:181)
  // Asking for one without the other silently gets version 1 — 2.15x the
  // bandwidth for the same content, with no error anywhere to say so.
  return capturedUrl((r) => r.connectSpectrum("40m", () => {})).then((url) => {
    assert.match(url, /\/ws\/user-spectrum\?/, url);
    assert.match(url, /(\?|&)mode=binary8(&|$)/, `mode=binary8 missing: ${url}`);
    assert.match(url, /(\?|&)version=2(&|$)/, `version=2 missing: ${url}`);
  });
});

test("the audio socket asks for Opus at protocol version 4", () => {
  // Version 4 changes the packet HEADER, not the Opus payload. The old fixed
  // 21-byte parse against a version 4 frame does not fail, it hands the decoder
  // a few bytes of audio as metadata.
  return capturedUrl((r) => {
    r.currentFrequency = 14200000;
    r.currentMode = "usb";
    return r.connectWebSocket();
  }).then((url) => {
    assert.match(url, /\/ws\?/, url);
    assert.match(url, /(\?|&)format=opus(&|$)/, `format=opus missing: ${url}`);
    assert.match(url, /(\?|&)version=4(&|$)/, `version=4 missing: ${url}`);
    assert.doesNotMatch(url, /version=2(&|$)/, `still asking for version 2: ${url}`);
  });
});

// ---------------------------------------------------------------------------
// Local customisations that a re-sync from upstream must not silently drop
// ---------------------------------------------------------------------------
//
// static/minimal-radio.js is re-synced wholesale from the server's copy when the
// protocol moves. These two changes are this addon's own and have no upstream
// equivalent, so nothing but this test would notice them going missing.

test("the SSB passband stays at 50-2400 Hz, matching ubersdr.py", () => {
  const r = radio();
  r.setBandwidthForMode("usb");
  assert.strictEqual(r.bandwidthLow, 50);
  assert.strictEqual(r.bandwidthHigh, 2400, "upstream's 2850 was re-synced in");
  r.setBandwidthForMode("lsb");
  assert.strictEqual(r.bandwidthLow, -2400, "upstream's -2850 was re-synced in");
  assert.strictEqual(r.bandwidthHigh, -50);
  // A fresh instance defaults to the same, before any mode is chosen.
  assert.strictEqual(radio().bandwidthHigh, 2400);
});

test("the AGC overrides are still sent, once, on connect", () => {
  const r = radio();
  const sent = [];
  r.ws = { readyState: FakeWebSocket.OPEN, send: (s) => sent.push(JSON.parse(s)) };
  global.WebSocket = FakeWebSocket;      // for the readyState comparison
  try {
    r.sendAGCCommand();
  } finally {
    delete global.WebSocket;
  }
  assert.deepStrictEqual(sent, [{
    type: "set_agc",
    agcHangTime: 1.0,
    agcRecoveryRate: 100.0,
    agcThreshold: -10.0,
  }], "the values must match AGC_* in ubersdr.py");
});
