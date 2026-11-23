// Review window. Goal: one click for the common case.
// Confident items are pre-selected and shown as compact rows; anything that
// needs a decision opens up. "Save N to Google Calendar" saves all selected.
import {
  api, ApiError, getSettings, getDraft, putDraft, deleteDraft, el, newId,
  safeCalendarLink, parseReminderList, serviceUrl,
} from "./common.js";

const EVENT_TYPES = ["deadline", "assignment", "exam", "meeting", "reminder", "other"];
// Highest priority first; colours match the Google Calendar event colours.
const AREAS = [["job", "Internship / Job"], ["work", "Work"], ["college", "College"], ["personal", "Personal"]];
const JOB_REMINDERS = { timed: [2880, 1440, 120], allDay: [3780, 2340, 900] };
const $ = (id) => document.getElementById(id);

let settings;
let draft;
let saveTimer;
const previews = new Map(); // task id -> { when } | { error }

function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => putDraft(draft), 250);
}

// ---------------------------------------------------------------- state view
function showState(kind, ...content) {
  const box = $("state");
  box.replaceChildren();
  if (!kind) return;
  box.append(el("div", { class: kind === "loading" ? "center" : `notice ${kind}` }, ...content));
}

function showLoading() {
  const started = Date.now();
  const counter = el("span", { class: "muted small", text: "" });
  showState("loading", el("span", { class: "spinner" }),
    "Reading the message with the local model…", el("br"), counter);
  const t = setInterval(() => {
    if (!counter.isConnected) return clearInterval(t);
    const s = Math.round((Date.now() - started) / 1000);
    counter.textContent = s > 3 ? `${s}s (the first run after starting Ollama is slower)` : "";
  }, 1000);
}

// ---------------------------------------------------------------- extraction
async function extract() {
  const text = draft.text || "";
  if (!text.trim()) {
    showState("warn", "No text was captured from the page. Paste the text into “Selected text” above and press “Analyse again”, or add a task manually.");
    $("source-box").open = true;
    renderFooter();
    return;
  }
  $("tasks").replaceChildren();
  showLoading();
  try {
    const { data } = await api("/api/extract", {
      method: "POST",
      body: {
        text,
        timezone: settings.timezone,
        reference_time: draft.reference_time,
        untimed_deadline: settings.untimed_deadline,
        reader: settings.user_name || undefined,
        auto_resolve: settings.auto_resolve !== false,
      },
      timeoutMs: 300000,
      autoStart: true,
      onStarting: () => showState("loading", el("span", { class: "spinner" }),
        "D-Day was off. Switching it on… (up to a minute after a restart)"),
    });
    const kept = (draft.tasks || []).filter((t) => t.ui === "saved");
    draft.tasks = [...kept, ...data.tasks.map(toCardState)];
    draft.warnings = data.warnings || [];
    draft.sent_at = data.message_sent_at || null;
    draft.status = "extracted";
    await putDraft(draft);
    render();
  } catch (err) {
    renderExtractError(err);
  }
}

function renderExtractError(err) {
  const tips = {
    service_unreachable: "Your selection is saved. Turn D-Day on from the toolbar popup, then press Retry.",
    ollama_unavailable: "Start Ollama (open the Ollama app or run “ollama serve”), then press Retry.",
    model_not_found: "Install the model with “ollama pull qwen3:1.7b”, then press Retry.",
    ollama_timeout: "Try selecting a shorter piece of text.",
  };
  showState("error",
    el("strong", { text: "Couldn't extract tasks. " }), err.message || String(err),
    tips[err.code] ? el("div", { text: tips[err.code] }) : null,
    el("div", { class: "actions" },
      el("button", { text: "Retry", onclick: extract }),
      el("button", { text: "Enter manually", onclick: addManual })));
  renderFooter();
}

function defaultReminders(allDay) {
  return allDay ? settings.reminders_all_day : settings.reminders_timed;
}

