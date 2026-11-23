"""
Inbox: messages waiting to become calendar events.

Messages arrive from the phone (Telegram bot), from the dashboard's paste box,
and later from anything else. Each is stored locally (SQLite in the D-Day data
folder), processed by the local model one at a time, and kept until the user
saves or dismisses its tasks. Nothing here talks to Google except save_item(),
which is only called when the user presses Save (on the PC or in Telegram).
"""

import json
import logging
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

import calendar_client
import extractor

log = logging.getLogger("dday.inbox")

STATUSES = ("queued", "processing", "ready", "saved", "dismissed", "failed")
OPEN_STATUSES = ("queued", "processing", "ready", "failed")
EDITABLE_FIELDS = {"title", "description", "due_date", "due_time", "end_time", "all_day", "area",
                   "event_type", "location", "reminders", "selected"}
RETENTION_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Inbox:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    text TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    reference_time TEXT,
                    status TEXT NOT NULL,
                    tasks_json TEXT,
                    warnings_json TEXT,
                    error TEXT,
                    meta_json TEXT,
                    updated_at TEXT NOT NULL
                )""")
            self._db.commit()

    # -------------------------------------------------------------- basics
    def _row(self, row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        d = dict(row)
        d["tasks"] = json.loads(d.pop("tasks_json") or "[]")
        d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        return d

    def add(self, source: str, text: str, reference_time: Optional[str] = None,
            meta: Optional[Dict[str, Any]] = None) -> int:
        text = (text or "").strip()
        if not text:
            raise ValueError("Empty message.")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO items (source, text, received_at, reference_time, status, meta_json, updated_at)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (source, text[:20000], _now(), reference_time, json.dumps(meta or {}), _now()))
            self._db.commit()
            return int(cur.lastrowid)

    def get(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())

    def list(self, statuses: Optional[Iterable[str]] = None, limit: int = 50) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM items", []
        if statuses:
            statuses = list(statuses)
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args += statuses
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [self._row(r) for r in self._db.execute(q, args).fetchall()]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def update(self, item_id: int, **fields) -> None:
        cols, args = [], []
        for k, v in fields.items():
            if k == "tasks":
                k, v = "tasks_json", json.dumps(v)
            elif k == "warnings":
                k, v = "warnings_json", json.dumps(v)
            elif k == "meta":
                k, v = "meta_json", json.dumps(v)
            elif k not in ("status", "error", "reference_time"):
                raise ValueError(f"Unknown field {k}")
            cols.append(f"{k} = ?")
            args.append(v)
        cols.append("updated_at = ?")
        args += [_now(), item_id]
        with self._lock:
            self._db.execute(f"UPDATE items SET {', '.join(cols)} WHERE id = ?", args)
            self._db.commit()

    def update_meta(self, item_id: int, **patch) -> None:
        with self._lock:
            item = self.get(item_id)
            if item:
                self.update(item_id, meta={**item["meta"], **patch})

    def edit_task(self, item_id: int, task_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a user edit to one task (validated field by field)."""
        clean = {}
        for k, v in (patch or {}).items():
            if k not in EDITABLE_FIELDS:
                continue
            if k in ("all_day", "selected"):
                v = bool(v)
            elif k == "reminders":
                if not isinstance(v, list) or len(v) > 5 or not all(isinstance(x, int) and 0 <= x <= 40320 for x in v):
                    raise ValueError("Reminders must be up to 5 numbers between 0 and 40320.")
            elif k == "due_date" and v is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
                raise ValueError("Date must be YYYY-MM-DD.")
            elif k in ("due_time", "end_time") and v not in (None, "") and not re.fullmatch(r"\d{2}:\d{2}", str(v)):
                raise ValueError("Time must be HH:MM.")
            elif k == "area" and v not in calendar_client.AREAS:
                raise ValueError("Unknown category.")
            elif k == "event_type" and v not in calendar_client.EVENT_TYPES:
                raise ValueError("Unknown type.")
            elif k in ("title", "description", "location"):
                v = str(v or "")[:6000]
            clean[k] = v or None if k in ("due_time", "end_time", "location") else v
        with self._lock:
            item = self.get(item_id)
            if not item:
                raise KeyError("No such inbox item.")
            for t in item["tasks"]:
                if t["id"] == task_id:
                    t.update(clean)
                    if clean.get("due_time"):
                        t["all_day"] = False if "all_day" not in clean else t["all_day"]
                    if t.get("all_day"):
                        t["due_time"] = t["end_time"] = None
                    self.update(item_id, tasks=item["tasks"])
                    return t
        raise KeyError("No such task.")

    def purge_old(self, days: int = RETENTION_DAYS) -> int:
        """Delete finished items (saved/dismissed) older than `days`."""
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        n = 0
        with self._lock:
            for row in self._db.execute("SELECT id, updated_at FROM items WHERE status IN ('saved','dismissed')").fetchall():
                try:
                    if datetime.fromisoformat(row["updated_at"]).timestamp() < cutoff:
                        self._db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
                        n += 1
                except ValueError:
                    pass
            self._db.commit()
        return n


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------

