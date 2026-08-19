// Voice Capture — operator UI (SPACE READY_4 analog mission).
//
// Plain JS, no framework, no build step, nothing fetched from a network.
// The browser is a display and input surface only: it NEVER touches the
// microphone. There is no getUserMedia, no MediaRecorder, no AudioContext
// anywhere in this file, and there must never be — the capture path lives in
// the Python process (CLAUDE.md, "Hard audio requirements").
//
// The server owns all session state. This page renders
// capture.domain.state.SessionStateMachine.snapshot(), which arrives both as
// the task_state WebSocket message and from GET /api/sessions/active. On load
// it re-syncs from the server, so a refresh mid-session recovers instead of
// breaking (ARCHITECTURE.md §4).
//
// This file serves both pages; it dispatches on <body data-page>.

"use strict";

// ---------------------------------------------------------------------------
// Constants. capture/config.py is Python and cannot be imported here; anything
// marked MIRRORS duplicates a value from it and must be kept in step.
// ---------------------------------------------------------------------------

const METER_FLOOR_DBFS_FALLBACK = -60; // MIRRORS config.METER_FLOOR_DBFS
let meterFloorDbfs = METER_FLOOR_DBFS_FALLBACK; // replaced if /api/devices reports one

const CLIP_HOLD_MS = 1200; // keep the live CLIPPING flag up this long after the last clipped frame
const LEVEL_STALE_MS = 2500; // no level message for this long during a session -> say so, do not freeze a lie
const WS_RETRY_MS = 1000;
const WS_RETRY_SLOW_MS = 5000;
const TICK_MS = 100;

// Bumping this forces the operator to re-confirm the checklist.
const CHECKLIST_VERSION = "1";

// Shown when the OS cannot tell us whether input enhancements are on
// (ARCHITECTURE.md §8: v1 ships the manual checklist, not a platform reader).
// The Windows 11 wording should be checked against the actual habitat laptop.
const ENHANCEMENT_CHECKLIST = [
  "Windows: Settings → System → Sound → (this input device) → Device properties → Audio enhancements is OFF.",
  "This device is the default input device, and no other device is selected anywhere.",
  "The microphone gain is at the taped, photographed setting. Nobody has moved it.",
  "No other application is using the microphone (meeting apps, browser tabs, voice assistants).",
  "The microphone is on its marked position and the mouth-to-microphone distance has been measured.",
];

// Human labels for file stems. Only shown when a task records more than one
// sound (task 8: /s/ then /z/), so the operator can never mix the two up.
const STEM_LABELS = {
  sustained_s: "this take is the S sound: ssss",
  sustained_z: "this take is the Z sound: zzzz",
};

// Borg CR-10, with the standard verbal anchors. Half steps are allowed.
//
// 6, 8 and 9 carry no wording on purpose: the published scale anchors
// only certain numbers, and the gaps are where a rater places an effort
// that falls BETWEEN two anchors. Inventing labels for them would change
// a validated instrument, so the UI derives a muted "between X and Y"
// hint from the neighbours instead — visibly different from a real anchor.
const BORG_SCALE = [
  { value: 0, label: "nothing at all" },
  { value: 0.5, label: "extremely weak — just noticeable" },
  { value: 1, label: "very weak" },
  { value: 2, label: "weak (light)" },
  { value: 3, label: "moderate" },
  { value: 4, label: "somewhat strong" },
  { value: 5, label: "strong (heavy)" },
  { value: 6, label: "" },
  { value: 7, label: "very strong" },
  { value: 8, label: "" },
  { value: 9, label: "" },
  { value: 10, label: "extremely strong — almost maximum" },
];

const SCREEN_IDS = [
  "start",
  "consent",
  "setup",
  "reference",
  "covariates",
  "task",
  "borg",
  "qc",
  "complete",
  "unknown",
];

const SESSION_SCREENS = new Set(["reference", "covariates", "task", "borg", "qc"]);

const STORE_SESSION = "vc.session_id";
const STORE_PARTICIPANT = "vc.participant";

// ---------------------------------------------------------------------------
// Tiny DOM helpers. Everything is built as nodes with textContent, so server
// text (file names, error messages, pseudonyms) can never be interpreted as
// markup.
// ---------------------------------------------------------------------------

function el(id) {
  return document.getElementById(id);
}

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const key of Object.keys(attrs)) {
      const value = attrs[key];
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.slice(0, 2) === "on" && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

function setText(id, text) {
  const node = el(id);
  if (node) node.textContent = text;
}

function fmt(value, digits) {
  return Number.isFinite(value) ? value.toFixed(digits === undefined ? 1 : digits) : "—";
}

function asArray(value) {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined) return [];
  return [value];
}

function jsonBlock(data) {
  let text;
  try {
    text = JSON.stringify(data, null, 2);
  } catch (err) {
    text = `(could not format the server response: ${err.message})`;
  }
  return h("pre", { class: "pre", text: text });
}

// ---------------------------------------------------------------------------
// HTTP. Every call checks res.ok and carries the server's own text forward —
// nothing here fails silently, and no spinner is ever left spinning.
// ---------------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function api(method, path, body) {
  const options = { method: method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(path, options);
  } catch (err) {
    throw new ApiError(
      `Cannot reach the capture server (${method} ${path}): ${err.message}. ` +
        "Is the python process still running?",
      0,
      null
    );
  }

  const text = await res.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      payload = null; // a non-JSON body is reported verbatim below
    }
  }

  if (!res.ok) {
    throw new ApiError(`HTTP ${res.status} — ${detailOf(payload, text, res.statusText)}`, res.status, payload);
  }
  return payload;
}

function detailOf(payload, text, statusText) {
  if (payload && typeof payload === "object") {
    const detail = payload.detail !== undefined ? payload.detail : payload.message;
    if (typeof detail === "string" && detail) return detail;
    if (detail !== undefined && detail !== null) {
      try {
        return JSON.stringify(detail);
      } catch (err) {
        // fall through to the raw text
      }
    }
  }
  return (text && text.slice(0, 800)) || statusText || "no detail from the server";
}

// A 4xx means the server refused and did nothing — recoverable, shown inline.
// Anything else (network loss, 5xx) means we do not know what happened, which
// is not something to whisper about in the recording path.
function isRefusal(err) {
  return err instanceof ApiError && err.status >= 400 && err.status < 500;
}

function isNotImplemented(err) {
  return err instanceof ApiError && err.status === 501;
}

// ---------------------------------------------------------------------------
// Application state (a mirror of the server's, never an authority on it).
// ---------------------------------------------------------------------------

const app = {
  showAllMics: false, // reveal virtual / unusable inputs in the picker
  demos: null, // which spoken examples exist; filled by loadDemoAvailability()
  page: document.body.dataset.page || "capture",
  localStep: "start", // start | consent | setup | complete — only before/after a session
  shownScreen: null,
  booting: false, // true between a reload and the first answer from the server
  startLoaded: false,
  participants: [],
  consentPoints: [],
  consentVersion: null,
  consentError: null,
  participant: null,
  participantRecord: null,
  deviceName: null,
  checklistOk: false,
  sessionId: null,
  snapshot: null,
  pendingBorg: null,
  lastResult: null,
  completeInfo: null,
  qc: null,
  fatal: null,
  takeCallInFlight: false,
  takeStartedAt: null,
  clipLatched: false,
  clipUntil: 0,
  lastLevelAt: 0,
  wsOpen: false,
};

function rememberSession(sessionId, participant) {
  app.sessionId = sessionId || null;
  if (participant) app.participant = participant;
  try {
    if (sessionId) window.sessionStorage.setItem(STORE_SESSION, sessionId);
    else window.sessionStorage.removeItem(STORE_SESSION);
    if (app.participant) window.sessionStorage.setItem(STORE_PARTICIPANT, app.participant);
  } catch (err) {
    console.warn("sessionStorage unavailable; session id kept in memory only", err);
  }
}

function recallSession() {
  try {
    return {
      sessionId: window.sessionStorage.getItem(STORE_SESSION),
      participant: window.sessionStorage.getItem(STORE_PARTICIPANT),
    };
  } catch (err) {
    console.warn("sessionStorage unavailable", err);
    return { sessionId: null, participant: null };
  }
}

function forgetSession() {
  app.sessionId = null;
  app.snapshot = null;
  app.lastResult = null;
  app.qc = null;
  app.pendingBorg = null;
  app.clipLatched = false;
  try {
    window.sessionStorage.removeItem(STORE_SESSION);
  } catch (err) {
    console.warn("sessionStorage unavailable", err);
  }
}

// ---------------------------------------------------------------------------
// Banners: loud, keyed, never used for a session-fatal error.
// ---------------------------------------------------------------------------

function setBanner(key, kind, message) {
  const host = el("banners");
  if (!host) return;
  let node = host.querySelector(`[data-key="${key}"]`);
  if (!node) {
    node = h("p", { class: "banner", dataset: { key: key } });
    host.append(node);
  }
  node.className = `banner ${kind === "bad" ? "banner-bad" : "banner-warn"}`;
  node.textContent = message;
}

function clearBanner(key) {
  const host = el("banners");
  if (!host) return;
  const node = host.querySelector(`[data-key="${key}"]`);
  if (node) node.remove();
}

function showInline(id, message) {
  const node = el(id);
  if (!node) return;
  node.textContent = message;
  node.hidden = false;
}

function hideInline(id) {
  const node = el(id);
  if (!node) return;
  node.textContent = "";
  node.hidden = true;
}

// ---------------------------------------------------------------------------
// Session-fatal error: full screen, message verbatim, no way to dismiss it
// into a session that is no longer recording (ARCHITECTURE.md §14).
// ---------------------------------------------------------------------------

function showFatal(code, message) {
  app.fatal = { code: code, message: message };
  app.takeStartedAt = null;
  setText("fatal-code", code);
  setText("fatal-message", message);
  const result = el("fatal-recheck-result");
  if (result) {
    result.hidden = true;
    result.textContent = "";
  }
  const overlay = el("fatal");
  if (overlay) overlay.hidden = false;
  const button = el("fatal-recheck");
  if (button) button.focus();
}

async function onFatalRecheck() {
  const out = el("fatal-recheck-result");
  if (out) {
    out.hidden = false;
    out.textContent = "Checking with the server…";
  }
  let active;
  try {
    active = await fetchActive();
  } catch (err) {
    if (out) out.textContent = `Still cannot reach the server: ${err.message}`;
    return;
  }
  if (active && active.fatal) {
    if (out) {
      out.textContent =
        `The server still has session ${active.sessionId} open and marked failed: ${active.fatal}. ` +
        "It will refuse every further take. End the session with the button above, " +
        "then start a new session once the microphone is working.";
    }
    return;
  }

  // Either no session is open, or the server does not consider this one
  // broken — there is no broken session left to continue into.
  app.fatal = null;
  const overlay = el("fatal");
  if (overlay) overlay.hidden = true;
  if (active) {
    rememberSession(active.sessionId, active.participant);
    if (active.snapshot) setSnapshot(active.snapshot);
    else render();
    return;
  }
  forgetSession();
  app.localStep = "start";
  app.startLoaded = false;
  render();
}

