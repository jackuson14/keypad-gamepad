# keypad-gamepad

A custom host-side **keyboard → Xbox 360 gamepad** mapper, built because the MonsGeek M1 V5 HE doesn't ship with a built-in Gamepad Mode (unlike the M1W HE / M1W V3 HE, which do).

Games see a real Xbox 360 controller via ViGEmBus. Works in every game that supports XInput (basically all modern games).

## Two paths: digital (this tool) and analog (in progress)

This tool — `mapper.py` / `gui.py` — is a **digital → digital** mapper. It reads your keyboard's
OS-level keypresses (binary: pressed / not pressed) and converts them into gamepad inputs.
Pressing W half-way gives you either 0% or 100% stick, not a partial tilt.

> **Correction (verified on hardware):** An earlier version of this README claimed the M1 V5 HE
> *cannot* expose analog Hall-Effect depth to the host — that "analog values are computed inside the
> MCU and never leave it." **That was wrong.** The keyboard streams real-time per-key analog depth
> over a vendor HID interface once you enable it. This is confirmed working on Windows (Python +
> hidapi, no admin needed): a Stage 1 probe received ~3,000 live depth events with values ranging
> 0 → ~720 per key. See `SCOPING.md`, `HANDOFF.md`, `stage1_probe.py`, and `tools/stage1_capture.py`.
> The **analog path** (true proportional sticks/triggers from key depth) is being built alongside this
> digital tool; the digital mapper remains as a simpler, dependency-light fallback.

**To approximate analog feel from digital input**, the digital mapper provides:

- **Stick ramp** — instead of instant 0 → 100%, the stick ramps up over N milliseconds while you hold the key. Great for racing / flight games.
- **Walk modifier** — hold Shift (or any key you configure) to make sticks deflect to 50% instead of 100%. Manual but effective for FPS sneak-walking.
- **Mouse → right stick** — mouse motion drives the right stick (with sensitivity and decay tuning). Lets you aim like a controller in games with no mouse support (Elden Ring couch co-op, etc.).

For **true analog** from the M1 V5 HE, use the analog path being built in this repo (reads real key
depth over HID — no firmware mod, no Xbox license, wired USB). Buying a Wooting / DrunkDeer /
Keychron Q1 HE with native gamepad mode is the off-the-shelf alternative if you don't want to run this.

## Analog mode (true proportional input) — verified working

The analog path reads each key's real Hall-Effect **depth** over a vendor HID interface and maps it to
proportional gamepad output: half-press W = half-tilt stick, feather a trigger-bound key = partial
throttle. Confirmed on hardware (wired M1 V5 HE, VID 0x3151 / PID 0x5030). **No Administrator needed**
for the keyboard side — unlike the digital mapper, this doesn't install OS-level keyboard hooks.

Files:

| File | Role |
|---|---|
| `hid_protocol.py` | Protocol library: device discovery, enable command, `DepthMonitor` (live `{key_index: depth}`). |
| `analog_mapper.py` | Engine: depth + calibration → analog targets → `vgamepad` Xbox 360 pad. `AnalogProfile`, `Keymap`. |
| `run_analog.py` | The usable CLI app: live readout, global **F8** pause (no admin), dry-run fallback. |
| `discovered_keymap.json` | `key_index ↔ key label` + per-key calibration (from the discovery tool). |
| `hidapi.dll` | Native backend for the `hid` package (vendored; fetch via `tools/fetch_hidapi.ps1`). |
| `tools/stage1_capture.py` | De-risk probe: confirm depth events stream. |
| `tools/stage2_discover.py` | Key-index discovery (sequence or live mode). |
| `tools/stage3_test.py` | Validate depth→analog math against hardware (dry-run). |

### Setup

1. **ViGEmBus 1.22.0+** (Windows 11 needs a current build — the 2020-era 1.17 fails with
   `VIGEM_ERROR_TARGET_NOT_PLUGGED_IN`): install from <https://github.com/nefarius/ViGEmBus/releases/latest>.
2. `pip install -r requirements.txt`
3. `powershell -ExecutionPolicy Bypass -File tools\fetch_hidapi.ps1` (vendors `hidapi.dll`).
4. Discover your keymap: `py tools/stage2_discover.py --sequence "w,a,s,d,e,r,shift,space"` (press the named
   keys once each, in order). Writes `discovered_keymap.json`.

### Run — GUI (recommended)

```powershell
py analog_gui.py
```

A control panel with: a ViGEmBus status banner, profile switcher, an editable bindings table
(`label → key_index → calibration max → target`), an in-app **Learn key** wizard (press a key, it
captures the `key_index` and full-press depth — no hardcoded table), global dead-zone / button-threshold
tuning, a **live preview** (stick dots + trigger bars that move with your key depth, even before you start
output), **Start/Stop output**, global **F8 pause**, and **minimize-to-tray** (if `pystray`/`Pillow` are
installed; degrades gracefully otherwise).

### Run — CLI

```powershell
py run_analog.py            # analog_fps profile (WASD = analog left stick)
py run_analog.py racing     # W/S = analog throttle/brake, A/D = analog steering
```

No admin required for either. F8 pauses globally (works from inside a game). Profiles live in
`~/.keypad-gamepad/analog_profiles/`. Note the analog path makes the digital mapper's `stick_ramp` and
`walk_modifier` hacks obsolete — pressing a key *lightly* IS the partial deflection.

### Build a standalone .exe

```powershell
py -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
# -> dist\keypad-gamepad-analog.exe  (single windowed file, ~20 MB)
```

The exe bundles `hidapi.dll`, vgamepad's client, and the tray backend, and seeds your keymap into
`~/.keypad-gamepad/` on first run. **ViGEmBus must still be installed separately** on the target machine
(it's a kernel driver). Verified: the frozen exe boots and attaches a virtual pad with no Python install.

## Setup (digital mapper)

### 1. Install ViGEmBus driver (one-time)

Download from <https://github.com/nefarius/ViGEmBus/releases> and run the installer. This creates the virtual gamepad device that Windows games see.

### 2. Install Python deps

```powershell
pip install -r requirements.txt
```

### 3. Run

```powershell
# Must be run as Administrator — the `keyboard` and `mouse` libraries install
# global hooks, which Windows requires admin for.
python gui.py
```

## Usage

1. Pick a profile (`default_fps` or `default_racing` come pre-loaded).
2. Edit bindings in the table (double-click a row, or use Add / Edit / Remove).
3. Tune sticks (ramp, walk deflection, mouse sensitivity).
4. Click **Start mapper**.
5. **F8** globally pauses/resumes — so you can alt-tab out of a game without uninstalling the virtual controller.

Profiles are saved to `~/.keypad-gamepad/profiles/` as JSON. Edit them by hand if you prefer.

## Architecture

```
gui.py    →  Tkinter control panel. Edits Profile objects.
                │
                ▼
mapper.py →  Mapper class polls keyboard + mouse state at 250Hz,
              computes Xbox gamepad state (sticks, triggers, buttons),
              pushes to vgamepad → ViGEmBus → Windows HID stack → game.
```

Polling at 250Hz gives 4ms granularity, which is well under the 16ms a 60Hz game can resolve. Stick ramps are smooth.

## Why not just use reWASD / JoyToKey?

You can. They do basically the same thing. This is open, free, scriptable, and you own the code — useful if you want to extend it later (e.g. per-game auto-profile switching, scripted button sequences, or eventually wire in real analog if MonsGeek ever releases SDK access).

## License

MIT, do whatever you want with it.
