// D-Day dashboard: inbox (phone + pasted messages), upcoming, history, settings.
// All text from messages is rendered with textContent (never innerHTML).

const $ = (id) => document.getElementById(id);
const AREAS = [["job", "Internship / Job"], ["work", "Work"], ["college", "College"], ["personal", "Personal"]];
const AREA_LABEL = Object.fromEntries(AREAS);
const SOURCE_LABEL = { telegram: "📱 Telegram", gtasks: "📱 Phone (Tasks)", paste: "📋 Pasted" };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "value") node.value = String(v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

// replaceChildren() would print null/false as text; skip empty entries.
function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function safeLink(url) {
  try {
    const u = new URL(url);
    if (u.protocol === "https:" && /(^|\.)google\.com$/.test(u.hostname)) return u.href;
  } catch { /* invalid */ }
  return null;
}

async function api(path, { method = "GET", body } = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", "X-DDay-Client": "app" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    const e = new Error("The D-Day service isn't running. Start it from the D-Day extension's switch or start-dday.bat.");
    e.code = "service_unreachable";
    throw e;
  }
  let data = null;
  try { data = await resp.json(); } catch { /* ignore */ }
  if (!resp.ok || !data || data.ok === false) {
    const err = (data && data.error) || {};
    const e = new Error(err.message || `Error (HTTP ${resp.status})`);
    e.code = err.code;
    e.extra = err;
    throw e;
  }
  return data;
}

// ------------------------------------------------------------------ formatting
function whenText(t) {
  if (!t.due_date) return "Choose a date";
  const [y, m, d] = t.due_date.split("-").map(Number);
  const day = new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  if (t.all_day || !t.due_time) return `${day} · all day`;
  return `${day}, ${t.due_time}${t.end_time ? `–${t.end_time}` : ""}`;
}

function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function countdown(startIso, allDay) {
  const start = allDay ? new Date(`${startIso}T00:00:00`) : new Date(startIso);
  const days = Math.floor((start - new Date(new Date().toDateString())) / 86400000);
  if (days < 0) return ["passed", false];
  if (days === 0) return ["today", true];
  if (days === 1) return ["tomorrow", true];
  return [`in ${days} days`, days <= 3];
}

// ------------------------------------------------------------------ tabs
let currentTab = "inbox";
function showTab(name) {
  currentTab = name;
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const s of document.querySelectorAll(".tab")) s.hidden = s.id !== `tab-${name}`;
  ({ inbox: loadInbox, upcoming: loadUpcoming, history: loadHistory, settings: loadSettings })[name]();
}

// ------------------------------------------------------------------ status
async function loadStatus() {
  const box = $("status-dots");
  const dot = (ok, label) => el("span", {}, el("span", { class: `dot ${ok === true ? "ok" : ok === false ? "bad" : "warn"}` }), label);
  try {
    const [h, tg, gt] = await Promise.all([api("/api/health"), api("/api/telegram"),
      api("/api/gtasks").catch(() => ({}))]);
    const phoneOk = (gt.permission && gt.list_ready && !(gt.error && gt.error.code)) || tg.paired;
    fill(box, 
      dot(h.ollama.reachable && h.ollama.model_installed, "Model"),
      dot(h.google.connected ? true : h.google.configured ? null : false, "Google"),
      dot(phoneOk ? true : null, phoneOk ? "Phone" : "Phone: not set up"));
  } catch (e) {
    fill(box, dot(false, "D-Day service not running"));
  }
}

// ------------------------------------------------------------------ inbox
let inboxTimer = null;
async function loadInbox() {
  clearTimeout(inboxTimer);
  let data;
  try {
    data = await api("/api/inbox?status=open");
  } catch (e) {
    $("inbox-list").replaceChildren(el("div", { class: "notice error", text: e.message }));
    inboxTimer = setTimeout(loadInbox, 8000);
    return;
  }
  const items = data.items;
  const waiting = items.filter((i) => i.status === "ready" && i.tasks.some((t) => t.ui !== "saved")).length;
  $("inbox-badge").hidden = !waiting;
  $("inbox-badge").textContent = String(waiting);
  $("inbox-empty").hidden = items.length > 0;
  // Don't re-render while the user is typing in an editor.
  if (!document.activeElement || !document.activeElement.closest(".editor")) {
    $("inbox-list").replaceChildren(...items.map(renderItem));
  }
  const busy = items.some((i) => i.status === "queued" || i.status === "processing");
  if (currentTab === "inbox") inboxTimer = setTimeout(loadInbox, busy ? 3000 : 15000);
}

