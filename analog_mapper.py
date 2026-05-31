"""
analog_mapper.py - true analog keyboard-to-gamepad engine for the MonsGeek M1 V5 HE.

Unlike mapper.py (which reads binary OS keypresses), this reads the keyboard's
real-time per-key analog depth over HID and maps it to proportional gamepad
output: half-pressing W gives a half-tilted left stick, feathering a key bound to
RT gives partial throttle, etc.

Pipeline:
    hid_protocol.DepthMonitor  ->  {key_index: depth}
                               ->  per-key calibration (dead-zone, max) -> value in [0,1]
                               ->  stick / trigger / button mixing
                               ->  vgamepad VX360Gamepad -> ViGEmBus -> game

Bindings are expressed in human-readable key labels ("w", "space"); the keymap
(from tools/stage2_discover.py -> discovered_keymap.json) resolves label ->
key_index, and the calibration block gives each key's full-press depth.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import vgamepad as vg

# Reuse the gamepad target vocabulary + button table from the digital mapper.
from mapper import XBOX_BUTTONS, SPECIAL_TARGETS, ALL_TARGETS  # noqa: F401
from hid_protocol import DepthMonitor, VID, PID

DEFAULT_MAX_DEPTH = 720          # observed full-press depth on the M1 V5 HE
DEFAULT_DEAD_ZONE = 60           # depth below this reads as 0 (top dead-zone / noise)

# Virtual-pad update rate (Hz): how often the engine pushes state to ViGEmBus.
# 1000 Hz = a 1ms tick. Capped at 1000: a sleep-based loop can't reliably go faster
# on Windows, and XInput/Xbox-360 emulation + games poll at 250-1000Hz, so higher
# wouldn't reach the game anyway. (This is the OUTPUT rate, unrelated to the
# keyboard's 8K key-polling, which this app doesn't use.)
DEFAULT_TICK_HZ = 1000
MIN_TICK_HZ, MAX_TICK_HZ = 50, 1000

# Keymap location. When frozen (PyInstaller), the bundled copy lives in the
# read-only _MEIPASS temp dir, so reads/writes must go to a writable user dir;
# the bundled copy seeds it on first run. In dev, it's just next to this file.
_USER_DIR = Path.home() / ".keypad-gamepad"
if getattr(sys, "frozen", False):
    DEFAULT_KEYMAP_PATH = _USER_DIR / "discovered_keymap.json"
    BUNDLED_KEYMAP_PATH = Path(getattr(sys, "_MEIPASS", ".")) / "discovered_keymap.json"
else:
    DEFAULT_KEYMAP_PATH = Path(__file__).resolve().parent / "discovered_keymap.json"
    BUNDLED_KEYMAP_PATH = DEFAULT_KEYMAP_PATH


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# ---------------------------------------------------------------------------
# Keymap (label <-> key_index + per-key calibration)
# ---------------------------------------------------------------------------

@dataclass
class Keymap:
    by_label: dict[str, int]                  # "w" -> 14
    calibration: dict[int, dict]              # 14 -> {"max": 720, "dead_zone": 60?}
    source_path: str | None = None            # where it was loaded from (for save)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_KEYMAP_PATH) -> "Keymap":
        p = Path(path)
        d = json.loads(p.read_text())
        by_label = {k: int(v) for k, v in d.get("by_label", {}).items()}
        calibration = {int(k): v for k, v in d.get("calibration", {}).items()}
        return cls(by_label=by_label, calibration=calibration, source_path=str(p))

    @classmethod
    def empty(cls) -> "Keymap":
        return cls(by_label={}, calibration={}, source_path=str(DEFAULT_KEYMAP_PATH))

    def max_depth(self, key_index: int) -> int:
        return int(self.calibration.get(key_index, {}).get("max", DEFAULT_MAX_DEPTH))

    def learn(self, label: str, key_index: int, max_depth: int) -> None:
        """Record (or update) a label -> key_index mapping and its calibration max."""
        self.by_label[label] = key_index
        self.calibration[key_index] = {"max": int(max_depth)}

    def save(self, path: Path | str | None = None) -> Path:
        p = Path(path or self.source_path or DEFAULT_KEYMAP_PATH)
        data = {
            "device": {"vid": VID, "pid": PID},
            "by_label": self.by_label,
            "calibration": {str(k): v for k, v in self.calibration.items()},
            "note": "key_index<->label + per-key calibration max; edited via analog_gui.",
        }
        p.write_text(json.dumps(data, indent=2))
        self.source_path = str(p)
        return p


def load_keymap() -> Keymap:
    """Load the keymap, preferring the writable user copy; seed it from the bundled
    default (frozen builds) when the user copy is absent; else return an empty map."""
    if Path(DEFAULT_KEYMAP_PATH).exists():
        return Keymap.load(DEFAULT_KEYMAP_PATH)
    if Path(BUNDLED_KEYMAP_PATH).exists():
        km = Keymap.load(BUNDLED_KEYMAP_PATH)
        km.source_path = str(DEFAULT_KEYMAP_PATH)  # future saves go to the writable dir
        return km
    return Keymap.empty()


# ---------------------------------------------------------------------------
# Profile (which key drives which gamepad target)
# ---------------------------------------------------------------------------

@dataclass
class AnalogProfile:
    name: str = "analog_default"
    bindings: dict[str, str] = field(default_factory=dict)   # label -> target
    dead_zone: int = DEFAULT_DEAD_ZONE                       # global default
    dead_zone_overrides: dict[str, int] = field(default_factory=dict)  # label -> dz
    button_threshold: float = 0.5                            # analog value -> digital press
    tick_hz: int = DEFAULT_TICK_HZ                           # virtual-pad output rate (Hz)

    @classmethod
    def default_fps(cls) -> "AnalogProfile":
        """WASD becomes a true analog left stick. Buttons on the discovered keys."""
        return cls(
            name="analog_fps",
            bindings={
                "w": "LSTICK_UP",
                "s": "LSTICK_DOWN",
                "a": "LSTICK_LEFT",
                "d": "LSTICK_RIGHT",
                "space": "A",       # jump
                "shift": "B",       # crouch/sprint toggle (your call)
                "e": "X",           # interact
                "r": "Y",           # reload
            },
        )

    @classmethod
    def default_racing(cls) -> "AnalogProfile":
        """Analog throttle/brake on W/S, analog steering on A/D. The showcase profile."""
        return cls(
            name="analog_racing",
            bindings={
                "w": "RT",          # progressive throttle
                "s": "LT",          # progressive brake
                "a": "LSTICK_LEFT", # analog steering
                "d": "LSTICK_RIGHT",
                "space": "A",       # handbrake
                "shift": "RB",      # upshift
                "e": "X",
                "r": "Y",           # reset
            },
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AnalogProfile":
        return cls(**d)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class AnalogMapper:
    """Drives a virtual Xbox 360 pad from live analog key depth.

    Output is recomputed on a fixed tick (default 1000Hz / 1ms) reading the latest
    depth snapshot, which decouples ViGEm update rate from the (much higher) event
    rate and keeps motion smooth. The most recent computed state is exposed via
    `last_state` for testing/telemetry. The tick rate is read live from
    profile.tick_hz each iteration, so changes apply without a restart.
    """

    # Fallback only; the live rate comes from profile.tick_hz via _tick_dt().
    TICK_HZ = DEFAULT_TICK_HZ
    TICK_DT = 1.0 / TICK_HZ

    def __init__(self, profile: AnalogProfile, keymap: Keymap, dry_run: bool = False,
                 monitor: DepthMonitor | None = None,
                 vid: int | None = None, pid: int | None = None) -> None:
        self.profile = profile
        self.keymap = keymap
        # dry_run: compute target state but don't create/drive a virtual pad. Lets
        # the engine run for testing/telemetry, and degrade gracefully when the
        # ViGEmBus driver is missing or unattachable.
        self.dry_run = dry_run
        self.vigem_error: str | None = None
        self.gamepad = None
        if not dry_run:
            self.gamepad = self._create_gamepad()
            if self.gamepad is None:
                # Couldn't attach after retries -> degrade to dry-run rather than crash.
                self.dry_run = True
        # An injected monitor (e.g. owned by a GUI for always-on live preview) is not
        # started/stopped by us; a self-created one is ours to manage. vid/pid select
        # which board the self-created monitor opens (None => auto-scan KNOWN_DEVICES).
        self._owns_monitor = monitor is None
        self.monitor = monitor if monitor is not None else DepthMonitor(vid=vid, pid=pid)
        self.running = False
        self.enabled = True
        self._runtime: dict[int, tuple[str, int, int]] = {}   # key_index -> (target, dz, max)
        self.last_state: dict = {}
        self._build_runtime()

    def _create_gamepad(self, retries: int = 5, delay: float = 0.4):
        """Create a VX360Gamepad, retrying transient ViGEmBus attach failures.

        ViGEmBus can intermittently return TARGET_NOT_PLUGGED_IN right after prior
        create/destroy churn; a short retry reliably recovers. Returns None (and
        records vigem_error) if it genuinely can't attach."""
        last = None
        for _ in range(retries):
            try:
                return vg.VX360Gamepad()
            except Exception as e:
                last = e
                time.sleep(delay)
        self.vigem_error = str(last)
        return None

    def _build_runtime(self) -> None:
        """Resolve bindings(label->target) + keymap(label->index) + calibration into
        a flat key_index -> (target, dead_zone, max_depth) table."""
        rt: dict[int, tuple[str, int, int]] = {}
        unresolved: list[str] = []
        for label, target in self.profile.bindings.items():
            ki = self.keymap.by_label.get(label)
            if ki is None:
                unresolved.append(label)
                continue
            dz = self.profile.dead_zone_overrides.get(label, self.profile.dead_zone)
            mx = self.keymap.max_depth(ki)
            rt[ki] = (target, dz, mx)
        self._runtime = rt
        self.unresolved_labels = unresolved

    def set_profile(self, profile: AnalogProfile) -> None:
        self.profile = profile
        self._build_runtime()

    @staticmethod
    def _value(depth: int, dead_zone: int, max_depth: int) -> float:
        if depth <= dead_zone:
            return 0.0
        return _clamp((depth - dead_zone) / max(1, max_depth - dead_zone), 0.0, 1.0)

    def compute_targets(self, depths: dict[int, int]) -> dict[str, float]:
        """Map the depth snapshot to {target: value in [0,1]} (max if keys collide)."""
        ts: dict[str, float] = {}
        for ki, (target, dz, mx) in self._runtime.items():
            v = self._value(depths.get(ki, 0), dz, mx)
            if v > 0.0:
                cur = ts.get(target, 0.0)
                if v > cur:
                    ts[target] = v
        return ts

    def compute_state(self, depths: dict[int, int]) -> dict:
        """Pure depth -> full gamepad state. Shared by the tick loop and the GUI's
        live preview so both reflect identical math. Returns rounded display values
        plus a `_raw` tuple of unrounded floats for driving the pad."""
        ts = self.compute_targets(depths)
        lx = _clamp(ts.get("LSTICK_RIGHT", 0.0) - ts.get("LSTICK_LEFT", 0.0), -1.0, 1.0)
        ly = _clamp(ts.get("LSTICK_UP", 0.0) - ts.get("LSTICK_DOWN", 0.0), -1.0, 1.0)
        rx = _clamp(ts.get("RSTICK_RIGHT", 0.0) - ts.get("RSTICK_LEFT", 0.0), -1.0, 1.0)
        ry = _clamp(ts.get("RSTICK_UP", 0.0) - ts.get("RSTICK_DOWN", 0.0), -1.0, 1.0)
        lt = ts.get("LT", 0.0)
        rt = ts.get("RT", 0.0)
        pressed = [name for name in XBOX_BUTTONS
                   if ts.get(name, 0.0) >= self.profile.button_threshold]
        return {
            "lx": round(lx, 3), "ly": round(ly, 3),
            "rx": round(rx, 3), "ry": round(ry, 3),
            "lt": round(lt, 3), "rt": round(rt, 3),
            "buttons": pressed,
            "_raw": (lx, ly, rx, ry, lt, rt),
        }

    def _tick(self) -> None:
        if not self.enabled:
            if self.gamepad is not None:
                self.gamepad.reset()
                self.gamepad.update()
            self.last_state = {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "buttons": []}
            return

        st = self.compute_state(self.monitor.snapshot())
        lx, ly, rx, ry, lt, rt = st["_raw"]

        if self.gamepad is not None:
            self.gamepad.reset()
            self.gamepad.left_joystick_float(x_value_float=lx, y_value_float=ly)
            self.gamepad.right_joystick_float(x_value_float=rx, y_value_float=ry)
            self.gamepad.left_trigger_float(value_float=lt)
            self.gamepad.right_trigger_float(value_float=rt)
            for name in st["buttons"]:
                self.gamepad.press_button(button=XBOX_BUTTONS[name])
            self.gamepad.update()

        self.last_state = {k: v for k, v in st.items() if k != "_raw"}

    def _tick_dt(self) -> float:
        """Live seconds-per-tick from profile.tick_hz, clamped to a sane range."""
        hz = getattr(self.profile, "tick_hz", DEFAULT_TICK_HZ) or DEFAULT_TICK_HZ
        return 1.0 / _clamp(hz, MIN_TICK_HZ, MAX_TICK_HZ)

    def _run_loop(self) -> None:
        next_tick = time.perf_counter()
        while self.running:
            self._tick()
            next_tick += self._tick_dt()
            sleep_for = next_tick - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_tick = time.perf_counter()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        if self.gamepad is not None:
            # Flush the first-read startup transient some XInput clients latch onto
            # before the first report delta: a sub-perceptible nudge, then zero. This
            # forces two report deltas so the pad reads a clean centred 0 at rest.
            try:
                self.gamepad.reset()
                self.gamepad.left_joystick_float(x_value_float=0.0, y_value_float=0.001)
                self.gamepad.update()
                self.gamepad.reset()
                self.gamepad.update()
            except Exception:
                pass
        if self._owns_monitor:
            self.monitor.start()
        self.running = True
        threading.Thread(target=self._run_loop, name="AnalogTick", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        time.sleep(self._tick_dt() * 2)
        if self._owns_monitor:
            try:
                self.monitor.stop()
            except Exception:
                pass
        if self.gamepad is not None:
            try:
                self.gamepad.reset()
                self.gamepad.update()
            except Exception:
                pass

    def toggle_enabled(self) -> None:
        self.enabled = not self.enabled


# ---------------------------------------------------------------------------
# Profile storage
# ---------------------------------------------------------------------------

ANALOG_PROFILE_DIR = Path.home() / ".keypad-gamepad" / "analog_profiles"


def save_profile(profile: AnalogProfile) -> Path:
    ANALOG_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    path = ANALOG_PROFILE_DIR / f"{profile.name}.json"
    path.write_text(json.dumps(profile.to_dict(), indent=2))
    return path


def load_profile(name: str) -> AnalogProfile:
    return AnalogProfile.from_dict(
        json.loads((ANALOG_PROFILE_DIR / f"{name}.json").read_text())
    )


def list_profiles() -> list[str]:
    if not ANALOG_PROFILE_DIR.exists():
        return []
    return sorted(p.stem for p in ANALOG_PROFILE_DIR.glob("*.json"))


def ensure_defaults_exist() -> None:
    ANALOG_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for prof in (AnalogProfile.default_fps(), AnalogProfile.default_racing()):
        path = ANALOG_PROFILE_DIR / f"{prof.name}.json"
        if not path.exists():
            save_profile(prof)
