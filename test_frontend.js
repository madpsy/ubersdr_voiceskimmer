#!/usr/bin/env node
//
// Smoke test for static/app.js.
//
// `node --check` only parses; it will happily pass a file that throws the
// instant it runs. That is exactly what shipped once: initMap() was called
// near the top of the file while the map state it touches is declared with
// `let` further down, so the call landed in the temporal dead zone and the
// whole dashboard died with "can't access lexical declaration 'map' before
// initialization". A parse check cannot see that; loading the file can.
//
// So this stubs just enough DOM to load app.js the way a browser would, then
// exercises the render paths that a page does on arrival. It is deliberately
// not a full DOM: the point is to catch load-time and wiring errors, not to
// assert on pixels.

const assert = require("assert");
const fs = require("fs");
const path = require("path");

// ---------------------------------------------------------------------------
// Minimal DOM
// ---------------------------------------------------------------------------

class El {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.handlers = {};
    this.style = {};
    this.value = "";
    this.checked = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this._html = "";
    this._text = undefined;
    this._classes = new Set();
    this.classList = {
      add: (c) => this._classes.add(c),
      remove: (c) => this._classes.delete(c),
      contains: (c) => this._classes.has(c),
      toggle: (c, on) => (on ? this._classes.add(c) : this._classes.delete(c)),
    };
  }
  set className(v) {
    this._classes = new Set(String(v).split(/\s+/).filter(Boolean));
  }
  get className() {
    return [...this._classes].join(" ");
  }
  set innerHTML(v) {
    this._html = String(v);
    this._text = undefined;
    if (!v) this.children = [];
  }
  get innerHTML() {
    if (this._text === undefined) return this._html;
    return this._text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  // esc() in app.js sets textContent and reads innerHTML back to escape.
  set textContent(v) {
    this._text = String(v);
  }
  get textContent() {
    return this._text !== undefined ? this._text : this._html.replace(/<[^>]*>/g, "");
  }
  appendChild(c) {
    c.parentNode = this;
    this.children.push(c);
    return c;
  }
  insertBefore(c, ref) {
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c);
    else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  get firstChild() {
    return this.children[0] || null;
  }
  get lastChild() {
    return this.children[this.children.length - 1] || null;
  }
  addEventListener(type, fn) {
    (this.handlers[type] = this.handlers[type] || []).push(fn);
  }
  fire(type, ev) {
    for (const fn of this.handlers[type] || []) fn(ev || {});
  }
  querySelector() {
    return new El("tbody");
  }
  querySelectorAll(sel) {
    if (sel !== "tr") return [];
    if (this._rowsFor === this._html) return this._rows;
    const out = [];
    const re = /<tr[^>]*?data-key="([^"]*)"[^>]*?data-freq="(\d+)"[^>]*?data-mode="([^"]*)"/g;
    let m;
    while ((m = re.exec(this._html))) {
      const e = new El("tr");
      e.dataset.key = m[1]; e.dataset.freq = m[2]; e.dataset.mode = m[3];
      e.parentNode = this;
      out.push(e);
    }
    this._rows = out; this._rowsFor = this._html;
    return out;
  }
  closest(sel) {
    const byTag = !sel.startsWith(".");
    const want = sel.replace(/^\./, "");
    let n = this;
    while (n) {
      if (byTag ? n.tagName === want.toUpperCase() : n._classes.has(want)) return n;
      n = n.parentNode;
    }
    return null;
  }
  setAttribute() {}
  removeAttribute() {}
  load() {}
  pause() {}
  play() { return Promise.resolve(); }
}

const byId = Object.create(null);
const ID_LIST = [
  "uptime", "stats", "transcripts", "receiver", "explain-backdrop",
  "explain-body", "explain-close", "confirmed-filter", "spots-filter",
  "map", "map-note", "layer-confirmed", "layer-spotted",
  "confirmed-stop", "spots-stop", "targets-stop",
];
for (const id of ID_LIST) byId[id] = new El("div");
byId["layer-confirmed"].checked = true;
byId["layer-spotted"].checked = true;

