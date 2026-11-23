// D-Day share app: receives a shared message (Android share sheet) and adds it to
// the "D-Day" list in the user's Google Tasks. D-Day on the PC imports that list.
// No server of our own: the page talks only to Google.
"use strict";

const TASKS_API = "https://tasks.googleapis.com/tasks/v1";
const SCOPE = "https://www.googleapis.com/auth/tasks";
const LIST_NAME = "D-Day";
const CLIENT_ID_RE = /^\d+-[a-z0-9]+\.apps\.googleusercontent\.com$/;
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ storage (per device)
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem("dday." + k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem("dday." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  del(k) { try { localStorage.removeItem("dday." + k); } catch { /* ignore */ } },
};

// ------------------------------------------------------------------ setup link: #setup=<client id>
(function readSetupLink() {
  const m = location.hash.match(/setup=([^&]+)/);
  if (!m) return;
  const id = decodeURIComponent(m[1]).trim();
  if (CLIENT_ID_RE.test(id)) {
    if (store.get("clientId") !== id) { store.del("token"); store.del("listId"); store.del("consented"); }
    store.set("clientId", id);
  }
  history.replaceState(null, "", location.pathname + location.search);  // drop only the #setup part
})();

// ------------------------------------------------------------------ views
function show(view) {
  for (const v of ["v-setup", "v-compose", "v-done"]) $(v).hidden = v !== view;
}
function status(text, kind = "") {
  $("status").textContent = text;
  $("status").className = `status ${kind}`;
}
function updateNet() { $("net").hidden = navigator.onLine; }

// ------------------------------------------------------------------ shared text
function sharedText() {
  const q = new URLSearchParams(location.search);
  const parts = [];
  for (const key of ["title", "text", "url"]) {
    const v = (q.get(key) || "").trim();
    if (v && !parts.some((p) => p.includes(v))) parts.push(v);
  }
  return parts.join("\n");
}

// ------------------------------------------------------------------ Google sign-in (token client)
let gisPromise = null;
function loadGis() {
  if (window.google && google.accounts && google.accounts.oauth2) return Promise.resolve();
  if (!gisPromise) {
    gisPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "https://accounts.google.com/gsi/client";
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => { gisPromise = null; reject(new Error("Couldn't load Google sign-in (no internet?).")); };
      document.head.appendChild(s);
    });
  }
  return gisPromise;
}

function cachedToken() {
  const t = store.get("token");
  return t && t.exp - 60000 > Date.now() ? t.value : null;
}

async function getToken(interactive) {
  const cached = cachedToken();
  if (cached) return cached;
  if (!interactive) return null;
  await loadGis();
  return new Promise((resolve, reject) => {
    const client = google.accounts.oauth2.initTokenClient({
      client_id: store.get("clientId"),
      scope: SCOPE,
      prompt: store.get("consented") ? "" : "consent",
      callback: (resp) => {
        if (resp.error) return reject(new Error(resp.error_description || resp.error));
        if (!google.accounts.oauth2.hasGrantedAllScopes(resp, SCOPE)) {
          return reject(new Error("Please allow access to Google Tasks, so D-Day can add the message there."));
        }
        store.set("token", { value: resp.access_token, exp: Date.now() + Number(resp.expires_in || 3600) * 1000 });
        store.set("consented", true);
        resolve(resp.access_token);
      },
      error_callback: (err) => reject(new Error(err && err.type === "popup_closed"
        ? "Sign-in was closed. Tap Send to try again."
        : "Couldn't open Google sign-in. Allow pop-ups for this page and try again.")),
    });
    client.requestAccessToken();
  });
}

// ------------------------------------------------------------------ Google Tasks
class NetError extends Error {}

