# Scoping: M1 V5 HE Analog Gamepad Mode on Windows

Based on reading [echtzeit-solutions/monsgeek-akko-linux PROTOCOL.md](https://github.com/echtzeit-solutions/monsgeek-akko-linux/blob/master/docs/PROTOCOL.md) end-to-end.

## TL;DR

**Feasible. Probably 1–2 weekends of work for one developer. ~600–1000 LOC. No firmware modification needed for wired USB.**

The reverse-engineering is done. We have the exact USB protocol, the exact HID interface, the exact event format for analog key depth, and a working reference implementation in Rust. All we have to do is port the *reading* side to Windows and wire it into a ViGEm output. That's the entire project.

## What we now know for certain

These are facts from the protocol doc, not guesses:

**The M1 V5 HE exposes real-time analog key depth over USB HID.** The mechanism:

1. We send a single one-shot HID Feature Report — `[0x1B, 0x01, 0, 0, 0, 0, 0, checksum]` — to enable depth monitoring. Done once at startup.
2. The keyboard then streams unsolicited Input Reports on its vendor HID interface (Report ID `0x05`), of the form `05 1B <depth_lo> <depth_hi> <key_index>`.
3. Depth is a 16-bit little-endian integer, typically ranging 0–400+. Reports arrive every 3–20ms during key movement.

For our wired M1 V5 HE that's:

- VID `0x3151`, PID `0x5030`
- Interface 2 (vendor config) for sending the enable command
- Interface 1 for reading the depth events (despite being labelled "multi-function", it carries Report ID 0x05 vendor input)

**Latency budget.** Wired SET→GET round-trip is ~1ms per the doc. Depth event stream arrives at 3–20ms intervals (i.e. 50–333Hz per key). The keyboard's polling rate is configurable up to 8000Hz. Even at the slow end, 20ms is well within the threshold where humans perceive a controller as "responsive" (industry consensus is ~30–50ms end-to-end).

**Checksum.** Bit7 mode for our purposes: pad to 8 bytes, sum bytes 0–6, store `255 - (sum & 255)` at byte 7. Trivial.

**No license / no firmware flash.** We're using the stock firmware as MonsGeek shipped it. We're just reading data they're already emitting. The "Xbox license" excuse they gave for not adding gamepad mode is irrelevant to us — we're not pretending to be an Xbox controller *from the keyboard*. We're pretending to be one *from the PC*, via ViGEmBus, which is what every key-to-pad tool does.

## What was wrong in my first response

I owe you an explicit correction. I said:

> "Analog values are computed inside the keyboard's MCU and never leave it as analog values — only as digital keypresses (after the MCU has thresholded them)."

False for this keyboard. The analog values absolutely do leave the MCU when you ask for them via `SET_MAGNETISM_REPORT 0x1B`. I assumed the worst case without checking. The reverse-engineering project proves the opposite.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Your game (Forza, Elden Ring, Hades, whatever)         │
│  Sees a real Xbox 360 controller                        │
└─────────────────────┬───────────────────────────────────┘
                      │ XInput API
                      ▼
┌─────────────────────────────────────────────────────────┐
│  ViGEmBus (Windows kernel driver)                       │
│  Creates virtual Xbox 360 device                        │
└─────────────────────┬───────────────────────────────────┘
                      │ ioctl
                      ▼
┌─────────────────────────────────────────────────────────┐
│  OUR APP (Python or Rust, Windows)                      │
│  - HID listener: subscribe to key depth events          │
│  - Mapper: depth + key_index → stick/trigger float      │
│  - ViGEm driver: push state every ~4ms                  │
└─────────────────────┬───────────────────────────────────┘
                      │ HID Feature Reports + Input Reports
                      ▼
┌─────────────────────────────────────────────────────────┐
│  MonsGeek M1 V5 HE (VID:3151 PID:5030)                  │
│  Stock firmware. Depth monitoring enabled at startup.   │
└─────────────────────────────────────────────────────────┘
```

Note the keyboard still acts as a normal keyboard simultaneously — interface 0 keeps emitting standard keypresses. This is good: typing in chat still works while the gamepad is active. The user can decide per-app whether their game reads the keyboard, the gamepad, or both.

## What the code has to do

### 1. Find and open the device

```python
import hid  # hidapi via the `hid` package on Windows

VID, PID = 0x3151, 0x5030

# Two endpoints we care about:
#   Interface 2 (usage_page=0xFFFF, usage=0x02) — for sending Feature Reports
#   Interface 1 (Report ID 0x05) — for receiving depth events
config_dev = next(d for d in hid.enumerate(VID, PID)
                  if d['usage_page'] == 0xFFFF and d['usage'] == 0x02)
input_dev = next(d for d in hid.enumerate(VID, PID)
                 if d['interface_number'] == 1)
```

On Windows, hidapi exposes each HID collection as a separate device path, so we open them independently. (This is messier than Linux's single hidraw node, but it's manageable.)

### 2. Enable depth monitoring

```python
def checksum_bit7(payload: bytes) -> int:
    return (255 - (sum(payload[:7]) & 0xFF)) & 0xFF

def send_feature(dev, cmd_bytes: list[int]) -> None:
    # Pad to 8, checksum, pad to 64, prepend Report ID 0
    msg = cmd_bytes + [0] * (8 - len(cmd_bytes))
    msg[7] = checksum_bit7(bytes(msg))
    msg = [0] + msg + [0] * (64 - len(msg))  # Report ID 0
    dev.send_feature_report(bytes(msg))

# Enable: [0x1B, 0x01, 0, 0, 0, 0, 0, checksum]
send_feature(config_dev, [0x1B, 0x01, 0, 0, 0, 0, 0])
```

### 3. Listen for depth events

```python
# Stream events from interface 1
while running:
    data = input_dev.read(64, timeout_ms=100)
    if not data or data[0] != 0x05:
        continue
    event_type = data[1]
    if event_type == 0x1B:  # KeyDepth event
        depth = data[2] | (data[3] << 8)
        key_index = data[4]
        on_depth(key_index, depth)
```

### 4. Maintain per-key depth state and compute gamepad state

This is where the actual mapping work lives. The keyboard sends *deltas* — only the keys that changed. We keep a `dict[int, int]` of `{key_index: current_depth}` and recompute the gamepad state on every event (or on a fixed 250Hz timer, whichever we prefer).

**Calibration.** The doc says depth values "typically range 0-400+". The actual max depends on the switch and its calibration. We need a one-time calibration step — press each mapped key fully, record the max depth — to know what "100%" means. Without this, the stick won't reach full deflection.

There's also the GET_MULTI_MAGNETISM 0xE5 command with sub-command 0xFE (CALIBRATION) which returns the raw per-key calibration values the keyboard itself uses. Using those would let us skip the manual calibration step. Worth investigating but a manual calibration UI is the safer fallback.

**Dead zones.** Switches don't trigger at depth 0; there's an unavoidable top dead zone (the magnet's resting position isn't zero). We expose a configurable per-key threshold so depths below it map to 0% stick output.

**Per-key mapping.** Same model as the digital mapper I already built — `key_index → "LSTICK_UP"`, etc. — but now the binding emits an analog value `0.0–1.0` proportional to `(depth - dead_zone) / (max_depth - dead_zone)`, clamped.

**Stick math.** Same as the digital mapper: `lstick_x = right - left`, `lstick_y = up - down`. Now those values are floats, not just 0/1.

### 5. Push to ViGEm

Identical to the digital mapper — `vgamepad.VX360Gamepad`, `left_joystick_float()`, `right_joystick_float()`, `left_trigger_float()`, `right_trigger_float()`. Already proven working in the code I wrote you.

## Key index mapping

Big known unknown: **we don't know what key_index corresponds to which physical key.** The protocol doc says key_index goes in byte 4 of the event, but doesn't list the mapping for the M1 V5 specifically. (It's a 75% layout, ~84 keys.)

Options:
- **Discovery mode in our app** — press each key in turn while the app prints the indices. The user labels them in a setup wizard. Ten-minute one-time setup.
- **Read it from echtzeit-solutions' code** — they must have figured it out for their TUI. Check `iot_driver_linux/src/` for the layout table.

I'd build the discovery mode regardless, since switch reorderings (e.g. swapping caps lock for Fn) would invalidate any hardcoded table.

## Risks and unknowns

| Risk | Likelihood | Mitigation |
|---|---|---|
| Windows hidapi behaves differently than Linux hidraw — feature report timing, blocking reads, etc. | Medium | Section 3.4 of the protocol doc already warns about this; expect retries and timing tweaks. |
| `SET_MAGNETISM_REPORT 0x1B` only persists for the current power cycle, so the keyboard stops streaming after sleep/wake. | Medium | App watches for the gap, re-sends enable when it sees no events for >2s while keys are held. |
| Key index mapping changes across firmware versions. | Low | Discovery mode handles it. |
| Wireless (PID 0x503A) has 220ms RF latency, per the doc. | Already known | Recommend wired-only mode for gaming. |
| MonsGeek pushes a firmware update that disables 0x1B for non-licensed apps. | Very low | Hasn't happened to any other open-source reverse-engineering project. We can pin firmware version if needed. |
| Polling at 8000Hz produces too many events to process. | Low | Even at 8kHz × 75 keys × on-change-only, we're talking <50k events/sec in worst case. Trivial. |

## What's NOT in scope (v1)

- Wireless/Bluetooth analog support — wired only first.
- Per-game profile auto-switching — manual switching is fine.
- A reimplementation of MonsGeek Driver's full feature set (LED config, macros, etc.) — out of scope.
- Trying to expose the gamepad mode at the keyboard level — that would require firmware modification, which we explicitly chose not to do.

## Implementation plan if you want to proceed

| Stage | Deliverable | Effort |
|---|---|---|
| 1 | Throwaway script: open device, enable 0x1B, dump depth events to console. Confirms the protocol works on your specific keyboard before we build anything. | ~1 hour |
| 2 | Key discovery wizard — press WASD + a few others, record indices. | ~2 hours |
| 3 | Mapping engine — depth dict → ViGEm gamepad state. Reuse the digital mapper's profile system. | ~4 hours |
| 4 | GUI — bindings, calibration, dead-zone tuning, profile switching. | ~4 hours |
| 5 | Polish — re-enable monitoring on sleep/wake, packaging as a single .exe, tray icon. | ~3 hours |

**Stage 1 alone is the critical risk-reducer.** If it works, the rest is a regular software project. If it doesn't — say, Windows hidapi can't reach interface 1's vendor reports for some reason — we'd know immediately and could pivot before sinking more time in.

Recommend doing Stage 1 next.
