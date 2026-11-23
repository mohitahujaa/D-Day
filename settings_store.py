"""Server-side preferences used by the dashboard and the Telegram inbox (JSON in the data folder)."""

import json
import os
import re
import threading
from typing import Any, Dict
from zoneinfo import ZoneInfo

DEFAULTS: Dict[str, Any] = {
    "timezone": "Asia/Kolkata",
    "user_name": "",
    "untimed_deadline": "all_day",   # or "end_of_day"
    "auto_resolve": True,
    "share_client_id": "",                                    # Web OAuth client used by the phone share app
    "share_app_url": "https://mohitahujaa.github.io/dday-share/",
}


class SettingsStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
        return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get()
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                continue
            if k == "timezone":
                try:
                    ZoneInfo(str(v))
                except Exception:
                    raise ValueError(f"Unknown timezone: {v}")
            elif k == "untimed_deadline" and v not in ("all_day", "end_of_day"):
                raise ValueError("untimed_deadline must be all_day or end_of_day.")
            elif k == "auto_resolve":
                v = bool(v)
            elif k == "user_name":
                v = str(v or "").strip()[:60]
            elif k == "share_client_id":
                v = str(v or "").strip()
                if v and not re.fullmatch(r"\d+-[a-z0-9]+\.apps\.googleusercontent\.com", v):
                    raise ValueError("That doesn't look like a Google OAuth client ID (it ends with .apps.googleusercontent.com).")
            elif k == "share_app_url":
                v = str(v or "").strip()
                if not re.fullmatch(r"https://[\w.-]+(/[\w./-]*)?", v):
                    raise ValueError("The share app address must start with https://")
                if not v.endswith("/"):
                    v += "/"
            current[k] = v
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2)
            os.replace(tmp, self.path)
        return current