// Close out a session whose recording path failed. The takes it completed are
// already final on disk and in both ledgers; this releases the microphone and
// frees the server so the next session can start without restarting the app.
// It cannot resume recording — the server refuses every take on a failed
// session, so this is an exit, never a "carry on anyway".
async function onFatalEndSession() {
  const button = el("fatal-end-session");
  const out = el("fatal-recheck-result");
  if (!app.sessionId) {
    if (out) {
      out.hidden = false;
      out.textContent = "There is no session open to end.";
    }
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = "Ending…";
  }
  if (out) {
    out.hidden = false;
    out.textContent = "Ending the session…";
  }
  try {
    const data = await api("POST", `/api/sessions/${encodeURIComponent(app.sessionId)}/complete`);
    app.completeInfo = data && typeof data === "object" ? data : {};
    app.fatal = null;
    const overlay = el("fatal");
    if (overlay) overlay.hidden = true;
    forgetSession();
    app.localStep = "complete";
    app.startLoaded = false;
    render();
  } catch (err) {
    if (out) {
      out.textContent =
        `The session was NOT ended: ${err.message}. Every completed take is still ` +
        "safe on disk. Restart the capture application if this keeps failing.";
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "End this session and release the microphone";
    }
  }
}

// ---------------------------------------------------------------------------
// Level meter. Driven entirely by numbers the server sends.
// ---------------------------------------------------------------------------

function dbfsToPercent(dbfs) {
  if (!Number.isFinite(dbfs)) return 0;
  const floor = meterFloorDbfs < 0 ? meterFloorDbfs : METER_FLOOR_DBFS_FALLBACK;
  const percent = (1 - dbfs / floor) * 100;
  return Math.max(0, Math.min(100, percent));
}

function applyLevel(msg) {
  app.lastLevelAt = Date.now();
  const rms = Number(msg.rms_dbfs);
  const peak = Number(msg.peak_dbfs);
  const clipping = msg.clipping === true;

  const fill = el("meter-fill");
  const peakMark = el("meter-peak");
  const readout = el("meter-readout");
  if (fill) {
    fill.style.width = `${dbfsToPercent(rms)}%`;
    fill.classList.toggle("clipping", clipping);
    fill.classList.toggle("hot", !clipping && Number.isFinite(peak) && peak > -6);
  }
  if (peakMark) {
    if (Number.isFinite(peak)) {
      peakMark.hidden = false;
      peakMark.style.left = `calc(${dbfsToPercent(peak)}% - 1px)`;
    } else {
      peakMark.hidden = true;
    }
  }
  if (readout) {
    readout.textContent = `rms ${fmt(rms)} dBFS · peak ${fmt(peak)} dBFS`;
  }
  const stale = el("meter-stale");
  if (stale) stale.hidden = true;

  if (clipping) {
    app.clipUntil = Date.now() + CLIP_HOLD_MS;
    app.clipLatched = true;
  }
  updateClipIndicators();
}

function updateClipIndicators() {
  const live = el("clip-indicator");
  if (live) live.hidden = Date.now() > app.clipUntil;
  const latch = el("clip-latch");
  if (latch) latch.hidden = !app.clipLatched;
}

function meterGoesStale() {
  const fill = el("meter-fill");
  const peakMark = el("meter-peak");
  const readout = el("meter-readout");
  const stale = el("meter-stale");
  // A frozen bar reads as signal, so drop it to zero rather than lie.
  if (fill) {
    fill.style.width = "0%";
    fill.classList.remove("clipping", "hot");
  }
  if (peakMark) peakMark.hidden = true;
  if (readout) readout.textContent = "no level data";
  if (stale) stale.hidden = false;
}

// ---------------------------------------------------------------------------
// Snapshot handling. normalize* functions are deliberately forgiving about
// missing fields: this page must never blow up on a payload it half-recognises.
// ---------------------------------------------------------------------------

function normalizeSnapshot(raw) {
  if (!raw || typeof raw !== "object") return null;
  const rawTask = raw.task && typeof raw.task === "object" ? raw.task : null;
  return {
    phase: typeof raw.phase === "string" ? raw.phase : null,
    take_state: typeof raw.take_state === "string" ? raw.take_state : "idle",
    slot_index: Number.isFinite(raw.slot_index) ? raw.slot_index : null,
    total_slots: Number.isFinite(raw.total_slots) ? raw.total_slots : null,
    redo_mode: raw.redo_mode === true,
    task: rawTask
      ? {
          number: Number(rawTask.number),
          key: String(rawTask.key || ""),
          title: String(rawTask.title || ""),
          instruction: String(rawTask.instruction || ""),
          stem: String(rawTask.stem || ""),
          take: Number(rawTask.take),
          takes_total: Number(rawTask.takes_total),
          stop: String(rawTask.stop || "manual"),
          target_s: Number.isFinite(rawTask.target_s) ? rawTask.target_s : null,
          spoken_demo: rawTask.spoken_demo === true,
          borg: rawTask.borg === true,
        }
      : null,
  };
}

function setSnapshot(next) {
  if (!next) return;
  const previous = app.snapshot;
  app.snapshot = next;
  maybePromptBorg(previous, next);
  if (previous && previous.take_state === "recording" && next.take_state !== "recording") {
    app.takeStartedAt = null;
  }
  render();
}

// One place decides when the Borg question is due: after the last take of a
// task that collects it. Covers the safety-cap auto-stop too, because that
// also arrives as a task_state push.
function maybePromptBorg(previous, next) {
  if (!previous || !previous.task || !previous.task.borg) return;
  if (previous.take_state !== "recording" || next.take_state !== "saved") return;
  const taskFinished =
    previous.redo_mode || !next.task || next.task.number !== previous.task.number;
  if (!taskFinished) return;
  app.pendingBorg = { taskNumber: previous.task.number, title: previous.task.title };
}

async function fetchActive() {
  let data;
  try {
    data = await api("GET", "/api/sessions/active");
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null; // no session open
    throw err;
  }
  if (!data || typeof data !== "object") return null;
  if (data.active === false || data.session_id === null) return null;

  const source = data.session && typeof data.session === "object" ? data.session : data;
  const state = source.state && typeof source.state === "object" ? source.state : source.snapshot;
  const snapshot = normalizeSnapshot(state && typeof state === "object" ? state : source);
  const sessionId = source.session_id || source.sid || data.session_id || null;
  if (!sessionId && !(snapshot && snapshot.phase)) return null;
  return {
    sessionId: sessionId,
    participant: source.participant || data.participant || null,
    // Non-null once recording has failed server-side. The UI must block on it:
    // every further take command would be refused anyway.
    fatal: source.fatal === undefined ? null : source.fatal,
    snapshot: snapshot,
  };
}

// Re-sync from the server. Used on load, after every state-changing POST, and
// whenever the live connection comes back.
async function syncActive() {
  let active;
  try {
    active = await fetchActive();
    clearBanner("resync");
  } catch (err) {
    setBanner(
      "resync",
      "warn",
      `Could not re-check the session with the server: ${err.message} — ` +
        "the screen may be out of date. Reload the page to try again."
    );
    return null;
  }

  if (!active) {
    if (app.sessionId) {
      // The server has no session any more (completed, or the process
      // restarted). Do not pretend one is still running.
      forgetSession();
      app.localStep = app.localStep === "complete" ? "complete" : "start";
      app.startLoaded = false;
    }
    render();
    return null;
  }

  rememberSession(active.sessionId || app.sessionId, active.participant);
  if (active.fatal) showFatal("session_fatal", String(active.fatal));
  if (active.snapshot) setSnapshot(active.snapshot);
  else render();
  return active;
}

// ---------------------------------------------------------------------------
// Screen routing. The server's phase decides; local steps only cover the part
// of the flow that happens before a session exists (and the closing screen).
// ---------------------------------------------------------------------------

function currentScreen() {
  // A reload during a session: show nothing but "re-syncing" until the server
  // has said where the session actually is. Flashing the start screen at an
  // operator mid-session is how a good session gets abandoned.
  if (app.booting) return "unknown";
  if (app.pendingBorg) return "borg";
  const snapshot = app.snapshot;
  if (app.sessionId && snapshot && snapshot.phase) {
    switch (snapshot.phase) {
      case "reference_measures":
        return "reference";
      case "covariates":
        return "covariates";
      case "task_battery":
        return "task";
      case "qc_review":
        return "qc";
      case "complete":
        return "complete";
      default:
        return "unknown";
    }
  }
  return app.localStep;
}

function render() {
  const screen = currentScreen();

  for (const name of SCREEN_IDS) {
    const node = el(`screen-${name}`);
    if (node) node.hidden = name !== screen;
  }

  renderBadge();

  if (screen === "consent") renderConsent();
  else if (screen === "setup") renderSetup();
  else if (screen === "reference") renderReferenceCounts();
  else if (screen === "covariates") renderCovariateCounts();
  else if (screen === "task") renderTask();
  else if (screen === "borg") renderBorg();
  else if (screen === "qc") renderQc();
  else if (screen === "complete") renderComplete();
  else if (screen === "unknown") renderUnknown();

  const meter = el("meter-bar");
  if (meter) meter.hidden = !(app.sessionId && SESSION_SCREENS.has(screen));

  if (screen !== app.shownScreen) {
    const previous = app.shownScreen;
    app.shownScreen = screen;
    onScreenEnter(screen, previous);
  }
}

function onScreenEnter(screen) {
  if (screen === "start" && !app.startLoaded) {
    app.startLoaded = true;
    loadStart();
  }
  if (screen === "qc") loadQc();
}

function renderBadge() {
  const badge = el("session-badge");
  if (!badge) return;
  if (app.sessionId) {
    badge.hidden = false;
    badge.textContent = `${app.participant || "participant"} · ${app.sessionId}`;
  } else if (app.participant && app.localStep !== "start") {
    badge.hidden = false;
    badge.textContent = `${app.participant} · no session yet`;
  } else {
    badge.hidden = true;
  }
}

// ---------------------------------------------------------------------------
// 1. Start screen: what is on disk, the microphone, and who is recording.
// ---------------------------------------------------------------------------

function loadStart() {
  loadSummary();
  loadDevices();
  loadParticipants();
  loadDemoAvailability();
  loadConsentText();
}

async function loadSummary() {
  const block = el("summary-block");
  if (!block) return;
  clear(block);
  block.append(h("p", { class: "muted", text: "Loading…" }));
  let data;
  try {
    data = await api("GET", "/api/summary");
  } catch (err) {
    clear(block);
    block.append(
      h("p", { class: "error", text: `Cannot read what is on disk: ${err.message}` }),
      h("button", { class: "btn btn-quiet", type: "button", onclick: loadSummary }, "Try again")
    );
    return;
  }

  clear(block);
  const counts = data && typeof data === "object" ? data : {};
  const list = h("dl", { class: "kv" });
  const known = [
    ["sessions", "Sessions on disk"],
    ["takes", "Takes on disk"],
    ["partials", "Unfinished .partial files"],
  ];
  const seen = new Set();
  for (const pair of known) {
    if (counts[pair[0]] === undefined) continue;
    seen.add(pair[0]);
    list.append(h("dt", { text: pair[1] }), h("dd", { text: String(counts[pair[0]]) }));
  }
  for (const key of Object.keys(counts)) {
    if (seen.has(key)) continue;
    list.append(h("dt", { text: key }), h("dd", { text: String(counts[key]) }));
  }
  if (!list.childNodes.length) {
    list.append(h("dt", { text: "server reply" }), h("dd", { text: "no counts returned" }));
  }
  block.append(list);

  const partials = Number(counts.partials);
  if (Number.isFinite(partials) && partials > 0) {
    block.append(
      h("p", {
        class: "notice",
        text:
          `${partials} unfinished .partial file(s) on disk. A take did not finish cleanly — ` +
          "check that the previous session ended properly before recording more.",
      })
    );
  }
  const sessions = Number(counts.sessions);
  if (Number.isFinite(sessions) && sessions === 0) {
    block.append(h("p", { class: "muted", text: "No sessions recorded yet." }));
  }
}

