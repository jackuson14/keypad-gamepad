"""
Stage 3 test harness.

Runs the AnalogMapper engine against the real keyboard in dry_run mode (no ViGEm
output needed) and samples the computed gamepad state, so we can confirm that key
DEPTH produces PROPORTIONAL output -- the whole point of the analog path.

It logs a row whenever the state changes meaningfully, then prints a summary that
calls out how many "intermediate" (partially-deflected) samples each axis saw.
Lots of intermediate values on an axis == genuine analog, not binary.

Usage:
    py tools/stage3_test.py [fps|racing] [duration_seconds]
"""

from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from analog_mapper import AnalogProfile, AnalogMapper, Keymap  # noqa: E402

AXES = ["lx", "ly", "rx", "ry", "lt", "rt"]


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "fps"
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    profile = AnalogProfile.default_racing() if which == "racing" else AnalogProfile.default_fps()

    try:
        keymap = Keymap.load()
    except FileNotFoundError:
        print("ERROR: discovered_keymap.json not found -- run tools/stage2_discover.py first.",
              file=sys.stderr)
        return 1

    mapper = AnalogMapper(profile, keymap, dry_run=True)
    print(f"[stage3] profile={profile.name}  dry_run=True (no ViGEm output)")
    print(f"[stage3] bindings: {profile.bindings}")
    if mapper.unresolved_labels:
        print(f"[stage3] WARNING unresolved (not in keymap): {mapper.unresolved_labels}")
    print(f"[stage3] runtime key_index->(target,deadzone,max): {mapper._runtime}")

    try:
        mapper.start()
    except Exception as e:
        print(f"ERROR starting engine: {e}", file=sys.stderr)
        return 3

    print(f"\n[stage3] running {duration:.0f}s -- PRESS KEYS NOW.")
    print("         Suggested: press W slowly fully down then release (watch the ramp),")
    print("         repeat with A/D, then tap SPACE / E.\n")
    print(f"  {'t':>5} | {'lx':>6} {'ly':>6} {'rx':>6} {'ry':>6} {'lt':>6} {'rt':>6} | buttons")
    print("  " + "-" * 64)

    samples: list[dict] = []
    last_print = None
    t0 = time.monotonic()
    deadline = t0 + duration
    while time.monotonic() < deadline:
        st = dict(mapper.last_state) if mapper.last_state else {}
        if st:
            samples.append(st)
            changed = (
                last_print is None
                or any(abs(st.get(a, 0) - last_print.get(a, 0)) > 0.04 for a in AXES)
                or st.get("buttons") != last_print.get("buttons")
            )
            interesting = any(abs(st.get(a, 0)) > 0.02 for a in AXES) or st.get("buttons")
            if changed and interesting:
                t = time.monotonic() - t0
                print(f"  {t:5.1f} | "
                      + " ".join(f"{st.get(a,0):6.2f}" for a in AXES)
                      + f" | {','.join(st.get('buttons', []))}")
                last_print = st
        time.sleep(0.03)

    mapper.stop()

    # --- summary ---
    print("\n" + "=" * 64)
    print("STAGE 3 SUMMARY")
    print("=" * 64)
    mon = mapper.monitor
    print(f"  reader reads: total={mon.reads_total}  non-empty={mon.reads_nonempty}  "
          f"event_types={[hex(t) for t in sorted(mon.last_event_types)] or 'none'}")
    print(f"  depth events received: {mon.event_count}")
    print(f"  samples captured:      {len(samples)}")
    print(f"  {'axis':>4} | {'max|v|':>7} | {'intermediate samples (0.05<|v|<0.95)':>38}")
    print(f"  {'-'*4}-+-{'-'*7}-+-{'-'*38}")
    analog_axes = []
    for a in AXES:
        vals = [abs(s.get(a, 0)) for s in samples]
        mx = max(vals) if vals else 0.0
        inter = sum(1 for v in vals if 0.05 < v < 0.95)
        if inter >= 3:
            analog_axes.append(a)
        print(f"  {a:>4} | {mx:7.2f} | {inter:>38}")
    buttons_seen = sorted({b for s in samples for b in s.get("buttons", [])})
    print(f"  buttons seen: {buttons_seen or 'none'}")
    print("=" * 64)
    if analog_axes:
        print(f"  RESULT: ANALOG CONFIRMED on axes {analog_axes} -- depth drives proportional output.")
        return 0
    print("  RESULT: no clear intermediate values. Either keys weren't feathered slowly,")
    print("          or only full presses/taps were used. Re-run and press W *slowly*.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