function toCardState(t) {
  return {
    ...t,
    include_source: settings.include_source,
    reminders: Array.isArray(t.reminders) && t.reminders.length ? t.reminders : defaultReminders(!!t.all_day),
    reminders_touched: false,
    include: t.selected !== undefined ? !!t.selected : !!t.due_date,
    expanded: !t.due_date,
    idempotency_key: newId(),
    ui: "editing", // editing | saving | saved | skipped
    result: null,
    error: null,
  };
}

function addManual() {
  draft.tasks = draft.tasks || [];
  draft.tasks.push(toCardState({
    id: newId(), title: "", description: "", event_type: "deadline", item_type: "DEADLINE",
    due_date: null, due_time: null, end_time: null, all_day: true, timezone: settings.timezone,
    area: "personal", location: null, source_text: (draft.text || "").slice(0, 400) || null, candidates: [], ambiguities: [],
    notes: [], confidence: null, confidence_label: "manual", date_status: "MANUAL", selected: true,
  }));
  draft.status = "extracted";
  persist();
  render();
}

// ---------------------------------------------------------------- rendering
function isReady(t) {
  return t.ui === "editing" && t.include && t.due_date && t.title && t.title.trim()
    && !(previews.get(t.id) || {}).error;
}

function render() {
  const tasks = draft.tasks || [];
  const list = $("tasks");
  list.replaceChildren();
  if (draft.sent_at) {
    const d = new Date(draft.sent_at);
    list.append(el("div", { class: "muted small", text: `Dates like “tomorrow” were read relative to when the message was sent (${d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}).` }));
  }
  if (draft.warnings && draft.warnings.length) {
    list.append(el("div", { class: "notice warn" }, draft.warnings.join(" ")));
  }
  if (!tasks.length) {
    showState("warn", "No deadlines or events were found in this selection. ",
      el("button", { class: "link", text: "Add one manually", onclick: addManual }));
    renderFooter();
    return;
  }
  const open = tasks.filter((t) => t.ui === "editing" || t.ui === "saving").length;
  showState(open === 0 ? "ok" : null, open === 0 ? "All done. You can close this window." : "");
  tasks.forEach((t) => list.append(renderCard(t)));
  renderFooter();
  if (open === 0) finish();
}

function renderFooter() {
  const btn = $("save-all");
  const tasks = draft.tasks || [];
  const ready = tasks.filter(isReady);
  const saving = tasks.some((t) => t.ui === "saving");
  btn.hidden = !tasks.some((t) => t.ui === "editing" || t.ui === "saving");
  btn.disabled = saving || ready.length === 0;
  btn.textContent = saving ? "Saving…"
    : ready.length === 0 ? "Tick an item to save"
      : `Save ${ready.length} to Google Calendar`;
}

function taskForApi(t) {
  return {
    title: t.title,
    description: t.description,
    event_type: t.event_type,
    item_type: t.item_type,
    due_date: t.due_date,
    due_time: t.all_day ? null : t.due_time,
    end_time: t.all_day ? null : t.end_time || null,
    all_day: t.all_day,
    timezone: settings.timezone,
    area: t.area || null,
    location: t.location || null,
    reminders: t.reminders,
    include_source: !!t.include_source,
    source_text: t.include_source ? t.source_text : null,
  };
}

function apiSettings() {
  const { timezone, deadline_mode, deadline_duration_minutes, event_duration_minutes,
    reminders_timed, reminders_all_day, calendar_id } = settings;
  return { timezone, deadline_mode, deadline_duration_minutes, event_duration_minutes,
    reminders_timed, reminders_all_day, calendar_id };
}

function savedCard(t) {
  const link = t.result && safeCalendarLink(t.result.html_link);
  return el("article", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", { class: t.ui === "skipped" ? "done-title" : "", text: t.title || "(untitled)" }),
      el("span", { class: "badge", text: t.ui === "saved" ? "Saved" : "Skipped" })),
    t.ui === "saved" ? el("div", { class: "notice ok" },
      t.result.status === "already_created" ? "Already in your calendar: " : "Added to Google Calendar: ",
      el("span", { text: t.result.when || "" }),
      link ? el("div", {}, el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: "Open event in Google Calendar ↗" })) : null)
      : null,
    t.ui === "skipped" ? el("button", { class: "link", text: "Undo skip", onclick: () => { t.ui = "editing"; persist(); render(); } }) : null);
}