class InboxWorker:
    """Runs queued messages through the local model, one at a time."""

    RETRY_SECONDS = 30

    def __init__(self, inbox: Inbox, extract_fn: Callable[[str, Optional[str]], Dict[str, Any]]):
        self.inbox = inbox
        self.extract_fn = extract_fn
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._q: "queue.Queue[int]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def enqueue(self, item_id: int) -> None:
        self._q.put(item_id)

    def start(self) -> None:
        for item in self.inbox.list(("queued", "processing"), limit=200):
            self.inbox.update(item["id"], status="queued")
            self.enqueue(item["id"])
        self._thread = threading.Thread(target=self._run, name="dday-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item_id = self._q.get(timeout=1)
            except queue.Empty:
                continue
            if not self.process(item_id):
                # Ollama not available yet: try again later without blocking others forever.
                threading.Timer(self.RETRY_SECONDS, self.enqueue, args=(item_id,)).start()

    def process(self, item_id: int) -> bool:
        """Extract one item. Returns False if it should be retried later."""
        item = self.inbox.get(item_id)
        if not item or item["status"] not in ("queued", "failed"):
            return True
        self.inbox.update(item_id, status="processing", error=None)
        try:
            result = self.extract_fn(item["text"], item.get("reference_time"))
        except (extractor.OllamaUnavailable, extractor.OllamaTimeout) as exc:
            self.inbox.update(item_id, status="queued", error=str(exc))
            return False
        except Exception as exc:  # model output, empty text, etc.
            log.warning("Inbox item %s failed: %s", item_id, type(exc).__name__)
            self.inbox.update(item_id, status="failed", error=str(exc)[:500])
            self._notify(item_id)
            return True
        tasks = result.get("tasks", [])
        for t in tasks:
            t.setdefault("ui", "pending")
        self.inbox.update(item_id, status="ready", tasks=tasks, warnings=result.get("warnings", []), error=None)
        self._notify(item_id)
        return True

    def _notify(self, item_id: int) -> None:
        item = self.inbox.get(item_id)
        for fn in list(self.listeners):
            try:
                fn(item)
            except Exception:
                log.exception("Inbox listener failed")


# ---------------------------------------------------------------------------
# Saving (only on explicit user confirmation)
# ---------------------------------------------------------------------------

def save_item(inbox: Inbox, calendar, item_id: int, cal_settings: Dict[str, Any],
              task_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create Google Calendar events for the selected (or given) tasks of one inbox item."""
    item = inbox.get(item_id)
    if not item:
        raise KeyError("No such inbox item.")
    results = []
    stop_error = None
    for t in item["tasks"]:
        wanted = (t["id"] in task_ids) if task_ids is not None else bool(t.get("selected"))
        if not wanted or t.get("ui") == "saved":
            continue
        if stop_error:
            break
        key = f"inbox{item_id}-{t['id'][:16]}"
        try:
            res = calendar.create_event(t, cal_settings, key)
            t["ui"], t["result"], t["error"] = "saved", res, None
            results.append({"task_id": t["id"], "ok": True, **res})
        except calendar_client.DuplicateSuspected as exc:
            t["ui"], t["result"], t["error"] = "saved", {"status": "duplicate", **exc.extra}, None
            results.append({"task_id": t["id"], "ok": True, "status": "duplicate", **exc.extra})
        except calendar_client.CalendarError as exc:
            t["error"] = {"code": exc.code, "message": str(exc)}
            results.append({"task_id": t["id"], "ok": False, "code": exc.code, "message": str(exc)})
            if exc.code in ("auth_required", "google_not_configured", "network_error", "rate_limited"):
                stop_error = exc  # the rest would fail the same way
    remaining = [t for t in item["tasks"] if t.get("selected") and t.get("ui") != "saved"]
    status = "saved" if results and not remaining and all(r["ok"] for r in results) else item["status"]
    inbox.update(item_id, tasks=item["tasks"], status=status)
    return {"results": results, "status": status, "item": inbox.get(item_id)}