function renderItem(item) {
  const card = el("article", { class: "card" });
  const status = { queued: "Waiting for the model…", processing: "Reading…", ready: "", failed: "Couldn't read it", saved: "Saved" }[item.status] || item.status;
  card.append(el("div", { class: "item-head" },
    el("span", { class: "src", text: SOURCE_LABEL[item.source] || item.source }),
    status ? el("span", { class: "small" }, item.status === "processing" ? el("span", { class: "spinner" }) : null, status) : null,
    el("span", { class: "small muted", text: ago(item.received_at) })));
  const preview = item.text.length > 220 ? item.text.slice(0, 220) + "…" : item.text;
  card.append(el("div", { class: "msg", text: preview }));

  if (item.status === "failed") {
    card.append(el("div", { class: "notice error", text: item.error || "Unknown error" }),
      el("div", { class: "actions" },
        el("button", { text: "Try again", onclick: async () => { await api(`/api/inbox/${item.id}/retry`, { method: "POST" }); loadInbox(); } }),
        el("button", { text: "Dismiss", onclick: () => dismiss(item.id) })));
    return card;
  }
  if (item.status !== "ready" && item.status !== "saved") return card;
  if (!item.tasks.length) {
    card.append(el("div", { class: "muted small", text: "No deadlines or events found." }),
      el("div", { class: "actions" }, el("button", { text: "Dismiss", onclick: () => dismiss(item.id) })));
    return card;
  }
  for (const t of item.tasks) card.append(renderTask(item, t));
  const n = item.tasks.filter((t) => t.selected && t.ui !== "saved" && t.due_date).length;
  const msg = el("span", { class: "small muted" });
  card.append(el("div", { class: "actions" },
    el("button", { class: "primary", text: n ? `Save ${n} to Google Calendar` : "Tick a task to save", disabled: !n,
      onclick: async (e) => {
        e.target.disabled = true; e.target.textContent = "Saving…";
        try {
          const res = await api(`/api/inbox/${item.id}/save`, { method: "POST", body: { confirmed: true } });
          const bad = res.results.find((r) => !r.ok);
          if (bad) {
            msg.textContent = bad.message;
            if (bad.code === "auth_required") msg.append(" ", el("a", { href: "/auth/start", target: "_blank", rel: "noopener", text: "Connect Google Calendar" }));
          }
        } catch (err) { msg.textContent = err.message; }
        loadInbox();
      } }),
    el("button", { text: "Dismiss", onclick: () => dismiss(item.id) }), msg));
  return card;
}

async function dismiss(id) {
  await api(`/api/inbox/${id}/dismiss`, { method: "POST" });
  loadInbox();
}

function renderTask(item, t) {
  const saved = t.ui === "saved";
  const box = el("div", { class: `task${saved ? " saved" : ""}` });
  const edit = async (patch) => {
    try {
      const res = await api(`/api/inbox/${item.id}/tasks/${t.id}`, { method: "POST", body: patch });
      Object.assign(t, res.task);
    } catch (e) { alertBox(box, e.message); }
  };
  const check = el("input", { type: "checkbox", checked: !!t.selected && !saved, disabled: saved, title: "Include when saving",
    onchange: async (e) => { await edit({ selected: e.target.checked }); loadInbox(); } });
  const area = el("select", { class: `area-chip area-${t.area || "personal"}`, disabled: saved, "aria-label": "Category",
    onchange: async (e) => { await edit({ area: e.target.value }); loadInbox(); } },
  AREAS.map(([v, label]) => el("option", { value: v, text: label, selected: v === (t.area || "personal") })));
  const link = saved && t.result && safeLink(t.result.html_link);
  box.append(el("div", { class: "task-row" }, check,
    el("span", { class: "title", text: t.title || "(untitled)" }),
    el("span", { class: `when${t.due_date ? "" : " missing"}`, text: whenText(t) }),
    area,
    saved ? (link ? el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: "Open ↗" }) : el("span", { class: "small", text: "Saved" }))
      : el("button", { class: "link small", text: "Edit", onclick: () => toggleEditor(box, item, t, edit) })));
  const notes = [...(t.ambiguities || []).map((a) => `⚠️ ${a}`), ...(t.notes || [])];
  if (!saved && notes.length) box.append(el("div", { class: "sub muted", text: notes.join(" ") }));
  if (t.error && !saved) box.append(el("div", { class: "sub notice error", text: t.error.message }));
  if (!saved && t.candidates && t.candidates.length > 1) {
    const chips = el("div", { class: "chips sub" });
    for (const c of t.candidates) {
      if (c.date === t.due_date) continue;
      chips.append(el("button", { text: `Change to ${c.label}`, onclick: async () => { await edit({ due_date: c.date, selected: true }); loadInbox(); } }));
    }
    if (chips.childNodes.length) box.append(chips);
  }
  return box;
}

