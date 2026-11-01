"""Desktop launcher (what the "Registro de Mantención" shortcut runs, with pythonw: no console window).

Starts the local server, opens the app in its own Edge window (like an installed program) and stops the
server when that window is closed. Double-clicking again while it is open brings the window to the front.
The window uses the normal Edge profile, so the microphone permission already given to 127.0.0.1 applies.
"""
import ctypes
import os
import pathlib
import subprocess
import time
import urllib.request
import webbrowser
import winreg
from ctypes import wintypes

ROOT = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("VOICE_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
TITLE = "Registro de mantención por voz"   # <title> of app/static/index.html: the app window is found by it
LOG = pathlib.Path(os.environ.get("VOICE_RUNTIME", ROOT / "data")) / "app.log"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)


def server_up():
    try:
        urllib.request.urlopen(URL + "api/estado", timeout=1)
        return True
    except OSError:
        return False


def app_windows():
    """Visible top-level windows showing the app (its own window, or a browser tab whose title starts with it)."""
    found = []

    def check(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(TITLE):
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(check), 0)
    return found


def bring_to_front(hwnd):
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)


def find_edge():
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                path = winreg.QueryValue(key, None)
            if path and os.path.exists(path):
                return path
        except OSError:
            pass
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        path = os.path.join(os.environ.get(var, ""), "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.exists(path):
            return path
    return None


def fail(text):
    user32.MessageBoxW(None, text, TITLE, 0x10)  # MB_ICONERROR


def main():
    # one launcher at a time: a second double-click only brings the open window forward
    kernel32.CreateMutexW(None, False, f"Local\\RegistroMantencionVoz{PORT}")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        for _ in range(150):
            windows = app_windows()
            if windows:
                bring_to_front(windows[0])
                return
            time.sleep(0.2)
        return

    if not server_up():
        LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG, "a", encoding="utf-8")
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} inicio desde el acceso directo\n")
        log.flush()
        env = {**os.environ, "VOICE_PARENT_PID": str(os.getpid()), "PYTHONIOENCODING": "utf-8"}
        server = subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "python.exe"), str(ROOT / "app" / "server.py"), "--no-browser"],
                                  cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.time() + 120
        while not server_up():
            if server.poll() is not None or time.time() > deadline:
                server.kill()
                fail(f"No se pudo iniciar la aplicación.\nDetalle en: {LOG}")
                return
            time.sleep(0.25)
    elif app_windows():   # already open (e.g. started with start_app.bat)
        bring_to_front(app_windows()[0])
        return

    before = set(app_windows())   # e.g. an old browser tab: not the window opened now
    edge = find_edge()
    if edge:
        subprocess.Popen([edge, f"--app={URL}", "--window-size=1320,960"])
    else:
        webbrowser.open(URL)
    # stay while the window is open; the server watches this process and stops when it ends
    started, ours = time.time(), set()
    while not ours:
        if time.time() - started > 90:
            return
        time.sleep(0.3)
        ours = set(app_windows()) - before
    while any(user32.IsWindow(h) for h in ours):
        time.sleep(1)


if __name__ == "__main__":
    main()