async function loadDevices() {
  const block = el("device-block");
  if (!block) return;
  clear(block);
  block.append(h("p", { class: "muted", text: "Loading…" }));
  let data;
  try {
    data = await api("GET", "/api/devices");
  } catch (err) {
    clear(block);
    block.append(
      h("p", { class: "error", text: `Cannot read the input device: ${err.message}` }),
      h("button", { class: "btn btn-quiet", type: "button", onclick: loadDevices }, "Try again")
    );
    // Without a device report there is no way to know the enhancement state,
    // so the manual checklist still has to be confirmed.
    renderChecklist(null, "unknown");
    return;
  }

  const info = data && typeof data === "object" ? data : {};
  const input = info.input && typeof info.input === "object" ? info.input : info;
  const name =
    firstString([info.input_device_name, input.name, info.name, info.device, info.input_device]) ||
    null;
  const gain = firstDefined([info.os_gain_reading, info.gain, input.gain]);
  const status = String(
    firstString([info.enhancement_status, info.enhancements, input.enhancement_status]) || "unknown"
  ).toLowerCase();
  const floor = Number(info.meter_floor_dbfs);
  if (Number.isFinite(floor) && floor < 0) meterFloorDbfs = floor;
  app.deviceName = name;

  clear(block);
  const list = h("dl", { class: "kv" });
  list.append(
    h("dt", { text: "Input device" }),
    h("dd", {
      text: (name || "not reported") + (info.host_api ? ` — ${info.host_api}` : ""),
    })
  );
  list.append(
    h("dt", { text: "OS gain reading" }),
    h("dd", {
      text:
        gain === undefined || gain === null
          ? `unreadable${info.os_gain_note ? ` — ${info.os_gain_note}` : ""}`
          : String(gain),
    })
  );
  list.append(h("dt", { text: "Audio enhancements" }), h("dd", { text: status }));
  if (info.capture_sample_rate_hz !== undefined) {
    list.append(
      h("dt", { text: "Capture format" }),
      h("dd", {
        text: `${info.capture_sample_rate_hz} Hz · ${info.capture_subtype || ""} · ${
          info.capture_channels === 1 ? "mono" : `${info.capture_channels} ch`
        }`,
      })
    );
  }
  block.append(list);
  block.append(
    h("details", {}, h("summary", { text: "Full device report (logged with the session)" }), jsonBlock(info))
  );
  block.append(
    h("p", {
      class: "muted",
      text: "This application can only read the gain. There is no control anywhere in it that changes the input gain.",
    })
  );

  // Warnings come from the server, which knows the host API. A rate
  // mismatch means resampling on MME and DirectSound, and means nothing on
  // WDM-KS and WASAPI: those open the hardware pin and would have refused
  // the rate outright rather than quietly converting. Re-deriving that rule
  // here is how this screen ended up warning about a path measured to be
  // bit-exact.
  const deviceWarnings = asArray(info.warnings).filter(function (each) {
    return typeof each === "string" && each.trim();
  });
  if (deviceWarnings.length) {
    for (const warning of deviceWarnings) {
      block.append(h("p", { class: "notice", text: warning }));
    }
  } else if (info.host_api) {
    block.append(
      h("p", {
        class: "ok-note",
        text: `Recording via ${info.host_api}. Nothing in this path alters the signal before capture.`,
      })
    );
  }

  // The wording comes from the server when it supplies it, so there is one
  // copy of the checklist and it cannot drift out of step with the run log.
  const supplied = asArray(info.manual_checklist).filter((each) => typeof each === "string");
  const micSteps = asArray(info.microphone_checklist).filter((each) => typeof each === "string");
  renderChecklist(name, status, (supplied.concat(micSteps)).length ? supplied.concat(micSteps) : ENHANCEMENT_CHECKLIST);
  loadMicPicker();
}


// ---------------------------------------------------------------------------
// Microphone picker.
//
// The same physical microphone appears once per host API and they are NOT
// equivalent: MME and DirectSound run through the Windows mixer and resample
// silently, while WASAPI and WDM-KS open the hardware directly. The server
// probes every device at the study's exact capture format and returns the
// warnings rendered here, so the operator sees the problem BEFORE recording
// rather than discovering it in the audio afterwards.

async function loadMicPicker() {
  const block = el("mic-picker");
  if (!block) return;
  clear(block);
  block.append(h("p", { class: "muted", text: "Looking for microphones…" }));

  let data;
  try {
    data = await api("GET", "/api/devices/inputs");
  } catch (err) {
    clear(block);
    block.append(
      h("p", { class: "error", text: `Cannot list microphones: ${err.message}` }),
      h("button", { class: "btn btn-quiet", type: "button", onclick: loadMicPicker }, "Try again")
    );
    return;
  }

  const groups = asArray(data.groups);
  const required = data.required || {};
  const locked = data.session_active === true;
  const offered = groups.filter(function (g) {
    return g.offer_by_default === true;
  });
  const hidden = groups.length - offered.length;
  const shown = app.showAllMics ? groups : offered;

  clear(block);

  if (locked) {
    block.append(
      h("p", {
        class: "notice",
        text:
          "A session is in progress, so the microphone cannot be changed. " +
          "Swapping devices mid-session would make its takes incomparable.",
      })
    );
  }

  if (!shown.length) {
    block.append(
      h("p", {
        class: "error",
        text: "No microphone can record " + (required.sample_rate_hz || 48000) + " Hz mono. Is it plugged in?",
      }),
      h("button", { class: "btn btn-quiet", type: "button", onclick: loadMicPicker }, "Rescan")
    );
    return;
  }

  // A select, not a grid of tiles: Windows lists the same microphone once per
  // host API, so the raw list is mostly aliases of two or three real devices.
  const select = h("select", { class: "mic-select", id: "mic-select", disabled: locked });
  let selectedGroup = null;
  for (const group of shown) {
    const path =
      group.recommended === true
        ? "clean signal path"
        : group.supports_capture
        ? "usable, with warnings"
        : "cannot record the study format";
    const extra = [];
    if (group.path_count > 1) extra.push(`${group.path_count} paths`);
    if (group.is_os_default) extra.push("Windows default");
    if (group.is_virtual) extra.push("not a real microphone");
    if (group.is_output_pin) extra.push("speaker, not a microphone");

    const option = h("option", {
      value: String(group.index),
      selected: group.is_selected === true,
      disabled: !group.supports_capture,
      text: `${group.name} — ${group.host_api} · ${path}${extra.length ? " · " + extra.join(" · ") : ""}`,
    });
    if (group.is_selected) selectedGroup = group;
    select.append(option);
  }
  select.addEventListener("change", function () {
    selectMic(Number(select.value));
  });

  block.append(h("label", { class: "stacked" }, "Microphone", select));

  // What the current choice actually means, spelled out rather than left in
  // a dropdown line the operator has already stopped reading.
  if (!data.selected) {
    block.append(
      h("p", {
        class: "notice",
        text:
          "No microphone chosen, so Windows' default is used — often a path " +
          "that resamples. Pick one above before the first session.",
      })
    );
  } else if (selectedGroup && asArray(selectedGroup.warnings).length) {
    for (const warning of selectedGroup.warnings) {
      block.append(h("p", { class: "notice", text: warning }));
    }
  } else if (selectedGroup) {
    block.append(
      h("p", {
        class: "ok-note",
        text: `Recording from ${selectedGroup.name} via ${selectedGroup.host_api} at ${Math.round(
          selectedGroup.default_samplerate
        )} Hz. Nothing in this path alters the signal.`,
      })
    );
  }

  const actions = h("div", { class: "row-actions row-actions-left" });
  actions.append(h("button", { class: "btn btn-quiet btn-small", type: "button", onclick: loadMicPicker }, "Rescan"));
  if (data.selected && !locked) {
    actions.append(
      h("button", { class: "btn btn-quiet btn-small", type: "button", onclick: clearMicSelection }, "Use Windows default")
    );
  }
  if (hidden > 0 || app.showAllMics) {
    actions.append(
      h(
        "button",
        {
          class: "btn btn-quiet btn-small",
          type: "button",
          onclick: function () {
            app.showAllMics = !app.showAllMics;
            loadMicPicker();
          },
        },
        app.showAllMics
          ? "Hide virtual and unusable inputs"
          : `Show ${hidden} hidden input${hidden === 1 ? "" : "s"}`
      )
    );
  }
  block.append(actions);

  if (hidden > 0 && !app.showAllMics) {
    block.append(
      h("p", {
        class: "hint",
        text:
          `${hidden} entr${hidden === 1 ? "y is" : "ies are"} hidden: virtual ` +
          "routers, speaker pins Windows exposes as inputs, and devices that " +
          "cannot record the study's format.",
      })
    );
  }
}

async function selectMic(index) {
  const block = el("mic-picker");
  try {
    await api("POST", "/api/devices/select", { index: index });
  } catch (err) {
    if (block) block.append(h("p", { class: "error", text: `Could not select that microphone: ${err.message}` }));
    return;
  }
  await loadMicPicker();
  await loadDevices();
}

async function clearMicSelection() {
  const block = el("mic-picker");
  try {
    await api("POST", "/api/devices/select/clear");
  } catch (err) {
    if (block) block.append(h("p", { class: "error", text: `Could not clear the selection: ${err.message}` }));
    return;
  }
  await loadMicPicker();
}

function firstString(candidates) {
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return null;
}

function firstDefined(candidates) {
  for (const candidate of candidates) {
    if (candidate !== undefined && candidate !== null) return candidate;
  }
  return undefined;
}

function checklistKey(deviceName) {
  return `vc.checklist.${CHECKLIST_VERSION}.${deviceName || "unknown-device"}`;
}

function readChecklistDone(deviceName) {
  try {
    return window.localStorage.getItem(checklistKey(deviceName)) === "confirmed";
  } catch (err) {
    console.warn("localStorage unavailable; the checklist must be confirmed again", err);
    return false;
  }
}

function writeChecklistDone(deviceName, done) {
  try {
    if (done) window.localStorage.setItem(checklistKey(deviceName), "confirmed");
    else window.localStorage.removeItem(checklistKey(deviceName));
  } catch (err) {
    console.warn("localStorage unavailable; the confirmation is not remembered", err);
  }
}

