"""Bridge to Wispr Flow, the desktop dictation app (https://wisprflow.ai).

Wispr Flow types its transcript into whichever text field has the keyboard focus while its push-to-talk
shortcut (Ctrl+Win on Windows) is held. The app's "Dictar" button in Wispr mode focuses the field and asks this
module to hold that shortcut until the technician stops, so the same button works for both engines and on a
touch screen. The shortcut is read from Wispr's own settings, so a user-changed shortcut keeps working.

Speech goes to Wispr's cloud service (internet needed); the local Whisper engine stays fully offline.
VOICE_WISPR_DRYRUN=1 records the key presses instead of sending them (used by the tests).
"""
import ctypes
import json
import os
import pathlib
import threading
import time
from ctypes import wintypes

CONFIG = pathlib.Path(os.environ.get("APPDATA", "")) / "Wispr Flow" / "config.json"
PROCESS = "wispr flow.exe"
DEFAULT_PTT = (0xA2, 0x5B)          # Left Ctrl + Left Win, Wispr's default on Windows
VK_ESCAPE = 0x1B                    # Wispr's "dismiss" key: cancels the dictation
MAX_HOLD_S = 95                     # the app stops a dictation at 90 s; never leave keys held longer
DRY_RUN = os.environ.get("VOICE_WISPR_DRYRUN") == "1"
KEY_NAMES = {0xA2: "Ctrl", 0xA3: "Ctrl", 0x11: "Ctrl", 0x5B: "Win", 0x5C: "Win", 0xA0: "Shift", 0xA1: "Shift",
             0x10: "Shift", 0xA4: "Alt", 0xA5: "Alt", 0x12: "Alt", 0x20: "Space"}
EXTENDED = {0x5B, 0x5C, 0xA3, 0xA5}  # keys that need KEYEVENTF_EXTENDEDKEY

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t))


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t))


class _INPUTUNION(ctypes.Union):
    _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = (("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260))


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def process_running(name=PROCESS):
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return False
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == name:
                return True
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snap)


def _send(vks, up):
    events = (INPUT * len(vks))()
    for i, vk in enumerate(vks):
        flags = (0x0002 if up else 0) | (0x0001 if vk in EXTENDED else 0)  # KEYUP | EXTENDEDKEY
        events[i].type = 1  # INPUT_KEYBOARD
        events[i].u.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
    sent = user32.SendInput(len(vks), events, ctypes.sizeof(INPUT))
    if sent != len(vks):
        raise OSError(f"SendInput sent {sent} of {len(vks)} keys (error {ctypes.get_last_error()})")


class WisprBridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._held = None          # keys currently held down
        self._timer = None
        self._shortcut = (None, DEFAULT_PTT)
        self._running = (0.0, False)
        self.log = []              # dry-run record: ("down"|"up", keys)

    def shortcut(self):
        """Wispr's push-to-talk keys from its settings, e.g. {"162+91": "ptt"} -> (162, 91)."""
        try:
            mtime = CONFIG.stat().st_mtime
        except OSError:
            return DEFAULT_PTT
        if self._shortcut[0] != mtime:
            keys = DEFAULT_PTT
            try:
                shortcuts = json.loads(CONFIG.read_text(encoding="utf-8"))["prefs"]["user"]["shortcuts"]
                found = [k for k, v in shortcuts.items() if v == "ptt"]
                if found:
                    keys = tuple(int(x) for x in found[0].split("+"))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self._shortcut = (mtime, keys)
        return self._shortcut[1]

    def running(self):
        if DRY_RUN:
            return True
        checked, value = self._running
        if time.monotonic() - checked > 5:
            value = process_running()
            self._running = (time.monotonic(), value)
        return value

    def status(self):
        keys = self.shortcut()
        return {"instalado": DRY_RUN or CONFIG.exists(), "abierto": self.running(),
                "atajo": " + ".join(KEY_NAMES.get(k, f"tecla {k}") for k in keys), "grabando": self._held is not None}

    def _keys(self, keys, up):
        if DRY_RUN:
            self.log.append(("up" if up else "down", tuple(keys)))
        else:
            _send(keys, up)

    def press(self):
        """Hold the push-to-talk shortcut (Wispr starts listening)."""
        with self._lock:
            if self._held:
                return
            keys = self.shortcut()
            self._keys(keys, up=False)
            self._held = keys
            self._timer = threading.Timer(MAX_HOLD_S, self.release)
            self._timer.daemon = True
            self._timer.start()

    def release(self):
        """Let go of the shortcut (Wispr transcribes and types into the focused field)."""
        with self._lock:
            if not self._held:
                return False
            if self._timer:
                self._timer.cancel()
            self._keys(tuple(reversed(self._held)), up=True)
            self._held = None
            return True

    def cancel(self):
        """Stop and tell Wispr to discard the dictation (its Esc shortcut)."""
        if self.release():
            time.sleep(0.05)
            self._keys((VK_ESCAPE,), up=False)
            self._keys((VK_ESCAPE,), up=True)


bridge = WisprBridge()
