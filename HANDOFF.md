# Handoff to Claude Code

This is a handoff doc for continuing work on a custom "analog gamepad mode" for the **MonsGeek M1 V5 HE** keyboard on Windows. Read this first, then the linked docs.

## Project goal

The MonsGeek M1 V5 HE is a Hall Effect keyboard whose firmware deliberately omits Gamepad Mode (unlike its siblings M1W HE / M1W V3 HE). MonsGeek claimed it was an Xbox licensing issue, but that's at best a half-truth — XInput needs a license, generic HID gamepad / ViGEm-emulated Xbox 360 does not.

We want to build a Windows host app that:
1. Reads the keyboard's **real-time analog key depth** (the keyboard exposes this over HID — see protocol doc below).
2. Maps depth values per-key to gamepad outputs (left stick, right stick, triggers, buttons).
3. Emulates a virtual Xbox 360 controller via **ViGEmBus** so any XInput game sees a real gamepad.

End user is **the project owner** — a competent developer, so don't over-explain basics, but do flag tradeoffs and unknowns explicitly.

## What's already in this directory

| File | Status | Purpose |
|---|---|---|
| `README.md` | Done | High-level project intro and digital-mapper instructions. |
| `requirements.txt` | Done (digital path) | `vgamepad`, `keyboard`, `mouse`. The analog path will add `hid`. |
| `mapper.py` | Done, untested on hardware | Digital keyboard→gamepad mapper. Reads OS-level keypresses (no analog). Useful fallback; ramp + walk-modifier features approximate analog feel from digital input. |
| `gui.py` | Done, untested on hardware | Tkinter GUI for `mapper.py`. Profile editor, binding table, tuning sliders, F8 pause hotkey. |
| `SCOPING.md` | **READ THIS** | Full scoping doc for the analog path. Architecture, code sketches, risks, and a stage plan. |
| `stage1_probe.py` | Ready to run | The next thing to execute. ~150-line throwaway that enables depth monitoring and dumps events to console. **This is the de-risking step.** |

## Critical context you must know

### The reverse engineering is already done

