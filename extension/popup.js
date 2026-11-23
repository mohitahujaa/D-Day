import {
  api, el, getSettings, saveSettings, getDrafts, deleteDraft, newId, putDraft,
  parseReminderList, serviceUrl, safeCalendarLink, nativeHost,
} from "./common.js";

const $ = (id) => document.getElementById(id);
let settings;

function line(ok, label, detail) {
  const cls = ok === true ? "ok" : ok === false ? "bad" : "warn";
  return el("div", { class: "status-line" },
    el("span", {}, el("span", { class: `dot ${cls}` }), label),
    el("span", { class: "muted small", text: detail }));
}

function setPower(on, label) {
  $("power").checked = on;
  $("power-label").textContent = label || (on ? "On" : "Off");
}

function powerMessage(text, kind = "error") {
  const box = $("power-msg");
  box.hidden = !text;
  box.className = `small notice ${kind}`;
  box.textContent = text || "";
}

async function onPowerToggle(e) {
  const turnOn = e.target.checked;
  const sw = $("power");
  sw.disabled = true;
  setPower(turnOn, turnOn ? "Starting…" : "Stopping…");
  powerMessage(turnOn ? "Starting Ollama and D-Day… (up to a minute after a restart)" : "", "warn");
  try {
    const res = await nativeHost(turnOn ? "start" : "stop");
    powerMessage(res.message || "", "warn");
    if (!turnOn) powerMessage("D-Day is off. The model has been unloaded to free memory.", "ok");
  } catch (err) {
    powerMessage(err.message);
  } finally {
    sw.disabled = false;
    await loadStatus();
    loadUpcoming();
  }
}

async function loadStatus() {
  const box = $("status");
  const actions = $("google-actions");
  box.replaceChildren(el("div", { class: "muted small", text: "Checking…" }));
  actions.replaceChildren();
  try {
    const { data } = await api("/api/health", { timeoutMs: 5000 });
    setPower(true);
    const o = data.ollama;
    const g = data.google;
    box.replaceChildren(
      line(true, "Local service", "running"),
      line(o.reachable && o.model_installed ? true : false, "Local model",
        !o.reachable ? "Ollama not running" : o.model_installed ? o.model : `run: ollama pull ${o.model}`),
      line(g.connected ? true : g.configured ? null : false, "Google Calendar",
        g.connected ? "connected" : g.configured ? "not connected" : "not configured (see README)"),
    );
    if (g.configured && !g.connected) {
      actions.append(el("button", { class: "primary", text: "Connect Google Calendar",
        onclick: () => chrome.tabs.create({ url: serviceUrl(settings, "/auth/start") }) }));
    }
    if (g.connected) {
      actions.append(el("button", { text: "Disconnect Google", onclick: async () => {
        await api("/api/auth/disconnect", { method: "POST" });
        loadStatus();
      } }));
    }
  } catch (err) {
    setPower(false);
    box.replaceChildren(line(false, "D-Day", "off"),
      el("div", { class: "muted small", text: "Switch it on above, or just use “Add to D-Day”: it turns itself on." }));
  }
}