const docHandlers = {};
const documentBody = new El("body");
global.document = {
  body: documentBody,
  getElementById: (id) => byId[id] || (byId[id] = new El("div")),
  querySelector: () => new El("tbody"),
  createElement: (t) => new El(t),
  // Recorded rather than discarded: the collapse toggles are delegated on
  // document, so a stub that swallows this cannot test them at all.
  addEventListener: (type, fn) => {
    (docHandlers[type] = docHandlers[type] || []).push(fn);
  },
};
global.__fireDocument = (type, ev) => {
  for (const fn of docHandlers[type] || []) fn(ev);
};
global.window = { location: { href: "http://localhost/" } };
const storage = {};
global.localStorage = {
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: (k) => { delete storage[k]; },
};
global.__storage = storage;
global.EventSource = class {
  constructor() {}
  addEventListener() {}
};
global.Audio = class {
  play() {
    return Promise.resolve();
  }
};
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
// The band palette lives in index.html as --band on .band-cN; bandColour
// resolves it through a probe element rather than duplicating it in JS, so the
// stub has to answer for that probe.
const BAND_HEX = ["#58a6ff", "#39c5cf", "#a371f7", "#f778ba",
                  "#7fb3d5", "#d2a8ff", "#b08968", "#9aa5b1"];
global.getComputedStyle = (el) => ({
  getPropertyValue: (prop) => {
    if (prop !== "--band") return "";
    for (const c of el._classes) {
      const m = /^band-c(\d)$/.exec(c);
      if (m) return BAND_HEX[Number(m[1])];
    }
    return "";
  },
  display: "block",
  opacity: "1",
});

// Chart.js stand-in: records the config so the chart wiring can be asserted
// without a canvas.
global.__charts = [];
global.Chart = class {
  constructor(canvas, config) {
    this.canvas = canvas; this.data = config.data; this.options = config.options;
    this.updates = 0;
    global.__charts.push(this);
  }
  update() { this.updates++; }
};
// No network in this test: every fetch never settles, so the page renders
// from whatever the test feeds it directly.
global.fetch = () => new Promise(() => {});

// MinimalRadio stand-in: records calls so the preview wiring can be asserted
// without a WebSocket or an AudioContext.
global.WebSocket = { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 };
global.__radioCalls = [];
// Models the part of MinimalRadio the preview logic actually depends on:
// isPlaying flips only AFTER the socket opens, and ws exists while connecting.
global.__holdConnecting = false;
global.MinimalRadio = class {
  constructor() { this.isPlaying = false; this.ws = null; global.__radioCalls.push(["new"]); }
  startPreview(f, m) {
    global.__radioCalls.push(["start", f, m]);
    this.currentFrequency = f;
    this.ws = { readyState: WebSocket.CONNECTING };
    if (global.__holdConnecting) return new Promise(() => {});   // never opens
    this.ws.readyState = WebSocket.OPEN;
    this.isPlaying = true;
    return Promise.resolve();
  }
  changeFrequency(f, m) { this.currentFrequency = f; global.__radioCalls.push(["tune", f, m]); }
  stopPreview() {
    this.isPlaying = false; this.ws = null;
    global.__radioCalls.push(["stop"]);
    return Promise.resolve();
  }
};

// Leaflet is absent here on purpose — initMap must degrade cleanly when the
// library did not load rather than taking the whole dashboard down with it.

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

const APP = path.join(__dirname, "static", "app.js");
let src = fs.readFileSync(APP, "utf8");

// Reach inside the IIFE so the test drives the production functions rather
// than a copy of them.
const HOOK =
  "\n  globalThis.__test = { applyState, renderConfirmedRow, renderSpotRow," +
  " renderTargets, appendTranscript, setLiveTranscript, redrawConfirmed," +
  " redrawSpots, bandClass, mapStations, tooltipHTML, countryFlag," +
  " flagHTML, panels, els, startRowAudio, stopRowAudio, buildChart, bandColour, bandClass };\n})();\n";
const patched = src.replace(/\n\}\)\(\);\s*$/, HOOK);
assert.notStrictEqual(patched, src, "could not find the IIFE close in app.js");

// This is the check that matters most: if app.js throws on load, this line
// raises and the suite fails.
eval(patched);
const T = globalThis.__test;

function stopRowAudioForTest() { T.stopRowAudio(); }

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`  ok   ${name}`);
  } catch (err) {
    failures++;
    console.log(`  FAIL ${name}\n       ${err.message}`);
  }
}

console.log("test_frontend.js");

check("app.js loads without throwing", () => {
  assert.ok(T, "test hook missing");
});