function renderChecklist(deviceName, status, items) {
  const block = el("checklist-block");
  if (!block) return;
  const steps = asArray(items).length ? items : ENHANCEMENT_CHECKLIST;
  clear(block);

  if (status === "off") {
    app.checklistOk = true;
    block.append(h("p", { class: "ok-note", text: "The OS reports that input enhancements are off." }));
    renderPicker();
    return;
  }

  if (status === "on") {
    app.checklistOk = false;
    block.append(
      h("p", {
        class: "error",
        text:
          "The OS reports that input enhancements are ON. They must be turned off before recording — " +
          "they destroy the measures this study depends on.",
      })
    );
  }

  if (readChecklistDone(deviceName)) {
    app.checklistOk = true;
    block.append(
      h("p", { class: "ok-note", text: `Microphone checklist confirmed for: ${deviceName || "this device"}.` }),
      h(
        "button",
        {
          class: "btn btn-quiet",
          type: "button",
          onclick: function () {
            writeChecklistDone(deviceName, false);
            renderChecklist(deviceName, status, steps);
          },
        },
        "Check it again"
      )
    );
    renderPicker();
    return;
  }

  app.checklistOk = false;
  block.append(
    h("p", {
      class: "notice",
      text: "The enhancement setting cannot be read on this machine. Confirm every line by hand before the first session.",
    })
  );

  const list = h("ul", { class: "checklist" });
  const boxes = [];
  const confirm = h("button", { class: "btn btn-go", type: "button", disabled: true }, "All confirmed");

  // Ticking fifteen boxes one at a time, under time pressure, is how a
  // checklist stops being read at all. The bulk control is honest about what
  // it asserts, and the count keeps the number of steps visible.
  const counter = h("span", { class: "hint" });
  const selectAll = h("input", { type: "checkbox", id: "chk-all" });

  function refresh() {
    const ticked = boxes.filter(function (each) {
      return each.checked;
    }).length;
    confirm.disabled = ticked !== boxes.length;
    counter.textContent = `${ticked} of ${boxes.length} confirmed`;
    selectAll.checked = ticked === boxes.length;
    // Part-way through reads as neither on nor off, so the control does not
    // claim everything is done when it is not.
    selectAll.indeterminate = ticked > 0 && ticked < boxes.length;
  }

  selectAll.addEventListener("change", function () {
    const value = selectAll.checked;
    for (const box of boxes) box.checked = value;
    refresh();
  });

  for (let i = 0; i < steps.length; i += 1) {
    const box = h("input", { type: "checkbox", id: `chk-${i}` });
    box.addEventListener("change", refresh);
    boxes.push(box);
    list.append(h("li", {}, box, h("label", { for: `chk-${i}`, text: steps[i] })));
  }

  const head = h(
    "div",
    { class: "checklist-head" },
    h(
      "label",
      { class: "select-all", for: "chk-all" },
      selectAll,
      h("span", { text: "I have done all of these" })
    ),
    counter
  );

  confirm.addEventListener("click", function () {
    writeChecklistDone(deviceName, true);
    renderChecklist(deviceName, status, steps);
  });
  block.append(head, list, h("div", { class: "row-actions" }, confirm));
  refresh();
  renderPicker();
}

async function loadParticipants() {
  const block = el("participant-block");
  if (!block) return;
  clear(block);
  block.append(h("p", { class: "muted", text: "Loading…" }));
  let data;
  try {
    data = await api("GET", "/api/participants");
  } catch (err) {
    clear(block);
    block.append(
      h("p", { class: "error", text: `Cannot read the participant list: ${err.message}` }),
      h("button", { class: "btn btn-quiet", type: "button", onclick: loadParticipants }, "Try again")
    );
    return;
  }

  const rows = Array.isArray(data) ? data : asArray(data && data.participants);
  app.participants = rows.map(normalizeParticipant).filter((each) => each.pseudonym);
  renderPicker();
}

// The consent wording is served from config.CONSENT_POINTS so the screen and
// the stored consent record can never drift apart. It is never written into
// this file: a front-end copy of a GDPR text is a copy that goes stale.
async function loadConsentText() {
  try {
    const data = await api("GET", "/api/participants/consent-text");
    app.consentPoints = asArray(data && data.points).filter((each) => typeof each === "string");
    app.consentVersion = firstString([data && data.version]);
    app.consentError = null;
  } catch (err) {
    app.consentPoints = [];
    app.consentVersion = null;
    app.consentError = err.message;
  }
  if (app.shownScreen === "consent") renderConsent();
}

// Guards against fetching the list over and over when the participant simply
// has no record on the server.
let participantLookupInFlight = false;

async function ensureParticipantRecord() {
  if (!app.participant || participantLookupInFlight) return;
  if (app.participantRecord && app.participantRecord.pseudonym === app.participant) return;
  participantLookupInFlight = true;
  try {
    const data = await api("GET", "/api/participants");
    const rows = Array.isArray(data) ? data : asArray(data && data.participants);
    app.participants = rows.map(normalizeParticipant).filter((each) => each.pseudonym);
    for (const row of app.participants) {
      if (row.pseudonym === app.participant) app.participantRecord = row;
    }
    if (!app.participantRecord || app.participantRecord.pseudonym !== app.participant) {
      // Keep a placeholder so the passage panel stops saying "looking up".
      app.participantRecord = { pseudonym: app.participant, consented: null, passage: null, passageText: null };
    }
    clearBanner("participant");
    if (app.shownScreen === "task") renderTask();
  } catch (err) {
    setBanner(
      "participant",
      "warn",
      `Could not read the participant record: ${err.message} — the fixed passage cannot be shown on screen.`
    );
  } finally {
    participantLookupInFlight = false;
  }
}

function normalizeParticipant(raw) {
  if (typeof raw === "string") {
    return { pseudonym: raw, consented: null, passage: null, passageText: null, nextSession: null, expected: null };
  }
  const row = raw && typeof raw === "object" ? raw : {};
  let consented = firstDefined([row.has_consent, row.consented]);
  if (typeof consented !== "boolean") consented = null;
  let passage = firstDefined([row.passage_set, row.passage_configured, row.has_passage]);
  if (typeof passage !== "boolean") {
    passage = typeof row.passage_text === "string" ? row.passage_text.trim().length > 0 : null;
  }
  return {
    pseudonym: firstString([row.pseudonym, row.id, row.participant, row.name]),
    consented: consented,
    passage: passage,
    passageText: typeof row.passage_text === "string" ? row.passage_text : null,
    nextSession: Number.isFinite(row.next_session_number) ? row.next_session_number : null,
    expected: Number.isFinite(row.expected_sessions) ? row.expected_sessions : null,
  };
}

function renderPicker() {
  const block = el("participant-block");
  if (!block) return;
  clear(block);

  if (!app.participants.length) {
    block.append(
      h("p", {
        class: "notice",
        text: "No participants yet. Add the first one below.",
      })
    );
    block.append(participantForm());
    return;
  }

  if (!app.checklistOk) {
    block.append(
      h("p", { class: "notice", text: "Confirm the microphone checklist above before the first session." })
    );
  }

  const picker = h("div", { class: "picker" });
  for (const participant of app.participants) {
    const sub = [];
    if (participant.consented === true) sub.push("consent recorded");
    else if (participant.consented === false) sub.push("consent needed first");
    else sub.push("consent status unknown — will ask");
    if (Number.isFinite(participant.nextSession)) {
      sub.push(
        `next: session ${participant.nextSession}${
          Number.isFinite(participant.expected) ? ` of ${participant.expected}` : ""
        }`
      );
    }
    if (participant.passage === false) sub.push("no passage set for task 6");

    // The chooser and the passage editor sit side by side rather than
    // nested: a button inside a button is invalid HTML and the inner one
    // would never receive its own clicks.
    const cell = h("div", { class: "picker-cell" });
    cell.append(
      h(
        "button",
        {
          class: "btn",
          type: "button",
          disabled: !app.checklistOk,
          onclick: function () {
            choose(participant);
          },
        },
        h("span", { class: "name", text: participant.pseudonym }),
        h("span", { class: participant.passage === false ? "sub sub-warn" : "sub", text: sub.join(" · ") })
      )
    );
    const tools = h("div", { class: "cell-tools" });
    tools.append(
      h(
        "button",
        {
          class: "btn btn-quiet btn-small",
          type: "button",
          onclick: function () {
            editPassage(participant);
          },
        },
        participant.passage === false ? "Set passage" : "Change passage"
      )
    );
    // Removing a mistyped pseudonym is not the same act as withdrawing
    // someone from the study. The server refuses this once any session has
    // been recorded and points at withdrawal, which also deletes the audio
    // and records that it happened.
    const started = Number.isFinite(participant.nextSession) && participant.nextSession > 1;
    tools.append(
      h(
        "button",
        {
          class: "btn btn-quiet btn-small btn-danger-quiet",
          type: "button",
          title: started
            ? "This person has recordings — use Withdraw on the dashboard"
            : "Remove this pseudonym from the register",
          onclick: function () {
            removeParticipant(participant, started);
          },
        },
        "Remove"
      )
    );
    cell.append(tools);
    picker.append(cell);
  }
  block.append(picker);
  block.append(participantForm());
}

// ---------------------------------------------------------------------------
// Add or update a participant from the home screen.
//
// The registry stores pseudonyms only: the name-to-pseudonym key lives
// outside the data directory entirely and is the operator's to keep (GDPR,
// ARCHITECTURE.md §11). The passage is the fixed connected-speech text for
// task 6, reused identically every session, so it belongs with the person
// rather than being retyped.

function participantForm(prefill) {
  const values = prefill || { pseudonym: "", passage: "" };
  const wrap = h("div", { class: "card-inset block" });
  wrap.append(
    h("h3", { text: values.pseudonym ? `Passage for ${values.pseudonym}` : "Add a participant" })
  );

  const idInput = h("input", {
    type: "text",
    id: "new-participant-id",
    autocomplete: "off",
    spellcheck: "false",
    placeholder: "e.g. P01",
    value: values.pseudonym,
  });
  const passageInput = h("textarea", {
    id: "new-participant-passage",
    rows: "3",
    spellcheck: "false",
    placeholder: "The passage this person reads every session, in their own language.",
  });
  passageInput.value = values.passage || "";

  if (values.pseudonym) idInput.readOnly = true;
  const idField = h("label", { class: "stacked" }, "Pseudonym", idInput);
  const passageField = h(
    "label",
    { class: "stacked" },
    "Connected-speech passage (task 6)",
    passageInput
  );

  wrap.append(idField, passageField);
  wrap.append(
    h("p", {
      class: "hint",
      text:
        "Pseudonyms only — never a real name. Keep the name-to-pseudonym key " +
        "separately, away from this laptop's data folder. Leave the passage " +
        "blank for now if it is not decided yet; it can be added later and an " +
        "existing passage is never overwritten by a blank one.",
    })
  );

  const error = h("p", { class: "error", hidden: true });
  const save = h(
    "button",
    {
      class: "btn btn-go",
      type: "button",
      onclick: function () {
        saveParticipant(idInput.value, passageInput.value, save, error);
      },
    },
    values.pseudonym ? "Save passage" : "Add participant"
  );
  const actions = h("div", { class: "row-actions row-actions-left" }, save);
  if (values.pseudonym) {
    actions.append(
      h("button", { class: "btn btn-quiet", type: "button", onclick: renderPicker }, "Cancel")
    );
  }
  wrap.append(actions, error);
  return wrap;
}