/** Assumptions ("Assumed the coming Monday") with one-tap alternatives. */
function assumptionBlock(t, update) {
  const box = el("div", { class: "assume" });
  const notes = (t.notes || []).filter(Boolean);
  if (notes.length) box.append(el("div", { class: "muted small", text: notes.join(" ") }));
  if (t.candidates && t.candidates.length > 1) {
    const chips = el("div", { class: "chips" });
    t.candidates.forEach((c, ci) => {
      if (t.due_date === c.date) return;
      chips.append(el("button", {
        text: t.due_date ? `Change to ${c.label}` : c.label,
        onclick: () => {
          const patch = { due_date: c.date, chosen_candidate: ci, include: true };
          if (t.candidate_time && !t.due_time) patch.due_time = t.candidate_time;
          update(patch, true);
        },
      }));
    });
    if (chips.childNodes.length) box.append(chips);
  }
  return box.childNodes.length ? box : null;
}

function renderCard(t) {
  if (t.ui === "saved" || t.ui === "skipped") return savedCard(t);
  const card = el("article", { class: `card${t.include ? "" : " unselected"}` });

  let previewTimer;
  const update = (patch, rerender = false) => {
    Object.assign(t, patch);
    t.error = null;
    persist();
    if (rerender) render();
    else { clearTimeout(previewTimer); previewTimer = setTimeout(refreshPreview, 300); }
  };

  const include = el("input", { type: "checkbox", checked: !!t.include, title: "Include when saving",
    onchange: (e) => update({ include: e.target.checked }, true) });
  const when = el("span", { class: "small when" });
  const conf = t.confidence_label === "manual" ? null
    : el("span", { class: `badge ${t.confidence_label}`, title: `confidence ${t.confidence}`, text: t.confidence_label });

  const areaSel = el("select", {
    class: `area-chip area-${t.area || "personal"}`, title: "Category", "aria-label": "Category",
    onchange: (e) => {
      const patch = { area: e.target.value };
      if (e.target.value === "job" && !t.reminders_touched) {
        patch.reminders = t.all_day ? JOB_REMINDERS.allDay : JOB_REMINDERS.timed;
      }
      update(patch, true);
    },
  }, AREAS.map(([v, label]) => el("option", { value: v, text: label, selected: v === (t.area || "personal") })));
  card.append(el("div", { class: "card-head" },
    el("label", { class: "inline head-title" }, include, el("strong", { text: t.title || "(untitled)" })),
    el("span", { class: "head-right" }, areaSel, conf)));
  card.append(el("div", { class: "summary" }, when,
    t.location ? el("span", { class: "muted small", text: ` · ${t.location}` }) : null,
    el("button", { class: "link small edit", text: t.expanded ? "Done" : "Edit",
      onclick: () => update({ expanded: !t.expanded }, true) })));

  // Things that need attention stay visible even when the card is compact.
  if (t.ambiguities && t.ambiguities.length) {
    card.append(el("div", { class: "notice warn" }, el("ul", {}, t.ambiguities.map((a) => el("li", { text: a })))));
  }
  const assume = assumptionBlock(t, update);
  if (assume) card.append(assume);
  if (t.expanded) card.append(editor(t, update));
  if (t.error) card.append(renderSaveError(t));

  async function refreshPreview() {
    if (t.ui === "saving") return;
    if (!t.due_date) { when.textContent = "Choose a date"; previews.set(t.id, { error: "no date" }); renderFooter(); return; }
    if (!t.title || !t.title.trim()) { when.textContent = "Add a title"; previews.set(t.id, { error: "no title" }); renderFooter(); return; }
    try {
      const { data } = await api("/api/events/preview", { method: "POST", body: { task: taskForApi(t), settings: apiSettings() } });
      const past = new Date(`${t.due_date}T${t.all_day ? "23:59" : (t.due_time || "23:59")}`) < new Date();
      when.textContent = data.when + (past ? " · in the past" : "");
      when.classList.toggle("past", past);
      previews.set(t.id, { when: data.when });
    } catch (err) {
      when.textContent = err.code === "service_unreachable" ? "Service not running" : `Can't save yet: ${err.message}`;
      previews.set(t.id, err.code === "service_unreachable" ? {} : { error: err.message });
    }
    renderFooter();
  }
  refreshPreview();
  return card;
}