function toggleEditor(box, item, t, edit) {
  const existing = box.querySelector(".editor");
  if (existing) { existing.remove(); loadInbox(); return; }
  const f = (label, input) => el("div", {}, el("label", { text: label }), input);
  const title = el("input", { value: t.title || "", maxlength: "300" });
  const date = el("input", { type: "date", value: t.due_date || "" });
  const time = el("input", { type: "time", value: t.due_time || "" });
  const end = el("input", { type: "time", value: t.end_time || "" });
  const allDay = el("input", { type: "checkbox", checked: !!t.all_day });
  const loc = el("input", { value: t.location || "", maxlength: "500" });
  const msg = el("span", { class: "small muted" });
  box.append(el("div", { class: "editor" },
    f("Title", title),
    el("div", { class: "row" }, f("Date", date), f("Time", time), f("Ends", end)),
    el("label", { class: "inline" }, allDay, "All-day"),
    f("Location or link", loc),
    el("div", { class: "actions" },
      el("button", { class: "primary", text: "Done", onclick: async () => {
        await edit({ title: title.value, due_date: date.value || null, due_time: allDay.checked ? null : (time.value || null),
          end_time: allDay.checked ? null : (end.value || null), all_day: allDay.checked || !time.value,
          location: loc.value || null, selected: true });
        msg.textContent = "Saved changes";
        loadInbox();
      } }), msg)));
}

function alertBox(box, text) {
  box.append(el("div", { class: "sub notice error", text }));
}

async function addPaste() {
  const text = $("paste-text").value.trim();
  if (!text) return;
  $("paste-add").disabled = true;
  try {
    await api("/api/inbox", { method: "POST", body: { text, reference_time: new Date().toISOString() } });
    $("paste-text").value = "";
    $("paste-msg").textContent = "Added. Reading it now…";
    setTimeout(() => { $("paste-msg").textContent = ""; }, 3000);
    loadInbox();
  } catch (e) { $("paste-msg").textContent = e.message; }
  $("paste-add").disabled = false;
}

// ------------------------------------------------------------------ upcoming
async function loadUpcoming() {
  const box = $("upcoming-list");
  fill(box, el("p", { class: "muted", text: "Loading from Google Calendar…" }));
  let data;
  try { data = await api("/api/events"); } catch (e) {
    fill(box, el("div", { class: "notice warn" }, e.message, " ",
      e.code === "auth_required" ? el("a", { href: "/auth/start", target: "_blank", rel: "noopener", text: "Connect Google Calendar" }) : null));
    return;
  }
  if (!data.events.length) { fill(box, el("p", { class: "muted", text: "Nothing upcoming from D-Day." })); return; }
  const groups = AREAS.map(([key, label]) => [key, label, data.events.filter((e) => (e.area || "personal") === key)])
    .filter(([, , list]) => list.length);
  fill(box, ...groups.flatMap(([key, label, list]) => [
    el("div", { class: "group-title" }, el("span", { class: `dot area-dot area-${key}` }), label, el("span", { class: "muted small", text: `(${list.length})` })),
    ...list.map((ev) => {
      const [cd, soon] = countdown(ev.all_day ? ev.start : (["deadline", "assignment"].includes(ev.event_type) ? ev.end : ev.start), ev.all_day);
      const link = safeLink(ev.html_link);
      return el("div", { class: "row-line" },
        el("span", { class: "grow" }, link ? el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: ev.title }) : ev.title),
        el("span", { class: "small", text: whenText({ due_date: (ev.start || "").slice(0, 10), due_time: ev.all_day ? null : new Date(["deadline", "assignment"].includes(ev.event_type) ? ev.end : ev.start).toTimeString().slice(0, 5), all_day: ev.all_day }) }),
        el("span", { class: `countdown${soon ? " soon" : ""}`, text: cd }));
    }),
  ]));
}