async function saveParticipant(pseudonym, passage, button, error) {
  const id = String(pseudonym || "").trim();
  error.hidden = true;
  if (!id) {
    error.hidden = false;
    error.textContent = "Enter a pseudonym first.";
    return;
  }
  button.disabled = true;
  const label = button.textContent;
  button.textContent = "Saving…";
  try {
    // null (not "") means "leave any existing passage alone", so re-adding
    // someone cannot wipe the text they have been reading all week.
    const text = String(passage || "").trim();
    await api("POST", "/api/participants", {
      pseudonym: id,
      passage_text: text ? text : null,
    });
  } catch (err) {
    error.hidden = false;
    error.textContent = `Not saved: ${err.message}`;
    return;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
  await loadParticipants();
}

async function removeParticipant(participant, hasRecordings) {
  const block = el("participant-block");
  if (hasRecordings) {
    window.alert(
      `${participant.pseudonym} already has recorded sessions.

` +
        "Use Withdraw on the adherence dashboard instead. That deletes their " +
        "audio and metadata as well, and records that the withdrawal " +
        "happened — removing only the name here would leave the recordings " +
        "behind with nothing describing them."
    );
    return;
  }
  const sure = window.confirm(
    `Remove ${participant.pseudonym} from the participant list?

` +
      "They have no recordings, so this only clears the pseudonym and any " +
      "consent record. It cannot be undone, but nothing is lost."
  );
  if (!sure) return;
  try {
    await api("DELETE", `/api/participants/${encodeURIComponent(participant.pseudonym)}`);
  } catch (err) {
    if (block) {
      block.append(h("p", { class: "error", text: `Not removed: ${err.message}` }));
    }
    return;
  }
  if (app.participant === participant.pseudonym) {
    app.participant = null;
    app.participantRecord = null;
  }
  await loadParticipants();
}

function editPassage(participant) {
  const block = el("participant-block");
  if (!block) return;
  clear(block);
  block.append(
    h("button", { class: "btn btn-quiet", type: "button", onclick: renderPicker }, "Back to the list")
  );
  // Pre-fill with what is stored, so an edit is an edit rather than a retype.
  block.append(
    participantForm({
      pseudonym: participant.pseudonym,
      passage: participant.passageText || "",
    })
  );
}

function choose(participant) {
  app.participant = participant.pseudonym;
  app.participantRecord = participant;
  rememberSession(null, participant.pseudonym);
  hideInline("consent-error");
  hideInline("setup-error");
  app.localStep = participant.consented === true ? "setup" : "consent";
  render();
}

// ---------------------------------------------------------------------------
// UTC entry. Nothing is ever pre-filled from the laptop clock: the habitat
// clocks are deliberately scrambled (CLAUDE.md, "Time").
// ---------------------------------------------------------------------------

function readUtc(prefix) {
  const date = el(`${prefix}-utc-date`);
  const time = el(`${prefix}-utc-time`);
  if (!date || !time) return null;
  const day = date.value;
  let clock = time.value;
  if (!day || !clock) return null;
  if (clock.length === 5) clock = `${clock}:00`;
  return `${day}T${clock}`;
}

// Open the browser's own calendar / clock as soon as the field is touched.
//
// <input type="date"> and type="time" already carry native pickers, but they
// only open from a small icon at the right-hand edge, which is fiddly for a
// tired operator. showPicker() opens the same native widget from anywhere in
// the field. It is Chrome/Edge 99+; where it is missing, or where the browser
// refuses because the call did not come from a user gesture, the field still
// works exactly as before by typing — hence the guarded call rather than a
// hand-built calendar we would have to maintain.
//
// There is deliberately NO "use the current time" button anywhere near these
// fields. The habitat clocks are scrambled on purpose, so the laptop cannot
// tell the time; filling this from the device clock would silently record a
// fiction as the trusted timestamp (CLAUDE.md, Time).
function attachNativePicker(node) {
  if (!node || typeof node.showPicker !== "function") return;
  const open = function () {
    try {
      node.showPicker();
    } catch (err) {
      // NotAllowedError (no user gesture) or unsupported: typing still works.
    }
  };
  node.addEventListener("click", open);
  node.addEventListener("keydown", function (event) {
    // Enter or Space on a focused field opens the picker too, so the flow
    // works without a mouse.
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });
}

// Nudge the entered time without reopening the picker. Reading a watch
// usually means "the picker got me close, now match the seconds", and
// retyping the whole field for that is where mistakes come from. These shift
// the value the OPERATOR entered; no clock on this machine is consulted.
function nudgeUtc(prefix, minutes, announce) {
  const date = el(`${prefix}-utc-date`);
  const time = el(`${prefix}-utc-time`);
  if (!date || !time || !date.value || !time.value) {
    if (announce) announce("Pick a date and time first, then adjust.");
    return;
  }
  let clock = time.value;
  if (clock.length === 5) clock = `${clock}:00`;
  // Parsed as UTC and formatted back as UTC, so this arithmetic never picks
  // up the machine's timezone.
  const asUtc = new Date(`${date.value}T${clock}Z`);
  if (Number.isNaN(asUtc.getTime())) {
    if (announce) announce("That date and time could not be read.");
    return;
  }
  asUtc.setUTCMinutes(asUtc.getUTCMinutes() + minutes);
  const iso = asUtc.toISOString();
  date.value = iso.slice(0, 10);
  // hh:mm to match the field's step: the operator never enters seconds, and
  // every nudge is a whole number of minutes, so they are always :00.
  time.value = iso.slice(11, 16);
  date.dispatchEvent(new Event("input", { bubbles: true }));
}

function utcNudgeControls(prefix, onChange) {
  const row = h("div", { class: "row-actions row-actions-left nudge-row" });
  const note = h("span", { class: "hint" });
  const announce = function (text) {
    note.textContent = text || "";
  };
  const steps = [
    ["−1 h", -60],
    ["−1 min", -1],
    ["+1 min", 1],
    ["+1 h", 60],
  ];
  for (const [label, minutes] of steps) {
    row.append(
      h(
        "button",
        {
          class: "btn btn-quiet btn-small",
          type: "button",
          onclick: function () {
            announce("");
            nudgeUtc(prefix, minutes, announce);
            if (onChange) onChange(readUtc(prefix));
          },
        },
        label
      )
    );
  }
  row.append(note);
  return row;
}

function wireUtc(prefix, onChange) {
  for (const suffix of ["-utc-date", "-utc-time"]) {
    const node = el(prefix + suffix);
    if (!node) continue;
    attachNativePicker(node);
    node.addEventListener("input", function () {
      const value = readUtc(prefix);
      const preview = el(`${prefix}-utc-preview`);
      if (preview) preview.textContent = value ? `${value} UTC` : "—";
      if (onChange) onChange(value);
    });
  }
  // Added once, from JS, so the two screens that enter a UTC time cannot
  // drift apart.
  const entry = el(`${prefix}-utc-date`);
  const container = entry && entry.closest(".utc-entry");
  if (container) {
    const next = container.nextElementSibling;
    const alreadyThere = next && next.classList && next.classList.contains("nudge-row");
    if (!alreadyThere) {
      container.parentNode.insertBefore(
        utcNudgeControls(prefix, onChange),
        container.nextSibling
      );
    }
  }
}

// ---------------------------------------------------------------------------
// 2. Consent.
// ---------------------------------------------------------------------------

function renderConsent() {
  setText("consent-who", app.participant || "");
  const list = el("consent-points");
  if (!list) return;
  clear(list);

  if (!app.consentPoints.length) {
    list.append(
      h(
        "li",
        { class: "error" },
        h("span", {
          text:
            "The consent text could not be loaded from the server, so it cannot be shown or agreed to. " +
            "Nothing may be recorded without consent. " +
            (app.consentError ? `Server said: ${app.consentError}` : ""),
        }),
        h("button", { class: "btn btn-quiet", type: "button", onclick: loadConsentText }, "Try again")
      )
    );
  } else {
    for (const point of app.consentPoints) list.append(h("li", { text: point }));
  }
  setText("consent-version", app.consentVersion ? `Consent version ${app.consentVersion}` : "");
  updateConsentSubmit();
}

function updateConsentSubmit() {
  const submit = el("consent-submit");
  const agree = el("consent-agree");
  if (!submit || !agree) return;
  submit.disabled = !(agree.checked && app.consentPoints.length && readUtc("consent"));
}

function markConsented() {
  for (const participant of app.participants) {
    if (participant.pseudonym === app.participant) participant.consented = true;
  }
  if (app.participantRecord) app.participantRecord.consented = true;
}

async function onConsentSubmit() {
  const submit = el("consent-submit");
  const utc = readUtc("consent");
  if (!utc || !app.participant) return;
  hideInline("consent-error");
  submit.disabled = true;
  const label = submit.textContent;
  submit.textContent = "Recording consent…";
  try {
    await api("POST", `/api/participants/${encodeURIComponent(app.participant)}/consent`, {
      agreed: true,
      utc_operator_entered: utc,
    });
    markConsented();
    app.localStep = "setup";
    render();
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      // Already on disk. Re-consent is a protocol event, never a silent
      // replace — so offer the only safe move: carry on.
      const node = el("consent-error");
      if (node) {
        clear(node);
        node.hidden = false;
        node.append(
          h("span", { text: err.message }),
          h(
            "button",
            {
              class: "btn btn-quiet",
              type: "button",
              onclick: function () {
                markConsented();
                app.localStep = "setup";
                render();
              },
            },
            "Keep the existing record and continue"
          )
        );
      }
    } else {
      showInline("consent-error", `Consent was NOT recorded: ${err.message}`);
    }
  } finally {
    submit.textContent = label;
    updateConsentSubmit();
  }
}

// ---------------------------------------------------------------------------
// 3. Session setup.
// ---------------------------------------------------------------------------

function renderSetup() {
  setText("setup-who", app.participant || "");
  updateSetupSubmit();
}

function updateSetupSubmit() {
  const submit = el("setup-submit");
  if (!submit) return;
  const utc = readUtc("setup");
  submit.disabled = !utc;
  submit.textContent = utc ? "Start session" : "Enter the UTC time first";
}

async function onSetupSubmit() {
  const submit = el("setup-submit");
  const utc = readUtc("setup");
  if (!utc || !app.participant) return;
  hideInline("setup-error");

  const form = readForm(el("screen-setup"));
  if (form.errors.length) {
    showInline("setup-error", form.errors.join(" "));
    return;
  }

  submit.disabled = true;
  submit.textContent = "Starting…";
  try {
    const body = Object.assign({ participant: app.participant, utc_operator_entered: utc }, form.values);
    const data = await api("POST", "/api/sessions", body);
    const sessionId =
      (data && (data.session_id || data.sid)) || (data && data.session && data.session.session_id) || null;
    rememberSession(sessionId, app.participant);
    const snapshot = normalizeSnapshot(data && data.state ? data.state : data);
    if (snapshot && snapshot.phase) setSnapshot(snapshot);
    const active = await syncActive();
    if (!app.sessionId && !active) {
      showInline(
        "setup-error",
        "The server did not return a session id and reports no active session. " +
          "The session may not have started — do not record until this is resolved."
      );
    }
  } catch (err) {
    showInline("setup-error", `The session did not start: ${err.message}`);
  } finally {
    updateSetupSubmit();
  }
}

// ---------------------------------------------------------------------------
// Forms. Three states per field, exactly as capture.domain.models encodes them:
//   value        -> answered
//   "n/a"        -> operator marked it not available
//   omitted/null -> never asked
// Nothing here can block progression (CLAUDE.md, Metadata).
// ---------------------------------------------------------------------------

function readForm(root) {
  const values = {};
  const errors = [];
  if (!root) return { values: values, errors: errors };

  for (const field of root.querySelectorAll("[data-field]")) {
    const name = field.dataset.field;
    const type = field.dataset.type || "text";
    const na = field.querySelector(".na-btn");
    if (na && na.getAttribute("aria-pressed") === "true") {
      values[name] = "n/a";
      continue;
    }
    if (type === "choice") {
      const chosen = field.querySelector(".choice[aria-pressed='true']");
      if (chosen) values[name] = coerceChoice(chosen.dataset.value);
      continue;
    }
    const input = field.querySelector("input, textarea, select");
    if (!input) continue;
    const raw = String(input.value || "").trim();
    if (!raw) continue; // never asked
    if (type === "number" || type === "int") {
      const parsed = Number(raw);
      if (!Number.isFinite(parsed)) {
        errors.push(`"${raw}" is not a number for ${name.replace(/_/g, " ")}.`);
        continue;
      }
      values[name] = type === "int" ? Math.round(parsed) : parsed;
    } else {
      values[name] = raw;
    }
  }
  return { values: values, errors: errors };
}

function coerceChoice(value) {
  if (value === "true") return true;
  if (value === "false") return false;
  const parsed = Number(value);
  return Number.isFinite(parsed) && String(parsed) === value ? parsed : value;
}

