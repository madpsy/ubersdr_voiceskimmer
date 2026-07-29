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
  querySelectorAll() {
    return [];
  }
  closest(sel) {
    const want = sel.replace(/^\./, "");
    let n = this;
    while (n) {
      if (n._classes.has(want)) return n;
      n = n.parentNode;
    }
    return null;
  }
  setAttribute() {}
  removeAttribute() {}
  load() {}
  pause() {}
}

const byId = Object.create(null);
const ID_LIST = [
  "uptime", "stats", "transcripts", "receiver", "explain-backdrop",
  "explain-body", "explain-close", "confirmed-filter", "spots-filter",
  "map", "map-note", "layer-confirmed", "layer-spotted",
];
for (const id of ID_LIST) byId[id] = new El("div");
byId["layer-confirmed"].checked = true;
byId["layer-spotted"].checked = true;

const docHandlers = {};
global.document = {
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
// No network in this test: every fetch never settles, so the page renders
// from whatever the test feeds it directly.
global.fetch = () => new Promise(() => {});

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
  " flagHTML, panels, els };\n})();\n";
const patched = src.replace(/\n\}\)\(\);\s*$/, HOOK);
assert.notStrictEqual(patched, src, "could not find the IIFE close in app.js");

// This is the check that matters most: if app.js throws on load, this line
// raises and the suite fails.
eval(patched);
const T = globalThis.__test;

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
