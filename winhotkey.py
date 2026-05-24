"""
winhotkey.py - register a global hotkey on Windows without Administrator.

Uses Win32 RegisterHotKey (which, unlike the low-level keyboard hooks the digital
mapper relies on, does NOT require elevation). The hotkey is thread-affine: it must
be registered and its messages pumped on the same thread, so we run a small daemon
message loop.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable

VK_F8 = 0x77


def start_hotkey(vk: int, on_trigger: Callable[[], None], stop_flag: Callable[[], bool],
                 hotkey_id: int = 1) -> bool:
    """Start a background listener that calls `on_trigger()` when `vk` is pressed.

    Runs until `stop_flag()` returns True. Returns True if the hotkey registered
    (False if, e.g., another app already owns it)."""
    user32 = ctypes.windll.user32
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    PM_REMOVE = 0x0001
    state = {"ok": False}
    ready = threading.Event()

    def loop():
        state["ok"] = bool(user32.RegisterHotKey(None, hotkey_id, MOD_NOREPEAT, vk))
        ready.set()
        if not state["ok"]:
            return
        msg = wintypes.MSG()
        try:
            while not stop_flag():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY:
                        try:
                            on_trigger()
                        except Exception:
                            pass
                time.sleep(0.02)
        finally:
            user32.UnregisterHotKey(None, hotkey_id)

    threading.Thread(target=loop, name="HotkeyListener", daemon=True).start()
    ready.wait(timeout=1.0)
    return state["ok"]
