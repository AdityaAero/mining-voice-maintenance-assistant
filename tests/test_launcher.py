"""Desktop launcher test (what the shortcut does), on a test port with a temporary data folder.

Runs launcher.pyw with pythonw like a double-click: the server must start without a console, the app must open
in its own Edge window, a second double-click must not open a second window, and closing the window must stop
the server. A real Edge window appears on screen for a few seconds.

usage: python tests/test_launcher.py [screenshot_dir]
"""
import ctypes
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from ctypes import wintypes

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHOTS = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "screenshots"
PORT = 8797
TITLE = "Registro de mantención por voz"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
failures = []
user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def windows():
    found = []

    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if user32.IsWindowVisible(hwnd) and buf.value.startswith(TITLE):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return set(found)


def server_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/estado", timeout=1)
        return True
    except OSError:
        return False


def wait(cond, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.25)
    return False


SHOTS.mkdir(parents=True, exist_ok=True)
runtime = pathlib.Path(tempfile.mkdtemp(prefix="voice_launch_"))
env = {**os.environ, "VOICE_PORT": str(PORT), "VOICE_RUNTIME": str(runtime), "VOICE_WISPR_DRYRUN": "1",
       "VOICE_MACHINES": str(ROOT / "data" / "machines.example.csv")}
before = windows()
launcher = subprocess.Popen([str(PYTHONW), str(ROOT / "launcher.pyw")], env=env)
ours = set()
try:
    check(wait(server_up, 90), "double-click starts the local server")
    check(wait(lambda: windows() - before, 60), "the app opens in its own window")
    ours = windows() - before
    hwnd = next(iter(ours), None)
    if hwnd:
        time.sleep(3)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        shot = SHOTS / "ventana_app.png"
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Add-Type -AssemblyName System.Drawing;"
                        f"$b=New-Object System.Drawing.Bitmap {r.right - r.left},{r.bottom - r.top};"
                        "$g=[System.Drawing.Graphics]::FromImage($b);"
                        f"$g.CopyFromScreen({r.left},{r.top},0,0,$b.Size);$b.Save('{shot}')"], timeout=30)
        print(f"       window {r.right - r.left}x{r.bottom - r.top}, screenshot {shot}")
    second = subprocess.Popen([str(PYTHONW), str(ROOT / "launcher.pyw")], env=env)
    check(wait(lambda: second.poll() is not None, 15) and len(windows() - before) == 1,
          "a second double-click brings the window forward instead of opening another")
    for h in ours:
        user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE, as when the user closes the window
    check(wait(lambda: launcher.poll() is not None, 20), "closing the window ends the launcher")
    check(wait(lambda: not server_up(), 20), "closing the window stops the server (frees memory)")
    log = (runtime / "app.log").read_text(encoding="utf-8", errors="replace") if (runtime / "app.log").exists() else ""
    check("inicio desde el acceso directo" in log, "server output goes to data/app.log (no console window)")
finally:
    for h in ours:
        if user32.IsWindow(h):
            user32.PostMessageW(h, 0x0010, 0, 0)
    if launcher.poll() is None:
        launcher.kill()
    wait(lambda: not server_up(), 10)
    shutil.rmtree(runtime, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
sys.exit(1 if failures else 0)