// ------------------------------------------------------------------ history
async function loadHistory() {
  const box = $("history-list");
  try {
    const { events } = await api("/api/history");
    if (!events.length) { fill(box, el("p", { class: "muted", text: "Nothing saved yet." })); return; }
    fill(box, ...events.map((e) => {
      const link = safeLink(e.html_link);
      return el("div", { class: "row-line" },
        el("span", { class: "grow" }, link ? el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: e.summary }) : e.summary),
        el("span", { class: "small muted", text: e.when || "" }),
        el("span", { class: "small muted", text: e.created_at ? `saved ${ago(new Date(e.created_at * 1000).toISOString())}` : "" }));
    }));
  } catch (e) { fill(box, el("div", { class: "notice error", text: e.message })); }
}

// ------------------------------------------------------------------ settings
async function loadSettings() {
  try {
    const { settings } = await api("/api/settings");
    $("pref-name").value = settings.user_name || "";
    $("pref-tz").value = settings.timezone;
    $("pref-untimed").value = settings.untimed_deadline;
    $("pref-auto").checked = settings.auto_resolve !== false;
  } catch (e) { $("pref-msg").textContent = e.message; }
  if (!$("tz-list").childNodes.length) {
    try { for (const tz of Intl.supportedValuesOf("timeZone")) $("tz-list").append(el("option", { value: tz })); } catch { /* old browser */ }
  }
  try {
    const a = await api("/api/autostart");
    $("pref-startup").checked = a.enabled;
    $("pref-startup").disabled = !a.supported;
  } catch { /* ignore */ }
  loadShare();
  loadGTasks();
  loadTelegram();
  loadGoogle();
}

async function loadShare() {
  const box = $("share-box");
  let info;
  try { info = await api("/api/share"); } catch (e) { fill(box, el("p", { class: "muted", text: e.message })); return; }
  const input = el("input", { value: info.client_id || "", placeholder: "1234567890-abc123.apps.googleusercontent.com",
    spellcheck: "false", autocomplete: "off" });
  const msg = el("span", { class: "small muted" });
  const save = el("button", { class: "primary", text: "Save", onclick: async () => {
    try { await api("/api/share", { method: "POST", body: { share_client_id: input.value.trim() } }); loadShare(); }
    catch (e) { msg.textContent = e.message; }
  } });
  const intro = el("p", { class: "small", text: "Long-press a message in WhatsApp → Share → D-Day. It lands in your \"D-Day\" Google Tasks list from anywhere, and this PC picks it up. Needs the Google Tasks setup below first." });
  const stepClient = el("li", {}, "Google Cloud Console → your D-Day project → APIs & Services → ",
    el("strong", { text: "Credentials" }), " → Create credentials → OAuth client ID → ",
    el("strong", { text: "Web application" }), ". Under ", el("strong", { text: "Authorized JavaScript origins" }), " add ",
    el("code", { class: "mono", text: info.app_origin }), " → Create. Copy the Client ID and paste it here: ",
    el("a", { href: "https://console.cloud.google.com/apis/credentials", target: "_blank", rel: "noopener", text: "Open Credentials ↗" }));
  if (!info.client_id) {
    fill(box, intro, el("ol", { class: "steps small" }, stepClient), input, el("div", { class: "actions" }, save, msg));
    return;
  }
  fill(box, intro,
    el("ol", { class: "steps small" },
      el("li", {}, "On your phone, scan this code (or open the link below) in ", el("strong", { text: "Chrome" }), ":"),
    ),
    el("img", { class: "qr", src: `/app/share-qr.svg?v=${encodeURIComponent(info.client_id)}`, alt: "QR code with your D-Day share app setup link" }),
    el("p", { class: "mono", text: info.setup_url }),
    el("ol", { class: "steps small", start: "2" },
      el("li", {}, "In Chrome's ⋮ menu tap ", el("strong", { text: "Add to Home screen → Install" }), "."),
      el("li", {}, "In WhatsApp, long-press a message → Share → ", el("strong", { text: "D-Day" }), " → ", el("strong", { text: "Send" }),
        ". The first time, sign in with the same Google account and allow Google Tasks.")),
    el("details", {}, el("summary", { class: "small muted", text: "Change the client ID" }), input, el("div", { class: "actions" }, save, msg)));
}

