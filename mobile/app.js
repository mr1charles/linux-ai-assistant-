// Little Toby web app — the phone companion for Android, or any browser.
// (On iPhone, the native Little Toby app does all this and more.)
//
// Pairing: the computer's QR code opens this page with #pair=<code>, a
// one-time eight-character code. The page sends it with a random id for this
// phone, shows the six-digit number the computer is also showing, and waits
// while you approve on the computer. Only then does this phone receive its
// own token, which it keeps in local storage and sends with every request.
// The code is wiped from the address bar as soon as it's read.
//
// Updates: /api/state is long-polled — the computer answers the moment
// anything changes, or after ~20 s with nothing new — so the task list,
// permission prompts and reply update live without hammering the computer.
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "toby.pairing";
const DEVICE_KEY = "toby.device";
const STATE_WORDS = {
  thinking: "Thinking", preparing: "Getting ready", working: "Working", waiting: "Waiting",
  needs_permission: "Needs your OK", paused: "Paused", completed: "Done", failed: "Didn't finish",
  cancelled: "Stopped",
};
let token = null;
let version = null;
let lastReply = "";
let polling = false;
let backoff = 1000;
let approvalId = null;
let taskState = null;

function store(key, value) {
  try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch (_) {}
}
function load(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

function deviceId() {
  let id = load(DEVICE_KEY);
  if (!id) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    id = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    store(DEVICE_KEY, id);
  }
  return id;
}

function deviceName() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone (web app)";
  if (/iPad/.test(ua)) return "iPad (web app)";
  if (/Android/.test(ua)) return "Android phone (web app)";
  return "Browser (web app)";
}

async function api(path, body, { auth = true } = {}) {
  const opts = { method: body === undefined ? "GET" : "POST", headers: {}, cache: "no-store" };
  if (auth) opts.headers.Authorization = "Bearer " + token;
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (_) {}
  if (resp.status === 401 && auth) { unpair("This phone was unpaired. Pair it again to keep using Toby."); throw new Error("unpaired"); }
  return { ok: resp.ok, status: resp.status, data };
}

// -- status line ----------------------------------------------------------------
function setStatus(kind, text) {
  $("dot").className = "dot " + kind;
  $("status-text").textContent = text;
}

// -- rendering ------------------------------------------------------------------------
function render(state) {
  const busy = !!state.busy;
  const name = (state.computer && state.computer.name) || "your computer";
  if (state.power === "sleeping") setStatus("offline", name + " is going to sleep");
  else setStatus(busy ? "busy" : "online", busy ? "Working on " + name : "Connected to " + name);

  const face = $("face");
  face.classList.toggle("thinking", busy && (!state.task || state.task.state === "thinking"));

  const task = state.task;
  if (state.reply && state.reply !== lastReply) {
    lastReply = state.reply;
    $("reply").textContent = state.reply;
    if (!busy) {
      face.classList.remove("happy");
      void face.getBoundingClientRect();   // restart the hop animation
      face.classList.add("happy");
      speak(state.reply);
    }
  } else if (busy && !state.reply) {
    $("reply").textContent = task ? "On it: " + task.text : "Thinking…";
  }

  const steps = (task && task.steps) || [];
  $("task").hidden = !task || (steps.length === 0 && !busy);
  if (task) {
    taskState = task.state;
    $("task-title").textContent = task.text;
    $("task-state").textContent = STATE_WORDS[task.state] || "";
    $("task-state").className = "state " + task.state;
    const done = steps.filter((s) => s.status === "done").length;
    $("progress-bar").style.width = steps.length ? Math.round((done / steps.length) * 100) + "%" : "0%";
    $("steps").replaceChildren(...steps.map((s) => {
      const li = document.createElement("li");
      li.className = s.status;
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = s.label;
      li.append(label);
      if (s.detail && (s.status === "error" || s.status === "done")) {
        const d = document.createElement("span");
        d.className = "detail";
        d.textContent = s.detail;
        li.append(d);
      }
      return li;
    }));
    $("cancel").hidden = !busy;
    $("pause").hidden = !busy;
    $("pause").textContent = task.state === "paused" ? "Resume" : "Pause";
  }

  const approval = (state.approvals || [])[0];
  $("confirm").hidden = !approval;
  approvalId = approval ? approval.id : null;
  if (approval) {
    $("confirm-text").textContent = approval.title.replace(/\?$/, "");
    $("confirm-details").replaceChildren(...(approval.details || []).map((d) => {
      const li = document.createElement("li");
      li.textContent = d;
      return li;
    }));
    const restricted = approval.level === "restricted";
    $("confirm").classList.toggle("restricted", restricted);
    $("confirm-reason").hidden = !restricted;
    $("confirm-reason").textContent = restricted ? "Careful: " + approval.reason + "." : "";
  }
}