function formatWhen(ev) {
  const dayFmt = { weekday: "short", day: "numeric", month: "short" };
  if (ev.all_day) {
    const [y, m, d] = ev.start.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, dayFmt);
  }
  // Deadlines are saved as a block that ends at the due time; show the due time.
  const isDeadline = ev.event_type === "deadline" || ev.event_type === "assignment";
  const at = new Date(isDeadline ? ev.end : ev.start);
  const time = at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${at.toLocaleDateString(undefined, dayFmt)}, ${isDeadline ? "due " : ""}${time}`;
}

async function loadUpcoming() {
  const box = $("upcoming");
  try {
    const { data } = await api("/api/events", { timeoutMs: 15000 });
    if (!data.events.length) {
      box.replaceChildren("Nothing upcoming. Tasks you save with D-Day appear here.");
      return;
    }
    box.replaceChildren(...data.events.map((ev) => {
      const link = safeCalendarLink(ev.html_link);
      const title = link
        ? el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: ev.title })
        : el("span", { text: ev.title });
      const dot = el("span", { class: `dot area-dot area-${ev.area || "none"}`, title: ev.area || "" });
      return el("div", { class: "status-line" }, el("span", { class: "upcoming-title" }, dot, title),
        el("span", { class: "muted small", text: formatWhen(ev) }));
    }));
  } catch (err) {
    box.replaceChildren(err.code === "auth_required" || err.code === "google_not_configured"
      ? "Connect Google Calendar to see your tasks."
      : err.code === "service_unreachable" ? "Start the D-Day service to see your tasks." : err.message);
  }
}

async function loadDrafts() {
  const drafts = Object.values(await getDrafts())
    .filter((d) => d.status !== "done")
    .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  const box = $("drafts");
  if (!drafts.length) { box.replaceChildren("None"); return; }
  box.replaceChildren(...drafts.slice(0, 8).map((d) => {
    const preview = (d.text || "(empty)").replace(/\s+/g, " ").slice(0, 60);
    return el("div", { class: "status-line" },
      el("button", { class: "link", text: preview, title: "Reopen",
        onclick: () => openReview(d.id) }),
      el("button", { class: "link small", text: "Discard", onclick: async () => { await deleteDraft(d.id); loadDrafts(); } }));
  }));
}

function openReview(id) {
  chrome.windows.create({ url: chrome.runtime.getURL(`review.html?draft=${id}`), type: "popup", width: 460, height: 760 });
  window.close();
}

function fillSettings() {
  $("timezone").value = settings.timezone;
  $("untimed").value = settings.untimed_deadline;
  $("deadline-mode").value = settings.deadline_mode;
  $("deadline-dur").value = settings.deadline_duration_minutes;
  $("event-dur").value = settings.event_duration_minutes;
  $("rem-timed").value = settings.reminders_timed.join(", ");
  $("rem-allday").value = settings.reminders_all_day.join(", ");
  $("include-source").checked = !!settings.include_source;
  $("port").value = settings.port;
  $("user-name").value = settings.user_name || "";
  $("auto-resolve").checked = settings.auto_resolve !== false;
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  $("use-browser-tz").textContent = `Use browser timezone (${browserTz})`;
  $("use-browser-tz").onclick = () => { $("timezone").value = browserTz; };
  const list = $("tz-list");
  try {
    for (const tz of Intl.supportedValuesOf("timeZone")) list.append(el("option", { value: tz }));
  } catch { /* older browsers */ }
}

function validTimezone(tz) {
  try { new Intl.DateTimeFormat("en", { timeZone: tz }); return true; } catch { return false; }
}

async function onSave() {
  const msg = $("saved-msg");
  const tz = $("timezone").value.trim() || "Asia/Kolkata";
  const remT = parseReminderList($("rem-timed").value);
  const remA = parseReminderList($("rem-allday").value);
  const dd = Number($("deadline-dur").value);
  const ed = Number($("event-dur").value);
  const port = Number($("port").value);
  const problems = [];
  if (!validTimezone(tz)) problems.push("unknown timezone");
  if (!remT || !remA) problems.push("reminders must be up to 5 numbers (0-40320)");
  if (!(dd >= 1 && dd <= 1440) || !(ed >= 1 && ed <= 1440)) problems.push("durations must be 1-1440 minutes");
  if (!(port >= 1024 && port <= 65535)) problems.push("invalid port");
  if (problems.length) { msg.textContent = problems.join("; "); return; }
  settings = await saveSettings({
    timezone: tz, untimed_deadline: $("untimed").value, deadline_mode: $("deadline-mode").value,
    deadline_duration_minutes: dd, event_duration_minutes: ed,
    reminders_timed: remT, reminders_all_day: remA,
    include_source: $("include-source").checked, port,
    user_name: $("user-name").value.trim().slice(0, 60), auto_resolve: $("auto-resolve").checked,
  });
  msg.textContent = "Saved";
  setTimeout(() => { msg.textContent = ""; }, 1500);
  loadStatus();
}

async function init() {
  settings = await getSettings();
  fillSettings();
  $("save-settings").addEventListener("click", onSave);
  $("refresh").addEventListener("click", () => { loadStatus(); loadUpcoming(); });
  $("power").addEventListener("change", onPowerToggle);
  $("open-dashboard").addEventListener("click", async () => {
    try { await api("/api/health", { timeoutMs: 3000, autoStart: true }); } catch { /* opens anyway */ }
    chrome.tabs.create({ url: serviceUrl(settings, "/app") });
    window.close();
  });
  $("paste").addEventListener("click", async () => {
    const id = newId();
    await putDraft({ id, text: "", created_at: Date.now(), reference_time: new Date().toISOString(), status: "new", tasks: null });
    openReview(id);
  });
  loadStatus();
  loadUpcoming();
  loadDrafts();
}

init();