function countAnswered(root) {
  let answered = 0;
  let total = 0;
  if (!root) return { answered: 0, total: 0 };
  for (const field of root.querySelectorAll("[data-field]")) {
    total += 1;
    const na = field.querySelector(".na-btn");
    if (na && na.getAttribute("aria-pressed") === "true") {
      answered += 1;
      continue;
    }
    if ((field.dataset.type || "text") === "choice") {
      if (field.querySelector(".choice[aria-pressed='true']")) answered += 1;
      continue;
    }
    const input = field.querySelector("input, textarea, select");
    if (input && String(input.value || "").trim()) answered += 1;
  }
  return { answered: answered, total: total };
}

function wireForm(root, onChange) {
  if (!root) return;
  for (const field of root.querySelectorAll("[data-field]")) {
    const na = field.querySelector(".na-btn");
    if (na) {
      na.addEventListener("click", function () {
        const now = na.getAttribute("aria-pressed") !== "true";
        na.setAttribute("aria-pressed", now ? "true" : "false");
        field.classList.toggle("na", now);
        if (now) {
          // What is on screen is what gets sent: clear any stale answer.
          for (const input of field.querySelectorAll("input, textarea, select")) input.value = "";
          for (const choice of field.querySelectorAll(".choice")) choice.setAttribute("aria-pressed", "false");
        }
        for (const input of field.querySelectorAll("input, textarea, select")) input.disabled = now;
        if (onChange) onChange();
      });
    }
    for (const choice of field.querySelectorAll(".choice")) {
      choice.addEventListener("click", function () {
        const now = choice.getAttribute("aria-pressed") !== "true";
        for (const sibling of field.querySelectorAll(".choice")) sibling.setAttribute("aria-pressed", "false");
        choice.setAttribute("aria-pressed", now ? "true" : "false");
        if (now && na) {
          na.setAttribute("aria-pressed", "false");
          field.classList.remove("na");
        }
        if (onChange) onChange();
      });
    }
    for (const input of field.querySelectorAll("input, textarea, select")) {
      input.addEventListener("input", function () {
        if (na && na.getAttribute("aria-pressed") === "true") {
          na.setAttribute("aria-pressed", "false");
          field.classList.remove("na");
        }
        if (onChange) onChange();
      });
    }
  }
}

function renderCounts(rootId, outId, noun) {
  const counts = countAnswered(el(rootId));
  const missing = counts.total - counts.answered;
  setText(
    outId,
    missing === 0
      ? `All ${counts.total} ${noun} filled in.`
      : `${counts.answered} of ${counts.total} ${noun} filled in. ${missing} left blank will be recorded as "never asked" — that does not stop the session.`
  );
}

function renderReferenceCounts() {
  renderCounts("screen-reference", "reference-count", "measures");
}

function renderCovariateCounts() {
  renderCounts("screen-covariates", "covariates-count", "covariates");
}

