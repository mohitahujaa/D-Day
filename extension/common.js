// Shared helpers for the D-Day extension pages and service worker.

export const DEFAULT_SETTINGS = Object.freeze({
  port: 8765,
  timezone: "Asia/Kolkata",
  untimed_deadline: "all_day",       // "all_day" | "end_of_day"
  deadline_mode: "ends_at_due",      // "ends_at_due" | "starts_at_due"
  deadline_duration_minutes: 30,
  event_duration_minutes: 60,
  reminders_timed: [60, 1440],
  reminders_all_day: [900],
  include_source: false,
  calendar_id: "primary",
  user_name: "",
  auto_resolve: true,
});

export const MAX_SELECTION_CHARS = 20000;
export const DRAFT_TTL_MS = 14 * 24 * 3600 * 1000;

export async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(settings || {}) };
}

export async function saveSettings(patch) {
  const current = await getSettings();
  const next = { ...current, ...patch };
  await chrome.storage.local.set({ settings: next });
  return next;
}

export function serviceUrl(settings, path) {
  const port = Number(settings.port) || DEFAULT_SETTINGS.port;
  return `http://127.0.0.1:${port}${path}`;
}

/** Error with a machine-readable code from the local service. */
export class ApiError extends Error {
  constructor(code, message, status, extra = {}) {
    super(message);
    this.code = code;
    this.status = status;
    this.extra = extra;
  }
}

export const NATIVE_HOST = "com.dday.host";

/** Ask the native helper to "start", "stop" or report "status" of the local service. */
export function nativeHost(cmd) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd }, (resp) => {
        const err = chrome.runtime.lastError;
        if (err || !resp) {
          reject(new ApiError("host_missing",
            "The D-Day on/off helper isn't installed. Run: python native_host/install.py", 0));
        } else {
          resolve(resp);
        }
      });
    } catch (e) {
      reject(new ApiError("host_missing", String(e), 0));
    }
  });
}

/**
 * Call the local service. With autoStart, if D-Day is off it is switched on
 * through the native helper and the call is retried once.
 */
export async function api(path, opts = {}) {
  try {
    return await apiOnce(path, opts);
  } catch (err) {
    if (!(opts.autoStart && err.code === "service_unreachable")) throw err;
    if (opts.onStarting) opts.onStarting();
    let started;
    try {
      started = await nativeHost("start");
    } catch {
      throw err; // helper not installed: keep the original, clearer message
    }
    if (!started.service) {
      throw new ApiError("service_unreachable", started.message || err.message, 0);
    }
    return apiOnce(path, opts);
  }
}

async function apiOnce(path, { method = "GET", body, timeoutMs = 15000 } = {}) {
  const settings = await getSettings();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let resp;
  try {
    resp = await fetch(serviceUrl(settings, path), {
      method,
      headers: { "Content-Type": "application/json", "X-DDay-Client": "extension" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
      credentials: "omit",
      cache: "no-store",
    });
  } catch (err) {
    if (err.name === "AbortError") {
      throw new ApiError("timeout", "The D-Day service took too long to respond.", 0);
    }
    throw new ApiError(
      "service_unreachable",
      "D-Day is switched off. Turn it on with the switch in the D-Day toolbar popup (or run start-dday.bat).",
      0,
    );
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await resp.json(); } catch { /* non-JSON */ }
  if (!resp.ok || !data || data.ok === false) {
    const e = (data && data.error) || {};
    throw new ApiError(e.code || `http_${resp.status}`, e.message || `Service error (HTTP ${resp.status}).`, resp.status, e);
  }
  return { data, status: resp.status };
}

// ---- drafts: the selected text and the user's edits survive failures ----

export async function getDrafts() {
  const { drafts } = await chrome.storage.local.get("drafts");
  return drafts || {};
}

export async function getDraft(id) {
  return (await getDrafts())[id] || null;
}

export async function putDraft(draft) {
  const drafts = await getDrafts();
  drafts[draft.id] = { ...draft, updated_at: Date.now() };
  await chrome.storage.local.set({ drafts });
}

export async function deleteDraft(id) {
  const drafts = await getDrafts();
  delete drafts[id];
  await chrome.storage.local.set({ drafts });
}

export async function pruneDrafts() {
  const drafts = await getDrafts();
  const now = Date.now();
  let changed = false;
  for (const [id, d] of Object.entries(drafts)) {
    if (now - (d.updated_at || d.created_at || 0) > DRAFT_TTL_MS) {
      delete drafts[id];
      changed = true;
    }
  }
  if (changed) await chrome.storage.local.set({ drafts });
}

export function newId() {
  const a = new Uint8Array(16);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function cleanSelection(text) {
  if (typeof text !== "string") return { text: "", truncated: false };
  // Normalise whitespace but keep line breaks (they help the model split items).
  let t = text.replace(/\r\n?/g, "\n").replace(/[ \t ]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  const truncated = t.length > MAX_SELECTION_CHARS;
  if (truncated) t = t.slice(0, MAX_SELECTION_CHARS);
  return { text: t, truncated };
}

/** Safe DOM builder: text is always assigned via textContent, never innerHTML. */
export function el(tag, attrs = {}, ...children) {
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

/** Only allow links that point at Google Calendar. */
export function safeCalendarLink(url) {
  try {
    const u = new URL(url);
    if (u.protocol === "https:" && /(^|\.)google\.com$/.test(u.hostname)) return u.href;
  } catch { /* invalid */ }
  return null;
}

export function parseReminderList(text) {
  const parts = String(text || "").split(/[\s,]+/).filter(Boolean);
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => !Number.isInteger(n) || n < 0 || n > 40320)) return null;
  return [...new Set(nums)].slice(0, 5);
}
