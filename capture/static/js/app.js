// Plain JS, no framework, no build step. The browser is a display and input
// surface only — it never touches the microphone.

"use strict";

const summaryEl = document.getElementById("summary");
const wsStateEl = document.getElementById("ws-state");
const meterFill = document.getElementById("meter-fill");
const clipIndicator = document.getElementById("clip-indicator");

// --- Startup summary (sessions/takes on disk so far) -----------------------

async function loadSummary() {
  try {
    const res = await fetch("/api/summary");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      summaryEl.textContent = body.detail || `Summary unavailable (HTTP ${res.status})`;
      summaryEl.className = "error";
      return;
    }
    const data = await res.json();
    summaryEl.textContent =
      `On disk so far: ${data.sessions} sessions, ${data.takes} takes.`;
    summaryEl.className = "";
  } catch (err) {
    summaryEl.textContent = `Cannot reach the capture server: ${err}`;
    summaryEl.className = "error";
  }
}

// --- WebSocket: level meter + task state + errors (server push only) -------

const DBFS_FLOOR = -60; // meter range: -60 dBFS .. 0 dBFS

function onWsMessage(msg) {
  if (msg.type === "level") {
    const pct = Math.max(0, Math.min(100, (1 - msg.rms_dbfs / DBFS_FLOOR) * 100));
    meterFill.style.width = `${pct}%`;
    meterFill.classList.toggle("clipping", msg.clipping);
    clipIndicator.hidden = !msg.clipping;
  } else if (msg.type === "task_state") {
    // Step 4: render the session flow from server state.
  } else if (msg.type === "error") {
    // Session-fatal errors must be a full-screen block, not a toast (step 4).
    wsStateEl.textContent = `ERROR: ${msg.code} — ${msg.message}`;
    wsStateEl.className = "error";
  }
}

function connectWs() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { wsStateEl.textContent = "WebSocket: connected."; };
  ws.onmessage = (ev) => onWsMessage(JSON.parse(ev.data));
  ws.onclose = () => {
    wsStateEl.textContent = "WebSocket: disconnected — retrying…";
    setTimeout(connectWs, 1000);
  };
}

loadSummary();
connectWs();
