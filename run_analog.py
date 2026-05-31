"""
run_analog.py - the usable analog gamepad app (CLI).

Starts the analog engine for a chosen profile, drives a virtual Xbox 360 pad via
ViGEmBus, and shows a live one-line readout of stick/trigger/button output. Press
keys on your M1 V5 HE and a game (or Windows' joy.cpl) sees a real controller with
PROPORTIONAL sticks and triggers driven by how far you press each key.

  - F8 toggles pause globally (works from inside a game; no admin required).
  - Ctrl+C quits cleanly.
  - If ViGEmBus can't attach (e.g. outdated driver), it runs in dry-run mode so you
    can still see the computed output; install/upgrade ViGEmBus for real output.

Usage:
    py run_analog.py                 # fps profile, auto-detect keyboard
    py run_analog.py racing
    py run_analog.py <profile_name>  # any saved analog profile
    py run_analog.py --list-devices  # show which known HE keyboards are connected
    py run_analog.py --vid 0x3151 --pid 0x5030   # target a specific board
"""

from __future__ import annotations

import argparse
import sys
import time

from analog_mapper import (
    AnalogMapper, AnalogProfile, Keymap,
    MIN_TICK_HZ, MAX_TICK_HZ,
    ensure_defaults_exist, list_profiles, load_profile,
)
from hid_protocol import KNOWN_DEVICES, auto_detected_devices, list_present_devices
from winhotkey import start_hotkey, VK_F8


def pick_profile(name: str) -> AnalogProfile:
    if name == "fps":
        return AnalogProfile.default_fps()
    if name == "racing":
        return AnalogProfile.default_racing()
    if name in list_profiles():
        return load_profile(name)
    print(f"[warn] profile '{name}' not found; using analog_fps. "
          f"Available: {list_profiles()}")
    return AnalogProfile.default_fps()


def fmt_bar(v: float, width: int = 8) -> str:
    """Tiny signed bar for a -1..1 value."""
    n = int(round(abs(v) * width))
    fill = "#" * n + "-" * (width - n)
    return fill


def _hex_or_int(s: str) -> int:
    """argparse type: accept 0x-prefixed hex ('0x3151') or plain decimal."""
    return int(s, 0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="keypad-gamepad analog CLI runner.")
    p.add_argument("profile", nargs="?", default="fps",
                   help="profile: 'fps', 'racing', or any saved analog profile name")
    p.add_argument("--vid", type=_hex_or_int, default=None,
                   help="target a specific keyboard VID (e.g. 0x3151); default = auto-scan")
    p.add_argument("--pid", type=_hex_or_int, default=None,
                   help="target a specific keyboard PID (e.g. 0x5030); default = auto-scan")
    p.add_argument("--list-devices", action="store_true",
                   help="list known HE keyboards and whether each is connected, then exit")
    p.add_argument("--hz", type=int, default=None,
                   help="virtual pad output rate in Hz (50-1000, default 1000); "
                        "overrides the profile's saved rate for this run")
    return p.parse_args(argv)


def list_devices_command() -> int:
    present = {(d.vid, d.pid) for d in list_present_devices()}
    print("Known HE keyboards (auto-scan order):")
    for d in KNOWN_DEVICES:
        hit = (d.vid, d.pid) in present
        print(f"  [{'x' if hit else ' '}] {d}  -- {'connected' if hit else 'not found'}")
    extra = auto_detected_devices()
    if extra:
        print("\nAuto-detected by vendor signature (unlisted model/dongle, treated as unverified):")
        for d in extra:
            print(f"  [x] {d}  -- connected")
    if not present:
        print("\nNothing connected. Plug in via USB-C / pair the dongle, or pass --vid/--pid "
              "for a board on another vendor id.")
    return 0


def main() -> int:
    args = parse_args()
    if args.list_devices:
        return list_devices_command()
    if (args.vid is None) != (args.pid is None):
        print("ERROR: pass --vid and --pid together (or neither, to auto-scan).", file=sys.stderr)
        return 2

    ensure_defaults_exist()
    profile = pick_profile(args.profile)
    if args.hz is not None:
        profile.tick_hz = max(MIN_TICK_HZ, min(MAX_TICK_HZ, args.hz))

    try:
        keymap = Keymap.load()
    except FileNotFoundError:
        print("ERROR: discovered_keymap.json not found. Run tools/stage2_discover.py first.",
              file=sys.stderr)
        return 1

    # Let the engine attach (with retries) and fall back to dry-run on its own;
    # a separate pre-check would create+destroy a target and provoke the very
    # transient attach failure we want to avoid.
    mapper = AnalogMapper(profile, keymap, dry_run=False, vid=args.vid, pid=args.pid)

    print("=" * 60)
    print(f" keypad-gamepad ANALOG  |  profile: {profile.name}  |  {profile.tick_hz} Hz")
    print("=" * 60)
    if mapper.dry_run:
        print(" [!] ViGEmBus not attachable -> DRY-RUN (no real pad output).")
        if mapper.vigem_error:
            print(f"     reason: {mapper.vigem_error}")
        print("     Install/upgrade ViGEmBus (1.22.0) for real gamepad output.")
    else:
        print(" [ok] Virtual Xbox 360 pad attached via ViGEmBus.")
    if mapper.unresolved_labels:
        print(f" [!] bindings with no discovered key_index: {mapper.unresolved_labels}")
    print(f" bindings: {profile.bindings}")
    print(" F8 = pause/resume   |   Ctrl+C = quit")
    print("-" * 60)

    try:
        mapper.start()
    except RuntimeError as e:
        print(f"\n [x] {e}")
        mapper.stop()
        return 1
    dev = mapper.monitor.device
    if dev is not None:
        print(f" [ok] Keyboard: {dev}")
    f8_ok = start_hotkey(VK_F8, mapper.toggle_enabled, lambda: not mapper.running)
    if not f8_ok:
        print(" [!] couldn't register F8 hotkey (pause via Ctrl+C still works).")

    try:
        while True:
            st = mapper.last_state or {}
            paused = "" if mapper.enabled else "  [PAUSED]"
            line = (f"\r L[{fmt_bar(st.get('lx',0))}|{fmt_bar(st.get('ly',0))}] "
                    f"R[{fmt_bar(st.get('rx',0))}|{fmt_bar(st.get('ry',0))}] "
                    f"LT{st.get('lt',0):.2f} RT{st.get('rt',0):.2f} "
                    f"btn:{','.join(st.get('buttons', [])) or '-':<12}"
                    f"{paused}   ")
            sys.stdout.write(line)
            sys.stdout.flush()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n[exit] stopping...")
    finally:
        mapper.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