let gtTimer = null;
async function loadGTasks() {
  clearTimeout(gtTimer);
  const box = $("gt-box");
  let st;
  try { st = await api("/api/gtasks"); } catch (e) { fill(box, el("p", { class: "muted", text: e.message })); return; }
  const err = st.error || {};
  const steps = el("ol", { class: "steps small" },
    el("li", {}, "In Google Cloud Console, open your D-Day project → APIs & Services → Library → ",
      el("strong", { text: "Google Tasks API" }), " → Enable. ",
      el("a", { href: "https://console.cloud.google.com/apis/library/tasks.googleapis.com", target: "_blank", rel: "noopener", text: "Open it ↗" })),
    el("li", {}, "Reconnect Google below and tick ", el("strong", { text: "Google Tasks" }), " on the permission screen. ",
      el("a", { href: "/auth/start", target: "_blank", rel: "noopener", class: "button-link", text: "Reconnect Google" })),
    el("li", {}, "On your phone, open the ", el("strong", { text: "Google Tasks" }), " app. D-Day creates a list called ",
      el("strong", { text: st.list_name }), ". Add a task there and paste the message as its title or details."));
  if (!st.google_connected) {
    fill(box, el("p", { class: "muted", text: "Connect Google Calendar first (below)." }));
    return;
  }
  if (!st.permission || err.code === "api_disabled" || err.code === "permission_missing") {
    fill(box,
      el("p", { class: "small", text: "Send messages from your phone by adding them to a Google Tasks list. D-Day picks them up within a minute when the PC is on." }),
      err.code ? el("div", { class: "notice warn small", text: err.message }) : null,
      steps,
      el("button", { class: "link small", text: "I've done this, check again", onclick: async () => { await api("/api/gtasks/check", { method: "POST" }); setTimeout(loadGTasks, 4000); } }));
    gtTimer = setTimeout(loadGTasks, 15000);
    return;
  }
  const last = st.last_check ? ago(st.last_check) : "not yet";
  fill(box,
    el("p", {}, el("span", { class: `dot ${err.code ? "warn" : "ok"}` }),
      st.list_ready ? "Connected. " : "Setting up… ",
      "Add messages to the ", el("strong", { text: `"${st.list_name}"` }), " list in Google Tasks on your phone."),
    err.code ? el("div", { class: "notice warn small", text: err.message }) : null,
    el("p", { class: "small muted", text: `Checks every minute · last check ${last} · ${st.imported} imported. Picked-up tasks are ticked off on your phone.` }),
    el("div", { class: "actions" },
      el("button", { text: "Check now", onclick: async () => { await api("/api/gtasks/check", { method: "POST" }); setTimeout(() => { loadGTasks(); loadInbox(); }, 4000); } }),
      el("button", { class: "link small", text: st.enabled ? "Pause phone inbox" : "Resume phone inbox",
        onclick: async () => { await api("/api/gtasks", { method: "POST", body: { enabled: !st.enabled } }); loadGTasks(); } })));
  gtTimer = setTimeout(loadGTasks, 30000);
}

async function savePrefs() {
  try {
    await api("/api/settings", { method: "POST", body: {
      user_name: $("pref-name").value, timezone: $("pref-tz").value.trim() || "Asia/Kolkata",
      untimed_deadline: $("pref-untimed").value, auto_resolve: $("pref-auto").checked } });
    $("pref-msg").textContent = "Saved";
  } catch (e) { $("pref-msg").textContent = e.message; }
  setTimeout(() => { $("pref-msg").textContent = ""; }, 2500);
}

async function loadGoogle() {
  const box = $("google-box");
  try {
    const h = await api("/api/health");
    if (h.google.connected) {
      fill(box, el("p", {}, el("span", { class: "dot ok" }), "Connected."),
        el("button", { text: "Disconnect", onclick: async () => { await api("/api/auth/disconnect", { method: "POST" }); loadGoogle(); } }));
    } else if (h.google.configured) {
      fill(box, el("p", { text: "Not connected." }), el("a", { href: "/auth/start", target: "_blank", rel: "noopener", class: "button-link", text: "Connect Google Calendar" }));
    } else {
      fill(box, el("p", { class: "muted", text: "Google OAuth isn't set up yet. See the README (Google Cloud setup)." }));
    }
  } catch (e) { fill(box, el("p", { class: "muted", text: e.message })); }
}