**[echtzeit-solutions/monsgeek-akko-linux](https://github.com/echtzeit-solutions/monsgeek-akko-linux)** is a Linux userspace driver that reverse-engineered the entire MonsGeek HID protocol — including the M1 V5 HE specifically (VID `0x3151`, PID `0x5030` wired). Their `docs/PROTOCOL.md` is the source of truth for everything in `SCOPING.md`.

**You should consult their Rust source code directly when stuck.** Specifically:
- `protocol/` — the wire-level protocol primitives
- `iot_driver_linux/src/` — the driver itself, including key index → physical key mapping (one of our open questions)
- `tests/` — example HID transactions you can compare against

Their license is GPL-3.0. We're producing independent code in Python/Windows so the license doesn't transitively bind us, but if you copy any of their data tables (keymaps, command codes) wholesale, attribute and respect the license.

### The protocol in 30 seconds

- Connect to interface with `usage_page=0xFFFF, usage=0x02` on the keyboard's HID composite device.
- Send a one-shot HID Feature Report `[0x1B, 0x01, 0, 0, 0, 0, 0, checksum]` to enable depth monitoring.
- Read Input Reports on a separate HID interface. Events with `event_type == 0x1B` carry analog depth: `[report_id, 0x1B, depth_lo, depth_hi, key_index]`.
- Depth is a 16-bit unsigned int. Range ~0–400+ depending on switch calibration.
- Checksum is `255 - (sum of first 7 bytes & 0xFF)` for "Bit7" commands (the default). Verified working with the protocol doc's F7=0x08 example.

Full reference: `SCOPING.md` and the linked PROTOCOL.md.

### What I (the previous Claude) got wrong

In the early conversation I asserted that the M1 V5 HE didn't expose analog depth — that all analog processing happened on-MCU and only digital keypresses reached the host. **This was wrong.** I corrected it explicitly to the owner once I found the protocol doc. If you find any stale optimism or pessimism in older artifacts, default to what `SCOPING.md` says — it's the most recent and most accurate view.

## Where to pick up

**The literal next step is to run `stage1_probe.py` on the owner's Windows machine.** It will either:

1. **Print depth events** when keys are pressed → green light. Move to Stage 2 (key index discovery wizard) per the plan in `SCOPING.md`.
2. **Print nothing** → diagnostic mode. The script already lists all enumerated HID interfaces on startup and tells the user what to try (run as Admin, close MonsGeek Driver app, try other interfaces). Likely failure modes:
   - Wrong input interface auto-picked. The script picks the first non-config interface with a vendor usage page; this is a heuristic. May need to enumerate and try each.
   - MonsGeek Driver app is running and has claimed the vendor interface exclusively.
   - `hidapi` on Windows behaves differently for feature reports than on Linux. Section 3.4 of the protocol doc covers Linux hidraw quirks; Windows may have its own.
   - Firmware version too old. Check with GET_REV (0x80).

**Do not build Stages 2-5 before Stage 1 succeeds.** The whole point of Stage 1 is to de-risk the assumption that this works on Windows hidapi at all. If it fails, fixing the probe is the next priority — don't move on to GUI work with an unverified protocol.

## How to interact with the owner

- He has the keyboard in front of him; you don't. Ask him to run commands and paste output when you need real-world data.
- He's pragmatic — happy to debug things, doesn't want hand-holding on basics, does want clear flagging of tradeoffs.
- Earlier turns of the conversation established that he picked "Build host-side digital→gamepad mapper" first, then "Read protocol docs and scope out a Windows analog version" — that's how we got to the current state. He's invested in the analog path now; don't relitigate the digital-vs-analog tradeoff unless something major changes.
- If you discover that something in `SCOPING.md` is wrong, tell him directly. Don't bury corrections.

## Stage plan (from SCOPING.md, for quick reference)

| Stage | Deliverable | Effort |
|---|---|---|
| **1** | **`stage1_probe.py` — dumps depth events. Confirms protocol works on his keyboard.** | ~1h |
| 2 | Key discovery wizard — press each binding key, record its `key_index`. | ~2h |
| 3 | Mapping engine — `{key_index: depth}` dict → ViGEm gamepad state. Reuse profile system from `mapper.py`. | ~4h |
| 4 | GUI — bindings table, calibration UI, dead-zone tuning, profile switcher. | ~4h |
| 5 | Polish — re-enable depth monitoring after sleep/wake, single-exe packaging, tray icon. | ~3h |

## Open questions to keep in mind

1. **Key index → physical key mapping.** Protocol doc doesn't list this for the M1 V5 specifically. Either look it up in echtzeit-solutions' Rust source, or build it via the discovery wizard.
2. **Calibration values.** GET_MULTI_MAGNETISM (0xE5) sub-command 0xFE returns the keyboard's own per-key calibration values. Using these would let us skip manual calibration. Worth investigating in Stage 2 or 3.
3. **Re-enable after sleep/wake.** `SET_MAGNETISM_REPORT 0x1B` likely doesn't persist across the keyboard sleeping. The app should detect the event-stream gap and re-send the enable command.
4. **Wireless mode (PID 0x503A).** Protocol doc says 220ms RF round-trip for forwarded commands — too slow for gaming. Recommend wired-only for v1; wireless is a stretch goal.

## Suggested first message to the owner

Something like: "I've read the handoff and SCOPING.md. Ready to run `stage1_probe.py` on your keyboard whenever you are — make sure the MonsGeek Driver app is closed first, then run it from an Administrator PowerShell. Paste me the output and we'll go from there."

Don't rebuild things that already work. Don't write a new scoping doc — `SCOPING.md` is the spec. Do read echtzeit-solutions' protocol doc and source code as needed; it's the authoritative reference.

Good luck.