function editor(t, update) {
  const box = el("div", { class: "editor" });
  box.append(el("label", { text: "Title", for: `title-${t.id}` }),
    el("input", { id: `title-${t.id}`, value: t.title || "", maxlength: "300",
      oninput: (e) => update({ title: e.target.value }) }));

  const typeSel = el("select", { onchange: (e) => update({ event_type: e.target.value }) },
    EVENT_TYPES.map((v) => el("option", { value: v, text: v[0].toUpperCase() + v.slice(1), selected: v === t.event_type })));
  const allDay = el("input", { type: "checkbox", checked: !!t.all_day, onchange: (e) => {
    const patch = { all_day: e.target.checked };
    if (!t.reminders_touched) patch.reminders = defaultReminders(e.target.checked);
    update(patch, true);
  } });
  box.append(el("div", { class: "row" },
    el("div", {}, el("label", { text: "Type" }), typeSel),
    el("div", {}, el("label", { text: " " }), el("label", { class: "inline" }, allDay, "All-day"))));

  const row = el("div", { class: "row" }, el("div", {}, el("label", { text: "Date" }),
    el("input", { type: "date", value: t.due_date || "", oninput: (e) => update({ due_date: e.target.value || null, include: true }) })));
  if (!t.all_day) {
    row.append(
      el("div", {}, el("label", { text: t.event_type === "deadline" || t.event_type === "assignment" ? "Due at" : "Starts" }),
        el("input", { type: "time", value: t.due_time || "", oninput: (e) => update({ due_time: e.target.value || null }) })),
      el("div", {}, el("label", { text: "Ends (optional)" }),
        el("input", { type: "time", value: t.end_time || "", oninput: (e) => update({ end_time: e.target.value || null }) })));
  }
  box.append(row);

  box.append(el("label", { text: "Location or link" }),
    el("input", { value: t.location || "", maxlength: "500", oninput: (e) => update({ location: e.target.value || null }) }));

  box.append(el("label", { text: t.all_day ? "Reminders (minutes before midnight; 900 = 9:00 the day before)" : "Reminders (minutes before start)" }),
    el("input", { value: (t.reminders || []).join(", "), oninput: (e) => {
      const list = parseReminderList(e.target.value);
      e.target.setCustomValidity(list ? "" : "Use up to 5 numbers between 0 and 40320");
      if (list) update({ reminders: list, reminders_touched: true });
    } }));

  box.append(el("label", { text: "Description" }),
    el("textarea", { rows: "2", value: t.description || "", maxlength: "6000",
      oninput: (e) => update({ description: e.target.value }) }));

  if (t.source_text) {
    box.append(el("label", { text: "Source excerpt" }), el("blockquote", { text: t.source_text }),
      el("label", { class: "inline" },
        el("input", { type: "checkbox", checked: !!t.include_source, onchange: (e) => update({ include_source: e.target.checked }) }),
        "Include this excerpt in the event description (sent to Google)"));
  }
  box.append(el("div", { class: "actions" },
    el("button", { text: "Save just this one", disabled: t.ui === "saving" || !t.due_date, onclick: () => save(t) }),
    el("button", { text: "Skip", disabled: t.ui === "saving", onclick: () => { t.ui = "skipped"; persist(); render(); } })));
  return box;
}

function renderSaveError(t) {
  const e = t.error;
  const box = el("div", { class: "notice error" }, el("div", { text: e.message }));
  const actions = el("div", { class: "actions" });
  if (e.code === "auth_required") {
    actions.append(el("button", { text: "Connect Google Calendar", onclick: connectGoogle }));
    box.append(el("div", { text: "Your task is kept here. After connecting, press Save again." }));
  } else if (e.code === "google_not_configured") {
    box.append(el("div", { text: "Your task is kept here. See the README to set up Google Calendar." }));
  } else if (e.code === "duplicate_suspected") {
    const link = safeCalendarLink(e.html_link);
    if (link) actions.append(el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: "View existing event ↗" }));
    actions.append(el("button", { text: "Save another copy", onclick: () => save(t, true) }));
  } else if (e.code !== "invalid_task") {
    box.append(el("div", { text: "Your task is kept here; you can retry." }));
  }
  if (actions.childNodes.length) box.append(actions);
  return box;
}

