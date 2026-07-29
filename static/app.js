// app.js — live dashboard for the voice callsign skimmer.
//
// Loads the initial snapshot from /api/state, then subscribes to /api/events
// (SSE) for incremental updates. All URLs are relative — <base> in
// index.html (set from X-Forwarded-Prefix server-side) makes them resolve
// correctly whether this page is opened directly or through UberSDR's addon
// proxy.

(() => {
  "use strict";

  const TRANSCRIPT_MAX_LINES = 400;

  const els = {
    uptime: document.getElementById("uptime"),
    stats: document.getElementById("stats"),
    transcripts: document.getElementById("transcripts"),
    confirmedBody: document.querySelector("#confirmed-table tbody"),
    spotsBody: document.querySelector("#spots-table tbody"),
    targetsBody: document.querySelector("#targets-table tbody"),
    receiver: document.getElementById("receiver"),
  };

  // Client-side copies, keyed the same way the server keeps them, so each
  // incremental event only has to touch one row instead of asking the
  // server for a full snapshot again.
  const confirmed = new Map();   // "callsign|freq_bucket" -> detection dict
  let startTime = Date.now() / 1000;

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
      sec.innerHTML =
        `<h2 class="tp-head">` +
        `<span class="tp-dot" title="Whisper connection"></span>` +
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
        transcript: sec.querySelector(".transcript"),
        scroll: sec.querySelector(".scroll"),
        liveLine: null,
        lastFinalText: null,
        listening: false,
      };
      p.listen.addEventListener("click", () => setListening(p, !p.listening));
      p.audio.addEventListener("error", () => {
        if (p.listening) setListening(p, false);
      });
      panels.set(w.id, p);
    }
  }

  const panelFor = (e) => panels.get(e && e.worker != null ? e.worker : 0);

  // Green once Whisper is attached and transcribing, red when the attach
  // failed or the session dropped, grey while still coming up. The failure is
  // otherwise silent — whisper.max_users defaults to 2 on the server, so a
  // second worker is routinely refused while the first runs on happily.
  function renderStatus(st) {
    const p = panelFor(st);
    if (!p || !st) return;
    const up = st.connected === true, down = st.connected === false;
    p.dot.classList.toggle("up", up);
    p.dot.classList.toggle("down", down);
    p.dot.title = st.detail || (up ? "transcribing" : down ? "not connected" : "connecting");
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
      })
      .catch(() => {});   // not fatal; the dashboard is fine without it
  }
  loadReceiver();

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

    const line = document.createElement("div");
    line.className = "final";
    line.innerHTML = lineHTML(entry, "✓");
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
      p.liveLine.className = "partial";
      p.transcript.appendChild(p.liveLine);
    }
    p.liveLine.innerHTML = lineHTML(entry, "…");
    if (atBottom) p.scroll.scrollTop = p.scroll.scrollHeight;
  }

  // -- Confirmed callsigns table --------------------------------------------

  function renderConfirmedRow(det) {
    confirmed.set(det.key, det);
    redrawConfirmed();
  }

  function redrawConfirmed() {
    const rows = [...confirmed.values()].sort(
      (a, b) => (b.timestamp || 0) - (a.timestamp || 0)
    );
    if (rows.length === 0) {
      els.confirmedBody.innerHTML =
        '<tr class="empty-row"><td colspan="10">no callsigns confirmed yet</td></tr>';
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
          `<tr><td class="call">${star}${esc(d.normalised)}</td>` +
          `<td>${esc(d.band)}</td><td>${fmtFreq(d.frequency)}</td>` +
          `<td>${esc((d.mode || "").toUpperCase())}</td>` +
          `<td class="name">${esc(d.name || "")}</td>` +
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
  }

  function renderSpotRow(spot) {
    if (els.spotsBody.querySelector(".empty-row")) els.spotsBody.innerHTML = "";
    const row = document.createElement("tr");
    row.innerHTML =
      `<td>${fmtTime(spot.time)}</td><td class="call">${esc(spot.callsign)}</td>` +
      `<td>${fmtFreq(spot.freq)}</td><td>${esc(spot.comment)}</td>`;
    els.spotsBody.insertBefore(row, els.spotsBody.firstChild);
    while (els.spotsBody.children.length > 100) {
      els.spotsBody.removeChild(els.spotsBody.lastChild);
    }
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
          `<tr><td>${esc(t.band)}</td><td>${fmtFreq(t.dial_freq)}</td>` +
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
    els.spotsBody.innerHTML = "";
    // Snapshot spots arrive oldest-first and renderSpotRow inserts each at the
    // top, so replaying them in order leaves the newest at the top — matching
    // how live spots land. Reversing here as well would flip it back and put
    // the oldest first on every page load.
    (state.spots || []).forEach(renderSpotRow);
    renderTargets(state.targets);
  }

  // -- Wiring -----------------------------------------------------------------

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
      appendTranscript(entry);
      setLiveTranscript(null, panelFor(entry));
    });
    es.addEventListener("live", (e) => setLiveTranscript(JSON.parse(e.data)));
    es.addEventListener("confirmed", (e) => renderConfirmedRow(JSON.parse(e.data)));
    es.addEventListener("spot", (e) => {
      const spot = JSON.parse(e.data);
      renderSpotRow(spot);
      markSpotted(spot);
    });
    es.addEventListener("stats", (e) => renderStats(JSON.parse(e.data)));
    es.addEventListener("targets", (e) => renderTargets(JSON.parse(e.data)));
    es.addEventListener("signal", (e) => renderSignal(JSON.parse(e.data)));
    es.addEventListener("status", (e) => renderStatus(JSON.parse(e.data)));
    es.onerror = () => {
      // The browser's EventSource auto-reconnects; nothing to do here beyond
      // letting the connection indicator (uptime keeps ticking) imply it.
    };
  }
  connect();
})();
