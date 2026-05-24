"""
Stage 3 end-to-end test: keyboard depth -> engine -> ViGEmBus -> XInput readback.

This is the ultimate proof. It runs the analog engine with REAL ViGEm output, then
reads the virtual controller back through the Windows XInput API -- i.e. exactly what
a game would see. If feathering W produces an intermediate sThumbLY in XInput, the
full chain works.

Usage:
    py tools/stage3_xinput_test.py [fps|racing] [duration]
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

from analog_mapper import AnalogProfile, AnalogMapper, Keymap  # noqa: E402


class XINPUT_GAMEPAD(Structure):
    _fields_ = [
        ("wButtons", c_ushort), ("bLeftTrigger", c_ubyte), ("bRightTrigger", c_ubyte),
        ("sThumbLX", c_short), ("sThumbLY", c_short),
        ("sThumbRX", c_short), ("sThumbRY", c_short),
    ]


class XINPUT_STATE(Structure):
    _fields_ = [("dwPacketNumber", c_ulong), ("Gamepad", XINPUT_GAMEPAD)]


def load_xinput():
    for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return ctypes.windll.LoadLibrary(dll + ".dll")
        except Exception:
            continue
    return None


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "fps"
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
    profile = AnalogProfile.default_racing() if which == "racing" else AnalogProfile.default_fps()

    xinput = load_xinput()
    if xinput is None:
        print("ERROR: no XInput DLL found.", file=sys.stderr)
        return 1

    def read_slot(i):
        st = XINPUT_STATE()
        if xinput.XInputGetState(i, byref(st)) == 0:  # ERROR_SUCCESS
            return st
        return None

    connected_before = {i for i in range(4) if read_slot(i) is not None}

    keymap = Keymap.load()
    mapper = AnalogMapper(profile, keymap, dry_run=False)
    mapper.start()
    time.sleep(0.3)

    connected_after = {i for i in range(4) if read_slot(i) is not None}
    new = sorted(connected_after - connected_before)
    slot = new[0] if new else (sorted(connected_after)[0] if connected_after else 0)
    print(f"[xinput] profile={profile.name}  connected slots before={sorted(connected_before)} "
          f"after={sorted(connected_after)} -> using slot {slot}")
    print(f"[xinput] running {duration:.0f}s -- PRESS KEYS NOW (feather W slowly; try A/D; tap Space).\n")
    print(f"  {'t':>5} | {'LX':>7} {'LY':>7} {'RX':>7} {'RY':>7} | {'LT':>4} {'RT':>4} | buttons")
    print("  " + "-" * 70)

    samples = []
    last = None
    t0 = time.monotonic()
    deadline = t0 + duration
    while time.monotonic() < deadline:
        st = read_slot(slot)
        if st is not None:
            g = st.Gamepad
            row = (g.sThumbLX, g.sThumbLY, g.sThumbRX, g.sThumbRY,
                   g.bLeftTrigger, g.bRightTrigger, g.wButtons)
            samples.append(row)
            changed = last is None or any(abs(row[i] - last[i]) > 1500 for i in range(4)) \
                or abs(row[4] - last[4]) > 20 or abs(row[5] - last[5]) > 20 or row[6] != last[6]
            if changed and (any(abs(v) > 1500 for v in row[:4]) or row[4] or row[5] or row[6]):
                t = time.monotonic() - t0
                print(f"  {t:5.1f} | {row[0]:7d} {row[1]:7d} {row[2]:7d} {row[3]:7d} | "
                      f"{row[4]:4d} {row[5]:4d} | 0x{row[6]:04x}")
                last = row
        time.sleep(0.04)

    mapper.stop()

    print("\n" + "=" * 70)
    print("XINPUT READBACK SUMMARY (what a game sees)")
    print("=" * 70)
    names = ["LX", "LY", "RX", "RY"]
    analog = []
    for i, nm in enumerate(names):
        vals = [abs(s[i]) for s in samples]
        mx = max(vals) if vals else 0
        inter = sum(1 for v in vals if 3000 < v < 30000)  # partial deflection
        if inter >= 3:
            analog.append(nm)
        print(f"  thumb {nm}: max|v|={mx:6d} (of 32767)   intermediate(partial) samples={inter}")
    lt = [s[4] for s in samples]; rt = [s[5] for s in samples]
    print(f"  LT max={max(lt) if lt else 0}/255 (partial={sum(1 for v in lt if 10<v<245)})  "
          f"RT max={max(rt) if rt else 0}/255 (partial={sum(1 for v in rt if 10<v<245)})")
    if any(10 < v < 245 for v in lt + rt):
        analog.append("trigger")
    print("=" * 70)
    if analog:
        print(f"  RESULT: END-TO-END ANALOG CONFIRMED via XInput on {analog}.")
        print("          A real game would see proportional input from key depth.")
        return 0
    print("  RESULT: no intermediate XInput values seen. Re-run and feather keys slowly,")
    print("          or check the slot selection above.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
