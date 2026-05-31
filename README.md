# keypad-gamepad

Turn a **MonsGeek / Akko Hall-Effect keyboard** into a virtual **Xbox 360 controller with true
analog sticks and triggers**, driven by how far you press each key. It's the gamepad mode the
firmware leaves out — with no firmware mod, no Xbox license, and **no Administrator**. Wired USB
or the 2.4 GHz dongle.

Built and verified on the **MonsGeek M1 V5 HE**, but the keyboard is auto-detected: sibling HE
boards on the same vendor protocol work too — see [Other keyboards](#other-keyboards-monsgeek--akko-he).

> Half-press **W** → half-tilted stick. Feather a trigger-bound key → partial throttle. Any XInput game
> sees a real controller.

![Analog control panel](docs/analog_gui.png)

Demo

<img width="400" height="225" alt="Forza Horizon 6 2026-05-26 23-16-09" src="https://github.com/user-attachments/assets/0e777683-1dff-45b7-b592-3be8a6685788" />





## How it works

```text
key depth (HID)  →  calibration  →  virtual Xbox 360 pad (ViGEmBus)  →  your game (XInput)
```

The keyboard streams real-time per-key analog depth over a vendor HID interface; this app maps that depth
to proportional gamepad output and feeds it through ViGEmBus.

## Requirements

- Windows 10/11 and a MonsGeek/Akko HE keyboard, **wired or via its 2.4 GHz dongle**. The
  **M1 V5 HE** is verified on both (wired `0x3151:0x5030`, dongle `0x3151:0x5038`); other HE
  boards are auto-detected — see [Other keyboards](#other-keyboards-monsgeek--akko-he).
- The **[ViGEmBus driver](https://github.com/nefarius/ViGEmBus/releases/latest)** — 1.22.0+ (older builds
  fail on Windows 11). One-time install.
- Either the prebuilt `.exe` (from Releases) **or** Python 3.10+ to run from source.

## Quick start (prebuilt .exe)

1. Install **ViGEmBus** (link above).
2. Download **`keypad-gamepad-analog.exe`** from the [Releases](../../releases) page and run it.
3. Click **Start output**, then press keys. **F8** pauses.

First run ships a default WASD keymap; click **Learn key** in the app to teach it your own keys.

## Run from source

Install **ViGEmBus** (link above), then run these from the project folder:

```powershell
pip install -r requirements.txt                                 # 1. dependencies
powershell -ExecutionPolicy Bypass -File tools\fetch_hidapi.ps1 # 2. fetch hidapi.dll
py analog_gui.py                                                # 3. launch the GUI
```

Prefer the CLI? Use it instead of step 3:

```powershell
py run_analog.py            # WASD = analog left stick
py run_analog.py racing     # W/S = throttle/brake, A/D = steering
```

No Administrator needed. **F8** pauses globally (even from inside a game). Verify the controller anytime
with `Win+R` → `joy.cpl`.

## Using the app

- **Profiles** — `analog_fps` (WASD = left stick) and `analog_racing` (W/S = triggers, A/D = steering)
  ship by default. Saved to `~/.keypad-gamepad/analog_profiles/`.
- **Bindings** — a table of `key → gamepad target`; Add / Edit / Remove.
- **Learn key** — press a key and the app records its hardware index and full-press depth. No hardcoded
  layout, so switch reorderings don't matter.
- **Tuning** — dead-zone (ignore light touches), button threshold, and the **output rate**
  (virtual-pad update rate, default 1000 Hz; `--hz` on the CLI). Note this is the gamepad
  output rate, capped at 1000 — *not* the keyboard's 8K key-polling, which this app doesn't use.
- **Live preview** — stick dots and trigger bars move with your key depth, before and while output runs.
- **Check for updates** — the header shows the version and a **Check for updates** button. It asks
  GitHub for the latest release; if a newer one exists it downloads the new
  `keypad-gamepad-analog.exe` to your **Downloads** folder (it won't overwrite the running copy —
  close the app and run the downloaded file to update). The first launch may show a Windows
  SmartScreen prompt because the `.exe` isn't code-signed. Running from source? It points you to
  `git pull` instead.

## Other keyboards (MonsGeek / Akko HE)

The depth protocol is shared across MonsGeek/Akko Hall-Effect keyboards on RongYuan
firmware, so sibling boards generally work with no code change. Detection is two-tier:

- **Known devices** — a small registry (`KNOWN_DEVICES` in [`hid_protocol.py`](hid_protocol.py))
  of confirmed boards with friendly names. Verified so far: the **M1 V5 HE wired**
  (`0x3151:0x5030`) and its **2.4 GHz dongle** (`0x3151:0x5038`) — both tested on hardware.
- **Capability auto-detect** — if no known board is connected, the app scans for *any*
  MonsGeek-vendor device exposing the vendor depth signature (a `0xFFFF/0x02` config
  interface) and uses it. This is what makes **other models and their 2.4 GHz dongles
  work automatically**, even though each dongle enumerates under its own unpredictable
  PID (the M1 V5's, for instance, is `0x5038` wired-vs-`0x5030` — not guessable).

The GUI shows the detected board in its title bar; the CLI prints it. Other commands:

- **See what's connected (known + auto-detected):** `py run_analog.py --list-devices`
- **Target a specific board:** `py run_analog.py --vid 0x3151 --pid 0x5038`
- **A board on a different vendor id** (e.g. some Akko models) won't auto-detect until
  its vendor id is added to `HE_VENDOR_IDS`. Confirm it first, without editing source:

  ```powershell
  py tools/hid_enumerate.py            # find its VID:PID and vendor interfaces
  py stage1_probe.py --vid 0xVVVV --pid 0xPPPP   # press W/A/S/D — depth should ramp
  ```

  If depth ramps up and down, add the vendor id to `HE_VENDOR_IDS` (and optionally a
  named `KnownDevice` entry). Then use **Learn key** to teach it your layout — key
  indices aren't assumed.

> **2.4 GHz dongle:** verified to stream analog depth byte-identically to wired on the
> M1 V5 HE. Wireless adds a little radio latency, so wired USB is still marginally
> snappier for twitch games — but the dongle is fully supported.

## Build the .exe yourself

```powershell
pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
# -> dist\keypad-gamepad-analog.exe   (single windowed file, ~20 MB)
```

Bundles `hidapi.dll`, the vgamepad client, the tray backend, and the certifi CA bundle (so the
in-app update check can verify HTTPS). ViGEmBus still installs separately (it's a kernel driver and
can't be packed into the exe).

**Releasing:** pushing a `vX.Y.Z` tag triggers [`.github/workflows/release.yml`](.github/workflows/release.yml),
which builds the `.exe` on `windows-latest` and uploads it to a GitHub Release. The workflow syncs
`version.py` to the tag at build time, so the released `.exe` self-reports the right version; bump
`version.py` in the tagged commit too, so source runs report it correctly.

## Project layout

| File | Role |
| --- | --- |
| `analog_gui.py` | GUI control panel — the recommended entry point. |
| `run_analog.py` | CLI runner with a live readout and F8 pause. |
| `analog_mapper.py` | Engine: depth + calibration → analog targets → Xbox 360 pad. |
| `hid_protocol.py` | HID protocol + `DepthMonitor` (live `{key_index: depth}`). |
| `winhotkey.py` | No-admin global hotkey (F8) via `RegisterHotKey`. |
| `tools/` | `fetch_hidapi.ps1`, key-discovery + verification scripts, `build_exe.ps1`. |
| `mapper.py`, `gui.py` | Older digital-only mapper (see below). |

## Digital mapper (fallback)

`mapper.py` / `gui.py` is the original **digital** mapper: it reads OS keypresses (binary, not analog) and
fakes analog feel with stick-ramp, a walk modifier, and mouse-to-right-stick. It needs Administrator
(global keyboard/mouse hooks). The analog path above makes those hacks unnecessary — pressing lightly *is*
the partial deflection — but the digital mapper is kept as a dependency-light fallback. Run it with
`python gui.py` (as admin).

## Credits & license

- HID protocol reverse-engineered by **[echtzeit-solutions/monsgeek-akko-linux](https://github.com/echtzeit-solutions/monsgeek-akko-linux)**
  (GPL-3.0). This project is an independent Windows/Python implementation; the keymap is discovered
  empirically rather than copied from their source.
- Virtual gamepad via [ViGEmBus](https://github.com/nefarius/ViGEmBus) and
  [vgamepad](https://github.com/yannbouteiller/vgamepad).
- License: **MIT** — see [LICENSE.md](LICENSE.md). Do whatever you want with it.
