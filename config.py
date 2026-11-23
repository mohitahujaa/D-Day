"""Runtime configuration for the local D-Day service (environment variables only)."""

import os
from dataclasses import dataclass, field
from typing import List

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") \
        or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "D-Day")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = int(os.environ.get("DDAY_PORT", "8765"))
    data_dir: str = os.environ.get("DDAY_DATA_DIR") or _default_data_dir()
    default_timezone: str = os.environ.get("DDAY_TIMEZONE", "Asia/Kolkata")
    client_secrets_file: str = os.environ.get("DDAY_GOOGLE_CLIENT_SECRETS", "")
    # Optional: pin the extension ID(s) allowed to call the API (comma separated).
    allowed_extension_ids: List[str] = field(default_factory=lambda: [
        x.strip() for x in os.environ.get("DDAY_EXTENSION_IDS", "").split(",") if x.strip()
    ])

    def resolve_client_secrets(self) -> str:
        """First existing of: env var, <data_dir>/client_secret.json, ./credentials/client_secret.json."""
        for path in (
            self.client_secrets_file,
            os.path.join(self.data_dir, "client_secret.json"),
            os.path.join(PROJECT_DIR, "credentials", "client_secret.json"),
        ):
            if path and os.path.isfile(path):
                return path
        return ""

    @property
    def token_path(self) -> str:
        return os.path.join(self.data_dir, "google_token.bin")

    @property
    def ledger_path(self) -> str:
        return os.path.join(self.data_dir, "created_events.json")

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}/oauth/callback"
