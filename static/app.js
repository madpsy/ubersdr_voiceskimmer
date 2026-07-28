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
    current: document.getElementById("current"),
    uptime: document.getElementById("uptime"),
    stats: document.getElementById("stats"),
    transcript: document.getElementById("transcript"),
    transcriptScroll: document.getElementById("transcript-scroll"),
    confirmedBody: document.querySelector("#confirmed-table tbody"),
    spotsBody: document.querySelector("#spots-table tbody"),
    targetsBody: document.querySelector("#targets-table tbody"),
    listen: document.getElementById("listen"),
    audio: document.getElementById("audio-el"),
  };

  // Client-side copies, keyed the same way the server keeps them, so each
  // incremental event only has to touch one row instead of asking the
  // server for a full snapshot again.
  const confirmed = new Map();   // normalised callsign -> detection dict
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

  // -- Header: current dwell -------------------------------------------------

  function renderCurrent(current) {
    if (!current) {
      els.current.innerHTML = '<span class="empty">waiting for a target…</span>';
      return;
    }
    const dx = current.dx_callsign
      ? ` <span class="dx">★ DX spot: ${esc(current.dx_callsign)}</span>`
      : "";
    els.current.innerHTML =
      `${esc(current.band)} &nbsp; <span class="freq">${fmtFreq(current.dial_freq)}</span> ` +
      `${esc((current.mode || "").toUpperCase())} &nbsp; ` +
      `SNR ${(current.snr ?? 0).toFixed(1)} dB &nbsp; conf ${(current.confidence ?? 0).toFixed(2)}${dx}`;
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

  function appendTranscript(entry) {
    const atBottom =
      els.transcriptScroll.scrollTop + els.transcriptScroll.clientHeight >=
      els.transcriptScroll.scrollHeight - 24;

    const line = document.createElement("div");
    const isFinal = entry.marker === "✓"; // ✓
    line.className = isFinal ? "final" : "partial";
    const where = entry.band ? `${entry.band} ${fmtFreq(entry.freq)}` : "";
    line.innerHTML =
      `<span class="meta">${fmtTime(entry.time)} ${esc(where)}</span> ` +
      `<span class="marker">${esc(entry.marker)}</span> ${esc(entry.text)}`;
    els.transcript.appendChild(line);

    while (els.transcript.children.length > TRANSCRIPT_MAX_LINES) {
      els.transcript.removeChild(els.transcript.firstChild);
    }
    if (atBottom) {
      els.transcriptScroll.scrollTop = els.transcriptScroll.scrollHeight;
    }
  }

  // -- Confirmed callsigns table --------------------------------------------

  function renderConfirmedRow(det) {
    confirmed.set(det.normalised, det);
    redrawConfirmed();
  }

  function redrawConfirmed() {
    const rows = [...confirmed.values()].sort(
      (a, b) => (b.timestamp || 0) - (a.timestamp || 0)
    );
    if (rows.length === 0) {
      els.confirmedBody.innerHTML =
        '<tr class="empty-row"><td colspan="9">no callsigns confirmed yet</td></tr>';
      return;
    }
    els.confirmedBody.innerHTML = rows
      .map((d) => {
        const star = d.agrees_with_dx_spot ? '<span class="star">★</span> ' : "";
        const who = d.name || d.country || "";
        let spotted = '<span class="badge no">no</span>';
        if (d.spotted_at) {
          spotted = '<span class="badge spotted">spotted</span>';
        }
        return (
          `<tr><td class="call">${star}${esc(d.normalised)}</td>` +
          `<td>${esc(d.band)}</td><td>${fmtFreq(d.frequency)}</td>` +
          `<td>${esc((d.mode || "").toUpperCase())}</td>` +
          `<td class="name">${esc(who)}</td>` +
          `<td>${fmtTime(d.first_seen)}</td><td>${fmtTime(d.timestamp)}</td>` +
          `<td>${d.hit_count ?? 1}</td><td>${spotted}</td></tr>`
        );
      })
      .join("");
  }

  // -- DX spots submitted ---------------------------------------------------

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
  let listening = false;

  function setListening(on) {
    listening = on;
    if (on) {
      // Cache-bust so a re-listen opens a fresh request rather than resuming
      // a stale buffered one.
      els.audio.src = "api/audio?t=" + Date.now();
      els.audio.play().catch((err) => {
        console.error("audio play failed", err);
        setListening(false);
      });
    } else {
      els.audio.pause();
      els.audio.removeAttribute("src");
      els.audio.load();
    }
    els.listen.classList.toggle("on", on);
    els.listen.textContent = on ? "🔊 Listening" : "🔈 Listen";
  }

  els.listen.addEventListener("click", () => setListening(!listening));
  els.audio.addEventListener("error", () => {
    if (listening) setListening(false);
  });

  function renderAudioAvailable(available) {
    els.listen.disabled = !available;
    els.listen.title = available
      ? "Hear what the scanner is tuned to — follows it as it hops"
      : "Audio session not ready yet";
    if (!available && listening) setListening(false);
  }

  // -- Full snapshot ----------------------------------------------------------

  function applyState(state) {
    renderCurrent(state.current);
    renderStats(state.stats);
    (state.transcript || []).forEach(appendTranscript);
    confirmed.clear();
    (state.confirmed || []).forEach((d) => confirmed.set(d.normalised, d));
    redrawConfirmed();
    els.spotsBody.innerHTML = "";
    // spots arrive oldest-first in the snapshot; render newest-first to match
    // the live insertBefore behaviour of renderSpotRow.
    [...(state.spots || [])].reverse().forEach(renderSpotRow);
    renderTargets(state.targets);
    renderAudioAvailable(!!state.audio_available);
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
    es.addEventListener("transcript", (e) => appendTranscript(JSON.parse(e.data)));
    es.addEventListener("confirmed", (e) => renderConfirmedRow(JSON.parse(e.data)));
    es.addEventListener("spot", (e) => renderSpotRow(JSON.parse(e.data)));
    es.addEventListener("stats", (e) => renderStats(JSON.parse(e.data)));
    es.addEventListener("targets", (e) => renderTargets(JSON.parse(e.data)));
    es.onerror = () => {
      // The browser's EventSource auto-reconnects; nothing to do here beyond
      // letting the connection indicator (uptime keeps ticking) imply it.
    };
  }
  connect();
})();
