"""
Stage 1: M1 V5 HE depth-monitoring probe.

The whole point of this script is to ANSWER ONE QUESTION before we build anything:
    "Does the M1 V5 HE stream analog key-depth events to a Python host on Windows?"

If yes → green light for the Windows analog gamepad project.
If no  → we find out now, before sinking time into a real GUI app.

Run as Administrator (hidapi feature reports on some Windows configs need elevation).

Usage:
    pip install hid    # binds to hidapi DLL; on Windows it ships with the wheel
    python stage1_probe.py

Expected output if it works:
    [info] config dev opened: path=...
    [info] input  dev opened: path=...
    [info] sent enable command (0x1B, 0x01), reply: aa ...
    [info] streaming depth events (Ctrl+C to stop)...
    key=41  depth=  15
    key=41  depth= 361
    key=41  depth=   0
    ...
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# The `hid` package is a pure-Python ctypes wrapper and needs the native
# hidapi.dll, which we vendor next to this script (see tools/fetch_hidapi.ps1).
# Python 3.8+ does NOT search cwd/PATH for ctypes DLLs, so register the dir
# explicitly before importing hid, or the import fails with "Unable to load...".
_HERE = os.path.dirname(os.path.abspath(__file__))
if hasattr(os, "add_dll_directory") and os.path.isdir(_HERE):
    os.add_dll_directory(_HERE)

try:
    import hid  # `pip install hid` (bindings to libhidapi)
except ImportError as e:
    print(f"ERROR: could not load the hid module / hidapi.dll: {e}", file=sys.stderr)
    print("  - Ensure hidapi.dll (x64) sits next to this script "
          "(run: powershell -ExecutionPolicy Bypass -File tools\\fetch_hidapi.ps1)",
          file=sys.stderr)
    print("  - Ensure the `hid` package is installed (pip install -r requirements.txt)",
          file=sys.stderr)
    sys.exit(1)


# From the protocol doc, section 7.2
VID, PID = 0x3151, 0x5030  # M1 V5 HE wired


def checksum_bit7(payload: bytes) -> int:
    """Pad-to-8 + invert-sum-of-first-7-bytes checksum, used by all standard commands."""
    return (255 - (sum(payload[:7]) & 0xFF)) & 0xFF


def build_feature_command(cmd_bytes: list[int]) -> bytes:
    """Build a 65-byte HID Feature Report: [report_id=0][cmd+params][checksum][padding]."""
    # Pad command part to 8 bytes if shorter
    msg = list(cmd_bytes) + [0] * max(0, 8 - len(cmd_bytes))
    # Compute checksum at offset 7 (within the 8-byte command, BEFORE the report ID)
    msg[7] = checksum_bit7(bytes(msg[:7]))
    # Prepend Report ID 0 and pad to 65 bytes total
    full = [0] + msg + [0] * (65 - 1 - len(msg))
    return bytes(full)


def find_devices(vid: int = VID, pid: int = PID) -> tuple[dict, dict]:
    """Find the two HID interfaces we need on an HE keyboard.

    Defaults to the M1 V5 HE; pass vid/pid to probe a different board (e.g. another
    MonsGeek/Akko model). Returns (config_info, input_info) — both dicts from
    hid.enumerate(). Raises if the keyboard isn't found.
    """
    all_ifaces = list(hid.enumerate(vid, pid))
    if not all_ifaces:
        raise RuntimeError(
            f"No device with VID:PID={vid:04x}:{pid:04x} found.\n"
            f"  - Is the keyboard plugged in via USB-C, or paired via the 2.4GHz dongle?\n"
            f"  - The M1 V5 dongle enumerates as PID 0x5038 (wired is 0x5030)."
        )

    print(f"[info] found {len(all_ifaces)} HID interfaces on the keyboard:")
    for d in all_ifaces:
        print(f"    interface={d.get('interface_number', '?'):>2}  "
              f"usage_page=0x{d.get('usage_page', 0):04x}  "
              f"usage=0x{d.get('usage', 0):04x}  "
              f"path={d.get('path', b'?').decode(errors='replace')[:80]}")

    # Config interface: vendor usage page 0xFFFF, usage 0x02 (per protocol doc 2.1)
    config = next(
        (d for d in all_ifaces if d.get("usage_page") == 0xFFFF and d.get("usage") == 0x02),
        None,
    )
    if config is None:
        raise RuntimeError("Couldn't find vendor config interface (usage_page=0xFFFF, usage=0x02)")

    # Input interface: the doc says events go on interface 1 (or vendor input report ID 0x05).
    # On Windows, multi-collection HID devices expose each Top Level Collection as
    # a separate "device". We pick the collection that's distinct from config and
    # that's most likely to carry vendor input. We'll try a few candidates if needed.
    candidates = [d for d in all_ifaces if d != config]
    if not candidates:
        raise RuntimeError("No input interface candidates found")

    # Prefer one whose usage_page suggests vendor input (0xFFxx range), else fall back
    # to the lowest-numbered non-config interface and let the user try others if needed.
    vendor_inputs = [d for d in candidates if (d.get("usage_page", 0) & 0xFF00) == 0xFF00]
    input_iface = vendor_inputs[0] if vendor_inputs else candidates[0]

    return config, input_iface


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 1: probe an HE keyboard for analog key-depth streaming.")
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=VID,
                    help=f"keyboard VID (default 0x{VID:04x}); accepts 0x-hex or decimal")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=PID,
                    help=f"keyboard PID (default 0x{PID:04x}); accepts 0x-hex or decimal")
    args = ap.parse_args()

    print(f"[info] looking for VID:PID={args.vid:04x}:{args.pid:04x}")
    try:
        config_info, input_info = find_devices(args.vid, args.pid)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"[info] config iface: interface={config_info.get('interface_number')} "
          f"usage=0x{config_info.get('usage', 0):04x}")
    print(f"[info] input  iface: interface={input_info.get('interface_number')} "
          f"usage=0x{input_info.get('usage', 0):04x}")

    # -- Open the config interface and send enable command --
    config_dev = hid.Device(path=config_info["path"])
    print("[info] config interface opened")

    enable_cmd = build_feature_command([0x1B, 0x01])  # depth monitoring ON
    print(f"[info] enable command: {enable_cmd[:10].hex(' ')}...")

    try:
        config_dev.send_feature_report(enable_cmd)
        print("[info] sent SET_MAGNETISM_REPORT 0x1B 0x01 (depth monitoring ON)")
    except Exception as e:
        print(f"ERROR: send_feature_report failed: {e}", file=sys.stderr)
        return 1

    # Read back the response to confirm acknowledgement.
    # Per protocol doc 3.3: byte 1 == 0xAA on success.
    try:
        # The hid lib's get_feature_report needs report_id + max length
        reply = config_dev.get_feature_report(0, 65)
        print(f"[info] reply: {reply[:8].hex(' ')}")
        if len(reply) > 1 and reply[1] == 0xAA:
            print("[info] keyboard ACKed the command")
        else:
            print(f"[warn] no 0xAA ack at byte 1, but it might still work — continuing")
    except Exception as e:
        print(f"[warn] couldn't read back the ack: {e}; continuing anyway")

    # -- Open the input interface and stream events --
    input_dev = hid.Device(path=input_info["path"])
    print(f"[info] input interface opened, streaming depth events (Ctrl+C to stop)...\n")
    print(f"  Try pressing W, A, S, D *slowly* — you should see depth values ramp up and down.")
    print(f"  Fully bottomed-out should give a depth in the hundreds (e.g. 350-450).\n")

    seen_event_types: set[int] = set()
    depth_event_count = 0
    last_status_time = time.monotonic()

    try:
        while True:
            data = input_dev.read(64, timeout=200)  # 200ms read timeout
            if not data:
                # No event in the last 200ms - check if we should print a heartbeat
                now = time.monotonic()
                if now - last_status_time > 5:
                    print(f"  ...still listening (no events in 5s — try pressing a key)")
                    last_status_time = now
                continue

            # Report ID is byte 0; on Windows hidapi may or may not include it.
            # The protocol expects vendor input with Report ID 0x05.
            # We accept both with-and-without to be robust.
            if len(data) < 5:
                continue
            if data[0] == 0x05:
                event_type, b2, b3, b4 = data[1], data[2], data[3], data[4]
            else:
                # Maybe report ID was stripped; treat data[0] as event_type
                event_type, b2, b3, b4 = data[0], data[1], data[2], data[3] if len(data) > 3 else 0

            if event_type not in seen_event_types:
                seen_event_types.add(event_type)
                print(f"[info] new event type seen: 0x{event_type:02X}")

            if event_type == 0x1B:  # KeyDepth event
                depth = b2 | (b3 << 8)
                key_index = b4
                depth_event_count += 1
                print(f"  key={key_index:>3}  depth={depth:>4}")

            # Heartbeat for non-depth events
            now = time.monotonic()
            if now - last_status_time > 5:
                print(f"  ({depth_event_count} depth events so far)")
                last_status_time = now

    except KeyboardInterrupt:
        print(f"\n[info] stopping. {depth_event_count} depth events received total.")

    # -- Cleanup: disable depth monitoring, close devices --
    print("[info] sending SET_MAGNETISM_REPORT 0x1B 0x00 (disable)")
    try:
        config_dev.send_feature_report(build_feature_command([0x1B, 0x00]))
    except Exception as e:
        print(f"[warn] couldn't disable cleanly: {e}")

    input_dev.close()
    config_dev.close()

    print()
    print("=" * 60)
    print("RESULT SUMMARY")
    print("=" * 60)
    if depth_event_count > 0:
        print(f"  ✓ Received {depth_event_count} analog depth events.")
        print(f"  ✓ Event types seen: {[hex(t) for t in sorted(seen_event_types)]}")
        print(f"  ✓ Green light for the Windows analog gamepad project.")
    else:
        print(f"  ✗ No depth events received.")
        print(f"  ✗ Event types seen: {[hex(t) for t in sorted(seen_event_types)] or 'none'}")
        print(f"  ✗ Things to try before giving up:")
        print(f"      1. Run as Administrator (right-click Python/cmd → Run as admin)")
        print(f"      2. Make sure MonsGeek Driver app is NOT running (it may grab the interface)")
        print(f"      3. Try the other non-config HID interfaces — see list at top of output")
        print(f"      4. Check firmware version; very old firmware may not support 0x1B")
    return 0 if depth_event_count > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
