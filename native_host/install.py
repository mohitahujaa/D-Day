"""
Register the D-Day native messaging host with Chrome and Edge (current user only).

    python native_host/install.py                 # auto-detects the D-Day extension ID
    python native_host/install.py <extension-id>  # or pass it (see chrome://extensions)
    python native_host/install.py --uninstall

Writes native_host/com.dday.host.json and native_host/dday_host.bat (machine
specific, git-ignored) and adds HKCU registry keys pointing at the manifest.
"""

import glob
import json
import os
import sys

HOST_NAME = "com.dday.host"
HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.join(os.path.dirname(HERE), "extension")
REG_KEYS = [
    "\\".join([r"Software\Google\Chrome\NativeMessagingHosts", HOST_NAME]),
    "\\".join([r"Software\Microsoft\Edge\NativeMessagingHosts", HOST_NAME]),
    "\\".join([r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts", HOST_NAME]),
]
PROFILE_ROOTS = [
    r"%LOCALAPPDATA%\Google\Chrome\User Data",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
]


def detect_extension_ids():
    """Find the ID(s) of the unpacked D-Day extension loaded from this folder."""
    ids = set()
    target = os.path.normcase(os.path.abspath(EXT_DIR))
    for root in PROFILE_ROOTS:
        base = os.path.expandvars(root)
        for pref in glob.glob(os.path.join(base, "*", "Secure Preferences")) + \
                glob.glob(os.path.join(base, "*", "Preferences")):
            try:
                data = json.load(open(pref, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for ext_id, e in ((data.get("extensions") or {}).get("settings") or {}).items():
                if os.path.normcase(e.get("path", "")) == target:
                    ids.add(ext_id)
    return sorted(ids)


def install(ext_ids):
    import winreg

    bat = os.path.join(HERE, "dday_host.bat")
    with open(bat, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write("@echo off\n")
        fh.write(f'"{sys.executable}" -u "%~dp0dday_host.py" %*\n')

    manifest = os.path.join(HERE, HOST_NAME + ".json")
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump({
            "name": HOST_NAME,
            "description": "D-Day: start/stop the local service",
            "path": "dday_host.bat",
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{i}/" for i in ext_ids],
        }, fh, indent=2)

    for key in REG_KEYS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, manifest)
    print(f"Registered {HOST_NAME} for extension(s): {', '.join(ext_ids)}")
    print("Manifest:", manifest)


def uninstall():
    import winreg
    for key in REG_KEYS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass
    for f in ("dday_host.bat", HOST_NAME + ".json"):
        try:
            os.remove(os.path.join(HERE, f))
        except OSError:
            pass
    print("Unregistered", HOST_NAME)


def main():
    if os.name != "nt":
        sys.exit("This installer is for Windows. On macOS/Linux, place the manifest in the browser's "
                 "NativeMessagingHosts folder (see Chrome's native messaging docs).")
    if "--uninstall" in sys.argv:
        uninstall()
        return
    ids = [a for a in sys.argv[1:] if not a.startswith("-")] or detect_extension_ids()
    if not ids:
        sys.exit("Could not find the D-Day extension. Load it from chrome://extensions first, "
                 "or pass its ID: python native_host/install.py <extension-id>")
    install(ids)


if __name__ == "__main__":
    main()
