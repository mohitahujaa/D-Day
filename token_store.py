"""
OAuth token storage.

* Windows: the token JSON is encrypted with DPAPI (CryptProtectData), so only
  the same Windows user account can decrypt it. No extra dependencies.
* Elsewhere: the OS keyring via the optional `keyring` package if installed,
  otherwise a file readable only by the current user (chmod 600).

Tokens are never logged.
"""

import json
import os
import sys
from typing import Optional

_KEYRING_SERVICE = "d-day-google-calendar"
_DPAPI_ENTROPY = b"D-Day OAuth token v1"


def _dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _dpapi(data: bytes, protect: bool) -> bytes:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in, entropy, blob_out = _blob(data), _blob(_DPAPI_ENTROPY), _DATA_BLOB()
        CRYPTPROTECT_UI_FORBIDDEN = 0x01
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise OSError("DPAPI operation failed")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)


class TokenStore:
    """Stores one JSON-serialisable token dict."""

    def __init__(self, path: str, backend: Optional[str] = None):
        self.path = path
        if backend is None:
            if _dpapi_available():
                backend = "dpapi"
            else:
                try:
                    import keyring  # noqa: F401
                    backend = "keyring"
                except ImportError:
                    backend = "file"
        self.backend = backend

    # -- public -------------------------------------------------------------
    def load(self) -> Optional[dict]:
        try:
            if self.backend == "keyring":
                import keyring
                raw = keyring.get_password(_KEYRING_SERVICE, self.path)
                return json.loads(raw) if raw else None
            if not os.path.exists(self.path):
                return None
            with open(self.path, "rb") as fh:
                data = fh.read()
            if self.backend == "dpapi":
                data = _dpapi(data, protect=False)
            return json.loads(data.decode("utf-8"))
        except Exception:
            # Corrupt or undecryptable (e.g. copied from another account):
            # treat as signed out rather than crashing.
            return None

    def save(self, token: dict) -> None:
        raw = json.dumps(token).encode("utf-8")
        if self.backend == "keyring":
            import keyring
            keyring.set_password(_KEYRING_SERVICE, self.path, raw.decode("utf-8"))
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if self.backend == "dpapi":
            raw = _dpapi(raw, protect=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, self.path)
        if self.backend == "file":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def clear(self) -> None:
        if self.backend == "keyring":
            try:
                import keyring
                keyring.delete_password(_KEYRING_SERVICE, self.path)
            except Exception:
                pass
            return
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