async function submitBlock(screenId, path, errorId, buttonId, label) {
  const button = el(buttonId);
  const form = readForm(el(screenId));
  hideInline(errorId);
  if (form.errors.length) {
    showInline(errorId, form.errors.join(" "));
    return;
  }
  if (!app.sessionId) {
    showInline(errorId, "There is no active session id. Reload the page to re-sync with the server.");
    return;
  }
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const data = await api("POST", `/api/sessions/${encodeURIComponent(app.sessionId)}${path}`, form.values);
    const snapshot = normalizeSnapshot(data && data.state ? data.state : data);
    if (snapshot && snapshot.phase) setSnapshot(snapshot);
    await syncActive();
  } catch (err) {
    showInline(errorId, `Not saved: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

// ---------------------------------------------------------------------------
// 5. Task battery.
// ---------------------------------------------------------------------------

function renderTask() {
  const snapshot = app.snapshot;
  const task = snapshot && snapshot.task;
  const button = el("take-button");
  if (!button) return;

  if (!task) {
    setText("task-progress", "");
    setText("task-title", "Waiting for the server");
    setText("task-instruction", "No task is currently open. Re-sync to find out where the session is.");
    setText("task-shape", "");
    const stale = el("task-stem");
    if (stale) stale.hidden = true;
    clear(el("task-passage"));
    clear(el("task-demo"));
    const idleTimer = el("take-elapsed");
    if (idleTimer) idleTimer.hidden = true;
    button.disabled = true;
    button.textContent = "—";
    return;
  }

  const step =
    Number.isFinite(snapshot.slot_index) && Number.isFinite(snapshot.total_slots)
      ? ` · recording ${snapshot.slot_index + 1} of ${snapshot.total_slots}`
      : "";
  setText(
    "task-progress",
    `Task ${task.number}${step} · take ${task.take} of ${task.takes_total}${
      snapshot.redo_mode ? " · RE-RECORD" : ""
    }`
  );
  setText("task-title", task.title);
  setText("task-instruction", task.instruction);

  const stemChip = el("task-stem");
  if (stemChip) {
    const label = STEM_LABELS[task.stem];
    const different = task.stem && task.stem !== task.key;
    stemChip.hidden = !different;
    stemChip.textContent = different ? label || task.stem.replace(/_/g, " ") : "";
  }

  setText("task-shape", shapeLine(task));
  renderPassage(task);
  renderDemo(task, snapshot.take_state === "recording");
  renderTakeResult();
  renderRedoNote(snapshot);

  const recording = snapshot.take_state === "recording";
  const autoStop = task.stop === "auto";
  button.classList.toggle("recording", recording);
  if (recording) {
    // An auto-stop take is stopped by the server, and the start request stays
    // open for its whole length — so the button is disabled while that request
    // is in flight. If it is NOT in flight and the take is still recording,
    // something went wrong: the operator gets a way out rather than a screen
    // with no working control.
    const serverIsStopping = autoStop && app.takeCallInFlight;
    button.textContent = serverIsStopping
      ? "Recording — it stops by itself"
      : autoStop
        ? "Stop recording (this one normally stops by itself)"
        : "Stop recording";
    button.disabled = app.takeCallInFlight;
  } else if (snapshot.take_state === "armed") {
    button.textContent = "Arming…";
    button.disabled = true;
  } else {
    button.textContent = "Start recording";
    button.disabled = app.takeCallInFlight;
  }

  const elapsed = el("take-elapsed");
  if (elapsed) elapsed.hidden = !recording;
}

function shapeLine(task) {
  if (task.stop === "auto") {
    return Number.isFinite(task.target_s)
      ? `Stops by itself after about ${fmt(task.target_s, 0)} seconds.`
      : "Stops by itself when it is finished.";
  }
  return Number.isFinite(task.target_s)
    ? `About ${fmt(task.target_s, 0)} seconds. Press Stop when the participant has finished.`
    : "No fixed length. Press Stop when the participant has finished.";
}

// The connected-speech passage is fixed per participant and read identically
// every session (CLAUDE.md task 6), so it is shown here rather than left to
// the operator's memory. In the participant's own native language.
function renderPassage(task) {
  const host = el("task-passage");
  if (!host) return;
  clear(host);
  if (task.key !== "connected_speech") return;

  const record = app.participantRecord;
  if (!record || record.pseudonym !== app.participant) {
    // A refresh mid-session loses the picker's copy; fetch it again rather
    // than claim there is no passage.
    host.append(h("p", { class: "hint", text: "Looking up the stored passage…" }));
    ensureParticipantRecord();
    return;
  }
  const passage = typeof record.passageText === "string" ? record.passageText.trim() : "";
  if (passage) {
    host.append(
      h("p", { class: "hint", text: "Their fixed passage — the same one every session:" }),
      h("p", { class: "passage-text", text: passage })
    );
    return;
  }
  host.append(
    h("p", {
      class: "notice",
      text:
        "No passage is stored for this participant. The same text must be read every session — " +
        "use the printed passage, and write down which one was used.",
    })
  );
}

// A spoken example exists for consonant tasks only. It is never rendered for a
// vowel task: hearing a pitch would anchor the participant's own, and
// fundamental frequency is one of the measures (CLAUDE.md, task battery).
// This plays a vendored file through an <audio> element — output only. It is
// not, and must never become, a path to the microphone.
function renderDemo(task, recording) {
  const host = el("task-demo");
  if (!host) return;
  clear(host);
  if (!task.spoken_demo) return;

  const source = `/static/audio/demo_${task.key}.wav`;
  // app.demos is filled once from GET /api/demos. Undefined means we have not
  // asked yet, in which case offer the control and let playback report for
  // itself; false means the server looked and the file is not there.
  const known = app.demos && Object.prototype.hasOwnProperty.call(app.demos, task.key);
  const present = known ? app.demos[task.key] === true : true;

  if (!present) {
    host.append(
      h("p", {
        class: "hint",
        text:
          "No recorded example for this task yet. Demonstrate it out loud " +
          "yourself, the same way every session. (Record the examples once " +
          "with: python -m tools.record_demos)",
      })
    );
    return;
  }

  const player = h("audio", { preload: "none", src: source });
  const status = h("span", { class: "hint" });
  const play = h(
    "button",
    {
      class: "btn btn-quiet",
      type: "button",
      disabled: recording,
      onclick: function () {
        status.textContent = "";
        const started = player.play();
        if (started && typeof started.catch === "function") {
          started.catch(function (err) {
            status.textContent = `Could not play the example: ${err.message}. Say the task out loud instead.`;
          });
        }
      },
    },
    "Play the spoken example"
  );
  player.addEventListener("error", function () {
    play.disabled = true;
    status.textContent =
      "The recorded example is missing. Demonstrate the task out loud instead.";
  });
  host.append(play, status, player);
}

// Which spoken examples exist. Asked once at startup so a task screen never
// offers a control that cannot work. Vowel tasks are absent by design.
async function loadDemoAvailability() {
  try {
    const data = await api("GET", "/api/demos");
    app.demos = (data && data.demos) || {};
  } catch (err) {
    // Not fatal: renderDemo falls back to offering the control and letting
    // playback report for itself.
    app.demos = null;
  }
}

function renderTakeResult() {
  const node = el("take-result");
  if (!node) return;
  const result = app.lastResult;
  if (!result) {
    node.hidden = true;
    node.textContent = "";
    node.className = "take-result";
    return;
  }
  const qc = result.qc && typeof result.qc === "object" ? result.qc : null;
  const warnings = qc ? asArray(qc.warnings) : [];
  const warned = warnings.length > 0 || (qc && qc.status === "warn");
  node.hidden = false;
  node.className = `take-result ${warned ? "warn" : "pass"}`;
  const parts = [`Saved: ${result.file || "(no file name returned)"}`];
  if (Number.isFinite(result.duration_s)) parts.push(`${fmt(result.duration_s)} s`);
  parts.push(warned ? `FLAGGED: ${warnings.join("; ") || "see the quick check"}` : "passed the quick check");
  node.textContent = parts.join(" · ");
}

function renderRedoNote(snapshot) {
  const host = el("task-redo");
  if (!host) return;
  clear(host);
  if (snapshot.redo_mode) {
    host.append(
      h("p", {
        text:
          "This is a re-record. The earlier file stays on disk untouched — this one becomes the take that counts. " +
          "When it is saved you go back to the quick-check list.",
      })
    );
    return;
  }
  // The server only reopens a slot from QC review (SessionStateMachine
  // .reopen_for_redo requires the qc_review phase), so there is deliberately no
  // button here that would be refused.
  host.append(
    h("p", {
      text:
        "To re-record a take: finish the battery, then use the Re-record button next to it in the quick-check list. " +
        "Nothing is ever overwritten — every attempt is kept.",
    })
  );
}

async function onTakeButton() {
  const snapshot = app.snapshot;
  const task = snapshot && snapshot.task;
  if (!task || !app.sessionId || app.takeCallInFlight) return;
  const base = `/api/sessions/${encodeURIComponent(app.sessionId)}/tasks/${task.number}/takes/${task.take}`;
  // The stem rides along so the server can refuse a stale screen: task 8
  // records /s/ and /z/ under the same task and take number, and the path
  // alone cannot tell them apart.
  const query = `?stem=${encodeURIComponent(task.stem || "")}`;
  if (snapshot.take_state === "recording") {
    await runTakeCall(`${base}/stop${query}`, "stop");
  } else {
    app.clipLatched = false;
    app.lastResult = null;
    updateClipIndicators();
    await runTakeCall(`${base}/start${query}`, "start");
  }
}

async function runTakeCall(url, kind) {
  hideInline("task-error");
  app.takeCallInFlight = true;
  const button = el("take-button");
  if (button) {
    button.disabled = true;
    button.textContent = kind === "start" ? "Starting…" : "Stopping…";
  }
  try {
    const data = await api("POST", url);
    handleTakeResponse(data);
  } catch (err) {
    if (isRefusal(err)) {
      // The server refused outright, so nothing was recorded and nothing is at
      // risk; correct the screen instead of blocking it.
      showInline(
        "task-error",
        `${isNotImplemented(err) ? "This is not wired up yet" : "The server refused"}: ${err.message}`
      );
      app.takeCallInFlight = false;
      await syncActive();
      return;
    }
    showFatal(`take_${kind}_failed`, `${err.message}\n\n${kind.toUpperCase()} request: ${url}`);
  } finally {
    app.takeCallInFlight = false;
    render();
  }
}

function handleTakeResponse(data) {
  if (!data || typeof data !== "object") return;
  if (data.status === "saved") {
    app.lastResult = data;
    app.takeStartedAt = null;
  } else if (data.status === "recording") {
    app.takeStartedAt = Date.now();
  }
  // Every take route returns the state machine's own snapshot alongside its
  // result, so the screen is correct even if the push has not arrived (or the
  // live connection is down).
  if (data.state && typeof data.state === "object") {
    const snapshot = normalizeSnapshot(data.state);
    if (snapshot && snapshot.phase) setSnapshot(snapshot);
  } else if (!app.wsOpen) {
    syncActive();
  }
}

function tick() {
  const elapsed = el("take-elapsed");
  const snapshot = app.snapshot;
  const recording = snapshot && snapshot.take_state === "recording";
  if (elapsed && recording) {
    elapsed.hidden = false;
    elapsed.textContent = app.takeStartedAt
      ? `${((Date.now() - app.takeStartedAt) / 1000).toFixed(1)} s`
      : "recording (started before this page was opened)";
  } else if (elapsed) {
    elapsed.hidden = true;
  }

  updateClipIndicators();

  const meter = el("meter-bar");
  if (meter && !meter.hidden && Date.now() - app.lastLevelAt > LEVEL_STALE_MS) meterGoesStale();
}

// ---------------------------------------------------------------------------
// 5b. Borg CR-10. A rating must never be the reason a session stops.
// ---------------------------------------------------------------------------

// The nearest worded anchors on either side of an unlabelled step, so a
// blank row reads as "between these two" rather than as a broken screen.
// Derived rather than hardcoded, so editing BORG_SCALE cannot desynchronise
// the hints from the anchors.
function borgBetween(index) {
  let below = null;
  let above = null;
  for (let i = index - 1; i >= 0; i--) {
    if (BORG_SCALE[i].label) {
      below = BORG_SCALE[i].label;
      break;
    }
  }
  for (let i = index + 1; i < BORG_SCALE.length; i++) {
    if (BORG_SCALE[i].label) {
      above = BORG_SCALE[i].label;
      break;
    }
  }
  if (below && above) return `between ${below} and ${above}`;
  if (below) return `above ${below}`;
  if (above) return `below ${above}`;
  return "";
}

function renderBorg() {
  const pending = app.pendingBorg;
  if (!pending) return;
  setText("borg-task", pending.title);
  const scale = el("borg-scale");
  if (!scale) return;
  clear(scale);
  BORG_SCALE.forEach(function (step, index) {
    const anchored = Boolean(step.label);
    scale.append(
      h(
        "button",
        {
          class: "btn borg-step" + (anchored ? "" : " borg-unanchored"),
          type: "button",
          onclick: function () {
            submitBorg(step.value);
          },
        },
        h("span", { class: "num", text: String(step.value) }),
        h("span", {
          class: anchored ? "borg-label" : "borg-label borg-hint",
          text: anchored ? step.label : borgBetween(index),
        })
      )
    );
  });
}

async function submitBorg(rating) {
  const pending = app.pendingBorg;
  if (!pending || !app.sessionId) return;
  hideInline("borg-error");
  try {
    await api(
      "POST",
      `/api/sessions/${encodeURIComponent(app.sessionId)}/tasks/${pending.taskNumber}/borg`,
      { rating: rating }
    );
    app.pendingBorg = null;
    render();
  } catch (err) {
    const node = el("borg-error");
    if (node) {
      clear(node);
      node.hidden = false;
      node.append(
        h("span", { text: `The rating was not saved: ${err.message}` }),
        h(
          "button",
          {
            class: "btn btn-quiet",
            type: "button",
            onclick: function () {
              app.pendingBorg = null;
              render();
            },
          },
          "Carry on without the rating"
        )
      );
    }
  }
}

// ---------------------------------------------------------------------------
// 6. QC review.
// ---------------------------------------------------------------------------

function renderQc() {
  if (app.qc) renderQcList(app.qc);
}

async function loadQc() {
  const list = el("qc-list");
  if (!list || !app.sessionId) return;
  hideInline("qc-error");
  clear(list);
  list.append(h("p", { class: "muted", text: "Loading the take list…" }));
  let data;
  try {
    data = await api("GET", `/api/sessions/${encodeURIComponent(app.sessionId)}/qc-summary`);
  } catch (err) {
    clear(list);
    showInline("qc-error", `Cannot read the quick-check list: ${err.message}`);
    return;
  }
  app.qc = data;
  renderQcList(data);
}

function renderQcList(data) {
  const list = el("qc-list");
  if (!list) return;
  clear(list);
  const takes = asArray(data && data.takes);
  const warned = takes.filter((take) => take && take.status === "warn").length;

  const headline = el("qc-headline");
  if (headline) {
    if (!takes.length) {
      headline.className = "qc-headline";
      headline.textContent = "No takes are recorded for this session yet.";
    } else if (warned) {
      headline.className = "qc-headline warn";
      headline.textContent = `${warned} of ${takes.length} takes flagged. Re-record them now.`;
    } else {
      headline.className = "qc-headline pass";
      headline.textContent = `All ${takes.length} takes passed the quick check.`;
    }
  }

  for (const take of takes) {
    if (!take || typeof take !== "object") continue;
    const warnings = asArray(take.warnings).filter((each) => typeof each === "string");
    const isWarn = take.status === "warn" || warnings.length > 0;
    const superseded = take.kept === false;
    const detail = [
      `task ${take.task_number}`,
      take.stem,
      `take ${take.take}`,
      take.redo ? `redo ${take.redo}` : null,
      Number.isFinite(take.duration_s) ? `${fmt(take.duration_s)} s` : null,
      superseded ? "superseded by a later re-record" : null,
    ]
      .filter(Boolean)
      .join(" · ");

    const body = h("div", {}, h("div", { class: "file", text: take.file || "(no file name)" }), h("div", { class: "detail", text: detail }));
    if (warnings.length) body.append(h("div", { class: "warnings", text: warnings.join("; ") }));

    const button = h(
      "button",
      {
        class: isWarn ? "btn btn-go" : "btn btn-quiet",
        type: "button",
        onclick: function () {
          redoTake(take.task_number, take.stem, take.take);
        },
      },
      "Re-record"
    );

    list.append(
      h(
        "div",
        { class: `qc-row${isWarn ? " warn" : ""}${superseded ? " superseded" : ""}` },
        h("div", { class: "mark", text: superseded ? "·" : isWarn ? "!" : "✓" }),
        body,
        button
      )
    );
  }
}

async function redoTake(taskNumber, stem, take) {
  if (!app.sessionId) return;
  hideInline("qc-error");
  // The stem rides as a query parameter because task 8 records two different
  // sounds under the same task number and take number, and the path alone
  // cannot tell them apart.
  const url =
    `/api/sessions/${encodeURIComponent(app.sessionId)}/tasks/${taskNumber}/takes/${take}/redo` +
    `?stem=${encodeURIComponent(stem || "")}`;
  app.clipLatched = false;
  app.lastResult = null;
  try {
    const data = await api("POST", url);
    handleTakeResponse(data);
    await syncActive();
  } catch (err) {
    if (isRefusal(err)) {
      showInline("qc-error", `The re-record was refused: ${err.message}`);
      await syncActive();
      return;
    }
    showFatal("redo_failed", `${err.message}\n\nREDO request: ${url}`);
  }
}

async function onCompleteSession() {
  const button = el("qc-complete");
  if (!app.sessionId || !button) return;
  hideInline("qc-error");
  button.disabled = true;
  button.textContent = "Finishing…";
  try {
    const data = await api("POST", `/api/sessions/${encodeURIComponent(app.sessionId)}/complete`);
    app.completeInfo = data && typeof data === "object" ? data : {};
    forgetSession();
    app.localStep = "complete";
    app.startLoaded = false;
    render();
  } catch (err) {
    showInline("qc-error", `The session was not closed: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "Complete session";
  }
}

function renderComplete() {
  const info = app.completeInfo || {};
  const takes = Number(info.takes);
  setText(
    "complete-detail",
    `${app.participant ? app.participant + " · " : ""}${info.session_id || app.sessionId || ""} — ` +
      `${Number.isFinite(takes) ? takes : "all"} takes saved.`
  );
}

function renderUnknown() {
  setText("unknown-title", app.booting ? "Re-syncing with the server" : "Unexpected session state");
  setText(
    "unknown-lead",
    app.booting
      ? "This page was reloaded. Reading the true state of the session from the server — the server holds it, not this page."
      : "The server reports a phase this screen does not know how to draw. Nothing has been lost — completed takes are on disk."
  );
  const node = el("unknown-detail");
  if (node) {
    let text;
    try {
      text = JSON.stringify({ session_id: app.sessionId, state: app.snapshot }, null, 2);
    } catch (err) {
      text = `(could not format the state: ${err.message})`;
    }
    node.textContent = text;
  }
}

// ---------------------------------------------------------------------------
// WebSocket: server push only. Commands go out as plain HTTP POSTs.
// ---------------------------------------------------------------------------

function onWsMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "level") {
    applyLevel(msg);
    return;
  }
  if (msg.type === "task_state") {
    const snapshot = normalizeSnapshot(msg);
    if (!app.sessionId) {
      // A refresh can leave us without the id the pushes do not carry.
      syncActive();
    }
    setSnapshot(snapshot);
    return;
  }
  if (msg.type === "error") {
    applyServerError(msg);
    return;
  }
  console.warn("Unknown WebSocket message type", msg);
}

function applyServerError(msg) {
  const code = String(msg.code || "error");
  const message = String(msg.message === undefined ? "" : msg.message);
  if (code === "path_collision") {
    // The one error that is not session-fatal: the take IS being written, just
    // under a suffixed name, and nothing was overwritten. Blocking the screen
    // here would stop the operator pressing Stop on a take that is recording
    // perfectly well. It stays up loudly for the rest of the session.
    setBanner("collision", "bad", message);
    return;
  }
  showFatal(code, message);
}

// Retry quickly at first (a dropped socket should come back at once), then
// slow down so a server with no WebSocket support is not hammered all mission.
let wsAttempts = 0;

function wsRetryDelay() {
  wsAttempts += 1;
  return wsAttempts <= 5 ? WS_RETRY_MS : WS_RETRY_SLOW_MS;
}

