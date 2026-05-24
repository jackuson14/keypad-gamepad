"""
Stage 1 capture harness (non-interactive).

Same protocol as stage1_probe.py, but instead of streaming until Ctrl+C it runs
for a fixed number of seconds, then prints a summary. This lets the depth-event
test be driven in one shot (Claude starts it; the user presses keys during the
window) and captured cleanly, rather than needing an interactive terminal.

As a bonus it records per-key min/max depth seen, which is the raw material for
the Stage 2 key-discovery wizard and per-key calibration.

Usage:
    py tools/stage1_capture.py [duration_seconds]   # default 30
"""

from __future__ import annotations

import os
import sys
import time

# Make the project root importable and put hidapi.dll on the DLL search path.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
if hasattr(os, "add_dll_directory") and os.path.isdir(_ROOT):
    os.add_dll_directory(_ROOT)

import hid  # noqa: E402
from stage1_probe import VID, PID, build_feature_command, find_devices  # noqa: E402


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    print(f"[capture] VID:PID={VID:04x}:{PID:04x}  window={duration:.0f}s")
    try:
        config_info, input_info = find_devices()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"[capture] config iface=if{config_info.get('interface_number')} "
          f"usage=0x{config_info.get('usage', 0):04x}")
    print(f"[capture] input  iface=if{input_info.get('interface_number')} "
          f"usage=0x{input_info.get('usage', 0):04x}")

    # --- open config, enable depth monitoring ---
    config_dev = hid.Device(path=config_info["path"])
    enable_cmd = build_feature_command([0x1B, 0x01])
    print(f"[capture] enable cmd: {enable_cmd[:9].hex(' ')} ...")
    try:
        config_dev.send_feature_report(enable_cmd)
        print("[capture] sent SET_MAGNETISM_REPORT 0x1B 0x01 (monitoring ON)")
    except Exception as e:
        print(f"ERROR: send_feature_report failed: {e}", file=sys.stderr)
        print("  -> often an Administrator-elevation issue on Windows hidapi.", file=sys.stderr)
        return 3

    try:
        reply = config_dev.get_feature_report(0, 65)
        print(f"[capture] ack reply: {bytes(reply[:8]).hex(' ')}"
              f"{'  (0xAA ok)' if len(reply) > 1 and reply[1] == 0xAA else '  (no 0xAA ack)'}")
    except Exception as e:
        print(f"[capture] (couldn't read ack: {e}; continuing)")

    # --- open input, stream for the window ---
    input_dev = hid.Device(path=input_info["path"])
    print(f"[capture] streaming for {duration:.0f}s -- PRESS KEYS NOW "
          f"(W A S D slowly, fully down then release)\n")

    seen_event_types: set[int] = set()
    depth_events = 0
    nonzero_events = 0
    per_key: dict[int, list[int]] = {}  # key_index -> [min, max, count]

    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        data = input_dev.read(64, timeout=200)
        if not data or len(data) < 5:
            continue
        if data[0] == 0x05:
            event_type, b2, b3, b4 = data[1], data[2], data[3], data[4]
        else:
            event_type, b2, b3, b4 = data[0], data[1], data[2], data[3]
        seen_event_types.add(event_type)
        if event_type == 0x1B:
            depth = b2 | (b3 << 8)
            key_index = b4
            depth_events += 1
            if depth > 0:
                nonzero_events += 1
            rec = per_key.get(key_index)
            if rec is None:
                per_key[key_index] = [depth, depth, 1]
            else:
                rec[0] = min(rec[0], depth)
                rec[1] = max(rec[1], depth)
                rec[2] += 1

    # --- disable + close ---
    try:
        config_dev.send_feature_report(build_feature_command([0x1B, 0x00]))
        print("\n[capture] sent 0x1B 0x00 (monitoring OFF)")
    except Exception as e:
        print(f"\n[capture] (couldn't disable cleanly: {e})")
    input_dev.close()
    config_dev.close()

    # --- summary ---
    print("=" * 64)
    print("STAGE 1 CAPTURE SUMMARY")
    print("=" * 64)
    print(f"  depth events (0x1B):  {depth_events}  ({nonzero_events} with depth>0)")
    print(f"  event types seen:     {[hex(t) for t in sorted(seen_event_types)] or 'none'}")
    if per_key:
        print(f"  distinct key indices: {len(per_key)}")
        print(f"  {'key_index':>9} | {'min':>5} | {'max':>5} | {'count':>6}")
        print(f"  {'-'*9}-+-{'-'*5}-+-{'-'*5}-+-{'-'*6}")
        for k in sorted(per_key, key=lambda k: per_key[k][1], reverse=True):
            lo, hi, cnt = per_key[k]
            print(f"  {k:>9} | {lo:>5} | {hi:>5} | {cnt:>6}")
    print("=" * 64)
    if nonzero_events > 0:
        print("  RESULT: GREEN LIGHT -- analog depth is streaming to the host.")
        return 0
    elif depth_events > 0:
        print("  RESULT: depth events arrived but all depth==0 -- keys may not have")
        print("          been pressed during the window, or the depth field decode is off.")
        return 0
    else:
        print("  RESULT: NO depth events. Try (1) Run as Administrator, (2) close the")
        print("          MonsGeek/KeyKey driver app, (3) re-run and press keys harder.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