async function tasksApi(token, method, path, body) {
  let resp;
  try {
    resp = await fetch(TASKS_API + path, {
      method,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new NetError("No internet connection.");
  }
  if (resp.status === 401) {
    store.del("token");
    throw new Error("Your Google sign-in expired. Tap Send again.");
  }
  let data = {};
  try { data = await resp.json(); } catch { /* empty body */ }
  if (!resp.ok) {
    const text = JSON.stringify(data).toLowerCase();
    if (resp.status === 403 && (text.includes("service_disabled") || text.includes("accessnotconfigured") || text.includes("has not been used"))) {
      throw new Error("The Google Tasks API is off in your Google Cloud project. Enable it (see the D-Day dashboard), then try again.");
    }
    const err = new Error((data.error && data.error.message) || `Google Tasks error (HTTP ${resp.status}).`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

async function listId(token) {
  const cached = store.get("listId");
  if (cached) return cached;
  let page = "";
  do {
    const data = await tasksApi(token, "GET", `/users/@me/lists?maxResults=100${page ? `&pageToken=${encodeURIComponent(page)}` : ""}`);
    const found = (data.items || []).find((l) => (l.title || "").trim().toLowerCase() === LIST_NAME.toLowerCase());
    if (found) { store.set("listId", found.id); return found.id; }
    page = data.nextPageToken || "";
  } while (page);
  const created = await tasksApi(token, "POST", "/users/@me/lists", { title: LIST_NAME });
  store.set("listId", created.id);
  return created.id;
}

function taskFor(text) {
  const firstLine = text.split("\n").find((l) => l.trim()) || text;
  let title = firstLine.trim();
  if (title.length > 120) title = title.slice(0, 117).trimEnd() + "…";
  const task = { title };
  if (text.trim() !== title) task.notes = text.slice(0, 8000);
  return task;
}

async function addToTasks(token, text) {
  try {
    await tasksApi(token, "POST", `/lists/${await listId(token)}/tasks`, taskFor(text));
  } catch (e) {
    if (e.status === 404) { // the list was deleted on the phone: recreate it once
      store.del("listId");
      await tasksApi(token, "POST", `/lists/${await listId(token)}/tasks`, taskFor(text));
    } else {
      throw e;
    }
  }
}

// ------------------------------------------------------------------ offline queue
function queue() { return store.get("queue") || []; }
function enqueue(text) { store.set("queue", [...queue(), { text, at: Date.now() }].slice(-50)); showQueue(); }
function showQueue() {
  const n = queue().length;
  $("queue").hidden = !n;
  $("queue").textContent = n ? `${n} message${n > 1 ? "s" : ""} waiting to be sent (they go out next time you're online and tap Send).` : "";
}
async function flushQueue(token) {
  let items = queue();
  while (items.length) {
    await addToTasks(token, items[0].text);
    items = items.slice(1);
    store.set("queue", items);
  }
  showQueue();
}

// ------------------------------------------------------------------ copy mode (clipboard)
// Android doesn't let any app read the clipboard in the background, so copy mode
// reads it when D-Day is opened (with the user's one-time permission).
const MIN_CLIP_CHARS = 6;
let autoTimer = null;

function hashText(text) {
  let h = 5381;
  for (let i = 0; i < text.length; i++) h = ((h * 33) ^ text.charCodeAt(i)) >>> 0;
  return h.toString(16) + ":" + text.length;
}

async function readClipboard() {
  if (!navigator.clipboard || !navigator.clipboard.readText) return null;
  try {
    return (await navigator.clipboard.readText() || "").trim();
  } catch {
    return null;  // permission not given yet, or the page isn't focused
  }
}

function cancelAuto(message) {
  clearTimeout(autoTimer);
  autoTimer = null;
  $("countdown").hidden = true;
  if (message) status(message);
}

async function checkClipboard() {
  if (!store.get("copyMode") || autoTimer || document.hidden || $("v-compose").hidden) return;
  if ($("msg").value.trim()) return;  // a shared, typed or pending message always wins over the clipboard
  const text = await readClipboard();
  if (text === null) {
    status("Tap 📋 Paste once so Chrome can ask for clipboard permission.");
    return;
  }
  if (text.length < MIN_CLIP_CHARS || hashText(text) === store.get("lastClip")) return;  // nothing new
  $("msg").value = text;
  $("msg-label").textContent = "Copied message (you can edit it)";
  if (!cachedToken()) {
    // Google sign-in must be confirmed by a tap (about once an hour).
    store.set("lastClip", hashText(text));
    status("Copied text is ready. Tap Send (Google asks you to confirm about once an hour).");
    return;
  }
  let left = 3;
  const tick = () => {
    if (left <= 0) { cancelAuto(); onSend(); return; }
    $("countdown-text").textContent = `Sending copied text in ${left}…`;
    left -= 1;
    autoTimer = setTimeout(tick, 1000);
  };
  $("countdown").hidden = false;
  tick();
}

async function onPaste() {
  const text = await readClipboard();
  if (text === null) { status("Chrome didn't allow reading the clipboard. Long-press the box and choose Paste.", "err"); return; }
  if (!text) { status("The clipboard is empty."); return; }
  $("msg").value = text;
  store.set("draft", text);
  status("");
}

function setCopyMode(on, check = true) {
  store.set("copyMode", !!on);
  $("copymode").checked = !!on;
  $("copyhelp").hidden = !on;
  if (!on) cancelAuto();
  else if (check) checkClipboard();
}

// ------------------------------------------------------------------ actions
async function onSend() {
  const text = $("msg").value.trim();
  const btn = $("send");
  if (!text && !queue().length) { status("Type or share a message first.", "err"); return; }
  btn.disabled = true;
  status("Sending…");
  if (!navigator.onLine) {
    if (text) enqueue(text);
    store.del("draft");
    done("Saved on this phone", "You're offline. It will be sent to D-Day next time you open this with internet.");
    btn.disabled = false;
    return;
  }
  try {
    const token = await getToken(true);  // a sign-in pop-up only the first time (and about once an hour)
    await flushQueue(token);
    if (text) await addToTasks(token, text);
    if (text) store.set("lastClip", hashText(text));
    store.del("draft");
    done("Sent to D-Day", "Your PC will pick it up within a minute (when it's on) and show it in the D-Day inbox.");
  } catch (e) {
    if (e instanceof NetError) {
      if (text) enqueue(text);
      store.del("draft");
      done("Saved on this phone", "No internet right now. It will be sent next time you open this with internet.");
    } else {
      status(e.message, "err");
    }
  } finally {
    btn.disabled = false;
  }
}

function done(title, text) {
  $("done-title").textContent = title;
  $("done-text").textContent = text;
  $("msg").value = "";
  show("v-done");
}

async function trySilentFlush() {
  if (!navigator.onLine || !queue().length) return;
  const token = cachedToken();
  if (!token) return;
  try { await flushQueue(token); } catch { /* keep them queued */ }
}

// ------------------------------------------------------------------ start
function start() {
  updateNet();
  window.addEventListener("online", () => { updateNet(); trySilentFlush(); });
  window.addEventListener("offline", updateNet);
  $("send").addEventListener("click", onSend);
  $("close").addEventListener("click", () => {
    window.close();
    setTimeout(() => show("v-compose"), 300);  // if the browser keeps the window open
  });
  $("another").addEventListener("click", () => { show("v-compose"); $("msg").focus(); });
  $("signout").addEventListener("click", async () => {
    const t = cachedToken();
    store.del("token"); store.del("consented"); store.del("listId");
    try { if (t) { await loadGis(); google.accounts.oauth2.revoke(t, () => {}); } } catch { /* offline */ }
    status("Signed out.", "ok");
  });
  $("msg").addEventListener("input", () => { store.set("draft", $("msg").value); cancelAuto(); });
  $("paste").addEventListener("click", onPaste);
  $("copymode").addEventListener("change", (e) => setCopyMode(e.target.checked));
  $("cancel-auto").addEventListener("click", () => cancelAuto("Cancelled. Tap Send when you're ready."));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkClipboard(); });
  window.addEventListener("focus", () => checkClipboard());

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});

  if (!store.get("clientId")) { show("v-setup"); return; }
  const shared = sharedText();
  if (shared) {
    history.replaceState(null, "", location.pathname);  // a reload must not resend
    $("msg").value = shared;
    $("msg-label").textContent = "Shared message (you can edit it)";
  } else {
    $("msg").value = store.get("draft") || "";
  }
  show("v-compose");
  showQueue();
  trySilentFlush();
  setCopyMode(store.get("copyMode"), false);
  if (!shared) checkClipboard();  // a shared message always wins over the clipboard
}

start();
