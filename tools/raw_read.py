"""
raw_read.py - dump RAW frames from an HE keyboard's vendor input interface.

Unlike stage1_capture (which only counts *decoded* 0x1B depth events), this prints
every non-empty frame as hex. That tells the difference between three cases that
all look like "no depth" otherwise:
  - the interface delivers ZERO frames  -> nothing is being forwarded (e.g. a 2.4GHz
    dongle that doesn't relay vendor input over the radio), or no key was pressed;
  - frames arrive but not as 0x1B/report-0x05 -> the dongle reframes depth telemetry;
  - frames arrive in the expected shape -> decode bug elsewhere.

It also re-sends the enable feature report periodically, in case the dongle needs
re-arming after a wake/handshake.

    py tools/raw_read.py --vid 0x3151 --pid 0x5038 --seconds 30
"""

from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
if hasattr(os, "add_dll_directory") and os.path.isdir(_ROOT):
    os.add_dll_directory(_ROOT)

import hid  # noqa: E402
from stage1_probe import build_feature_command, find_devices, VID, PID  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Dump raw vendor-input frames from an HE keyboard.")
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=VID)
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=PID)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--reenable-every", type=float, default=3.0,
                    help="re-send the enable feature report every N seconds (0 = once)")
    args = ap.parse_args()

    try:
        config_info, input_info = find_devices(args.vid, args.pid)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"[raw] VID:PID={args.vid:04x}:{args.pid:04x}  "
          f"config=if{config_info.get('interface_number')}  input=if{input_info.get('interface_number')}")
    cfg = hid.Device(path=config_info["path"])
    enable = build_feature_command([0x1B, 0x01])
    cfg.send_feature_report(enable)
    try:
        rb = bytes(cfg.get_feature_report(0, 65)[:12]).hex(" ")
    except Exception as e:
        rb = f"(readback failed: {e})"
    print(f"[raw] sent enable 0x1B 0x01; feature readback: {rb}")
    print(f"[raw] reading {args.seconds:.0f}s -- PRESS W A S D NOW (slowly, fully down then release)\n")

    inp = hid.Device(path=input_info["path"])
    start = time.monotonic()
    deadline = start + args.seconds
    last_reenable = start
    frames = 0
    shown = 0
    distinct: set[str] = set()

    while time.monotonic() < deadline:
        try:
            data = inp.read(64, timeout=200)
        except Exception as e:
            print(f"[raw] read error: {e}")
            break
        now = time.monotonic()
        if args.reenable_every and (now - last_reenable) >= args.reenable_every:
            try:
                cfg.send_feature_report(enable)
            except Exception:
                pass
            last_reenable = now
        if data:
            frames += 1
            sig = bytes(data[:2]).hex()
            distinct.add(sig)
            if shown < 50:
                print(f"  +{now - start:5.1f}s  len={len(data):2d}  {bytes(data[:20]).hex(' ')}")
                shown += 1

    try:
        cfg.send_feature_report(build_feature_command([0x1B, 0x00]))
    except Exception:
        pass
    inp.close()
    cfg.close()

    print(f"\n[raw] total non-empty frames: {frames}")
    print(f"[raw] distinct first-2-byte signatures: {sorted(distinct) or 'none'}")
    if frames == 0:
        print("[raw] VERDICT: the vendor input interface delivered ZERO frames.")
        print("      If keys were definitely pressed, the dongle isn't forwarding vendor")
        print("      input over 2.4GHz -- analog depth needs the wired (USB-C) connection.")
        return 2
    print("[raw] VERDICT: frames ARE arriving -- decode the hex above to map the dongle format.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