check("the chart draws 24 hours, zero-filled, stacked by band", () => {
  // Shaped exactly like /api/history: 24 buckets, most of them empty.
  const hour = 3600;
  const first = Math.floor(Date.now() / 1000 / hour) - 23;
  const buckets = [];
  for (let i = 0; i < 24; i++) {
    const b = { start: (first + i) * hour, confirmed: {}, spotted: {} };
    if (i === 21) { b.confirmed = { "20m": 7, "17m": 2 }; b.spotted = { "20m": 3 }; }
    if (i === 23) { b.confirmed = { "20m": 5, "40m": 3 }; b.spotted = { "40m": 1 }; }
    buckets.push(b);
  }
  global.__charts.length = 0;
  T.buildChart({ generated_at: Date.now() / 1000, bucket_seconds: hour,
                 buckets, bands: ["17m", "20m", "40m"] });

  assert.strictEqual(global.__charts.length, 1, "no chart created");
  const c = global.__charts[0];
  assert.strictEqual(c.data.labels.length, 24, "not a full 24-hour axis");
  // Two datasets per band, in two stacks.
  assert.strictEqual(c.data.datasets.length, 6);
  const stacks = new Set(c.data.datasets.map((d) => d.stack));
  assert.deepStrictEqual([...stacks].sort(), ["confirmed", "spotted"]);
  // Empty hours are present as zeros rather than absent.
  const conf20 = c.data.datasets.find((d) => d.label === "20m confirmed");
  assert.strictEqual(conf20.data.length, 24);
  assert.strictEqual(conf20.data[0], 0, "empty hour not zero-filled");
  assert.strictEqual(conf20.data[21], 7);
  assert.ok(c.options.scales.x.stacked && c.options.scales.y.stacked);
  assert.strictEqual(c.options.plugins.legend.display, false);
});

