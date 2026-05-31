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

The same protocol covers other MonsGeek/Akko HE keyboards on RongYuan firmware, so
device selection is data-driven: KNOWN_DEVICES lists the boards, find_devices()
auto-scans them (or takes an explicit vid/pid), and the interface split is keyed
off vendor usage pages rather than hardcoded interface numbers.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class KnownDevice:
    """One HE keyboard known to speak the RongYuan vendor depth protocol."""

    vid: int
    pid: int
    name: str
    transport: str = "wired"   # "wired" | "dongle" | "unknown"
    verified: bool = False     # True only if tested on real hardware by this project

    @property
    def id_str(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"

    def __str__(self) -> str:
        tag = "" if self.verified else " [unverified]"
        return f"{self.name} ({self.id_str}){tag}"


# Registry of MonsGeek/Akko-family HE keyboards that use the same vendor depth
# protocol (RongYuan firmware: 0x1B SET_MAGNETISM_REPORT + 0x1B KeyDepth events on
# vendor usage page 0xFFFF). Auto-scan tries these in order and uses the first one
# currently connected, so verified boards come first.
#
# Adding a board: append a KnownDevice here. Before trusting it, confirm it streams
# 0x1B depth events with:  py stage1_probe.py --vid 0x.... --pid 0x....
KNOWN_DEVICES: list[KnownDevice] = [
    KnownDevice(0x3151, 0x5030, "MonsGeek M1 V5 HE", "wired", verified=True),
    # Same keyboard over its 2.4GHz dongle. Verified on hardware: the dongle relays
    # depth telemetry byte-identically to wired (report 0x05 / event 0x1B / u16 depth),
    # just under a different PID. The paired dongle enumerates as 0x5038 — an earlier
    # 0x503A guess was never observed and has been dropped.
    KnownDevice(0x3151, 0x5038, "MonsGeek M1 V5 HE (2.4GHz dongle)", "dongle", verified=True),
]

# Vendor IDs whose HE keyboards use the RongYuan depth protocol. Capability-based
# auto-detect (below) recognises ANY device on these vendor IDs that exposes the
# vendor signature (a 0xFFFF/0x02 config interface), so other MonsGeek models — and
# their 2.4GHz dongles, which each enumerate under their own unpredictable PID —
# auto-detect without needing every PID hardcoded. Add a vendor id here (e.g. Akko's)
# once a board on it is confirmed to speak the protocol.
HE_VENDOR_IDS: set[int] = {0x3151}

# Back-compat module constants: default to the first (verified) device. Existing
# imports of VID/PID keep working; auto-scan covers the rest.
VID, PID = KNOWN_DEVICES[0].vid, KNOWN_DEVICES[0].pid

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


def _split_interfaces(ifaces: list[dict]) -> tuple[dict, dict]:
    """Pick (config_iface, input_iface) from one device's HID collections.

    Keyed off vendor usage pages, not hardcoded interface numbers, so it works for
    any board that exposes the protocol the same way."""
    config = next(
        (d for d in ifaces if d.get("usage_page") == 0xFFFF and d.get("usage") == 0x02),
        None,
    )
    if config is None:
        raise RuntimeError("vendor config interface (usage_page=0xFFFF, usage=0x02) not found")
    candidates = [d for d in ifaces if d is not config]
    vendor_inputs = [d for d in candidates if (d.get("usage_page", 0) & 0xFF00) == 0xFF00]
    input_iface = vendor_inputs[0] if vendor_inputs else (candidates[0] if candidates else None)
    if input_iface is None:
        raise RuntimeError("no input interface alongside the vendor config interface")
    return config, input_iface


def _has_vendor_signature(ifaces: list[dict]) -> bool:
    """True if this device exposes the RongYuan vendor depth signature: a
    0xFFFF/0x02 config interface plus a separate 0xFFxx vendor input interface."""
    has_cfg = any(d.get("usage_page") == 0xFFFF and d.get("usage") == 0x02 for d in ifaces)
    has_input = any(
        (d.get("usage_page", 0) & 0xFF00) == 0xFF00
        and not (d.get("usage_page") == 0xFFFF and d.get("usage") == 0x02)
        for d in ifaces
    )
    return has_cfg and has_input


def auto_detected_devices() -> list[KnownDevice]:
    """Connected devices on the HE vendor IDs that expose the vendor depth signature
    but aren't in KNOWN_DEVICES — e.g. an unlisted MonsGeek model or its 2.4GHz dongle.

    Gated to HE_VENDOR_IDS so we never send the enable feature report to unrelated
    hardware. The product string becomes the (unverified) device name."""
    known = {(d.vid, d.pid) for d in KNOWN_DEVICES}
    out: list[KnownDevice] = []
    for vid in HE_VENDOR_IDS:
        groups: dict[int, list[dict]] = {}
        for d in hid.enumerate(vid, 0):
            groups.setdefault(d["product_id"], []).append(d)
        for pid, ifaces in groups.items():
            if (vid, pid) in known or not _has_vendor_signature(ifaces):
                continue
            name = (ifaces[0].get("product_string") or "Unrecognised HE keyboard").strip()
            out.append(KnownDevice(vid, pid, name, "unknown", verified=False))
    return out


def list_present_devices() -> list[KnownDevice]:
    """KNOWN_DEVICES currently connected, plus any capability-detected HE boards."""
    present = [d for d in KNOWN_DEVICES if list(hid.enumerate(d.vid, d.pid))]
    return present + auto_detected_devices()


def find_devices(
    vid: int | None = None, pid: int | None = None
) -> tuple[dict, dict, KnownDevice]:
    """Locate (config_iface, input_iface, device) for an HE keyboard.

    With explicit vid+pid, target exactly that device (even if not in the registry).
    Otherwise auto-scan KNOWN_DEVICES and use the first one currently connected.
    Raises RuntimeError, listing what was searched, if nothing usable is found."""
    if vid is not None and pid is not None:
        targets = [
            next(
                (d for d in KNOWN_DEVICES if d.vid == vid and d.pid == pid),
                KnownDevice(vid, pid, "custom device", "unknown", verified=False),
            )
        ]
    else:
        # Known devices first (friendly names / verified ordering), then any
        # capability-detected board so unlisted models / dongles still work.
        targets = list(KNOWN_DEVICES) + auto_detected_devices()

    errors: list[str] = []
    for dev in targets:
        ifaces = list(hid.enumerate(dev.vid, dev.pid))
        if not ifaces:
            errors.append(f"{dev}: not connected")
            continue
        try:
            config, input_iface = _split_interfaces(ifaces)
        except RuntimeError as e:
            errors.append(f"{dev}: {e}")
            continue
        return config, input_iface, dev

    raise RuntimeError(
        "No usable HE keyboard found:\n  "
        + "\n  ".join(errors)
        + "\n  Plug in via USB-C, or pair the 2.4GHz dongle (both are auto-detected). "
        "To try an unlisted board, pass its IDs explicitly "
        "(run: py stage1_probe.py --vid 0x.... --pid 0x.... to confirm it speaks the protocol)."
    )


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
        vid: Optional[int] = None,
        pid: Optional[int] = None,
    ) -> None:
        self.on_event = on_event
        self.idle_reenable_s = idle_reenable_s
        # None/None => auto-scan KNOWN_DEVICES; set both to target a specific board.
        self.vid = vid
        self.pid = pid
        self.device: Optional[KnownDevice] = None  # which board we actually opened
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
        config_info, input_info, self.device = find_devices(self.vid, self.pid)
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
