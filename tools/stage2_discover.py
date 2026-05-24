"""
Stage 2: key-index discovery.

The keyboard reports analog depth tagged with a `key_index` (byte 4 of each 0x1B
event), but nothing tells us which physical key each index is. The protocol doc
doesn't list the table for the M1 V5, and it could change if switches are
reordered -- so we discover it empirically.

Two modes:

  SEQUENCE mode (driven, default when --sequence is given)
      You name the keys in order; the user presses them one at a time, fully, in
      that order. The tool segments the depth stream into per-key "press bursts",
      takes the distinct key_index values in first-press order, and zips them to
      your labels. Writes a keymap JSON (label -> key_index, plus observed peak
      depth per key for calibration).

      py tools/stage2_discover.py --sequence "w,a,s,d,space" --duration 40

  LIVE mode (no --sequence)
      Streams and prints each press burst (key_index + peak depth) as it closes.
      Handy to run interactively and just watch indices. Ctrl+C to stop.

      py tools/stage2_discover.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Project root on sys.path + hidapi.dll on the DLL search path.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
if hasattr(os, "add_dll_directory") and os.path.isdir(_ROOT):
    os.add_dll_directory(_ROOT)

import hid  # noqa: E402
from stage1_probe import VID, PID, build_feature_command, find_devices  # noqa: E402

# Press-detection thresholds. Full press is ~720 on this keyboard, so these are
# generous: a key counts as "pressed" once it passes HI, and "released" below LO.
THRESH_HI = 250
THRESH_LO = 80
DEFAULT_OUT = os.path.join(_ROOT, "discovered_keymap.json")


class Burst:
    __slots__ = ("key_index", "peak", "start", "end")

    def __init__(self, key_index: int, depth: int, t: float):
        self.key_index = key_index
        self.peak = depth
        self.start = t
        self.end = t

    def update(self, depth: float, t: float) -> None:
        self.peak = max(self.peak, depth)
        self.end = t


def open_devices():
    config_info, input_info = find_devices()
    config_dev = hid.Device(path=config_info["path"])
    config_dev.send_feature_report(build_feature_command([0x1B, 0x01]))
    try:
        config_dev.get_feature_report(0, 65)
    except Exception:
        pass
    input_dev = hid.Device(path=input_info["path"])
    return config_dev, input_dev


def close_devices(config_dev, input_dev) -> None:
    try:
        config_dev.send_feature_report(build_feature_command([0x1B, 0x00]))
    except Exception:
        pass
    try:
        input_dev.close()
    except Exception:
        pass
    try:
        config_dev.close()
    except Exception:
        pass


def stream_bursts(input_dev, duration: float | None, on_close=None) -> list[Burst]:
    """Stream depth events, segment into press bursts. Returns closed bursts in order.

    If `duration` is None, runs until KeyboardInterrupt.
    `on_close(burst)` is called as each burst completes (for live printing).
    """
    open_bursts: dict[int, Burst] = {}   # key_index -> in-progress burst
    closed: list[Burst] = []
    deadline = None if duration is None else time.monotonic() + duration
    try:
        while deadline is None or time.monotonic() < deadline:
            data = input_dev.read(64, timeout=200)
            if not data or len(data) < 5:
                continue
            if data[0] == 0x05:
                event_type, b2, b3, b4 = data[1], data[2], data[3], data[4]
            else:
                event_type, b2, b3, b4 = data[0], data[1], data[2], data[3]
            if event_type != 0x1B:
                continue
            depth = b2 | (b3 << 8)
            key_index = b4
            now = time.monotonic()
            b = open_bursts.get(key_index)
            if b is None:
                if depth >= THRESH_HI:
                    open_bursts[key_index] = Burst(key_index, depth, now)
            else:
                b.update(depth, now)
                if depth <= THRESH_LO:
                    closed.append(b)
                    del open_bursts[key_index]
                    if on_close:
                        on_close(b)
    except KeyboardInterrupt:
        pass
    # Close any still-held keys.
    for b in open_bursts.values():
        closed.append(b)
        if on_close:
            on_close(b)
    closed.sort(key=lambda b: b.start)
    return closed


def distinct_in_order(bursts: list[Burst]) -> list[int]:
    seen: list[int] = []
    for b in bursts:
        if b.key_index not in seen:
            seen.append(b.key_index)
    return seen


def peak_by_key(bursts: list[Burst]) -> dict[int, int]:
    peaks: dict[int, int] = {}
    for b in bursts:
        peaks[b.key_index] = max(peaks.get(b.key_index, 0), int(b.peak))
    return peaks


def main() -> int:
    ap = argparse.ArgumentParser(description="M1 V5 HE key-index discovery")
    ap.add_argument("--sequence", help="comma-separated key labels in press order, e.g. 'w,a,s,d,space'")
    ap.add_argument("--duration", type=float, default=40.0, help="capture window seconds (sequence mode)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output keymap JSON path")
    args = ap.parse_args()

    try:
        config_dev, input_dev = open_devices()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR opening devices (admin needed?): {e}", file=sys.stderr)
        return 3

    print("[stage2] monitoring ON")

    if not args.sequence:
        # LIVE mode
        print("[stage2] LIVE mode -- press keys; each completed press prints its index. Ctrl+C to stop.\n")
        def show(b: Burst):
            print(f"  press: key_index={b.key_index:>3}  peak_depth={int(b.peak):>4}")
        stream_bursts(input_dev, None, on_close=show)
        close_devices(config_dev, input_dev)
        return 0

    # SEQUENCE mode
    labels = [s.strip() for s in args.sequence.split(",") if s.strip()]
    print(f"[stage2] SEQUENCE mode: expecting {len(labels)} keys in this order:")
    print(f"         {labels}")
    print(f"[stage2] capturing for {args.duration:.0f}s -- press each key ONCE, fully, in order,")
    print(f"         pausing ~1s between keys. PRESS NOW.\n")

    bursts = stream_bursts(input_dev, args.duration)
    close_devices(config_dev, input_dev)
    print("[stage2] monitoring OFF\n")

    order = distinct_in_order(bursts)
    peaks = peak_by_key(bursts)

    print(f"[stage2] {len(bursts)} press bursts detected, in order:")
    for i, b in enumerate(bursts):
        print(f"    #{i+1:>2}  key_index={b.key_index:>3}  peak={int(b.peak):>4}")
    print(f"[stage2] distinct key indices in first-press order: {order}")
    print()

    if len(order) != len(labels):
        print("=" * 64)
        print(f"  MISMATCH: you named {len(labels)} keys but {len(order)} distinct keys were pressed.")
        print(f"  labels : {labels}")
        print(f"  indices: {order}")
        print("  Not writing a keymap. Re-run and press exactly the named keys, once each,")
        print("  fully (peak should exceed ~250), one at a time with a clear pause between.")
        print("=" * 64)
        return 2

    by_label = {label: idx for label, idx in zip(labels, order)}
    calibration = {str(idx): {"max": peaks.get(idx, 0)} for idx in order}
    out = {
        "device": {"vid": VID, "pid": PID},
        "by_label": by_label,
        "calibration": calibration,
        "note": "max = observed peak depth during discovery; refine with a dedicated calibration pass.",
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)

    print("=" * 64)
    print("  DISCOVERED KEYMAP")
    print("=" * 64)
    for label, idx in by_label.items():
        print(f"    {label:>8}  ->  key_index {idx:>3}   (peak depth {peaks.get(idx,0)})")
    print("=" * 64)
    print(f"  written to: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