check("chart colours come from the table palette", () => {
  // Read from the stylesheet, not a second copy in JS — the whole point is
  // that a band is the same colour in the chart and in the tables.
  for (const band of ["20m", "40m", "80m"]) {
    const colour = T.bandColour(band);
    assert.match(colour, /^#[0-9a-f]{6}$/i, `${band} -> ${colour}`);
  }
  assert.notStrictEqual(T.bandColour("20m"), T.bandColour("40m"));

  // And the palette it reads is the one index.html declares.
  const html = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");
  const declared = [...html.matchAll(/\.band-c(\d)\s*\{\s*--band:\s*(#[0-9a-f]{6})/gi)]
    .reduce((acc, m) => (acc[m[1]] = m[2].toLowerCase(), acc), {});
  assert.strictEqual(Object.keys(declared).length, 8, "expected 8 band colours");
  for (const band of ["160m", "80m", "20m"]) {
    const idx = T.bandClass(band).replace("band-c", "");
    assert.strictEqual(T.bandColour(band).toLowerCase(), declared[idx],
                       `${band} colour differs from the stylesheet`);
  }
});

check("a second refresh updates in place, not a new chart", () => {
  const hour = 3600;
  const first = Math.floor(Date.now() / 1000 / hour) - 23;
  const buckets = Array.from({ length: 24 }, (_, i) => ({
    start: (first + i) * hour, confirmed: {}, spotted: {},
  }));
  const before = global.__charts.length;
  const updates = global.__charts[0].updates;
  T.buildChart({ bucket_seconds: hour, buckets, bands: [] });
  assert.strictEqual(global.__charts.length, before, "rebuilt the chart");
  assert.strictEqual(global.__charts[0].updates, updates + 1, "did not update");
});

check("the explain modal stacks above the map", () => {
  // Leaflet's panes and controls carry their own z-indexes and
  // .leaflet-container creates no stacking context, so they compete with the
  // modal directly. The modal shipped at 50 against Leaflet's 1000 and opened
  // underneath the map. Compared against leaflet.css rather than a hardcoded
  // number so upgrading Leaflet cannot quietly reintroduce it.
  const dir = path.join(__dirname, "static");
  const html = fs.readFileSync(path.join(dir, "index.html"), "utf8");
  const leaflet = fs.readFileSync(path.join(dir, "leaflet.css"), "utf8");

  const modal = html.match(/#explain-backdrop\s*\{[\s\S]*?z-index:\s*(\d+)/);
  assert.ok(modal, "no z-index on #explain-backdrop");

  const highest = Math.max(
    ...[...leaflet.matchAll(/z-index:\s*(\d+)/g)].map((m) => Number(m[1]))
  );
  assert.ok(
    Number(modal[1]) > highest,
    `modal z-index ${modal[1]} is not above leaflet's highest (${highest})`
  );
});

check("a full state snapshot renders", () => {
  T.applyState({
    workers: [
      {
        id: 0,
        current: { band: "20m", dial_freq: 14226000, mode: "usb", snr: 41 },
        live: null,
        signal: { snr: 41, peak: 42, threshold: 40 },
        audio_available: true,
        status: { connected: true, detail: "" },
        transcript: [
          { time: 1, band: "20m", freq: 14226000, completed: true, text: "hello" },
        ],
      },
    ],
    confirmed: [
      {
        key: "G0VIM|14226000", normalised: "G0VIM", band: "20m",
        frequency: 14226000, mode: "usb", name: "Malcolm", country: "England",
        latitude: 52.2, longitude: -0.9, timestamp: 1, first_seen: 1,
        country_code: "GB", hit_count: 2, spotted_at: 2,
      },
    ],
    spots: [
      { key: "G0VIM|14226000", time: 2, callsign: "G0VIM", band: "20m",
        country_code: "GB", country: "England",
        freq: 14226000, comment: "[Voice] Malcolm" },
    ],
    targets: [
      { band: "20m", dial_freq: 14226000, mode: "usb", snr: 41,
        confidence: 0.8, visits: 1, callsigns_found: 1 },
    ],
    stats: { dwells: 1, unique_confirmed: 1 },
  });
});

check("band colours agree across surfaces", () => {
  assert.strictEqual(T.bandClass("20m"), T.bandClass("20m"));
  assert.notStrictEqual(T.bandClass("20m"), T.bandClass("40m"));
  assert.strictEqual(T.bandClass(""), "");
});

check("country codes become flags", () => {
  // Regional Indicator pair for GB.
  assert.strictEqual(T.countryFlag("GB"), "\u{1F1EC}\u{1F1E7}");
  assert.strictEqual(T.countryFlag("gb"), "\u{1F1EC}\u{1F1E7}");
  // Anything that is not two letters yields nothing rather than mojibake.
  for (const bad of ["", null, undefined, "G", "GBR", "G1", "12"]) {
    assert.strictEqual(T.countryFlag(bad), "", JSON.stringify(bad));
  }
});

check("flag markup carries the country as a tooltip", () => {
  const html = T.flagHTML("GB", 'England "home"');
  assert.ok(html.includes('class="flag"'));
  // The country name reaches an attribute, so quotes must be escaped or it
  // breaks out of the title.
  assert.ok(!/title="[^"]*"[^>]*"/.test(html), "unescaped quote in title");
  assert.strictEqual(T.flagHTML("", "Nowhere"), "");
});

check("flags render beside callsigns in both tables", () => {
  const conf = byId["confirmed-filter"];
  conf.value = "";
  T.redrawConfirmed();
  T.redrawSpots();
  assert.ok(
    T.els.confirmedBody.innerHTML.includes("flag"),
    "no flag in the confirmed table"
  );
  // The spot carries its own country_code from the backend; the frontend
  // does not look it up or derive it.
  assert.ok(
    T.els.spotsBody.innerHTML.includes("flag"),
    "no flag in the spots table"
  );
});

check("map tooltip leads with the flag", () => {
  const s = T.mapStations().get("G0VIM");
  const tip = T.tooltipHTML(s);
  assert.ok(tip.indexOf("flag") < tip.indexOf("G0VIM"), "flag not left of callsign");
});

check("map groups rows by callsign", () => {
  const stations = T.mapStations();
  const s = stations.get("G0VIM");
  assert.ok(s, "G0VIM not placed");
  assert.strictEqual(s.spotted, true, "spotted flag not set");
  assert.ok(T.tooltipHTML(s).includes("G0VIM"));
});

check("map degrades without Leaflet", () => {
  // initMap already ran at load with L undefined; nothing should have blown
  // up and the rest of the dashboard must still work.
  assert.strictEqual(typeof global.L, "undefined");
  T.redrawConfirmed();
  T.redrawSpots();
});

check("transcript scrollback is capped per panel", () => {
  const p = T.panels.get(0);
  assert.ok(p, "panel 0 missing");
  for (let i = 0; i < 260; i++) {
    T.appendTranscript(
      { time: i, band: "20m", freq: 14226000, completed: true, text: "line " + i },
      p
    );
  }
  const finals = p.transcript.children.filter((c) => c.className.includes("final"));
  assert.ok(finals.length <= 200, `kept ${finals.length} lines, expected <= 200`);
});

check("filters narrow the tables", () => {
  byId["confirmed-filter"].value = "zzzz";
  T.redrawConfirmed();
  assert.ok(byId["confirmed-filter"].classList.contains("active"));
  byId["confirmed-filter"].value = "";
  T.redrawConfirmed();
});

check("panels collapse and expand from the title bar", () => {
  const sec = byId["confirmed-panel"];
  sec.id = "confirmed-panel";
  sec.className = "panel";
  const btn = new El("button");
  btn.className = "panel-toggle";
  sec.appendChild(btn);
  // querySelector on the stub returns a throwaway, so hand back the real
  // button — setCollapsed updates its aria state.
  sec.querySelector = () => btn;

  assert.ok(!sec.classList.contains("collapsed"), "starts expanded");
  global.__fireDocument("click", { target: btn });
  assert.ok(sec.classList.contains("collapsed"), "did not collapse");
  global.__fireDocument("click", { target: btn });
  assert.ok(!sec.classList.contains("collapsed"), "did not expand again");
});

check("collapsed panels are remembered", () => {
  const sec = byId["spots-panel"];
  sec.id = "spots-panel";
  sec.className = "panel";
  const btn = new El("button");
  btn.className = "panel-toggle";
  sec.appendChild(btn);
  sec.querySelector = () => btn;

  global.__fireDocument("click", { target: btn });
  const saved = JSON.parse(global.__storage["voiceskimmer.collapsed"] || "[]");
  assert.ok(saved.includes("spots-panel"), `not persisted: ${JSON.stringify(saved)}`);

  global.__fireDocument("click", { target: btn });
  const after = JSON.parse(global.__storage["voiceskimmer.collapsed"] || "[]");
  assert.ok(!after.includes("spots-panel"), "expanding did not clear it");
});

check("a click elsewhere does not collapse anything", () => {
  const sec = byId["targets-panel"];
  sec.id = "targets-panel";
  sec.className = "panel";
  const notAToggle = new El("span");
  sec.appendChild(notAToggle);
  global.__fireDocument("click", { target: notAToggle });
  assert.ok(!sec.classList.contains("collapsed"));
});

check("clicking a table row previews that frequency and mode", () => {
  global.__radioCalls.length = 0;
  const rows = T.els.confirmedBody.querySelectorAll("tr");
  assert.ok(rows.length, "no rows carrying data-freq");
  T.els.confirmedBody.fire("click", { target: rows[0] });
  const start = global.__radioCalls.find((c) => c[0] === "start");
  assert.ok(start, `no startPreview: ${JSON.stringify(global.__radioCalls)}`);
  assert.strictEqual(start[1], 14226000);
  assert.strictEqual(start[2], "usb", "should use the recorded mode");
});

check("activity rows preview too", () => {
  stopRowAudioForTest();
  global.__radioCalls.length = 0;
  const rows = T.els.targetsBody.querySelectorAll("tr");
  assert.ok(rows.length, "no activity rows carrying data-freq");
  T.els.targetsBody.fire("click", { target: rows[0] });
  const start = global.__radioCalls.find((c) => c[0] === "start" || c[0] === "tune");
  assert.ok(start, `no preview started: ${JSON.stringify(global.__radioCalls)}`);
  assert.strictEqual(start[1], 14226000);
  assert.strictEqual(byId["targets-stop"].disabled, false, "activity Stop not armed");
  // One shared stream: the other panels' buttons arm as well.
  assert.strictEqual(byId["confirmed-stop"].disabled, false);
  stopRowAudioForTest();
});

check("switching rows retunes instead of reconnecting", () => {
  stopRowAudioForTest();
  const rows = T.els.confirmedBody.querySelectorAll("tr");
  const targets = T.els.targetsBody.querySelectorAll("tr");
  global.__radioCalls.length = 0;
  T.els.confirmedBody.fire("click", { target: rows[0] });      // first: connect
  T.els.targetsBody.fire("click", { target: targets[0] });     // second: retune
  const kinds = global.__radioCalls.filter((c) => c[0] !== "new").map((c) => c[0]);
  assert.deepStrictEqual(kinds, ["start", "tune"],
    `expected one start then a tune, got ${JSON.stringify(global.__radioCalls)}`);
  stopRowAudioForTest();
});

check("a click while still connecting retunes, not a second socket", () => {
  // radio.isPlaying is false throughout here — the old check read that and
  // would have opened a second WebSocket over the one in flight.
  stopRowAudioForTest();
  global.__holdConnecting = true;
  try {
    const rows = T.els.confirmedBody.querySelectorAll("tr");
    const targets = T.els.targetsBody.querySelectorAll("tr");
    global.__radioCalls.length = 0;
    T.els.confirmedBody.fire("click", { target: rows[0] });
    T.els.targetsBody.fire("click", { target: targets[0] });
    const starts = global.__radioCalls.filter((c) => c[0] === "start");
    const tunes = global.__radioCalls.filter((c) => c[0] === "tune");
    assert.strictEqual(starts.length, 1, "opened more than one session");
    assert.strictEqual(tunes.length, 1, "did not retune the pending session");
    assert.strictEqual(tunes[0][1], 14226000);
  } finally {
    global.__holdConnecting = false;
    stopRowAudioForTest();
  }
});

check("the Stop button in the title bar ends it", () => {
  const rows = T.els.confirmedBody.querySelectorAll("tr");
  T.els.confirmedBody.fire("click", { target: rows[0] });   // start something
  global.__radioCalls.length = 0;
  assert.strictEqual(byId["confirmed-stop"].disabled, false, "Stop not enabled while playing");
  byId["confirmed-stop"].fire("click");
  assert.ok(global.__radioCalls.some((c) => c[0] === "stop"), "no stopPreview");
  assert.strictEqual(byId["confirmed-stop"].disabled, true, "Stop still enabled when idle");
});

check("either table's Stop ends the shared stream", () => {
  const rows = T.els.spotsBody.querySelectorAll("tr");
  assert.ok(rows.length, "no spot rows carrying data-freq");
  T.els.spotsBody.fire("click", { target: rows[0] });
  assert.strictEqual(byId["confirmed-stop"].disabled, false,
                     "the other table's Stop should also arm");
  global.__radioCalls.length = 0;
  byId["spots-stop"].fire("click");
  assert.ok(global.__radioCalls.some((c) => c[0] === "stop"));
});

check("row preview and transcript listening are mutually exclusive", () => {
  const p = T.panels.get(0);
  assert.ok(p, "panel 0 missing");

  // Transcript first, then a row: the transcript must be stopped.
  p.listening = true;
  const rows = T.els.confirmedBody.querySelectorAll("tr");
  T.els.confirmedBody.fire("click", { target: rows[0] });
  assert.strictEqual(p.listening, false, "row preview did not stop the transcript");

  // Row first, then the transcript: the row preview must be stopped.
  global.__radioCalls.length = 0;
  p.listen.fire("click");                       // toggles listening on
  assert.ok(global.__radioCalls.some((c) => c[0] === "stop"),
            "listening did not stop the row preview");
  assert.strictEqual(byId["confirmed-stop"].disabled, true);
});

check("clicking the playing row again stops it", () => {
  global.__radioCalls.length = 0;
  const rows = T.els.confirmedBody.querySelectorAll("tr");
  T.els.confirmedBody.fire("click", { target: rows[0] });   // start
  T.els.confirmedBody.fire("click", { target: rows[0] });   // same row -> stop
  assert.ok(global.__radioCalls.some((c) => c[0] === "stop"));
  assert.strictEqual(byId["confirmed-stop"].disabled, true);
});

check("clicking a transcript line asks for an explanation", () => {
  const line = new El("div");
  line.className = "final";
  line.dataset.text = "Mike Zero Alpha Bravo Charlie";
  byId["transcripts"].appendChild(line);
  byId["transcripts"].fire("click", { target: line });
  assert.ok(byId["explain-backdrop"].classList.contains("open"));
});

console.log(
  failures === 0
    ? "\nall frontend checks passed"
    : `\n${failures} frontend check(s) failed`
);
process.exit(failures === 0 ? 0 : 1);
