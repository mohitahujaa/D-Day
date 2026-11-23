"""
Google Calendar integration for D-Day.

* OAuth 2.0 "installed app" flow with PKCE and a loopback redirect to the
  local D-Day service. Scope: calendar.events only (create events; no access
  to calendar settings or other Google data).
* Refresh tokens are stored with token_store (DPAPI on Windows).
* Events are created only from a user-confirmed task (the extension's Save
  button). Each review card carries an idempotency key that is turned into a
  deterministic Google event ID, so retries/double-clicks cannot create
  duplicates; a local ledger also warns before saving an identical event twice.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time as time_mod
import urllib.parse
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from token_store import TokenStore

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"  # optional: phone inbox via a "D-Day" Google Tasks list
SCOPES = [CALENDAR_SCOPE, TASKS_SCOPE]                 # requested
REQUIRED_SCOPES = [CALENDAR_SCOPE]                     # Calendar must be granted; Tasks may be declined
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

EVENT_TYPES = ("deadline", "exam", "meeting", "assignment", "reminder", "other")
AREAS = ("job", "work", "college", "personal")
# Google Calendar event colours: 11 Tomato, 9 Blueberry, 10 Basil, 5 Banana.
AREA_COLORS = {"job": "11", "work": "9", "college": "10", "personal": "5"}
DEADLINE_TYPES = ("deadline", "assignment")
MAX_REMINDER_MINUTES = 40320  # Google's limit (4 weeks)

DEFAULT_SETTINGS = {
    "timezone": "Asia/Kolkata",
    "untimed_deadline": "all_day",        # or "end_of_day"
    "deadline_mode": "ends_at_due",       # or "starts_at_due"
    "deadline_duration_minutes": 30,
    "event_duration_minutes": 60,
    "reminders_timed": [60, 1440],        # minutes before start
    "reminders_all_day": [900],           # 900 min before 00:00 = 09:00 the day before
    "calendar_id": "primary",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CalendarError(Exception):
    code = "calendar_error"
    http_status = 502

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.extra = extra


class NotConfigured(CalendarError):
    code = "google_not_configured"
    http_status = 503


class AuthRequired(CalendarError):
    code = "auth_required"
    http_status = 401


class RateLimited(CalendarError):
    code = "rate_limited"
    http_status = 429


class NetworkError(CalendarError):
    code = "network_error"
    http_status = 503


class DuplicateSuspected(CalendarError):
    code = "duplicate_suspected"
    http_status = 409


class InvalidTask(CalendarError):
    code = "invalid_task"
    http_status = 400


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def load_client_config(path: str) -> Dict[str, str]:
    if not path or not os.path.isfile(path):
        raise NotConfigured(
            "Google OAuth client is not configured. Download the OAuth client JSON "
            "(Desktop app) from Google Cloud Console and save it as client_secret.json "
            "(see README: Google Cloud setup)."
        )
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    section = data.get("installed") or data.get("web") or data
    if not section.get("client_id"):
        raise NotConfigured("client_secret.json has no client_id.")
    return {
        "client_id": section["client_id"],
        # For Desktop clients Google issues a secret that is not confidential,
        # but it is still read from the file, never hardcoded.
        "client_secret": section.get("client_secret", ""),
        "token_uri": section.get("token_uri", TOKEN_URI),
        "auth_uri": section.get("auth_uri", AUTH_URI),
    }


class GoogleAuth:
    PENDING_TTL = 600

    def __init__(self, client_secrets_path_fn: Callable[[], str], store: TokenStore,
                 session: Optional[requests.Session] = None, clock: Callable[[], float] = time_mod.time):
        self._secrets_path_fn = client_secrets_path_fn
        self.store = store
        self.http = session or requests.Session()
        self.clock = clock
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- status --------------------------------------------------------------
    def is_configured(self) -> bool:
        try:
            load_client_config(self._secrets_path_fn())
            return True
        except NotConfigured:
            return False

    def has_scope(self, scope: str) -> bool:
        tok = self.store.load() or {}
        return bool(tok.get("refresh_token")) and scope in (tok.get("scope") or "").split()

    def is_connected(self) -> bool:
        tok = self.store.load()
        return bool(tok and tok.get("refresh_token"))

    def client(self) -> Dict[str, str]:
        return load_client_config(self._secrets_path_fn())

    # -- authorization-code flow with PKCE ---------------------------------
    def authorization_url(self, redirect_uri: str) -> str:
        cfg = self.client()
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        now = self.clock()
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if v["expires"] > now}
            self._pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri,
                                    "expires": now + self.PENDING_TTL}
        params = {
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return cfg["auth_uri"] + "?" + urllib.parse.urlencode(params)

    def handle_callback(self, state: Optional[str], code: Optional[str], error: Optional[str] = None) -> None:
        if error:
            raise AuthRequired(f"Google sign-in was not completed ({error}).")
        with self._lock:
            pending = self._pending.pop(state or "", None)
        if not pending or pending["expires"] < self.clock():
            raise AuthRequired("Sign-in link expired or was not started by D-Day. Please try connecting again.")
        if not code:
            raise AuthRequired("Google did not return an authorization code.")
        cfg = self.client()
        data = {
            "code": code,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": pending["redirect_uri"],
            "grant_type": "authorization_code",
            "code_verifier": pending["verifier"],
        }
        tok = self._token_request(cfg["token_uri"], data)
        granted = set((tok.get("scope") or "").split())
        if granted and not set(REQUIRED_SCOPES) <= granted:
            raise AuthRequired("Calendar permission was not granted. Please allow access to Calendar events.")
        if not tok.get("refresh_token"):
            raise AuthRequired("Google did not return a refresh token. Remove D-Day from your Google "
                               "account permissions and connect again.")
        self._save(tok)

    def _token_request(self, uri: str, data: Dict[str, str]) -> Dict[str, Any]:
        try:
            resp = self.http.post(uri, data=data, timeout=20)
        except requests.exceptions.RequestException:
            raise NetworkError("Could not reach Google to complete sign-in. Check your internet connection.")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            err = body.get("error", "")
            if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
                raise AuthRequired(f"Google rejected the credentials ({err}). Please reconnect Google Calendar.",
                                   oauth_error=err)
            if resp.status_code >= 500:
                raise NetworkError("Google sign-in service is temporarily unavailable.")
            raise AuthRequired(f"Google token request failed ({err or resp.status_code}).", oauth_error=err)
        return body

    def _save(self, tok: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> None:
        stored = dict(previous or {})
        stored.update({
            "access_token": tok["access_token"],
            "expires_at": self.clock() + int(tok.get("expires_in", 3600)),
            "scope": tok.get("scope", stored.get("scope", " ".join(SCOPES))),
        })
        if tok.get("refresh_token"):
            stored["refresh_token"] = tok["refresh_token"]
        self.store.save(stored)

    # -- tokens ----------------------------------------------------------------
    def access_token(self, force_refresh: bool = False) -> str:
        self.client()  # raises NotConfigured with setup instructions
        tok = self.store.load()
        if not tok or not tok.get("refresh_token"):
            raise AuthRequired("Google Calendar is not connected.")
        if not force_refresh and tok.get("access_token") and tok.get("expires_at", 0) - 60 > self.clock():
            return tok["access_token"]
        cfg = self.client()
        try:
            new = self._token_request(cfg["token_uri"], {
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "refresh_token": tok["refresh_token"],
                "grant_type": "refresh_token",
            })
        except AuthRequired as exc:
            if exc.extra.get("oauth_error") == "invalid_grant":
                # Revoked, expired (7-day limit for apps in "Testing"), or password changed.
                self.store.clear()
                raise AuthRequired("Google access was revoked or has expired. Please reconnect Google Calendar.")
            raise
        self._save(new, previous=tok)
        return new["access_token"]

    def disconnect(self) -> None:
        tok = self.store.load()
        self.store.clear()
        if tok:
            try:
                self.http.post(REVOKE_URI, data={"token": tok.get("refresh_token") or tok.get("access_token")},
                               timeout=10)
            except requests.exceptions.RequestException:
                pass  # Local token is gone either way.


# ---------------------------------------------------------------------------
# Event payload construction (pure, no I/O)
# ---------------------------------------------------------------------------

def merge_settings(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for k, v in (settings or {}).items():
        if k in DEFAULT_SETTINGS and v is not None:
            out[k] = v
    return out


def _parse_reminders(value: Any) -> List[int]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s]+", value) if v]
    if not isinstance(value, (list, tuple)):
        raise InvalidTask("Reminders must be a list of minutes.")
    out = []
    for v in value:
        try:
            m = int(v)
        except (TypeError, ValueError):
            raise InvalidTask(f"Invalid reminder value: {v!r}")
        if not 0 <= m <= MAX_REMINDER_MINUTES:
            raise InvalidTask("Reminders must be between 0 and 40320 minutes (4 weeks).")
        if m not in out:
            out.append(m)
    if len(out) > 5:
        raise InvalidTask("Google Calendar allows at most 5 reminders.")
    return sorted(out, reverse=True)


def _parse_hhmm(value: Any, field_name: str) -> Optional[time]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", value):
        raise InvalidTask(f"{field_name} must be HH:MM.")
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise InvalidTask(f"{field_name} is not a valid time.")


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidTask("Text fields must be strings.")
    # Strip control characters except newlines/tabs.
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return value.strip()[:limit]


def build_event_payload(task: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate a reviewed task and build the Calendar API event body."""
    if not isinstance(task, dict):
        raise InvalidTask("Task must be an object.")
    s = merge_settings(settings)

    title = _clean_text(task.get("title"), 300)
    if not title:
        raise InvalidTask("A title is required.")
    event_type = task.get("event_type") or "other"
    if event_type not in EVENT_TYPES:
        raise InvalidTask("Unknown event type.")
    area = task.get("area")
    if area is not None and area not in AREAS:
        raise InvalidTask("Unknown category.")

    tz_name = task.get("timezone") or s["timezone"]
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        raise InvalidTask(f"Unknown timezone: {tz_name}")

    due_date_s = task.get("due_date")
    if not isinstance(due_date_s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_date_s):
        raise InvalidTask("Please choose a date (YYYY-MM-DD) before saving.")
    try:
        due_date = date.fromisoformat(due_date_s)
    except ValueError:
        raise InvalidTask("The date is not valid.")

    start_t = _parse_hhmm(task.get("due_time"), "Time")
    end_t = _parse_hhmm(task.get("end_time"), "End time")
    all_day = bool(task.get("all_day")) or start_t is None

    description = _clean_text(task.get("description"), 6000)
    parts = [description] if description else []
    if task.get("include_source") and task.get("source_text"):
        parts.append("Source excerpt:\n" + _clean_text(task.get("source_text"), 1500))

    event: Dict[str, Any] = {"summary": title}
    location = _clean_text(task.get("location"), 500)
    if location:
        event["location"] = location

    if all_day:
        event["start"] = {"date": due_date.isoformat()}
        event["end"] = {"date": (due_date + timedelta(days=1)).isoformat()}
        event["transparency"] = "transparent"
        reminders = _parse_reminders(task.get("reminders", s["reminders_all_day"]))
        when_label = due_date.strftime("%a %d %b %Y") + " (all day)"
    else:
        due_dt = datetime.combine(due_date, start_t, tzinfo=tz)
        if end_t is not None:
            start_dt = due_dt
            end_dt = datetime.combine(due_date, end_t, tzinfo=tz)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
        elif event_type in DEADLINE_TYPES or task.get("item_type") == "DEADLINE":
            dur = timedelta(minutes=int(task.get("duration_minutes") or s["deadline_duration_minutes"]))
            if s["deadline_mode"] == "starts_at_due":
                start_dt, end_dt = due_dt, due_dt + dur
            else:
                start_dt, end_dt = due_dt - dur, due_dt
            parts.insert(0, f"Deadline: {due_dt.strftime('%a %d %b %Y, %H:%M')} ({tz_name})")
        else:
            dur = timedelta(minutes=int(task.get("duration_minutes") or s["event_duration_minutes"]))
            start_dt, end_dt = due_dt, due_dt + dur
        if not timedelta(0) < end_dt - start_dt <= timedelta(days=14):
            raise InvalidTask("Event duration must be between 1 minute and 14 days.")
        event["start"] = {"dateTime": start_dt.isoformat(), "timeZone": tz_name}
        event["end"] = {"dateTime": end_dt.isoformat(), "timeZone": tz_name}
        reminders = _parse_reminders(task.get("reminders", s["reminders_timed"]))
        when_label = f"{start_dt.strftime('%a %d %b %Y, %H:%M')}–{end_dt.strftime('%H:%M')} ({tz_name})"

    parts.append("Added with D-Day")
    event["description"] = "\n\n".join(parts)
    event["reminders"] = {"useDefault": False,
                          "overrides": [{"method": "popup", "minutes": m} for m in reminders]}
    event["extendedProperties"] = {"private": {"dday": "1", "ddayType": event_type}}
    if area:
        event["colorId"] = AREA_COLORS[area]
        event["extendedProperties"]["private"]["ddayArea"] = area
    return {"event": event, "when": when_label, "timezone": tz_name, "all_day": all_day}


