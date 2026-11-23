"""
Start D-Day silently when Windows starts (a shortcut in the user's Startup folder).

The shortcut runs `pythonw native_host/dday_host.py --start`, which starts
Ollama (if needed) and the D-Day service with no window. Remove it from the
dashboard (Settings) or delete "D-Day.lnk" from shell:startup.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LINK_NAME = "D-Day.lnk"


def startup_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def link_path() -> str:
    return os.path.join(startup_dir(), LINK_NAME)


def supported() -> bool:
    return os.name == "nt" and bool(os.environ.get("APPDATA"))


def is_enabled() -> bool:
    return supported() and os.path.isfile(link_path())


def _pythonw() -> str:
    w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return w if os.path.isfile(w) else sys.executable


def enable() -> None:
    if not supported():
        raise OSError("Start with Windows is only available on Windows.")
    os.makedirs(startup_dir(), exist_ok=True)
    target = _pythonw().replace("'", "''")
    args = f'"{os.path.join(ROOT, "native_host", "dday_host.py")}" --start'.replace("'", "''")
    icon = os.path.join(ROOT, "extension", "icons", "icon48.png")
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
        "$s.TargetPath = '{target}'; $s.Arguments = '{args}'; $s.WorkingDirectory = '{cwd}'; "
        "$s.WindowStyle = 7; $s.Description = 'Start D-Day (local deadline assistant)'; $s.Save()"
    ).format(link=link_path().replace("'", "''"), target=target, args=args, cwd=ROOT.replace("'", "''"))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=True,
                   capture_output=True, timeout=30, creationflags=0x08000000)
    _ = icon  # .png can't be a shortcut icon; Windows shows the Python icon


def disable() -> None:
    try:
        os.remove(link_path())
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    if "--disable" in sys.argv:
        disable()
        print("Start with Windows: off")
    else:
        enable()
        print("Start with Windows: on ->", link_path())
