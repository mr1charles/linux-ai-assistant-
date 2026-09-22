// Little Toby phone app.
//
// Pairing: the laptop's QR code opens this page with #pair=<token>. The token
// is read once, kept in this phone's storage, and wiped from the address bar
// so it never sits in history or a screenshot. Every API call sends it as a
// bearer token.
//
// Updates: /api/state is long-polled — the laptop answers the moment
// anything changes, or after ~20 s with nothing new — so the task list and
// reply update live without the phone hammering the laptop.
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "toby.pairing";
let token = null;
let version = null;
let lastReply = "";
let polling = false;
let backoff = 1000;

function readToken() {
  const match = location.hash.match(/pair=([A-Za-z0-9_-]{32,})/);
  if (match) {
    try { localStorage.setItem(TOKEN_KEY, match[1]); } catch (_) {}
    history.replaceState(null, "", location.pathname);
    return match[1];
  }
  try { return localStorage.getItem(TOKEN_KEY); } catch (_) { return null; }
}

async function api(path, body, signal) {
  const opts = {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: "Bearer " + token },
    cache: "no-store",
    signal,
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (_) {}
  if (resp.status === 401) { unpair(); throw new Error("unpaired"); }
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
  setStatus(busy ? "busy" : "online", busy ? "Working on your laptop" : "Connected to your laptop");

  const face = $("face");
  face.classList.toggle("thinking", busy && !(state.steps || []).length);

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
    $("reply").textContent = state.task ? "On it: " + state.task : "Thinking…";
  }

  const steps = state.steps || [];
  $("task").hidden = steps.length === 0;
  if (steps.length) {
    $("task-title").textContent = busy ? (state.task || "Working on it") : "Done";
    const done = steps.filter((s) => s.status === "done").length;
    $("progress-bar").style.width = Math.round((done / steps.length) * 100) + "%";
    const list = $("steps");
    list.replaceChildren(...steps.map((s) => {
      const li = document.createElement("li");
      li.textContent = s.label;
      li.className = s.status;
      return li;
    }));
    $("cancel").hidden = !busy;
  }

  const confirm = state.confirm;
  $("confirm").hidden = !(confirm && confirm.pending);
  if (confirm && confirm.pending) $("confirm-text").textContent = confirm.text;
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
      setStatus("offline", "Can't reach your laptop");
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
    $("reply").textContent = "Can't reach your laptop. Is it on and online?";
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
function unpair() {
  token = null;
  try { localStorage.removeItem(TOKEN_KEY); } catch (_) {}
  $("pair").hidden = false;
  $("composer").hidden = true;
  setStatus("offline", "Not paired");
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
$("confirm-yes").addEventListener("click", () => api("/api/confirm", { answer: true }).catch(() => {}));
$("confirm-no").addEventListener("click", () => api("/api/confirm", { answer: false }).catch(() => {}));
$("cancel").addEventListener("click", () => api("/api/cancel", {}).catch(() => {}));
$("pair-save").addEventListener("click", () => {
  const code = $("pair-code").value.trim().replace(/^.*pair=/, "");
  if (code.length < 32) return;
  try { localStorage.setItem(TOKEN_KEY, code); } catch (_) {}
  paired(code);
});
document.addEventListener("visibilitychange", () => { if (!document.hidden && token) poll(); });

setupMic();
const saved = readToken();
if (saved) paired(saved); else unpair();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