// -- the long-poll loop -------------------------------------------------------------------
async function poll() {
  if (polling || !token) return;
  polling = true;
  while (token) {
    try {
      const q = version === null ? "" : "?since=" + version;
      const { ok, data } = await api("/api/state" + q);
      if (ok) {
        version = data.version;
        render(data);
        backoff = 1000;
      } else {
        await wait(backoff);
      }
    } catch (err) {
      if (err.message === "unpaired") break;
      setStatus("offline", "Can't reach your computer. It may be asleep, off or offline.");
      await wait(backoff);
      backoff = Math.min(backoff * 2, 15000);
    }
  }
  polling = false;
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// -- sending ----------------------------------------------------------------------------------
async function send(text) {
  text = text.trim();
  if (!text) return;
  $("text").value = "";
  $("reply").textContent = "Sending…";
  try {
    const { ok, data } = await api("/api/ask", { text });
    if (!ok) $("reply").textContent = data.message || data.error || "Toby couldn't take that right now.";
  } catch (_) {
    $("reply").textContent = "Can't reach your computer. Is it on and online?";
  }
}

// -- voice --------------------------------------------------------------------------------------
// Uses the phone's own speech recognition where the browser offers it
// (Chrome on Android; Safari on iPhone). Where it doesn't, the button is
// hidden and the keyboard's own dictation key does the same job.
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognizer = null;

function setupMic() {
  const mic = $("mic");
  if (!Recognition) { mic.hidden = true; return; }
  mic.addEventListener("click", () => {
    if (recognizer) { recognizer.stop(); return; }
    recognizer = new Recognition();
    recognizer.lang = navigator.language || "en-US";
    recognizer.interimResults = true;
    recognizer.continuous = false;
    mic.classList.add("listening");
    let finalText = "";
    recognizer.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalText += e.results[i][0].transcript;
        else interim += e.results[i][0].transcript;
      }
      $("text").value = finalText || interim;
    };
    recognizer.onend = () => {
      mic.classList.remove("listening");
      recognizer = null;
      if (finalText.trim()) send(finalText);
    };
    recognizer.onerror = () => { mic.classList.remove("listening"); recognizer = null; };
    recognizer.start();
  });
}

function speak(text) {
  if (!("speechSynthesis" in window) || document.hidden) return;
  try {
    speechSynthesis.cancel();
    speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  } catch (_) {}
}

// -- pairing ------------------------------------------------------------------------------------
const CODE_CHARS = /[^23456789ABCDEFGHJKMNPQRSTUVWXYZ]/g;

function pairStatus(html) {
  const el = $("pair-status");
  el.hidden = !html;
  el.replaceChildren();
  if (html) el.append(...html);
}

async function pairWithCode(raw) {
  const code = String(raw || "").toUpperCase().replace(CODE_CHARS, "");
  if (code.length !== 8) {
    pairStatus([document.createTextNode("The code is eight letters and numbers, like K7M4-XQ2P.")]);
    return;
  }
  pairStatus([document.createTextNode("Checking the code…")]);
  let claim;
  try {
    claim = await api("/api/pair/claim", { code, device_name: deviceName(), device_id: deviceId(), platform: "web" },
                      { auth: false });
  } catch (_) {
    pairStatus([document.createTextNode("Can't reach your computer from here.")]);
    return;
  }
  if (!claim.ok) {
    pairStatus([document.createTextNode(claim.data.error || "That code didn't work.")]);
    return;
  }
  const number = claim.data.compare;
  const big = document.createElement("span");
  big.className = "compare";
  big.textContent = number.slice(0, 3) + " " + number.slice(3);
  pairStatus([document.createTextNode("Approve this phone on " + claim.data.computer.name +
                                      ". It should be showing the same number:"), big]);
  for (let tries = 0; tries < 20; tries++) {
    let result;
    try {
      result = await api("/api/pair/wait?claim=" + encodeURIComponent(claim.data.claim), undefined, { auth: false });
    } catch (_) {
      await wait(2000);
      continue;
    }
    const status = result.data.status;
    if (status === "approved" && result.data.token) {
      store(TOKEN_KEY, result.data.token);
      pairStatus(null);
      paired(result.data.token);
      return;
    }
    if (status !== "pending") {
      const why = { denied: "The computer said no.", expired: "That took too long. Start pairing again.",
                    unknown: "The computer restarted. Start pairing again." }[status] || "Pairing didn't finish.";
      pairStatus([document.createTextNode(why)]);
      return;
    }
  }
}

function readPairingLink() {
  const match = location.hash.match(/pair=([A-Za-z0-9_-]+)/);
  if (!match) return null;
  history.replaceState(null, "", location.pathname);
  return match[1];
}

function unpair(message) {
  token = null;
  store(TOKEN_KEY, null);
  $("pair").hidden = false;
  $("composer").hidden = true;
  $("task").hidden = true;
  $("confirm").hidden = true;
  setStatus("offline", "Not paired");
  if (message) pairStatus([document.createTextNode(message)]);
}

function paired(t) {
  token = t;
  $("pair").hidden = true;
  $("composer").hidden = false;
  version = null;
  poll();
}

// -- wiring ---------------------------------------------------------------------------------------
$("composer").addEventListener("submit", (e) => { e.preventDefault(); send($("text").value); });
$("confirm-yes").addEventListener("click", () => {
  if (approvalId) api("/api/approve", { id: approvalId, allow: true }).catch(() => {});
});
$("confirm-no").addEventListener("click", () => {
  if (approvalId) api("/api/approve", { id: approvalId, allow: false }).catch(() => {});
});
$("cancel").addEventListener("click", () => api("/api/task/stop", {}).catch(() => {}));
$("pause").addEventListener("click", () =>
  api(taskState === "paused" ? "/api/task/resume" : "/api/task/pause", {}).catch(() => {}));
$("pair-save").addEventListener("click", () => pairWithCode($("pair-code").value));
document.addEventListener("visibilitychange", () => { if (!document.hidden && token) poll(); });

setupMic();
const link = readPairingLink();
const saved = load(TOKEN_KEY);
if (link && link.length >= 32) {
  // a link from before per-device pairing carried the token itself
  store(TOKEN_KEY, link);
  paired(link);
} else if (link) {
  unpair();
  $("pair-code").value = link;
  pairWithCode(link);
} else if (saved) {
  paired(saved);
} else {
  unpair();
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