function connectWs() {
  let ws;
  try {
    ws = new WebSocket(`ws://${location.host}/ws`);
  } catch (err) {
    setBanner("ws", "bad", `Cannot open the live connection: ${err.message}. Retrying…`);
    window.setTimeout(connectWs, wsRetryDelay());
    return;
  }

  ws.onopen = function () {
    app.wsOpen = true;
    wsAttempts = 0;
    clearBanner("ws");
    syncActive();
  };
  ws.onmessage = function (event) {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (err) {
      console.error("Unreadable WebSocket payload", event.data, err);
      setBanner("ws", "warn", "The server sent a message this page could not read. See the browser console.");
      return;
    }
    onWsMessage(msg);
  };
  ws.onclose = function () {
    app.wsOpen = false;
    const recording = app.snapshot && app.snapshot.take_state === "recording";
    meterGoesStale();
    setBanner(
      "ws",
      recording ? "bad" : "warn",
      recording
        ? "Live connection lost WHILE RECORDING. Do not assume this take is being captured — check the result when it stops."
        : "Live connection to the capture server lost. Retrying… the level meter is not live."
    );
    window.setTimeout(connectWs, wsRetryDelay());
  };
  ws.onerror = function () {
    // onclose always follows; the banner is set there.
  };
}

// ---------------------------------------------------------------------------
// Adherence dashboard.
// ---------------------------------------------------------------------------

const CELL_LOOK = {
  complete: { symbol: "✓", css: "cell-complete", word: "complete" },
  flagged: { symbol: "!", css: "cell-flagged", word: "flagged" },
  missing: { symbol: "·", css: "cell-missing", word: "missing" },
};

function cellLook(status) {
  return CELL_LOOK[status] || { symbol: "?", css: "cell-unknown", word: status || "unknown" };
}

async function loadAdherence() {
  const host = el("adherence-grid");
  if (!host) return;
  hideInline("adherence-error");
  clear(host);
  host.append(h("p", { class: "muted", text: "Loading…" }));
  let data;
  try {
    data = await api("GET", "/api/adherence");
  } catch (err) {
    clear(host);
    showInline("adherence-error", `Cannot read the adherence grid: ${err.message}`);
    return;
  }
  renderAdherence(data);
}

function renderAdherence(data) {
  const host = el("adherence-grid");
  if (!host) return;
  clear(host);

  const grid = data && typeof data === "object" ? data : {};
  const cells = grid.cells && typeof grid.cells === "object" ? grid.cells : {};
  let participants = asArray(grid.participants).filter((each) => typeof each === "string");
  if (!participants.length) participants = Object.keys(cells);

  const rows = participants.map(function (name) {
    return { name: name, statuses: asArray(cells[name]).map(normalizeCell) };
  });

  let expected = Number(grid.expected_sessions);
  if (!Number.isFinite(expected) || expected <= 0) {
    expected = rows.reduce(function (most, row) {
      return Math.max(most, row.statuses.length);
    }, 0);
  }

  setText(
    "adherence-meta",
    `${rows.length} participants · ${expected} sessions expected each · read from master_log.csv`
  );

  if (!rows.length || !expected) {
    host.append(
      h("p", { class: "notice", text: "The server returned no participants or no expected session count." }),
      jsonBlock(grid)
    );
    return;
  }

  const head = h("tr", {}, h("th", { class: "who-col", text: "Participant" }));
  for (let i = 1; i <= expected; i += 1) head.append(h("th", { text: String(i) }));
  head.append(h("th", { text: "Done" }), h("th", { text: "Gaps" }));

  const body = h("tbody");
  for (const row of rows) {
    let complete = 0;
    let flagged = 0;
    let missing = 0;
    let run = 0;
    let longestGap = 0;

    const tr = h("tr", {}, h("th", { class: "who-col", text: row.name }));
    for (let i = 0; i < expected; i += 1) {
      const status = row.statuses[i] || "missing";
      const look = cellLook(status);
      if (status === "complete") complete += 1;
      else if (status === "flagged") flagged += 1;
      else if (status === "missing") missing += 1;
      if (status === "missing") {
        run += 1;
        longestGap = Math.max(longestGap, run);
      } else {
        run = 0;
      }
      tr.append(
        h(
          "td",
          {},
          h("span", {
            class: `cell ${look.css}`,
            text: look.symbol,
            title: `${row.name} · session ${i + 1} · ${look.word}`,
            "aria-label": `session ${i + 1} ${look.word}`,
          })
        )
      );
    }

    tr.append(
      h("td", { class: "summary-col", text: `✓ ${complete} · ! ${flagged} · missing ${missing}` })
    );
    const gapCell = h("td", { class: "summary-col" });
    if (longestGap >= 2) {
      gapCell.append(h("span", { class: "gap-flag", text: `${longestGap} in a row missed` }));
      tr.className = "has-gap";
    } else {
      gapCell.append(h("span", { class: "muted", text: longestGap ? "1 missed" : "—" }));
      if (missing) tr.className = "has-gap";
    }
    tr.append(gapCell);
    body.append(tr);
  }

  host.append(h("table", { class: "grid" }, h("thead", {}, head), body));
}

function normalizeCell(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && typeof value.status === "string") return value.status;
  return "unknown";
}

async function runExport() {
  const button = el("export-run");
  const field = el("export-destination");
  const out = el("export-result");
  if (!button || !field || !out) return;
  clear(out);
  const destination = String(field.value || "").trim();
  if (!destination) {
    out.append(h("p", { class: "error", text: "Type the destination folder on the USB drive first." }));
    return;
  }
  button.disabled = true;
  button.textContent = "Copying and verifying…";
  out.append(h("p", { class: "muted", text: "Copying, then re-walking both trees to compare every file…" }));
  try {
    const data = await api("POST", "/api/export/usb", { destination: destination });
    clear(out);
    renderExportResult(out, data);
  } catch (err) {
    clear(out);
    out.append(
      h("p", { class: "error", text: `The export failed: ${err.message}` }),
      h("p", { class: "muted", text: "Nothing on this laptop has been changed. Do not delete anything." })
    );
    // A failed verification comes back as an error status WITH the full report
    // attached — show every mismatch, not just the headline.
    if (err instanceof ApiError && err.payload && typeof err.payload === "object") {
      renderExportResult(out, err.payload);
    }
  } finally {
    button.disabled = false;
    button.textContent = "Copy and verify";
  }
}

function renderExportResult(out, data) {
  const result = data && typeof data === "object" ? data : {};
  const mismatches = asArray(
    firstDefined([result.mismatches, result.differences, result.errors, result.problems]) || []
  );
  const verified = result.verified !== false && result.ok !== false;
  const status = String(result.status || "").toLowerCase();
  const good = verified && !mismatches.length && status !== "error" && status !== "mismatch" && status !== "failed";

  const counts = [];
  for (const key of [
    "destination",
    "files_copied",
    "bytes_copied",
    "source_file_count",
    "destination_file_count",
  ]) {
    if (result[key] !== undefined) counts.push(`${key.replace(/_/g, " ")}: ${result[key]}`);
  }

  if (good) {
    out.append(
      h("p", {
        class: "ok-note",
        text:
          typeof result.detail === "string" && result.detail
            ? result.detail
            : "Copy verified: every file is on the destination with the same byte size.",
      })
    );
  } else {
    out.append(
      h("p", {
        class: "error",
        text:
          typeof result.detail === "string" && result.detail
            ? result.detail
            : "VERIFICATION FAILED. The copy on the USB drive does NOT match this laptop. " +
              "Do not delete anything here. Fix the destination and run it again.",
      })
    );
    if (mismatches.length) {
      out.append(h("p", { class: "muted", text: `${mismatches.length} mismatch(es):` }));
      const list = h("ul");
      for (const mismatch of mismatches) {
        list.append(h("li", { text: typeof mismatch === "string" ? mismatch : JSON.stringify(mismatch) }));
      }
      out.append(list);
    }
  }
  if (counts.length) out.append(h("p", { class: "export-line muted", text: counts.join(" · ") }));
  out.append(h("details", {}, h("summary", { text: "Full server reply" }), jsonBlock(result)));
}

// ---------------------------------------------------------------------------
// Wiring.
// ---------------------------------------------------------------------------

function wireCapture() {
  wireUtc("consent", updateConsentSubmit);
  wireUtc("setup", updateSetupSubmit);

  const agree = el("consent-agree");
  if (agree) agree.addEventListener("change", updateConsentSubmit);
  const consentSubmit = el("consent-submit");
  if (consentSubmit) consentSubmit.addEventListener("click", onConsentSubmit);
  const consentBack = el("consent-back");
  if (consentBack) {
    consentBack.addEventListener("click", function () {
      app.localStep = "start";
      render();
    });
  }

  const setupBack = el("setup-back");
  if (setupBack) {
    setupBack.addEventListener("click", function () {
      app.localStep = "start";
      render();
    });
  }
  const setupSubmit = el("setup-submit");
  if (setupSubmit) setupSubmit.addEventListener("click", onSetupSubmit);

  wireForm(el("screen-setup"), null);
  wireForm(el("screen-reference"), renderReferenceCounts);
  wireForm(el("screen-covariates"), renderCovariateCounts);

  const referenceSubmit = el("reference-submit");
  if (referenceSubmit) {
    referenceSubmit.addEventListener("click", function () {
      submitBlock("screen-reference", "/reference-measures", "reference-error", "reference-submit", "Save and continue");
    });
  }
  const covariatesSubmit = el("covariates-submit");
  if (covariatesSubmit) {
    covariatesSubmit.addEventListener("click", function () {
      submitBlock("screen-covariates", "/covariates", "covariates-error", "covariates-submit", "Save and start recording");
    });
  }

  const takeButton = el("take-button");
  if (takeButton) takeButton.addEventListener("click", onTakeButton);

  const borgNa = el("borg-na");
  if (borgNa) {
    borgNa.addEventListener("click", function () {
      submitBorg("n/a");
    });
  }

  const qcRefresh = el("qc-refresh");
  if (qcRefresh) qcRefresh.addEventListener("click", loadQc);
  const qcComplete = el("qc-complete");
  if (qcComplete) qcComplete.addEventListener("click", onCompleteSession);

  const completeAgain = el("complete-again");
  if (completeAgain) {
    completeAgain.addEventListener("click", function () {
      app.completeInfo = null;
      app.participant = null;
      app.localStep = "start";
      app.startLoaded = false;
      render();
    });
  }

  const resync = el("unknown-resync");
  if (resync) resync.addEventListener("click", syncActive);

  const recheck = el("fatal-recheck");
  if (recheck) recheck.addEventListener("click", onFatalRecheck);

  const endFailed = el("fatal-end-session");
  if (endFailed) endFailed.addEventListener("click", onFatalEndSession);

  window.addEventListener("beforeunload", function (event) {
    if (app.snapshot && app.snapshot.take_state === "recording") {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  const remembered = recallSession();
  if (remembered.participant) app.participant = remembered.participant;
  if (remembered.sessionId) {
    app.sessionId = remembered.sessionId;
    app.booting = true;
  }

  render();
  syncActive().finally(function () {
    app.booting = false;
    render();
  });
  connectWs();
  window.setInterval(tick, TICK_MS);
}

function wireDashboard() {
  const refresh = el("adherence-refresh");
  if (refresh) refresh.addEventListener("click", loadAdherence);
  const exportRun = el("export-run");
  if (exportRun) exportRun.addEventListener("click", runExport);
  loadAdherence();
}

if (app.page === "dashboard") wireDashboard();
else wireCapture();
