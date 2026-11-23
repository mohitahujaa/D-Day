// D-Day service worker: context menu + keyboard shortcut -> review window.
import { cleanSelection, newId, putDraft, pruneDrafts } from "./common.js";

const MENU_ID = "dday-add";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "Add to D-Day",
      contexts: ["selection"],
    });
  });
  pruneDrafts();
});

chrome.runtime.onStartup.addListener(() => pruneDrafts());

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU_ID) return;
  // Prefer the live selection (keeps line breaks); fall back to the menu's
  // selectionText, which works even where scripts can't run (PDF viewer, etc.).
  let text = await readSelection(tab, info.frameId);
  if (!text || !text.trim()) text = info.selectionText || "";
  await openReview(text);
});

chrome.commands.onCommand.addListener(async (command, tab) => {
  if (command !== "add-selection") return;
  const text = await readSelection(tab, undefined);
  await openReview(text || "");
});

/** Reads only the user's current selection; never the rest of the page. */
async function readSelection(tab, frameId) {
  if (!tab || tab.id === undefined || tab.id < 0) return "";
  try {
    const target = { tabId: tab.id };
    if (typeof frameId === "number") target.frameIds = [frameId];
    else target.allFrames = true;
    const results = await chrome.scripting.executeScript({
      target,
      func: () => {
        const sel = window.getSelection ? window.getSelection().toString() : "";
        if (sel) return sel;
        const a = document.activeElement;
        if (a && (a.tagName === "TEXTAREA" || (a.tagName === "INPUT" && a.type === "text"))) {
          return a.value.substring(a.selectionStart || 0, a.selectionEnd || 0);
        }
        return "";
      },
    });
    const found = (results || []).map((r) => r && r.result).find((t) => typeof t === "string" && t.trim());
    return found || "";
  } catch {
    // Restricted page (chrome://, Web Store) or missing permission: use fallback.
    return "";
  }
}

async function openReview(rawText) {
  const { text, truncated } = cleanSelection(rawText);
  const draft = {
    id: newId(),
    text,
    truncated,
    created_at: Date.now(),
    reference_time: new Date().toISOString(),
    status: "new", // new | extracted | done
    tasks: null,
  };
  await putDraft(draft);
  const url = chrome.runtime.getURL(`review.html?draft=${draft.id}`);
  try {
    await chrome.windows.create({ url, type: "popup", width: 460, height: 760, focused: true });
  } catch {
    await chrome.tabs.create({ url });
  }
}