async function connectGoogle() {
  await chrome.tabs.create({ url: serviceUrl(settings, "/auth/start") });
}

// ---------------------------------------------------------------- saving
async function save(t, allowDuplicate = false, { quiet = false } = {}) {
  if (t.ui === "saving" && !quiet) return; // double-click guard (server is idempotent too)
  t.ui = "saving";
  t.error = null;
  await putDraft(draft);
  if (!quiet) render();
  try {
    const { data } = await api("/api/events", {
      method: "POST",
      body: {
        confirmed: true,
        task: taskForApi(t),
        settings: apiSettings(),
        idempotency_key: t.idempotency_key,
        allow_duplicate: allowDuplicate,
      },
      timeoutMs: 60000,
      autoStart: true,
    });
    t.ui = "saved";
    t.result = { status: data.status, html_link: data.html_link, when: data.when, event_id: data.event_id };
  } catch (err) {
    t.ui = "editing";
    t.error = err instanceof ApiError
      ? { code: err.code, message: err.message, html_link: err.extra && err.extra.html_link }
      : { code: "unknown", message: String(err) };
  }
  await putDraft(draft);
  if (!quiet) render();
  return t.ui === "saved";
}

const ACCOUNT_ERRORS = ["auth_required", "google_not_configured", "service_unreachable", "rate_limited", "timeout"];

async function saveAll() {
  const ready = (draft.tasks || []).filter(isReady);
  if (!ready.length) return;
  ready.forEach((t) => { t.ui = "saving"; });
  render();
  for (let i = 0; i < ready.length; i++) {
    const ok = await save(ready[i], false, { quiet: true });
    if (!ok && ready[i].error && ACCOUNT_ERRORS.includes(ready[i].error.code)) {
      // Same problem would hit the rest; leave them selected for the retry.
      for (const o of ready.slice(i + 1)) o.ui = "editing";
      break;
    }
  }
  await putDraft(draft);
  render();
}

async function finish() {
  draft.status = "done";
  await putDraft(draft);
  setTimeout(() => deleteDraft(draft.id), 1000);
  $("cancel").textContent = "Close";
  $("save-all").hidden = true;
}

// ---------------------------------------------------------------- init
async function init() {
  settings = await getSettings();
  $("tz").textContent = `Times in ${settings.timezone}`;
  const id = new URLSearchParams(location.search).get("draft");
  draft = id ? await getDraft(id) : null;
  if (!draft) {
    draft = { id: newId(), text: "", created_at: Date.now(), reference_time: new Date().toISOString(), status: "new", tasks: null };
    await putDraft(draft);
  }
  $("source").value = draft.text || "";
  $("source-len").textContent = String((draft.text || "").length);
  $("truncated").hidden = !draft.truncated;
  $("source").addEventListener("input", (e) => {
    draft.text = e.target.value.slice(0, 20000);
    $("source-len").textContent = String(draft.text.length);
    persist();
  });
  $("reanalyse").addEventListener("click", extract); // keeps already-saved cards
  $("add-manual").addEventListener("click", addManual);
  $("save-all").addEventListener("click", saveAll);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); saveAll(); }
  });
  $("cancel").addEventListener("click", async () => {
    const unsaved = (draft.tasks || []).some((t) => t.ui === "editing" && t.include);
    if (draft.status !== "done" && unsaved && !$("cancel").dataset.confirm) {
      $("cancel").dataset.confirm = "1";
      $("cancel").textContent = "Discard unsaved tasks?";
      return;
    }
    await deleteDraft(draft.id);
    window.close();
  });

  if (Array.isArray(draft.tasks)) render(); // restored draft (e.g. after a failure)
  else extract();
}

init();
