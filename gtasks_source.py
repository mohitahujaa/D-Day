"""
Phone capture through Google Tasks.

On the phone, add or share a message into a Google Tasks list called "D-Day"
(the title and/or details of a task). Every minute the PC imports new entries
into the local inbox, where the local model reads them, and marks them
completed so the phone shows they were picked up.

Uses the already connected Google account (scope: auth/tasks). Message text
goes through Google, which already hosts the user's calendar.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

import calendar_client

log = logging.getLogger("dday.gtasks")
API = "https://tasks.googleapis.com/tasks/v1"
LIST_NAME = "D-Day"
POLL_SECONDS = 60


class TasksError(Exception):
    def __init__(self, message: str, code: str = "tasks_error"):
        super().__init__(message)
        self.code = code


class GoogleTasksSource:
    def __init__(self, auth: calendar_client.GoogleAuth, inbox, worker, state_path: str,
                 session: Optional[requests.Session] = None, sleep=time.sleep):
        self.auth = auth
        self.inbox = inbox
        self.worker = worker
        self.state_path = state_path
        self.http = session or requests.Session()
        self.sleep = sleep
        self.last_error: Optional[Dict[str, str]] = None
        self.last_check: Optional[str] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ state
    def _state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, self.state_path)

    def status(self) -> Dict[str, Any]:
        state = self._state()
        connected = self.auth.is_connected()
        granted = self.auth.has_scope(calendar_client.TASKS_SCOPE)
        return {
            "enabled": state.get("enabled", True),
            "google_connected": connected,
            "permission": granted,
            "list_name": LIST_NAME,
            "list_ready": bool(state.get("list_id")),
            "imported": len(state.get("imported", [])),
            "last_check": self.last_check,
            "error": self.last_error,
            "running": bool(self._thread and self._thread.is_alive()),
        }

    def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            state = self._state()
            state["enabled"] = bool(enabled)
            self._save_state(state)
        if enabled:
            self.check_now()
        return self.status()

    # ------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, **kwargs) -> Any:
        refreshed = False
        for attempt in range(3):
            token = self.auth.access_token(force_refresh=refreshed)
            try:
                resp = self.http.request(method, API + path, headers={"Authorization": f"Bearer {token}"},
                                         timeout=20, **kwargs)
            except requests.exceptions.RequestException:
                if attempt < 2:
                    self.sleep(2 * (attempt + 1))
                    continue
                raise TasksError("Can't reach Google (no internet?).", "network_error")
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                continue
            if resp.status_code in (429, 500, 502, 503) and attempt < 2:
                self.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise self._error(resp)
            return resp.json() if resp.content else {}
        raise TasksError("Google Tasks didn't respond.", "network_error")

    @staticmethod
    def _error(resp) -> TasksError:
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        text = json.dumps(err).lower()
        if resp.status_code == 403 and ("accessnotconfigured" in text or "service_disabled" in text
                                        or "has not been used" in text or "is disabled" in text):
            return TasksError("The Google Tasks API is turned off in your Google Cloud project. Enable it, then "
                              "wait a minute.", "api_disabled")
        if resp.status_code in (401, 403):
            return TasksError("D-Day doesn't have permission to read Google Tasks. Reconnect Google and allow "
                              "access to Tasks.", "permission_missing")
        return TasksError(f"Google Tasks error (HTTP {resp.status_code}): {str(err.get('message', ''))[:150]}")

    # ------------------------------------------------------------ list + import
    def ensure_list(self) -> str:
        state = self._state()
        if state.get("list_id"):
            return state["list_id"]
        page = None
        while True:
            data = self._request("GET", "/users/@me/lists", params={"maxResults": 100, **({"pageToken": page} if page else {})})
            for tl in data.get("items", []):
                if (tl.get("title") or "").strip().lower() == LIST_NAME.lower():
                    state["list_id"] = tl["id"]
                    self._save_state(state)
                    return tl["id"]
            page = data.get("nextPageToken")
            if not page:
                break
        created = self._request("POST", "/users/@me/lists", json={"title": LIST_NAME})
        state["list_id"] = created["id"]
        self._save_state(state)
        log.info("Created Google Tasks list %r", LIST_NAME)
        return created["id"]

    def poll_once(self) -> int:
        """Import new tasks from the D-Day list. Returns how many were imported."""
        if not self.auth.is_connected():
            raise TasksError("Google isn't connected.", "not_connected")
        if not self.auth.has_scope(calendar_client.TASKS_SCOPE):
            raise TasksError("Reconnect Google and allow access to Google Tasks to use the phone inbox.",
                             "permission_missing")
        try:
            list_id = self.ensure_list()
            data = self._request("GET", f"/lists/{list_id}/tasks",
                                 params={"showCompleted": "false", "showHidden": "false", "maxResults": 100})
        except TasksError as exc:
            if "404" in str(exc):  # the list was deleted on the phone: make a new one next time
                state = self._state()
                state.pop("list_id", None)
                self._save_state(state)
            raise
        state = self._state()
        imported = set(state.get("imported", []))
        count = 0
        for task in data.get("items", []):
            tid = task.get("id")
            title = (task.get("title") or "").strip()
            notes = (task.get("notes") or "").strip()
            if notes and title.rstrip("…").strip() and notes.startswith(title.rstrip("…").strip()):
                text = notes  # title is just the start of the details (D-Day share app)
            else:
                text = "\n".join(p for p in (title, notes) if p)
            if not tid or tid in imported or not text or task.get("status") == "completed":
                continue
            ref = task.get("updated") or datetime.now(timezone.utc).isoformat()
            item_id = self.inbox.add("gtasks", text, reference_time=ref.replace("Z", "+00:00"),
                                     meta={"task_id": tid, "list_id": list_id})
            self.worker.enqueue(item_id)
            imported.add(tid)
            count += 1
            # Remember it before touching Google, so a failure below can't import it twice.
            state["imported"] = sorted(imported)[-2000:]
            self._save_state(state)
            try:
                self._request("PATCH", f"/lists/{list_id}/tasks/{tid}", json={"status": "completed"})
            except TasksError as exc:
                log.warning("Couldn't tick off task: %s", exc.code)
        return count

    # ------------------------------------------------------------ background loop
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dday-gtasks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def check_now(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            wait = POLL_SECONDS
            if self._state().get("enabled", True) and self.auth.is_connected():
                try:
                    n = self.poll_once()
                    self.last_error = None
                    if n:
                        log.info("Imported %d message(s) from Google Tasks", n)
                except TasksError as exc:
                    self.last_error = {"code": exc.code, "message": str(exc)}
                    wait = 300 if exc.code in ("api_disabled", "permission_missing") else POLL_SECONDS
                except calendar_client.CalendarError as exc:  # auth problems (revoked, not configured)
                    self.last_error = {"code": exc.code, "message": str(exc)}
                    wait = 300
                except Exception:
                    log.exception("Google Tasks check failed")
                    self.last_error = {"code": "internal_error", "message": "Unexpected error; see service.log."}
                self.last_check = datetime.now(timezone.utc).isoformat()
            self._wake.wait(wait)
            self._wake.clear()