let tgTimer = null;
async function loadTelegram() {
  clearTimeout(tgTimer);
  const box = $("tg-box");
  let st;
  try { st = await api("/api/telegram"); } catch (e) { fill(box, el("p", { class: "muted", text: e.message })); return; }
  if (!st.configured) {
    const token = el("input", { placeholder: "Paste the bot token from BotFather", autocomplete: "off", spellcheck: "false" });
    const msg = el("span", { class: "small muted" });
    fill(box, 
      el("p", { class: "small", text: "Forward messages from your phone to a private Telegram bot. Your PC reads them when it's on (Telegram keeps them for about a day)." }),
      el("ol", { class: "steps small" },
        el("li", {}, "On your phone, open Telegram and search for ", el("strong", { text: "@BotFather" }), " (blue tick)."),
        el("li", {}, "Send ", el("code", { text: "/newbot" }), ", pick any name, then a username ending in ", el("code", { text: "bot" }), "."),
        el("li", {}, "BotFather replies with a token like ", el("code", { text: "123456789:AA…" }), ". Paste it here:")),
      token,
      el("div", { class: "actions" }, el("button", { class: "primary", text: "Connect", onclick: (e) => connectTelegram(e.target, token, msg) }), msg));
    return;
  }
  if (!st.paired) {
    fill(box, 
      el("p", {}, "Almost done. In Telegram, open ",
        st.bot_username ? el("strong", { text: `@${st.bot_username}` }) : "the bot whose token you pasted",
        " and send this code:"),
      !st.bot_username && st.bot_id ? el("p", { class: "small muted" },
        "Not sure which bot? In BotFather send /mybots, open each bot → API Token: use the one whose token starts with ",
        el("strong", { text: st.bot_id }), ".") : null,
      el("div", { class: "code", text: st.pair_code }),
      st.pair_link ? el("p", { class: "small muted" }, "Or open this link on your phone: ", el("span", { text: st.pair_link })) : null,
      st.last_error ? el("p", { class: "small muted", text: st.last_error }) : null,
      el("p", { class: "small muted", text: "Waiting for your message…" }),
      el("button", { class: "link small", text: "Use a different bot", onclick: async () => { await api("/api/telegram/disconnect", { method: "POST" }); loadTelegram(); } }));
    tgTimer = setTimeout(loadTelegram, 3000);
    return;
  }
  fill(box, 
    el("p", {}, el("span", { class: "dot ok" }), "Connected to ", el("strong", { text: `@${st.bot_username}` }),
      ". Forward any message with a deadline to it from WhatsApp, SMS or email."),
    st.last_error ? el("div", { class: "notice warn small", text: st.last_error }) : null,
    el("button", { text: "Disconnect phone", onclick: async () => { await api("/api/telegram/disconnect", { method: "POST" }); loadTelegram(); loadStatus(); } }));
}

async function connectTelegram(button, tokenInput, msg) {
  button.disabled = true;
  msg.textContent = "Checking with Telegram… (can take up to 30 s on a slow connection)";
  try {
    await api("/api/telegram/token", { method: "POST", body: { token: tokenInput.value } });
    loadTelegram();
    loadStatus();
  } catch (err) {
    const wait = err.code === "telegram_rate_limited" && err.extra && err.extra.retry_after;
    if (!wait) { msg.textContent = err.message; button.disabled = false; return; }
    // Telegram asked us to slow down: count down and retry once by ourselves.
    let left = Number(wait) + 1;
    const tick = () => {
      if (!button.isConnected) return;
      if (left <= 0) { connectTelegram(button, tokenInput, msg); return; }
      msg.textContent = `Telegram asked us to wait. Retrying automatically in ${left} s…`;
      left -= 1;
      setTimeout(tick, 1000);
    };
    tick();
  }
}

// ------------------------------------------------------------------ init
for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
$("paste-add").addEventListener("click", addPaste);
$("paste-text").addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") addPaste(); });
$("pref-save").addEventListener("click", savePrefs);
$("pref-startup").addEventListener("change", async (e) => {
  try {
    const a = await api("/api/autostart", { method: "POST", body: { enabled: e.target.checked } });
    e.target.checked = a.enabled;
    $("pref-msg").textContent = a.enabled ? "D-Day will start with Windows." : "D-Day won't start with Windows.";
  } catch (err) { $("pref-msg").textContent = err.message; e.target.checked = !e.target.checked; }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) { loadStatus(); if (currentTab === "inbox") loadInbox(); } });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }).catch(() => {});
loadStatus();
setInterval(loadStatus, 30000);
const startTab = location.hash.slice(1);
showTab(["inbox", "upcoming", "history", "settings"].includes(startTab) ? startTab : "inbox");
window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (t) showTab(t); });
