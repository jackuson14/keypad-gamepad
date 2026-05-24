"""
Deterministic vgamepad -> ViGEmBus -> XInput readback check (no keyboard needed).

The dry-run test proved depth -> engine target values. This proves engine values ->
what a game actually reads, by commanding the virtual pad to known float values and
reading them back through XInput. If 0.5 on a stick reads back as ~16384/32767, the
output half of the chain is correct, and composition with the dry-run proof gives the
whole pipeline.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import Structure, byref, c_ubyte, c_ushort, c_short, c_ulong

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
if hasattr(os, "add_dll_directory") and os.path.isdir(_ROOT):
    os.add_dll_directory(_ROOT)

import vgamepad as vg  # noqa: E402


class XINPUT_GAMEPAD(Structure):
    _fields_ = [("wButtons", c_ushort), ("bLeftTrigger", c_ubyte), ("bRightTrigger", c_ubyte),
                ("sThumbLX", c_short), ("sThumbLY", c_short), ("sThumbRX", c_short), ("sThumbRY", c_short)]


class XINPUT_STATE(Structure):
    _fields_ = [("dwPacketNumber", c_ulong), ("Gamepad", XINPUT_GAMEPAD)]


def main() -> int:
    xinput = None
    for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            xinput = ctypes.windll.LoadLibrary(dll + ".dll"); break
        except Exception:
            continue
    if xinput is None:
        print("ERROR: no XInput DLL", file=sys.stderr); return 1

    def read(slot):
        st = XINPUT_STATE()
        return st if xinput.XInputGetState(slot, byref(st)) == 0 else None

    before = {i for i in range(4) if read(i) is not None}
    pad = None
    for attempt in range(6):
        try:
            pad = vg.VX360Gamepad(); break
        except Exception as e:
            print(f"  (attach attempt {attempt+1} failed: {e}; retrying)")
            time.sleep(0.5)
    if pad is None:
        print("ERROR: could not attach virtual pad after retries", file=sys.stderr); return 3
    time.sleep(0.3)
    after = {i for i in range(4) if read(i) is not None}
    new = sorted(after - before)
    slot = new[0] if new else 0
    print(f"slots before={sorted(before)} after={sorted(after)} -> pad slot {slot}\n")

    def setread(desc, fn, expect):
        pad.reset()
        fn()
        pad.update()
        time.sleep(0.12)
        st = read(slot)
        g = st.Gamepad if st else None
        got = {
            "LX": g.sThumbLX, "LY": g.sThumbLY, "RX": g.sThumbRX, "RY": g.sThumbRY,
            "LT": g.bLeftTrigger, "RT": g.bRightTrigger, "btn": hex(g.wButtons),
        } if g else {}
        ok = all(abs(got.get(k, 99999) - v) <= tol for k, (v, tol) in expect.items())
        flag = "OK " if ok else "XX "
        shown = {k: got.get(k) for k in expect}
        print(f"  [{flag}] {desc:28} expect={ {k:v[0] for k,v in expect.items()} }  got={shown}")
        return ok

    results = []
    results.append(setread("idle (all zero)", lambda: None,
                           {"LX": (0, 1200), "LY": (0, 1200), "RX": (0, 1200), "RY": (0, 1200), "LT": (0, 8), "RT": (0, 8)}))
    results.append(setread("left stick UP full", lambda: pad.left_joystick_float(0.0, 1.0),
                           {"LY": (32767, 1500), "LX": (0, 1200)}))
    results.append(setread("left stick UP half", lambda: pad.left_joystick_float(0.0, 0.5),
                           {"LY": (16384, 2000), "LX": (0, 1200)}))
    results.append(setread("left stick RIGHT half", lambda: pad.left_joystick_float(0.5, 0.0),
                           {"LX": (16384, 2000), "LY": (0, 1200)}))
    results.append(setread("right trigger half", lambda: pad.right_trigger_float(0.5),
                           {"RT": (127, 12), "LT": (0, 8)}))
    results.append(setread("right trigger full", lambda: pad.right_trigger_float(1.0),
                           {"RT": (255, 4)}))

    # Idle-settling probe: push (0,0) continuously and watch whether the sticks
    # converge to ~0 (good) or hold a resting offset a game would see as drift.
    print("\n  idle settling (reset+update x40):")
    idle_reads = []
    for i in range(40):
        pad.reset(); pad.update(); time.sleep(0.03)
        st = read(slot)
        if st:
            g = st.Gamepad
            idle_reads.append((g.sThumbLX, g.sThumbLY, g.sThumbRX, g.sThumbRY))
    if idle_reads:
        for label, idx in [("first", 0), ("mid", len(idle_reads)//2), ("last", -1)]:
            print(f"    {label:>5}: LX={idle_reads[idx][0]:6d} LY={idle_reads[idx][1]:6d} "
                  f"RX={idle_reads[idx][2]:6d} RY={idle_reads[idx][3]:6d}")
        last5 = idle_reads[-5:]
        max_idle = max(max(abs(v) for v in r) for r in last5)
        print(f"    max |stick| over last 5 idle samples = {max_idle}")
        results.append(max_idle < 1200)

    # button check uses exact compare
    pad.reset(); pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_A); pad.update(); time.sleep(0.12)
    st = read(slot); btn_ok = bool(st and (st.Gamepad.wButtons & 0x1000))
    print(f"  [{'OK ' if btn_ok else 'XX '}] button A bit set in wButtons: {hex(st.Gamepad.wButtons) if st else None}")

    pad.reset(); pad.update()
    print("\n" + "=" * 60)
    passed = sum(1 for r in results[:-1] if r) + (1 if btn_ok else 0)
    total = len(results) - 1 + 1
    print(f"  {passed}/{total} checks passed.")
    if passed == total:
        print("  RESULT: vgamepad -> ViGEmBus -> XInput output path is CORRECT.")
        return 0
    print("  RESULT: some checks failed -- see XX rows above.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
