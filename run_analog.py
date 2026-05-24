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
    py run_analog.py                 # fps profile
    py run_analog.py racing
    py run_analog.py <profile_name>  # any saved analog profile
"""

from __future__ import annotations

import sys
import time

from analog_mapper import (
    AnalogMapper, AnalogProfile, Keymap,
    ensure_defaults_exist, list_profiles, load_profile,
)
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


def main() -> int:
    ensure_defaults_exist()
    name = sys.argv[1] if len(sys.argv) > 1 else "fps"
    profile = pick_profile(name)

    try:
        keymap = Keymap.load()
    except FileNotFoundError:
        print("ERROR: discovered_keymap.json not found. Run tools/stage2_discover.py first.",
              file=sys.stderr)
        return 1

    # Let the engine attach (with retries) and fall back to dry-run on its own;
    # a separate pre-check would create+destroy a target and provoke the very
    # transient attach failure we want to avoid.
    mapper = AnalogMapper(profile, keymap, dry_run=False)

    print("=" * 60)
    print(f" keypad-gamepad ANALOG  |  profile: {profile.name}")
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

    mapper.start()
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
