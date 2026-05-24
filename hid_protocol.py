"""
hid_protocol.py - canonical MonsGeek M1 V5 HE HID protocol library.

This is the reusable, production-side counterpart to the throwaway stage1_probe.py.
It owns the wire-level details (device discovery, checksum, enable command) and a
DepthMonitor that runs a background reader thread maintaining a live {key_index:
depth} map.

Verified on hardware (Stage 1): wired M1 V5 HE (VID 0x3151 / PID 0x5030) streams
analog key-depth on interface 1 / Col05 (usage_page 0xFFFF, usage 0x01) after a
one-shot enable Feature Report on interface 2 (usage_page 0xFFFF, usage 0x02).
Depth is a little-endian u16, ~0 (released) to ~720 (fully bottomed out). No admin
required. No 0xAA ack is returned, but monitoring works regardless.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Optional

# hidapi.dll is vendored next to this file (tools/fetch_hidapi.ps1). Python 3.8+
# does not search cwd/PATH for ctypes DLLs, so register the directory explicitly.
# When frozen by PyInstaller, the DLL is bundled into sys._MEIPASS / next to the exe.
_dll_dirs = []
if getattr(sys, "frozen", False):
    _dll_dirs.append(getattr(sys, "_MEIPASS", ""))
    _dll_dirs.append(os.path.dirname(sys.executable))
_dll_dirs.append(os.path.dirname(os.path.abspath(__file__)))
for _d in _dll_dirs:
    if _d and os.path.isdir(_d) and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(_d)
        except Exception:
            pass

import hid  # noqa: E402

VID, PID = 0x3151, 0x5030  # M1 V5 HE, wired

# Protocol command bytes (from the reverse-engineered PROTOCOL.md)
CMD_SET_MAGNETISM_REPORT = 0x1B
EVENT_KEY_DEPTH = 0x1B
VENDOR_INPUT_REPORT_ID = 0x05


def checksum_bit7(payload: bytes) -> int:
    """Pad-to-8 + invert-sum-of-first-7-bytes checksum used by standard commands."""
    return (255 - (sum(payload[:7]) & 0xFF)) & 0xFF


def build_feature_command(cmd_bytes: list[int]) -> bytes:
    """Build a 65-byte Feature Report: [report_id=0][cmd+params][checksum][padding]."""
    msg = list(cmd_bytes) + [0] * max(0, 8 - len(cmd_bytes))
    msg[7] = checksum_bit7(bytes(msg[:7]))
    full = [0] + msg + [0] * (65 - 1 - len(msg))
    return bytes(full)


def find_devices() -> tuple[dict, dict]:
    """Locate (config_iface, input_iface) for the M1 V5 HE. Raises RuntimeError if absent."""
    ifaces = list(hid.enumerate(VID, PID))
    if not ifaces:
        raise RuntimeError(
            f"No device VID:PID={VID:04x}:{PID:04x}. Plugged in via USB-C (not the "
            f"wireless dongle, which is PID 0x503A)?"
        )
    config = next(
        (d for d in ifaces if d.get("usage_page") == 0xFFFF and d.get("usage") == 0x02),
        None,
    )
    if config is None:
        raise RuntimeError("Vendor config interface (usage_page=0xFFFF, usage=0x02) not found")
    candidates = [d for d in ifaces if d is not config]
    vendor_inputs = [d for d in candidates if (d.get("usage_page", 0) & 0xFF00) == 0xFF00]
    input_iface = vendor_inputs[0] if vendor_inputs else candidates[0]
    return config, input_iface


class DepthMonitor:
    """Opens the keyboard, enables depth monitoring, and maintains a live depth map.

    A background daemon thread reads input reports and updates `{key_index: depth}`.
    Read the current state with `snapshot()` / `get(key_index)` from any thread.

    Sleep/wake handling: SET_MAGNETISM_REPORT does not persist across the keyboard
    sleeping, so the reader re-sends the enable command once after each idle gap
    (no events for `idle_reenable_s`). It re-arms after the next event, so a normal
    no-keys-pressed pause costs at most one harmless feature report.
    """

    def __init__(
        self,
        on_event: Optional[Callable[[int, int], None]] = None,
        idle_reenable_s: float = 3.0,
    ) -> None:
        self.on_event = on_event
        self.idle_reenable_s = idle_reenable_s
        self._depths: dict[int, int] = {}
        self._lock = threading.Lock()
        self._running = False
        self._config_dev = None
        self._input_dev = None
        self._reader: Optional[threading.Thread] = None
        self._last_event = 0.0
        self._reenable_armed = True
        self.event_count = 0
        # diagnostics
        self.reads_total = 0
        self.reads_nonempty = 0
        self.last_event_types: set[int] = set()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        config_info, input_info = find_devices()
        self._config_dev = hid.Device(path=config_info["path"])
        self._enable()
        self._input_dev = hid.Device(path=input_info["path"])
        self._running = True
        self._last_event = time.monotonic()
        self._reenable_armed = True
        self._reader = threading.Thread(target=self._read_loop, name="DepthReader", daemon=True)
        self._reader.start()

    def stop(self) -> None:
        self._running = False
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._config_dev is not None:
            try:
                self._config_dev.send_feature_report(
                    build_feature_command([CMD_SET_MAGNETISM_REPORT, 0x00])
                )
            except Exception:
                pass
        for dev in (self._input_dev, self._config_dev):
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
        self._input_dev = self._config_dev = None

    # -- internals ---------------------------------------------------------

    def _enable(self) -> None:
        self._config_dev.send_feature_report(
            build_feature_command([CMD_SET_MAGNETISM_REPORT, 0x01])
        )
        try:
            self._config_dev.get_feature_report(0, 65)  # drain/ack; ignored
        except Exception:
            pass

    def _read_loop(self) -> None:
        while self._running:
            try:
                data = self._input_dev.read(64, timeout=200)
            except Exception:
                continue
            self.reads_total += 1
            now = time.monotonic()
            if not data or len(data) < 5:
                if self._reenable_armed and (now - self._last_event) > self.idle_reenable_s:
                    try:
                        self._enable()
                    except Exception:
                        pass
                    self._reenable_armed = False  # one re-enable per idle gap
                continue
            self.reads_nonempty += 1
            if data[0] == VENDOR_INPUT_REPORT_ID:
                event_type, b2, b3, b4 = data[1], data[2], data[3], data[4]
            else:
                event_type, b2, b3, b4 = data[0], data[1], data[2], data[3]
            self.last_event_types.add(event_type)
            if event_type != EVENT_KEY_DEPTH:
                continue
            depth = b2 | (b3 << 8)
            key_index = b4
            self._last_event = now
            self._reenable_armed = True
            self.event_count += 1
            with self._lock:
                self._depths[key_index] = depth
            if self.on_event is not None:
                try:
                    self.on_event(key_index, depth)
                except Exception:
                    pass

    # -- state access ------------------------------------------------------

    def snapshot(self) -> dict[int, int]:
        with self._lock:
            return dict(self._depths)

    def get(self, key_index: int) -> int:
        with self._lock:
            return self._depths.get(key_index, 0)