def event_id_for(idempotency_key: str, attempt: int = 0) -> str:
    """Deterministic Calendar event id (base32hex: 0-9a-v, length 5-1024)."""
    digest = hashlib.sha256(f"dday:{idempotency_key}:{attempt}".encode()).digest()[:20]
    return base64.b32hexencode(digest).decode().lower().rstrip("=")


def fingerprint(calendar_id: str, event: Dict[str, Any]) -> str:
    key = json.dumps([calendar_id, event["summary"].strip().lower(), event["start"], event["end"]], sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Local ledger of created events (duplicate prevention)
# ---------------------------------------------------------------------------

class EventLedger:
    MAX_ENTRIES = 1000

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("by_key"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"by_key": {}, "by_fingerprint": {}}

    def get_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read()["by_key"].get(key)

    def get_by_fingerprint(self, fp: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._read()
            k = data["by_fingerprint"].get(fp)
            return data["by_key"].get(k) if k else None

    def record(self, key: str, fp: str, entry: Dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            data["by_key"][key] = entry
            data["by_fingerprint"][fp] = key
            if len(data["by_key"]) > self.MAX_ENTRIES:
                oldest = sorted(data["by_key"].items(), key=lambda kv: kv[1].get("created_at", 0))
                for k, _ in oldest[: len(data["by_key"]) - self.MAX_ENTRIES]:
                    data["by_key"].pop(k, None)
                data["by_fingerprint"] = {f: k for f, k in data["by_fingerprint"].items() if k in data["by_key"]}
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Calendar client
# ---------------------------------------------------------------------------

class CalendarClient:
    MAX_ATTEMPTS = 4

    def __init__(self, auth: GoogleAuth, ledger: EventLedger,
                 session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time_mod.sleep):
        self.auth = auth
        self.ledger = ledger
        self.http = session or requests.Session()
        self.sleep = sleep
        self._key_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _key_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            if len(self._key_locks) > 200:
                self._key_locks.clear()
            return self._key_locks.setdefault(key, threading.Lock())

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Authorized request with refresh-on-401 and backoff on 429/5xx/network errors."""
        refreshed = False      # a 401 already triggered one token refresh
        force_refresh = False  # refresh before the next attempt
        delay = 1.0
        for attempt in range(self.MAX_ATTEMPTS):
            token = self.auth.access_token(force_refresh=force_refresh)
            force_refresh = False
            try:
                resp = self.http.request(method, url, headers={"Authorization": f"Bearer {token}"},
                                         timeout=20, **kwargs)
            except requests.exceptions.RequestException:
                if attempt < self.MAX_ATTEMPTS - 1:
                    self.sleep(delay)
                    delay *= 2
                    continue
                raise NetworkError("Could not reach Google Calendar. Check your internet connection; "
                                   "your task is kept and you can retry.")
            if resp.status_code == 401:
                if refreshed:
                    raise AuthRequired("Google rejected the access token. Please reconnect Google Calendar.")
                refreshed = force_refresh = True
                continue
            if resp.status_code == 429 or (resp.status_code == 403 and _is_rate_limit(resp)):
                if attempt < self.MAX_ATTEMPTS - 1:
                    self.sleep(_retry_after(resp, delay))
                    delay *= 2
                    continue
                raise RateLimited("Google Calendar is rate-limiting requests. Wait a minute and retry.")
            if resp.status_code >= 500:
                if attempt < self.MAX_ATTEMPTS - 1:
                    self.sleep(delay)
                    delay *= 2
                    continue
                raise NetworkError("Google Calendar is temporarily unavailable. Your task is kept; retry later.")
            if resp.status_code == 403:
                raise AuthRequired("D-Day does not have permission to create events. Reconnect and allow "
                                   "Calendar event access.")
            return resp
        raise NetworkError("Google Calendar request failed after retries.")

    def create_event(self, task: Dict[str, Any], settings: Optional[Dict[str, Any]],
                     idempotency_key: str, allow_duplicate: bool = False) -> Dict[str, Any]:
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key):
            raise InvalidTask("Missing or invalid idempotency key.")
        built = build_event_payload(task, settings)
        event = built["event"]
        calendar_id = merge_settings(settings)["calendar_id"] or "primary"
        fp = fingerprint(calendar_id, event)

        with self._key_lock(idempotency_key):
            existing = self.ledger.get_by_key(idempotency_key)
            if existing:
                return {**existing, "status": "already_created"}
            if not allow_duplicate:
                same = self.ledger.get_by_fingerprint(fp)
                if same:
                    raise DuplicateSuspected("An identical event was already saved with D-Day.",
                                             html_link=same.get("html_link"), event_id=same.get("event_id"))

            cal = urllib.parse.quote(calendar_id, safe="")
            for attempt in range(3):
                body = dict(event, id=event_id_for(idempotency_key, attempt))
                resp = self._request("POST", f"{CALENDAR_API}/calendars/{cal}/events", json=body)
                if resp.status_code in (200, 201):
                    created = resp.json()
                    status = "created"
                    break
                if resp.status_code == 409:
                    # Same deterministic id already exists: an earlier attempt
                    # succeeded (e.g. response lost). Return that event.
                    got = self._request("GET", f"{CALENDAR_API}/calendars/{cal}/events/{body['id']}")
                    if got.status_code == 200 and got.json().get("status") != "cancelled":
                        created = got.json()
                        status = "already_created"
                        break
                    continue  # it was deleted by the user; use a fresh id
                if resp.status_code == 404:
                    raise CalendarError(f"Calendar '{calendar_id}' was not found.")
                if resp.status_code == 400:
                    msg = _error_message(resp)
                    raise InvalidTask(f"Google rejected the event: {msg}")
                raise CalendarError(f"Unexpected response from Google Calendar (HTTP {resp.status_code}).")
            else:
                raise CalendarError("Could not create a unique event id.")

            entry = {
                "event_id": created.get("id"),
                "html_link": created.get("htmlLink"),
                "when": built["when"],
                "summary": event["summary"],
                "created_at": time_mod.time(),
            }
            self.ledger.record(idempotency_key, fp, entry)
            return {**entry, "status": status}


    def list_upcoming(self, calendar_id: str = "primary", max_results: int = 25,
                      now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Upcoming events created by D-Day (tagged dday=1), read live from Google."""
        now = now or datetime.now().astimezone()
        cal = urllib.parse.quote(calendar_id or "primary", safe="")
        params = {
            "privateExtendedProperty": "dday=1",
            "timeMin": (now - timedelta(days=1)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max(1, min(int(max_results), 100))),
        }
        resp = self._request("GET", f"{CALENDAR_API}/calendars/{cal}/events", params=params)
        if resp.status_code == 404:
            raise CalendarError(f"Calendar '{calendar_id}' was not found.")
        if resp.status_code != 200:
            raise CalendarError(f"Unexpected response from Google Calendar (HTTP {resp.status_code}).")
        out = []
        for ev in resp.json().get("items", []):
            if ev.get("status") == "cancelled":
                continue
            start, end = ev.get("start", {}), ev.get("end", {})
            all_day = "date" in start
            out.append({
                "event_id": ev.get("id"),
                "title": ev.get("summary") or "(untitled)",
                "start": start.get("date") or start.get("dateTime"),
                "end": end.get("date") or end.get("dateTime"),
                "all_day": all_day,
                "location": ev.get("location"),
                "event_type": ((ev.get("extendedProperties") or {}).get("private") or {}).get("ddayType"),
                "area": ((ev.get("extendedProperties") or {}).get("private") or {}).get("ddayArea"),
                "html_link": ev.get("htmlLink"),
            })
        return out


def _error_body(resp: requests.Response) -> Dict[str, Any]:
    try:
        body = resp.json()
        return body.get("error", {}) if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
    except ValueError:
        return {}


def _is_rate_limit(resp: requests.Response) -> bool:
    reasons = {e.get("reason") for e in _error_body(resp).get("errors", []) if isinstance(e, dict)}
    return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"})


def _retry_after(resp: requests.Response, default: float) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", default)), 30.0)
    except (TypeError, ValueError):
        return default


def _error_message(resp: requests.Response) -> str:
    return str(_error_body(resp).get("message") or f"HTTP {resp.status_code}")[:200]
