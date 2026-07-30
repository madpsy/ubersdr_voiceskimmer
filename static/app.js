// app.js — live dashboard for the voice callsign skimmer.
//
// Loads the initial snapshot from /api/state, then subscribes to /api/events
// (SSE) for incremental updates. All URLs are relative — <base> in
// index.html (set from X-Forwarded-Prefix server-side) makes them resolve
// correctly whether this page is opened directly or through UberSDR's addon
// proxy.

(() => {
  "use strict";

  // Per panel, not in total — with --parallel 2 each worker keeps its own
  // scrollback. Matches the server's per-worker deque so a reload shows the
  // same history the live view was holding.
  const TRANSCRIPT_MAX_LINES = 200;

  const els = {
    uptime: document.getElementById("uptime"),
    stats: document.getElementById("stats"),
    transcripts: document.getElementById("transcripts"),
    confirmedBody: document.querySelector("#confirmed-table tbody"),
    spotsBody: document.querySelector("#spots-table tbody"),
    targetsBody: document.querySelector("#targets-table tbody"),
    receiver: document.getElementById("receiver"),
    explainBackdrop: document.getElementById("explain-backdrop"),
    explainBody: document.getElementById("explain-body"),
    explainClose: document.getElementById("explain-close"),
    settingsBtn: document.getElementById("settings-btn"),
    settingsBackdrop: document.getElementById("settings-backdrop"),
    settingsBody: document.getElementById("settings-body"),
    settingsClose: document.getElementById("settings-close"),
    confirmedFilter: document.getElementById("confirmed-filter"),
    spotsFilter: document.getElementById("spots-filter"),
    confirmedStop: document.getElementById("confirmed-stop"),
    spotsStop: document.getElementById("spots-stop"),
    targetsStop: document.getElementById("targets-stop"),
  };

  // Client-side copies, keyed the same way the server keeps them, so each
  // incremental event only has to touch one row instead of asking the
  // server for a full snapshot again.
  const confirmed = new Map();   // "callsign|freq_bucket" -> detection dict
  // Spots are held here as well as rendered, because filtering has to be able
  // to bring a hidden row back — a table built by appending rows as they
  // arrive has nowhere to recover them from.
  const spots = [];              // newest first, capped at SPOTS_MAX_ROWS
  const SPOTS_MAX_ROWS = 100;
  let startTime = Date.now() / 1000;

  // -- Row filtering --------------------------------------------------------

  // Matched against the same values the row displays, so what you see is what
  // you filter. Frequency is included both formatted and raw so "14.226",
  // "14226" and "14226000" all find the same row.
  function haystack(parts) {
    return parts.filter((p) => p != null && p !== "").join(" ").toLowerCase();
  }

  function freqTerms(hz) {
    return hz ? [fmtFreq(hz), String(hz), String(hz / 1000)] : [];
  }

  // Space-separated terms all have to match, so "g0 england" narrows rather
  // than widening — the usual expectation for a filter box.
  function matches(hay, query) {
    if (!query) return true;
    return query.toLowerCase().split(/\s+/).filter(Boolean)
      .every((term) => hay.includes(term));
  }

  function filterQuery(input) {
    const q = input ? input.value.trim() : "";
    if (input) input.classList.toggle("active", q !== "");
    return q;
  }

  // -- Formatting -----------------------------------------------------------

  function fmtFreq(hz) {
    if (!hz) return "—";
    return (hz / 1e6).toFixed(3) + " MHz";
  }

  function fmtTime(unixSeconds) {
    if (!unixSeconds) return "—";
    const d = new Date(unixSeconds * 1000);
    return d.toLocaleTimeString([], { hour12: false });
  }

  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  // -- Band colours ---------------------------------------------------------

  // Wavelength order, so neighbouring bands never share a colour and the
  // assignment is self-evident rather than arbitrary. The bands the server
  // never reports voice activity for (2200m, 630m, 30m — EXCLUDED_BANDS in
  // activity.py) are left out so they do not consume a colour.
  //
  // Only eight colours, so this wraps: 160m and 10m share one, 80m and 6m,
  // and so on. Deliberate — a pairing that far apart in wavelength is
  // unlikely to be open at the same time, and the band is written on the row
  // regardless. The colour is for grouping at a glance, not identification.
  const BAND_ORDER = [
    "160m", "80m", "60m", "40m", "20m", "17m",
    "15m", "12m", "10m", "6m", "4m", "2m", "70cm",
  ];
  const BAND_COLOURS = 8;

  function bandClass(band) {
    if (!band) return "";
    const key = String(band).toLowerCase();
    let idx = BAND_ORDER.indexOf(key);
    if (idx < 0) {
      // A band this list does not know — still give it a stable colour
      // rather than leaving the row unmarked among coloured ones.
      let h = 0;
      for (let i = 0; i < key.length; i++) {
        h = (h * 31 + key.charCodeAt(i)) >>> 0;
      }
      idx = h;
    }
    return "band-c" + (idx % BAND_COLOURS);
  }

  // esc() is only safe for text content: innerHTML leaves quotes alone, so a
  // value interpolated into an attribute needs them handled too or a QRZ name
  // containing a double quote breaks out of it.
  function escAttr(s) {
    return esc(s).replace(/"/g, "&quot;");
  }

  // -- Country flags --------------------------------------------------------

  // An ISO 3166-1 alpha-2 code as a flag: the two Regional Indicator symbols
  // for its letters. Same construction as ubersdr_dxcluster's countryFlag.
  //
  // The code comes from the CTY block of the lookup response, never from
  // matching the country NAME — DXCC entity names are not ISO names, and the
  // stations this hears most are the worst cases ("England", "Scotland" and
  // "Wales" are all GB, and none of them is an ISO country).
  function countryFlag(code) {
    if (!code || code.length !== 2 || !/^[a-z]{2}$/i.test(code)) return "";
    const base = 0x1f1e6 - 65;
    const up = code.toUpperCase();
    return (
      String.fromCodePoint(up.charCodeAt(0) + base) +
      String.fromCodePoint(up.charCodeAt(1) + base)
    );
  }

  // Rendered with the country name as a tooltip so the flag is not the only
  // way to read it — several are hard to tell apart at 12px.
  function flagHTML(code, country) {
    const flag = countryFlag(code);
    if (!flag) return "";
    const label = country || code;
    return `<span class="flag" title="${escAttr(label)}">${flag}</span>`;
  }

  // QRZ names run from a first name to a full legal name to a club-station
  // description, and a long one stretches the column until the rest of the
  // table is pushed off screen. Truncated rather than wrapped so every row
  // stays one line high, with the full value on hover — and only then, so
  // short names do not carry a pointless tooltip.
  const NAME_MAX_CHARS = 25;

  function nameCell(value) {
    const s = value == null ? "" : String(value);
    if (s.length <= NAME_MAX_CHARS) return `<td class="name">${esc(s)}</td>`;
    return (
      `<td class="name" title="${escAttr(s)}">` +
      `${esc(s.slice(0, NAME_MAX_CHARS))}…</td>`
    );
  }

  // -- Collapsible panels ---------------------------------------------------

  // Which panels the user has collapsed, remembered across reloads — a panel
  // that reopened itself on every refresh would not be worth collapsing.
  // Keyed by element id; a panel without one still collapses, it just does
  // not persist.
  const COLLAPSE_KEY = "voiceskimmer.collapsed";
  const collapsed = new Set();
  try {
    JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]").forEach((id) =>
      collapsed.add(id)
    );
  } catch (err) {
    /* private browsing, disabled storage, corrupt value — start expanded */
  }

  function setCollapsed(section, on) {
    section.classList.toggle("collapsed", on);
    const btn = section.querySelector(".panel-toggle");
    if (btn) {
      btn.setAttribute("aria-expanded", on ? "false" : "true");
      btn.title = on ? "Expand" : "Collapse";
    }
    if (!section.id) return;
    if (on) collapsed.add(section.id);
    else collapsed.delete(section.id);
    try {
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsed]));
    } catch (err) {
      /* not being able to remember is not worth breaking the click over */
    }
    if (!on) onExpanded(section);
  }

  // A collapsed panel is display:none, so everything inside it measures zero
  // while hidden. Anything that reads its own size has to be told once it is
  // back on screen.
  //
  // Deferred to the next frame so the layout has actually been recomputed
  // rather than measured through the class change.
  function onExpanded(section) {
    requestAnimationFrame(() => {
      // Transcripts: while hidden, scrollTop/clientHeight/scrollHeight are all
      // 0, so appendTranscript's "was the reader at the bottom" check reads
      // true and pins scrollTop to 0. Every line that arrived while collapsed
      // therefore left it at the TOP, and expanding showed the oldest text
      // with the latest off-screen below.
      //
      // Only the transcripts: the tables sort newest-first, so the top is
      // already the latest there and scrolling them down would hide it.
      if (section.classList.contains("transcript-panel")) {
        const scroll = section.querySelector(".scroll");
        if (scroll) scroll.scrollTop = scroll.scrollHeight;
      }

      // Leaflet caches the container size it was last laid out at. Sized while
      // hidden that is 0x0, and it draws into a collapsed corner until told
      // otherwise.
      if (map && section.id === "map-panel") map.invalidateSize();

      // Chart.js resizes off a ResizeObserver, which does fire on becoming
      // visible — but only for a canvas it already knows about, and it costs
      // nothing to be certain.
      for (const c of [chart, topChart]) {
        if (c && section.contains(c.canvas)) c.resize();
      }
    });
  }

  function restoreCollapsed(section) {
    if (section && section.id && collapsed.has(section.id)) {
      setCollapsed(section, true);
    }
  }

  // Delegated, so the transcript panels — built later, and rebuilt whenever
  // the worker count changes — need no wiring of their own.
  document.addEventListener("click", (e) => {
    const btn =
      e.target && e.target.closest && e.target.closest(".panel-toggle");
    if (!btn) return;
    const section = btn.closest(".panel");
    if (section) setCollapsed(section, !section.classList.contains("collapsed"));
  });

  // The panels present in index.html. Only needed to restore persisted state:
  // a panel missing from this list still collapses on click, it just opens
  // again after a reload.
  function restoreStaticPanels() {
    for (const id of ["map-panel", "chart-panel", "top-panel",
                      "confirmed-panel", "spots-panel", "targets-panel"]) {
      restoreCollapsed(document.getElementById(id));
    }
  }

  // -- Per-worker panels -----------------------------------------------------

  // Each scanning worker is an independent session on its own frequency, so
  // it gets its own transcript, frequency readout, SNR and Listen button.
  // With one worker this is simply the single panel it always was.
  const panels = new Map();   // worker id -> panel refs

  function buildPanels(workers) {
    if (panels.size === workers.length) return;   // already built
    panels.clear();
    els.transcripts.innerHTML = "";
    const many = workers.length > 1;

    for (const w of workers) {
      const sec = document.createElement("section");
      sec.className = "panel transcript-panel";
      // Stable id so a collapsed panel stays collapsed across a reload.
      sec.id = `transcript-panel-${w.id}`;
      sec.innerHTML =
        `<h2 class="tp-head">` +
        `<button class="panel-toggle" aria-expanded="true" title="Collapse"></button>` +
        `<span class="tp-dot" title="Whisper connection"></span>` +
        `<span class="tp-activity" title="Incoming transcript activity"></span>` +
        `<span class="tp-label">${many ? `Transcript ${w.id + 1}` : "Live transcript"}</span>` +
        `<span class="tp-current"><span class="empty">waiting for a target…</span></span>` +
        `<span class="tp-signal" title="Live SNR from the audio stream. A peak above the threshold keeps this worker on the frequency."></span>` +
        `<button class="tp-listen" disabled title="Hear what this worker is tuned to — follows it as it hops">🔈 Listen</button>` +
        `</h2>` +
        `<div class="scroll"><div class="transcript"></div></div>` +
        `<audio hidden></audio>`;
      els.transcripts.appendChild(sec);

      const p = {
        id: w.id,
        current: sec.querySelector(".tp-current"),
        signal: sec.querySelector(".tp-signal"),
        listen: sec.querySelector(".tp-listen"),
        audio: sec.querySelector("audio"),
        dot: sec.querySelector(".tp-dot"),
        activity: sec.querySelector(".tp-activity"),
        activityTimer: null,
        transcript: sec.querySelector(".transcript"),
        scroll: sec.querySelector(".scroll"),
        liveLine: null,
        lastFinalText: null,
        lastFreq: null,
        listening: false,
      };
      p.listen.addEventListener("click", () => setListening(p, !p.listening));
      p.audio.addEventListener("error", () => {
        if (p.listening) setListening(p, false);
      });
      panels.set(w.id, p);
      restoreCollapsed(sec);
    }
  }

  const panelFor = (e) => panels.get(e && e.worker != null ? e.worker : 0);

  // Green once Whisper is attached and transcribing, red when the attach
  // failed or the session dropped, grey while still coming up. The failure is
  // otherwise silent — on a server that does not trust this client as a
  // container, whisper.max_users defaults to 2, so a second worker is
  // routinely refused while the first runs on happily.
  function renderStatus(st) {
    const p = panelFor(st);
    if (!p || !st) return;
    const up = st.connected === true, down = st.connected === false;
    p.dot.classList.toggle("up", up);
    p.dot.classList.toggle("down", down);
    p.dot.title = st.detail || (up ? "transcribing" : down ? "not connected" : "connecting");
  }

  // Briefly lights up next to the connection dot so a collapsed panel still
  // shows whether transcript segments are actually arriving, not just that
  // Whisper is connected.
  function flashActivity(p) {
    if (!p || !p.activity) return;
    p.activity.classList.remove("flash");
    // Force reflow so re-adding the class restarts the CSS animation.
    void p.activity.offsetWidth;
    p.activity.classList.add("flash");
    clearTimeout(p.activityTimer);
    p.activityTimer = setTimeout(() => p.activity.classList.remove("flash"), 900);
  }

  function renderCurrent(current) {
    const p = panelFor(current);
    if (!p) return;
    if (!current) {
      p.current.innerHTML = '<span class="empty">waiting for a target…</span>';
      return;
    }
    const dx = current.dx_callsign
      ? ` <span class="dx">★ ${esc(current.dx_callsign)}</span>`
      : "";
    p.current.innerHTML =
      `${esc(current.band)} <span class="freq">${fmtFreq(current.dial_freq)}</span> ` +
      `${esc((current.mode || "").toUpperCase())}${dx}`;
  }

  // -- Which receiver is this? ----------------------------------------------

  // /api/description belongs to UberSDR itself, not this addon, so it needs a
  // root-relative URL. A bare "api/description" would resolve against the
  // <base> tag and hit this addon under its proxy prefix instead. Only
  // reachable when served through the proxy — opened directly on the addon's
  // own port there is no UberSDR at the root, so the header is simply left
  // empty rather than showing an error.
  function loadReceiver() {
    fetch("/api/description")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        const rx = (d && d.receiver) || {};
        const bits = [rx.name, rx.location].filter(Boolean).map(esc);
        if (!rx.callsign && !bits.length) return;
        els.receiver.innerHTML =
          (rx.callsign ? `<span class="call">${esc(rx.callsign)}</span>` : "") +
          (rx.callsign && bits.length ? '<span class="sep">·</span>' : "") +
          bits.join('<span class="sep">·</span>');
        if (rx.callsign) document.title = `${rx.callsign} — Voice Skimmer`;
        const gps = rx.gps || {};
        setInstanceLocation(
          gps.lat, gps.lon,
          [rx.callsign, rx.location].filter(Boolean).join(" · ")
        );
      })
      .catch(() => {});   // not fatal; the dashboard is fine without it
  }
  // Called from the wiring section at the end of the file, not here: the map
  // state these touch is declared with `let` further down, and a `let` is in
  // its temporal dead zone until execution reaches it — calling up here threw
  // "can't access lexical declaration 'map' before initialization".

  // -- Live signal ----------------------------------------------------------

  // Measured from the audio frame headers rather than inferred from the
  // transcript, so it reads correctly even on a frequency Whisper produces
  // nothing for. The bar is scaled around the threshold rather than 0-100:
  // the interesting range is a few dB either side of it.
  function renderSignal(sig) {
    const p = panelFor(sig);
    if (!p) return;
    if (!sig || typeof sig.snr !== "number") {
      p.signal.innerHTML = "";
      return;
    }
    const thr = sig.threshold ?? 40;
    const lo = thr - 10, hi = thr + 10;
    const pct = Math.max(0, Math.min(100, ((sig.snr - lo) / (hi - lo)) * 100));
    const peak = typeof sig.peak === "number" ? sig.peak : sig.snr;

    // Colour the live reading by the instantaneous value: orange while it is
    // below the threshold, green once it reaches it. The peak is tracked
    // separately because that is what the dwell decision uses — it stays
    // green once cleared even as the live value dips between overs.
    const live = sig.snr >= thr;
    p.signal.classList.toggle("active", live);
    p.signal.classList.toggle("low", !live);
    p.signal.innerHTML =
      `SNR <span class="val">${sig.snr.toFixed(1)}</span>` +
      `<span class="bar"><i style="width:${pct.toFixed(0)}%"></i></span>` +
      `peak <span class="peak${peak >= thr ? " cleared" : ""}">` +
      `${peak.toFixed(1)}</span> / ${thr.toFixed(0)} dB`;
  }

  // -- Stats bar --------------------------------------------------------------

  function renderStats(stats) {
    if (!stats) return;
    const parts = [
      ["Dwells", stats.dwells],
      ["Segments", stats.segments],
      ["Candidates", stats.candidates],
      ["Validated", stats.validated],
      ["Rejected", stats.rejected],
      ["Unique confirmed", stats.unique_confirmed],
      ["DX agreements", stats.dx_agreements],
    ];
    els.stats.innerHTML = parts
      .map(([label, value]) => `${label}: <b>${value ?? 0}</b>`)
      .join("<span style=\"color:var(--border)\"> · </span>");
    if (typeof stats.uptime === "number") {
      startTime = Date.now() / 1000 - stats.uptime;
    }
  }

  function tickUptime() {
    els.uptime.textContent = "up " + fmtDuration(Date.now() / 1000 - startTime);
  }
  setInterval(tickUptime, 1000);

  // -- Live transcript ----------------------------------------------------

  // A completed segment is final and gets its own permanent line. An
  // incomplete one is the utterance currently being refined — WhisperLive
  // re-sends it as it grows and only one is ever in flight — so it lives in a
  // single trailing line that is replaced, not appended. This mirrors
  // static/extensions/whisper/main.js (transcript[] vs lastSegment); appending
  // each refinement instead renders one utterance as a column of near-
  // identical lines.
  function scrolledToBottom(p) {
    return (
      p.scroll.scrollTop + p.scroll.clientHeight >= p.scroll.scrollHeight - 24
    );
  }

  function lineHTML(entry, marker) {
    const where = entry.band ? `${entry.band} ${fmtFreq(entry.freq)}` : "";
    return (
      `<span class="meta">${fmtTime(entry.time)} ${esc(where)}</span> ` +
      `<span class="marker">${marker}</span> ${esc(entry.text)}`
    );
  }

  function appendTranscript(entry, panel) {
    // The panel is passed explicitly when replaying a snapshot, where the
    // caller already knows which worker's list it is walking. Live events
    // carry the worker on the entry itself.
    const p = panel || panelFor(entry);
    if (!p) return;

    // Never render the same text twice in a row. The backend suppresses the
    // known cause — audio in flight across a frequency hop arriving again
    // once reset_transcript clears the server's duplicate suppression — but
    // a repeated line is pure noise however it arises, and reading the same
    // sentence twice under two different frequencies actively misleads about
    // where it was heard.
    if (p.lastFinalText === entry.text) return;
    p.lastFinalText = entry.text;

    const atBottom = scrolledToBottom(p);

    // A visible break wherever the worker hopped frequency, so a wall of
    // text doesn't read as continuous speech across an unrelated retune.
    // Skipped on the very first line (lastFreq still null) — nothing to
    // separate it from yet.
    if (
      typeof entry.freq === "number" &&
      p.lastFreq !== null &&
      entry.freq !== p.lastFreq
    ) {
      const divider = document.createElement("div");
      divider.className = "freq-divider";
      p.transcript.insertBefore(divider, p.liveLine);
    }
    p.lastFreq = entry.freq;

    const line = document.createElement("div");
    line.className = ("final " + bandClass(entry.band)).trim();
    line.innerHTML = lineHTML(entry, "✓");
    // The text is carried on the element rather than looked up later: lines
    // are evicted as the transcript scrolls, and the click has to explain
    // exactly what was rendered here.
    line.dataset.text = entry.text;
    line.title = "Click to see what the extractor made of this line";
    // Keep the live line last so the in-progress text stays at the bottom.
    p.transcript.insertBefore(line, p.liveLine);

    while (p.transcript.children.length > TRANSCRIPT_MAX_LINES) {
      const first = p.transcript.firstChild;
      if (first === p.liveLine) break;
      p.transcript.removeChild(first);
    }
    if (atBottom) p.scroll.scrollTop = p.scroll.scrollHeight;
  }

  function setLiveTranscript(entry, panel) {
    const p = panel || panelFor(entry);
    if (!p) return;
    const atBottom = scrolledToBottom(p);
    if (!entry) {
      if (p.liveLine) {
        p.liveLine.remove();
        p.liveLine = null;
      }
      return;
    }
    if (!p.liveLine) {
      p.liveLine = document.createElement("div");
      p.transcript.appendChild(p.liveLine);
    }
    // Reassigned every update, not just at creation: the worker hops while
    // one live line is on screen, so its band can change under it.
    p.liveLine.className = ("partial " + bandClass(entry.band)).trim();
    p.liveLine.innerHTML = lineHTML(entry, "…");
    if (atBottom) p.scroll.scrollTop = p.scroll.scrollHeight;
  }

  // -- Map ------------------------------------------------------------------

  // One marker per CALLSIGN, not per confirmed row: the same station heard on
  // two bands is one place on the earth, and stacking two markers on the same
  // coordinates would hide one behind the other. The bands and frequencies it
  // was heard on go in the tooltip instead.
  //
  // A spotted callsign is necessarily also a confirmed one, so the two layers
  // partition rather than overlap — a marker belongs to "Spotted" once any of
  // its sightings has gone to the cluster, and to "Confirmed" until then.
  // Both on shows everything exactly once; Confirmed alone hides what has
  // already been spotted; Spotted alone answers "what did I actually submit".

  let map = null;
  let layerConfirmed = null;
  let layerSpotted = null;
  let instanceMarker = null;
  let mapRedrawQueued = false;

  function initMap() {
    if (map || typeof L === "undefined" || !document.getElementById("map")) return;
    map = L.map("map", {
      worldCopyJump: true,          // dragging past the antimeridian keeps markers
      zoomControl: true,
    }).setView([20, 0], 1);

    L.tileLayer(
      "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
      {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' +
          ' &copy; <a href="https://carto.com/attributions">CARTO</a>',
        maxZoom: 18,
      }
    ).addTo(map);

    layerConfirmed = L.layerGroup().addTo(map);
    layerSpotted = L.layerGroup().addTo(map);

    const bind = (input, layer) => {
      if (!input) return;
      input.addEventListener("change", () => {
        if (input.checked) layer.addTo(map);
        else map.removeLayer(layer);
        updateMapNote();
      });
    };
    bind(document.getElementById("layer-confirmed"), layerConfirmed);
    bind(document.getElementById("layer-spotted"), layerSpotted);
  }

  // Groups confirmed rows by callsign. Coordinates come from QRZ via the
  // scanner; plenty of records carry none, and those stations simply have no
  // marker — reported in the note under the map so their absence is not
  // mistaken for a bug.
  function mapStations() {
    const byCall = new Map();
    for (const d of confirmed.values()) {
      if (typeof d.latitude !== "number" || typeof d.longitude !== "number") continue;
      let s = byCall.get(d.normalised);
      if (!s) {
        s = {
          call: d.normalised, lat: d.latitude, lon: d.longitude,
          name: d.name || "", country: d.country || "",
          countryCode: d.country_code || "",
          spotted: false, rows: [],
        };
        byCall.set(d.normalised, s);
      }
      if (d.spotted_at) s.spotted = true;
      s.rows.push({
        band: d.band, freq: d.frequency,
        spotted_at: d.spotted_at || null, last: d.timestamp,
      });
    }
    return byCall;
  }

  function tooltipHTML(s) {
    const who = [s.name, s.country].filter(Boolean).join(", ");
    const rows = s.rows
      .slice()
      .sort((a, b) => (b.last || 0) - (a.last || 0))
      .map((r) => {
        const what = r.spotted_at
          ? `<span class="sp">spotted ${fmtTime(r.spotted_at)}</span>`
          : '<span class="cf">confirmed</span>';
        return `<div>${esc(r.band || "")} ${fmtFreq(r.freq)} · ${what}</div>`;
      })
      .join("");
    return (
      `<div class="map-tip">${flagHTML(s.countryCode, s.country)}` +
      `<span class="call">${esc(s.call)}</span>` +
      (who ? ` <span class="who">${esc(who)}</span>` : "") +
      `<div class="rows">${rows}</div></div>`
    );
  }

  function redrawMap() {
    if (!map) return;
    layerConfirmed.clearLayers();
    layerSpotted.clearLayers();

    for (const s of mapStations().values()) {
      const marker = L.circleMarker([s.lat, s.lon], {
        radius: 5,
        weight: 2,
        color: s.spotted ? "#e3b341" : "#58a6ff",
        fillColor: s.spotted ? "#e3b341" : "#58a6ff",
        fillOpacity: 0.55,
      });
      marker.bindTooltip(tooltipHTML(s), { direction: "top", opacity: 1 });
      marker.addTo(s.spotted ? layerSpotted : layerConfirmed);
    }
    updateMapNote();
  }

  // Coalesced: replaying a snapshot calls this once per confirmed row, and
  // rebuilding every marker each time would be wasted work before the browser
  // has painted any of them.
  function scheduleMapRedraw() {
    if (mapRedrawQueued || !map) return;
    mapRedrawQueued = true;
    requestAnimationFrame(() => {
      mapRedrawQueued = false;
      redrawMap();
    });
  }

  function updateMapNote() {
    const note = document.getElementById("map-note");
    if (!note) return;
    const placed = mapStations().size;
    const calls = new Set([...confirmed.values()].map((d) => d.normalised));
    const missing = calls.size - placed;
    const bits = [`${placed} station${placed === 1 ? "" : "s"} placed`];
    if (missing > 0) bits.push(`${missing} without coordinates in QRZ`);
    note.textContent = bits.join(" · ");
  }

  function setInstanceLocation(lat, lon, label) {
    if (!map || typeof lat !== "number" || typeof lon !== "number") return;
    // 0,0 is what an unconfigured GPS block reports, and it is in the Gulf of
    // Guinea — a receiver is never actually there.
    if (lat === 0 && lon === 0) return;
    if (instanceMarker) map.removeLayer(instanceMarker);
    instanceMarker = L.circleMarker([lat, lon], {
      radius: 7,
      weight: 2,
      color: "#3fb950",
      fillColor: "#3fb950",
      fillOpacity: 0.9,
    })
      .bindTooltip(
        `<div class="map-tip"><span class="call">${esc(label || "Receiver")}</span>` +
        '<div class="rows"><div class="cf">this instance</div></div></div>',
        { direction: "top", opacity: 1 }
      )
      .addTo(map);
    // Centre on the receiver: the stations it hears are mostly around it, and
    // a world view at zoom 1 puts it nowhere in particular.
    map.setView([lat, lon], 3);
  }

  // -- Row preview audio ----------------------------------------------------

  // Clicking a confirmed callsign or a submitted spot tunes a preview to that
  // frequency and plays it. This is NOT the transcript Listen button: that
  // relays what a scanning worker is hearing right now, wherever it happens to
  // be sitting. This opens a separate short-lived receiver session on the
  // frequency the callsign was heard on, which may be nothing at all by now.
  //
  // Uses MinimalRadio from the main instance (vendored into static/). It
  // builds its WebSocket from window.location.host, so the preview only works
  // when the dashboard is same-origin with UberSDR — i.e. reached through the
  // addon proxy, the same condition /api/description already has.

  let radio = null;          // created on first use; sessions are not free
  let playingKey = null;     // key of the row currently previewing

  function radioAvailable() {
    return typeof MinimalRadio !== "undefined";
  }

  function paintRowAudio() {
    for (const body of [els.confirmedBody, els.spotsBody, els.targetsBody]) {
      if (!body) continue;
      for (const tr of body.querySelectorAll("tr")) {
        tr.classList.toggle("playing", !!playingKey && tr.dataset.key === playingKey);
      }
    }
    for (const btn of [els.confirmedStop, els.spotsStop, els.targetsStop]) {
      if (!btn) continue;
      btn.disabled = !playingKey;
      btn.classList.toggle("on", !!playingKey);
    }
  }

  function stopRowAudio() {
    playingKey = null;
    if (radio) {
      // Best effort: a failure here must not leave the buttons stuck on.
      Promise.resolve(radio.stopPreview()).catch(() => {});
    }
    paintRowAudio();
  }

  function startRowAudio(row) {
    if (!radioAvailable() || !row || !row.freq) return;

    // One audio stream at a time. The transcript panels relay a scanner
    // session; this opens its own. Two at once would be unlistenable and
    // would hold a receiver slot for no reason.
    for (const p of panels.values()) {
      if (p.listening) stopListening(p);
    }

    // Clicking the row that is already playing stops it.
    if (playingKey === row.key) {
      stopRowAudio();
      return;
    }

    const mode = (row.mode || "").toLowerCase() ||
                 (row.freq < 10000000 ? "lsb" : "usb");
    playingKey = row.key;
    paintRowAudio();

    try {
      if (!radio) radio = new MinimalRadio();

      // Retune the existing socket rather than tearing it down and building
      // another: the reconnect is audible and costs a fresh /connection.
      //
      // Asked of the socket rather than radio.isPlaying, which only becomes
      // true AFTER the connection completes. Clicking a second row while the
      // first was still connecting would read as "not playing" and open a
      // second WebSocket over the top of the one in flight. CONNECTING counts
      // as retunable because changeFrequency just updates the pending
      // frequency, and the socket's own onopen sends the tune from it — so a
      // click mid-connect lands on the right frequency with one session.
      const ws = radio.ws;
      const retunable = ws && (ws.readyState === WebSocket.OPEN ||
                               ws.readyState === WebSocket.CONNECTING);

      if (retunable) {
        radio.changeFrequency(row.freq, mode);
      } else {
        Promise.resolve(radio.startPreview(row.freq, mode)).catch((err) => {
          console.error("preview failed", err);
          stopRowAudio();
        });
      }
    } catch (err) {
      console.error("preview failed", err);
      stopRowAudio();
    }
  }

  // Delegated: both tables are rewritten wholesale on every redraw.
  function wireRowClicks(body) {
    if (!body) return;
    body.addEventListener("click", (e) => {
      const tr = e.target.closest("tr");
      if (!tr || !tr.dataset.freq) return;
      startRowAudio({
        key: tr.dataset.key,
        freq: parseInt(tr.dataset.freq, 10),
        mode: tr.dataset.mode,
      });
    });
  }
  wireRowClicks(els.confirmedBody);
  wireRowClicks(els.spotsBody);
  wireRowClicks(els.targetsBody);
  for (const btn of [els.confirmedStop, els.spotsStop, els.targetsStop]) {
    if (btn) btn.addEventListener("click", stopRowAudio);
  }

  // -- Activity chart -------------------------------------------------------

  // Distinct stations per band per hour, confirmed and spotted, over a rolling
  // 24 hours. The server always returns all 24 buckets, zero-filled, so a
  // quiet night reads as quiet rather than as a missing axis.
  //
  // Two stacks per hour — confirmed and spotted — each segmented by band.
  // Spotted is necessarily a subset of confirmed, so showing them side by side
  // makes the gap between "heard" and "actually submitted" visible, which one
  // combined stack would hide.

  let chart = null;
  let chartRefreshTimer = null;

  // Resolved from the stylesheet rather than duplicated here, so the tables,
  // the transcript stripes and the chart cannot drift apart. bandClass gives
  // the class; the class carries --band.
  const bandColourCache = new Map();

  function bandColour(band) {
    const cls = bandClass(band);
    if (!cls) return "#8b949e";
    if (bandColourCache.has(cls)) return bandColourCache.get(cls);
    const probe = document.createElement("div");
    probe.className = cls;
    probe.style.display = "none";
    document.body.appendChild(probe);
    const value = getComputedStyle(probe).getPropertyValue("--band").trim();
    probe.remove();
    const colour = value || "#8b949e";
    bandColourCache.set(cls, colour);
    return colour;
  }

  // Chart.js wants rgba for the translucent confirmed bars; --band is hex.
  function withAlpha(hex, alpha) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  function hourLabel(unixSeconds) {
    const d = new Date(unixSeconds * 1000);
    return String(d.getHours()).padStart(2, "0") + ":00";
  }

  function renderChartLegend(bands) {
    const el = document.getElementById("chart-legend");
    if (!el) return;
    el.innerHTML = bands
      .map(
        (b) =>
          `<span><i style="background:${bandColour(b)}"></i>${esc(b)}</span>`
      )
      .join("");
  }

  function buildChart(data) {
    const canvas = document.getElementById("activity-chart");
    if (!canvas || typeof Chart === "undefined") return;

    const buckets = data.buckets || [];
    const bands = data.bands || [];
    const labels = buckets.map((b) => hourLabel(b.start));

    const datasets = [];
    for (const band of bands) {
      const colour = bandColour(band);
      datasets.push({
        label: `${band} confirmed`,
        data: buckets.map((b) => b.confirmed[band] || 0),
        backgroundColor: withAlpha(colour, 0.45),
        borderColor: colour,
        borderWidth: 1,
        stack: "confirmed",
      });
      datasets.push({
        label: `${band} spotted`,
        data: buckets.map((b) => b.spotted[band] || 0),
        backgroundColor: colour,
        borderColor: colour,
        borderWidth: 1,
        stack: "spotted",
      });
    }

    if (chart) {
      // Replace the data in place: rebuilding the Chart on every refresh
      // discards the hover state and flashes the canvas.
      chart.data.labels = labels;
      chart.data.datasets = datasets;
      chart.update("none");
    } else {
      chart = new Chart(canvas, {
        type: "bar",
        data: { labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          // Any bar under the cursor's x position, so hovering an hour shows
          // every band in it rather than only the segment actually hit.
          interaction: { mode: "index", intersect: false },
          scales: {
            x: {
              stacked: true,
              grid: { color: "#21262d" },
              ticks: { color: "#8b949e", font: { size: 9 }, maxRotation: 0,
                       autoSkipPadding: 8 },
            },
            y: {
              stacked: true,
              beginAtZero: true,
              grid: { color: "#21262d" },
              ticks: { color: "#8b949e", font: { size: 9 }, precision: 0 },
              title: { display: true, text: "stations", color: "#8b949e",
                       font: { size: 9 } },
            },
          },
          plugins: {
            // Replaced by our own compact legend in the title bar: Chart.js
            // would list two entries per band, which is 26 chips on a full
            // day of HF.
            legend: { display: false },
            tooltip: {
              backgroundColor: "#0d1117",
              borderColor: "#30363d",
              borderWidth: 1,
              titleColor: "#c9d1d9",
              bodyColor: "#c9d1d9",
              padding: 8,
              // Empty bands are the majority of any hour; listing them all as
              // zeros would bury the ones that matter.
              filter: (item) => item.parsed.y > 0,
              callbacks: {
                title: (items) => {
                  if (!items.length) return "";
                  const b = (data.buckets || [])[items[0].dataIndex];
                  return b ? hourLabel(b.start) + " – " +
                             hourLabel(b.start + (data.bucket_seconds || 3600))
                           : items[0].label;
                },
                label: (item) => `${item.dataset.label}: ${item.parsed.y}`,
                footer: (items) => {
                  let confirmed = 0, spotted = 0;
                  for (const i of items) {
                    if (i.dataset.stack === "spotted") spotted += i.parsed.y;
                    else confirmed += i.parsed.y;
                  }
                  return `${confirmed} confirmed · ${spotted} spotted`;
                },
              },
            },
          },
        },
      });
    }

    renderChartLegend(bands);
    const note = document.getElementById("chart-note");
    if (note) {
      const total = buckets.reduce(
        (n, b) => n + Object.values(b.confirmed).reduce((a, c) => a + c, 0), 0
      );
      note.textContent = bands.length
        ? `${total} station${total === 1 ? "" : "s"} across ` +
          `${bands.length} band${bands.length === 1 ? "" : "s"} · ` +
          "distinct callsigns per hour"
        : "nothing heard in the last 24 hours";
    }
  }

  // Top callsigns across every frequency, confirmed hits against submissions.
  // Blue/gold rather than band colours: a station heard on two bands has no
  // single band colour, and these match the map's own confirmed/spotted pair.
  let topChart = null;

  function buildTopChart(rows) {
    const canvas = document.getElementById("top-chart");
    if (!canvas || typeof Chart === "undefined") return;
    rows = rows || [];

    const labels = rows.map((r) => r.callsign);
    const datasets = [
      {
        label: "confirmed hits",
        data: rows.map((r) => r.confirmed),
        backgroundColor: "rgba(88, 166, 255, 0.55)",
        borderColor: "#58a6ff",
        borderWidth: 1,
      },
      {
        label: "DX submitted",
        data: rows.map((r) => r.spotted),
        backgroundColor: "rgba(227, 179, 65, 0.75)",
        borderColor: "#e3b341",
        borderWidth: 1,
      },
    ];

    if (topChart) {
      topChart.data.labels = labels;
      topChart.data.datasets[0].data = datasets[0].data;
      topChart.data.datasets[1].data = datasets[1].data;
      topChart._rows = rows;
      topChart.update("none");
    } else {
      topChart = new Chart(canvas, {
        type: "bar",
        data: { labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            // Not stacked: the two bars sit side by side so the gap between
            // heard and submitted is readable. Stacking would add them, which
            // means nothing — a submission is a subset of the hits.
            x: { grid: { display: false },
                 ticks: { color: "#8b949e", font: { size: 9 }, maxRotation: 0 } },
            y: { beginAtZero: true,
                 grid: { color: "#21262d" },
                 ticks: { color: "#8b949e", font: { size: 9 }, precision: 0 } },
          },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: "#0d1117",
              borderColor: "#30363d",
              borderWidth: 1,
              titleColor: "#c9d1d9",
              bodyColor: "#c9d1d9",
              padding: 8,
              callbacks: {
                title: (items) => {
                  const r = (topChart && topChart._rows || rows)[items[0].dataIndex];
                  if (!r) return items[0].label;
                  const who = [r.country].filter(Boolean).join("");
                  return r.callsign + (who ? ` — ${who}` : "");
                },
                label: (item) => `${item.dataset.label}: ${item.parsed.y}`,
                footer: (items) => {
                  const r = (topChart && topChart._rows || rows)[items[0].dataIndex];
                  return r && r.bands.length ? "bands: " + r.bands.join(", ") : "";
                },
              },
            },
          },
        },
      });
      topChart._rows = rows;
    }

    const note = document.getElementById("top-note");
    if (note) {
      note.textContent = rows.length
        ? "hits are every validated decode in the last 24 hours, summed " +
          "across all frequencies"
        : "no callsigns confirmed yet";
    }
  }

  function loadChart() {
    fetch("api/history")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) return;
        buildChart(d);
        buildTopChart(d.top_callsigns);
      })
      .catch(() => {});   // the rest of the dashboard is fine without it
  }

  // Coalesced: a busy band produces confirmations faster than the chart needs
  // redrawing, and its resolution is an hour.
  function scheduleChartRefresh() {
    if (chartRefreshTimer) return;
    chartRefreshTimer = setTimeout(() => {
      chartRefreshTimer = null;
      loadChart();
    }, 3000);
  }

  // -- Explain modal --------------------------------------------------------

  // Why a given transcript line did or did not produce a callsign. The
  // scanner's own logs answer this only for lines that got far enough to be
  // logged; the interesting cases are the silent ones, where a callsign is
  // plainly audible in the text and nothing came out.

  function closeExplain() {
    els.explainBackdrop.classList.remove("open");
  }

  function openExplain(text) {
    els.explainBackdrop.classList.add("open");
    els.explainBody.innerHTML = '<p class="ex-note">Analysing…</p>';
    fetch("api/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    })
      .then((r) => {
        // The endpoint allows one analysis per second per address. Clicking
        // down a column of lines is easy to do faster than that, so say so
        // plainly instead of showing a status code.
        if (r.status === 429) return Promise.reject(new Error("__RATE__"));
        if (!r.ok) return Promise.reject(new Error("HTTP " + r.status));
        return r.json();
      })
      .then(renderExplain)
      .catch((err) => {
        const msg =
          String(err.message) === "__RATE__"
            ? "One line per second, please — click again in a moment."
            : `Could not analyse this line: ${esc(String(err))}`;
        els.explainBody.innerHTML = `<p class="ex-note">${msg}</p>`;
      });
  }

  function tokenChip(t) {
    let cls = "ex-tok";
    if (t.maps_to === null) cls += " none";
    else if (t.strict) cls += " strict";

    const arrow = t.maps_to === null
      ? '<span class="tag">no match</span>'
      : ` → <b>${esc(t.maps_to)}</b>`;

    let tags = "";
    if (t.spelled) tags += '<span class="tag">spelled</span>';
    if (t.connector) tags += '<span class="tag">bridge</span>';
    if (t.suffix_word) tags += `<span class="tag">${esc(t.suffix_word)}</span>`;
    if (t.callsign_may_start_here) tags += '<span class="tag">cue</span>';

    return `<span class="${cls}">${esc(t.cased)}${arrow}${tags}</span>`;
  }

  function runBlock(run) {
    const promoted = run.accepted && run.accepted.length > 0;
    let why;
    if (run.outcome === "split") {
      why = `Two callsigns run together — split into ${esc(run.accepted.join(" + "))}`;
    } else if (run.outcome === "accepted") {
      why = `Accepted as ${esc(run.accepted.join(", "))}`;
    } else if (run.outcome === "below_evidence") {
      why =
        `Callsign-shaped, but the evidence score of ${run.evidence} is under ` +
        `the threshold of ${run.threshold} — too much of it came from ` +
        `ambiguous everyday words`;
    } else if (run.outcome === "split_below_evidence") {
      why = `Splits into ${esc((run.split_into || []).join(" + "))}, but scored ` +
        `${run.evidence} against a threshold of ${run.threshold}`;
    } else {
      why = "No part of this run is a legal callsign shape";
    }

    const tried = (run.attempts || [])
      .map((a) => `${a.text}${a.shaped ? "" : " (wrong shape)"}`)
      .join(", ");

    return (
      `<div class="ex-run${promoted ? " ok" : ""}">` +
      `<span class="assembled">${esc(run.text)}</span>` +
      ` <span class="ex-note">from “${esc(run.tokens.join(" "))}”</span>` +
      `<div class="why">${why}</div>` +
      (tried ? `<div class="why">Tried: ${esc(tried)}</div>` : "") +
      `</div>`
    );
  }

  function renderExplain(d) {
    const anyLookedUp = d.candidates.some((c) => c.verdict.reached_qrz);
    const parts = [];

    parts.push(
      `<div class="ex-summary ${anyLookedUp ? "yes" : "no"}">${esc(d.summary)}</div>`
    );

    parts.push("<h3>Line</h3>");
    parts.push(`<div class="ex-quote">${esc(d.text)}</div>`);
    if (d.truncated_at_stroke) {
      parts.push(
        '<p class="ex-note">A spoken “stroke” ends the callsign, so only ' +
        `“${esc(d.analysed_text)}” was analysed.</p>`
      );
    }

    if (d.tokens.length) {
      parts.push("<h3>What each word became</h3>");
      parts.push(
        `<div class="ex-tokens">${d.tokens.map(tokenChip).join("")}</div>`
      );
      const unmapped = d.tokens.filter((t) => t.maps_to === null).length;
      if (unmapped) {
        parts.push(
          `<p class="ex-note">${unmapped} word${unmapped === 1 ? "" : "s"} ` +
          "matched nothing. A run of letters stops at each of these, so a " +
          "mis-heard phonetic word in the middle of a callsign splits it in two.</p>"
        );
      }
    }

    if (d.runs.length) {
      parts.push("<h3>Runs assembled</h3>");
      parts.push(d.runs.map(runBlock).join(""));
    }

    if (d.candidates.length) {
      parts.push("<h3>Candidates</h3>");
      parts.push(
        '<table><thead><tr><th>Callsign</th><th>Source</th>' +
        "<th>Confidence</th><th>What happened</th></tr></thead><tbody>" +
        d.candidates
          .map((c) => {
            const badge = c.verdict.reached_qrz
              ? '<span class="badge spotted">looked up</span>'
              : '<span class="badge no">dropped</span>';
            return (
              `<tr><td class="call">${esc(c.normalised)}</td>` +
              `<td>${esc(c.source)}</td>` +
              `<td>${c.confidence.toFixed(2)}</td>` +
              `<td>${badge} ${esc(c.verdict.detail)}</td></tr>`
            );
          })
          .join("") +
        "</tbody></table>"
      );
      parts.push(
        '<p class="ex-note">“Looked up” means it reached QRZ. Whether it ' +
        "became a spot depends on QRZ holding the callsign, and on being " +
        `heard ${d.gates.spot_min_hits ?? "enough"} times on the same ` +
        "frequency.</p>"
      );
    }

    els.explainBody.innerHTML = parts.join("");
    els.explainBody.scrollTop = 0;
  }

  // -- Settings modal -----------------------------------------------------

  // The running scan's configuration doesn't change, so the first fetch is
  // cached rather than re-requested on every open.
  let settingsCache = null;

  function closeSettings() {
    els.settingsBackdrop.classList.remove("open");
  }

  function renderSettings(groups) {
    if (!groups || !groups.length) {
      els.settingsBody.innerHTML = '<p class="ex-note">No settings reported.</p>';
      return;
    }
    els.settingsBody.innerHTML = groups
      .map((g) => {
        const rows = (g.items || [])
          .map(
            (it) =>
              `<div class="set-row"><span class="label">${esc(it.label)}</span>` +
              `<span class="value">${esc(String(it.value))}</span></div>`
          )
          .join("");
        return `<div class="set-group"><h3>${esc(g.group)}</h3>${rows}</div>`;
      })
      .join("");
    els.settingsBody.scrollTop = 0;
  }

  function openSettings() {
    els.settingsBackdrop.classList.add("open");
    if (settingsCache) {
      renderSettings(settingsCache);
      return;
    }
    els.settingsBody.innerHTML = '<p class="ex-note">Loading…</p>';
    fetch("api/settings")
      .then((r) => {
        if (!r.ok) return Promise.reject(new Error("HTTP " + r.status));
        return r.json();
      })
      .then((groups) => {
        settingsCache = groups;
        renderSettings(groups);
      })
      .catch((err) => {
        els.settingsBody.innerHTML =
          `<p class="ex-note">Could not load settings: ${esc(String(err))}</p>`;
      });
  }

  // Delegated, because transcript lines are created and evicted constantly.
  els.transcripts.addEventListener("click", (e) => {
    const line = e.target.closest(".final");
    if (line && line.dataset.text) openExplain(line.dataset.text);
  });
  els.explainClose.addEventListener("click", closeExplain);
  els.explainBackdrop.addEventListener("click", (e) => {
    if (e.target === els.explainBackdrop) closeExplain();
  });
  els.settingsBtn.addEventListener("click", openSettings);
  els.settingsClose.addEventListener("click", closeSettings);
  els.settingsBackdrop.addEventListener("click", (e) => {
    if (e.target === els.settingsBackdrop) closeSettings();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeExplain(); closeSettings(); }
  });

  // Filters redraw from the client-side copies, so a row hidden by the filter
  // is still updated by incoming events and reappears the moment it matches.
  if (els.confirmedFilter) {
    els.confirmedFilter.addEventListener("input", redrawConfirmed);
  }
  if (els.spotsFilter) {
    els.spotsFilter.addEventListener("input", redrawSpots);
  }

  // -- Confirmed callsigns table --------------------------------------------

  function renderConfirmedRow(det) {
    confirmed.set(det.key, det);
    redrawConfirmed();
    scheduleMapRedraw();
  }

  function redrawConfirmed() {
    const query = filterQuery(els.confirmedFilter);
    const all = [...confirmed.values()].sort(
      (a, b) => (b.timestamp || 0) - (a.timestamp || 0)
    );
    const rows = all.filter((d) =>
      matches(
        haystack([
          d.normalised, d.band, d.mode, d.name, d.country,
          ...freqTerms(d.frequency),
        ]),
        query
      )
    );
    if (rows.length === 0) {
      const msg = all.length
        ? `no rows match “${esc(query)}”`
        : "no callsigns confirmed yet";
      els.confirmedBody.innerHTML =
        `<tr class="empty-row"><td colspan="10">${msg}</td></tr>`;
      return;
    }
    els.confirmedBody.innerHTML = rows
      .map((d) => {
        const star = d.agrees_with_dx_spot ? '<span class="star">★</span> ' : "";
        let spotted = '<span class="badge no">no</span>';
        if (d.spotted_at) {
          spotted = '<span class="badge spotted">spotted</span>';
        }
        return (
          `<tr class="${bandClass(d.band)}" data-key="${escAttr(d.key)}"` +
          ` data-freq="${d.frequency}" data-mode="${escAttr(d.mode || "")}">` +
          `<td class="call">${star}${flagHTML(d.country_code, d.country)}` +
          `${esc(d.normalised)}</td>` +
          `<td>${esc(d.band)}</td><td>${fmtFreq(d.frequency)}</td>` +
          `<td>${esc((d.mode || "").toUpperCase())}</td>` +
          nameCell(d.name) +
          `<td class="name">${esc(d.country || "")}</td>` +
          `<td>${fmtTime(d.first_seen)}</td><td>${fmtTime(d.timestamp)}</td>` +
          `<td>${d.hit_count ?? 1}</td><td>${spotted}</td></tr>`
        );
      })
      .join("");
  }

  // -- DX spots submitted ---------------------------------------------------

  // A spot going out also changes the confirmed row's Spotted badge. The
  // server records that on its own copy, so a page reload showed it
  // correctly, but the live table kept the pre-spot object and read "no"
  // until then.
  function markSpotted(spot) {
    const d = confirmed.get(spot.key);
    if (!d || d.spotted_at) return;
    d.spotted_at = spot.time;
    redrawConfirmed();
    // The marker moves from the Confirmed layer to the Spotted one.
    scheduleMapRedraw();
  }

  function renderSpotRow(spot) {
    spots.unshift(spot);                       // newest first
    if (spots.length > SPOTS_MAX_ROWS) spots.length = SPOTS_MAX_ROWS;
    redrawSpots();
  }

  function redrawSpots() {
    const query = filterQuery(els.spotsFilter);
    const rows = spots.filter((s) =>
      matches(
        haystack([s.callsign, s.comment, ...freqTerms(s.freq)]),
        query
      )
    );
    if (rows.length === 0) {
      const msg = spots.length
        ? `no rows match “${esc(query)}”`
        : "no spots submitted yet";
      els.spotsBody.innerHTML =
        `<tr class="empty-row"><td colspan="4">${msg}</td></tr>`;
      return;
    }
    els.spotsBody.innerHTML = rows
      .map(
        (s) =>
          `<tr class="${bandClass(s.band)}" data-key="${escAttr(s.key)}"` +
          ` data-freq="${s.freq}" data-mode="${escAttr(s.mode || "")}">` +
          `<td>${fmtTime(s.time)}</td>` +
          `<td class="call">${flagHTML(s.country_code, s.country)}` +
          `${esc(s.callsign)}</td>` +
          `<td>${fmtFreq(s.freq)}</td><td>${esc(s.comment)}</td></tr>`
      )
      .join("");
  }

  // -- Band/freq activity ---------------------------------------------------

  function renderTargets(targets) {
    if (!targets || targets.length === 0) {
      els.targetsBody.innerHTML =
        '<tr class="empty-row"><td colspan="8">no active voice targets</td></tr>';
      return;
    }
    els.targetsBody.innerHTML = targets
      .map((t) => {
        const dx = t.dx_callsign
          ? `<span class="star">★</span> ${esc(t.dx_callsign)}`
          : "";
        return (
          `<tr class="${bandClass(t.band)}" data-key="target:${t.dial_freq}"` +
          ` data-freq="${t.dial_freq}" data-mode="${escAttr(t.mode || "")}">` +
          `<td>${esc(t.band)}</td><td>${fmtFreq(t.dial_freq)}</td>` +
          `<td>${esc((t.mode || "").toUpperCase())}</td>` +
          `<td>${(t.snr ?? 0).toFixed(1)}</td><td>${(t.confidence ?? 0).toFixed(2)}</td>` +
          `<td>${dx}</td><td>${t.visits ?? 0}</td><td>${t.callsigns_found ?? 0}</td></tr>`
        );
      })
      .join("");
  }

  // -- Audio preview ---------------------------------------------------------

  // The stream is the scanner's own session relayed by the backend, so it
  // follows the scanner as it hops rather than being a separate receiver.
  // Starting it unmutes that session server-side; stopping re-mutes it, so
  // the src is cleared (not just paused) to actually drop the connection.
  // Listening is exclusive across panels. Two at once would overlap two
  // stations in the same pair of ears, and would hold both sessions unmuted
  // — each one only re-mutes when its own listener disconnects.
  //
  // Every call takes a generation token. play() rejects asynchronously, and
  // stopping a panel is itself a common cause of that rejection, so without
  // the token a late failure from the panel you just switched away from
  // would switch off the one you switched to.
  let listenGeneration = 0;

  function setListening(p, on) {
    const gen = ++listenGeneration;

    if (on) {
      for (const other of panels.values()) {
        if (other !== p && other.listening) stopListening(other);
      }
      // The other half of the exclusivity — see startRowAudio.
      if (playingKey) stopRowAudio();
    }

    p.listening = on;
    if (on) {
      // Each worker is its own session, so its own audio endpoint.
      // Cache-bust so a re-listen opens a fresh request rather than resuming
      // a stale buffered one.
      p.audio.src = `api/audio/${p.id}?t=` + Date.now();
      p.audio.play().catch((err) => {
        // Ignore if anything has changed since — this rejection is stale.
        if (gen !== listenGeneration) return;
        console.error("audio play failed", err);
        setListening(p, false);
      });
    } else {
      stopListening(p);
    }
    paintListen(p);
  }

  // Tearing down the element is what actually ends the HTTP request, which is
  // what makes the server drop the listener and re-mute that session. Pausing
  // alone would leave the connection open and the session unmuted.
  function stopListening(p) {
    p.listening = false;
    try {
      p.audio.pause();
      p.audio.removeAttribute("src");
      p.audio.load();
    } catch (err) {
      console.error("stopping audio failed", err);
    }
    paintListen(p);
  }

  function paintListen(p) {
    p.listen.classList.toggle("on", p.listening);
    p.listen.textContent = p.listening ? "🔊 Listening" : "🔈 Listen";
  }

  function renderAudioAvailable(p, available) {
    p.listen.disabled = !available;
    p.listen.title = available
      ? "Hear what this worker is tuned to — follows it as it hops"
      : "Audio session not ready yet";
    if (!available && p.listening) setListening(p, false);
  }

  // -- Full snapshot ----------------------------------------------------------

  function applyState(state) {
    const workers = state.workers || [];
    buildPanels(workers);
    renderStats(state.stats);
    for (const w of workers) {
      const p = panels.get(w.id);
      if (!p) continue;
      p.transcript.innerHTML = "";
      p.liveLine = null;
      p.lastFinalText = null;
      p.lastFreq = null;
      renderCurrent(w.current ? { ...w.current, worker: w.id } : null);
      (w.transcript || []).forEach((e) => appendTranscript(e, p));
      setLiveTranscript(w.live || null, p);
      renderSignal(w.signal ? { ...w.signal, worker: w.id } : null);
      renderAudioAvailable(p, !!w.audio_available);
      renderStatus(w.status ? { ...w.status, worker: w.id } : null);
    }
    confirmed.clear();
    (state.confirmed || []).forEach((d) => confirmed.set(d.key, d));
    redrawConfirmed();
    scheduleMapRedraw();
    // Snapshot spots arrive oldest-first and renderSpotRow unshifts each to
    // the front, so replaying them in order leaves the newest at the top —
    // matching how live spots land. Reversing here as well would flip it back
    // and put the oldest first on every page load.
    spots.length = 0;
    (state.spots || []).forEach(renderSpotRow);
    redrawSpots();
    renderTargets(state.targets);
  }

  // -- Wiring -----------------------------------------------------------------

  // Everything below runs after every declaration in this file, so nothing
  // here can hit a temporal dead zone. initMap first: loadReceiver places the
  // receiver marker once its fetch resolves.
  restoreStaticPanels();
  initMap();
  loadReceiver();
  loadChart();
  // The window is rolling, so it has to advance on its own — an idle dashboard
  // would otherwise keep showing an hour axis that ended when it loaded.
  setInterval(loadChart, 300000);

  fetch("api/state")
    .then((r) => r.json())
    .then(applyState)
    .catch((err) => console.error("initial state fetch failed", err));

  function connect() {
    const es = new EventSource("api/events");
    es.addEventListener("state", (e) => applyState(JSON.parse(e.data)));
    es.addEventListener("hop", (e) => renderCurrent(JSON.parse(e.data)));
    es.addEventListener("transcript", (e) => {
      // A segment completing supersedes the in-progress line it grew from.
      const entry = JSON.parse(e.data);
      const p = panelFor(entry);
      appendTranscript(entry, p);
      setLiveTranscript(null, p);
      flashActivity(p);
    });
    es.addEventListener("live", (e) => setLiveTranscript(JSON.parse(e.data)));
    es.addEventListener("confirmed", (e) => {
      renderConfirmedRow(JSON.parse(e.data));
      scheduleChartRefresh();
    });
    es.addEventListener("spot", (e) => {
      const spot = JSON.parse(e.data);
      renderSpotRow(spot);
      markSpotted(spot);
      scheduleChartRefresh();
    });
    es.addEventListener("stats", (e) => renderStats(JSON.parse(e.data)));
    es.addEventListener("targets", (e) => renderTargets(JSON.parse(e.data)));
    es.addEventListener("signal", (e) => renderSignal(JSON.parse(e.data)));
    es.addEventListener("status", (e) => renderStatus(JSON.parse(e.data)));
    es.addEventListener("audio_available", (e) => {
      const msg = JSON.parse(e.data);
      const p = panelFor(msg);
      if (p) renderAudioAvailable(p, msg.available);
    });
    es.onerror = () => {
      // The browser's EventSource auto-reconnects; nothing to do here beyond
      // letting the connection indicator (uptime keeps ticking) imply it.
    };
  }
  connect();
})();
