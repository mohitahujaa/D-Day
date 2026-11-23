"""
D-Day native messaging host: lets the browser extension switch D-Day on and off.

Chrome starts this program when the extension calls
chrome.runtime.sendNativeMessage("com.dday.host", {"cmd": ...}); only the
extension IDs listed in the host manifest (written by install.py) may call it.

Commands:
    {"cmd": "status"} -> {"ok": true, "service": bool, "ollama": bool}
    {"cmd": "start"}  -> starts Ollama and the D-Day service (windowless) if needed
    {"cmd": "stop"}   -> unloads the model and stops the D-Day service

It accepts no other input and runs nothing but Ollama and server.py.
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("DDAY_PORT", "8765"))
SERVICE = f"http://127.0.0.1:{PORT}"
OLLAMA = os.environ.get("DDAY_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


# ---------------------------------------------------------------- protocol
def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("<I", raw)
    if length > 64 * 1024:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send_message(obj) -> None:
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------- checks
def _get_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def service_up() -> bool:
    return _get_ok(SERVICE + "/")


def ollama_up() -> bool:
    return _get_ok(OLLAMA + "/api/tags")


def find_ollama():
    found = shutil.which("ollama")
    if found:
        return found
    candidate = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    return candidate if os.path.isfile(candidate) else None


def pythonw() -> str:
    exe_dir = os.path.dirname(sys.executable)
    w = os.path.join(exe_dir, "pythonw.exe")
    return w if os.path.isfile(w) else sys.executable


# ---------------------------------------------------------------- launching
def spawn_detached(args, cwd=None) -> None:
    """
    Start a background process that outlives this host. Chrome may run native
    hosts inside a Windows job object that is closed when the host exits, so
    break away from it; if that is not allowed, create the process via WMI,
    which is never part of Chrome's job.
    """
    if os.name != "nt":
        subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
             | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
    try:
        subprocess.Popen(args, cwd=cwd, creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        cmdline = subprocess.list2cmdline(args).replace("'", "''")
        ps = (f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
              f"@{{CommandLine='{cmdline}'; CurrentDirectory='{(cwd or ROOT)}'}}; exit $r.ReturnValue")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0x08000000, timeout=30)  # CREATE_NO_WINDOW


def wait_for(check, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.5)
    return check()


def status() -> dict:
    return {"ok": True, "service": service_up(), "ollama": ollama_up()}


def start() -> dict:
    if not ollama_up():
        exe = find_ollama()
        if exe:
            spawn_detached([exe, "serve"])
    if not service_up():
        spawn_detached([pythonw(), os.path.join(ROOT, "server.py")], cwd=ROOT)
    # The first start after a reboot can take a while (Ollama loads its runtime).
    wait_for(lambda: service_up() and ollama_up(), 60)
    out = status()
    problems = []
    if not out["ollama"]:
        problems.append("Ollama is not installed (https://ollama.com)." if not find_ollama()
                        else "Ollama did not start yet; try again in a moment.")
    if not out["service"]:
        problems.append(r"The D-Day service did not start; see %LOCALAPPDATA%\D-Day\service.log.")
    if problems:
        out["ok"] = out["service"]
        out["message"] = " ".join(problems)
    return out


def stop() -> dict:
    if service_up():
        req = urllib.request.Request(SERVICE + "/api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "X-DDay-Client": "extension"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except (urllib.error.URLError, OSError):
            pass
        wait_for(lambda: not service_up(), 8)
    return status()


COMMANDS = {"status": status, "start": start, "stop": stop}


def main() -> None:
    try:
        msg = read_message() or {}
        fn = COMMANDS.get(msg.get("cmd"))
        send_message(fn() if fn else {"ok": False, "message": "Unknown command."})
    except Exception as exc:  # never leave the extension hanging
        send_message({"ok": False, "message": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    if "--start" in sys.argv:  # used by the Windows Startup shortcut (autostart.py)
        start()
    else:
        main()
