"""
keypad-gamepad: Host-side keyboard-to-gamepad mapper for MonsGeek M1 V5 HE
(or any keyboard, really — there's nothing M1-specific here since we're
working from digital keypresses, not analog depth).

Emulates an Xbox 360 controller via ViGEmBus so games see a real gamepad.

Author: built for the project owner
Requires: Python 3.10+, vgamepad, keyboard, mouse, ViGEmBus driver
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

import keyboard
import mouse
import vgamepad as vg

# ---------------------------------------------------------------------------
# Profile data model
# ---------------------------------------------------------------------------

# Xbox 360 button names that vgamepad understands. Used as mapping targets.
XBOX_BUTTONS = {
    "A": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
    "B": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
    "X": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
    "Y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
    "LB": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
    "RB": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
    "BACK": vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
    "START": vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
    "LSTICK": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
    "RSTICK": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
    "DPAD_UP": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
    "DPAD_DOWN": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
    "DPAD_LEFT": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
    "DPAD_RIGHT": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
}

# Special targets (sticks and triggers, which aren't buttons)
SPECIAL_TARGETS = {
    "LSTICK_UP", "LSTICK_DOWN", "LSTICK_LEFT", "LSTICK_RIGHT",
    "RSTICK_UP", "RSTICK_DOWN", "RSTICK_LEFT", "RSTICK_RIGHT",
    "LT", "RT",  # triggers
}

ALL_TARGETS = set(XBOX_BUTTONS.keys()) | SPECIAL_TARGETS


@dataclass
class Profile:
    """A keyboard-to-gamepad mapping profile."""

    name: str = "default"
    # key (e.g. "w") -> gamepad target (e.g. "LSTICK_UP")
    bindings: dict[str, str] = field(default_factory=dict)
    # "walk" modifier - when held, sticks deflect to walk_deflection instead of 100%
    walk_modifier: str | None = "shift"
    walk_deflection: float = 0.5
    # Stick ramp time in ms (0 = instant, ~150 = smooth)
    stick_ramp_ms: int = 0
    # Mouse-to-right-stick sensitivity (higher = more sensitive)
    mouse_sensitivity: float = 8.0
    # Mouse decay - how fast right stick returns to center when mouse stops
    mouse_decay: float = 0.85
    # Use mouse for right stick at all?
    mouse_as_rstick: bool = True

    @classmethod
    def default_fps(cls) -> "Profile":
        """A sensible default for FPS / third-person games."""
        return cls(
            name="default_fps",
            bindings={
                "w": "LSTICK_UP",
                "s": "LSTICK_DOWN",
                "a": "LSTICK_LEFT",
                "d": "LSTICK_RIGHT",
                "space": "A",            # jump
                "ctrl": "B",             # crouch
                "e": "X",                # interact
                "r": "Y",                # reload
                "q": "LB",
                "f": "RB",
                "1": "DPAD_UP",
                "2": "DPAD_RIGHT",
                "3": "DPAD_DOWN",
                "4": "DPAD_LEFT",
                "tab": "BACK",
                "esc": "START",
            },
            walk_modifier="shift",
            walk_deflection=0.5,
            stick_ramp_ms=0,
            mouse_sensitivity=8.0,
            mouse_decay=0.85,
            mouse_as_rstick=True,
        )

    @classmethod
    def default_racing(cls) -> "Profile":
        """A sensible default for driving games. Ramped sticks help fake analog steering."""
        return cls(
            name="default_racing",
            bindings={
                "w": "RT",               # accelerate
                "s": "LT",               # brake
                "a": "LSTICK_LEFT",      # steer
                "d": "LSTICK_RIGHT",
                "space": "A",            # handbrake
                "shift": "RB",           # upshift
                "ctrl": "LB",            # downshift
                "r": "Y",                # reset car
                "tab": "BACK",
                "esc": "START",
            },
            walk_modifier=None,          # no walk in racing
            stick_ramp_ms=180,           # smooth steering (180ms to full lock)
            mouse_as_rstick=False,       # mouse usually not used in racing
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(**d)


# ---------------------------------------------------------------------------
# Stick ramping helpers
# ---------------------------------------------------------------------------

def ramp(current: float, target: float, ramp_ms: int, dt_ms: float) -> float:
    """Move `current` toward `target` over `ramp_ms` milliseconds.

    Returns the new value. If ramp_ms is 0, snaps instantly.
    """
    if ramp_ms <= 0 or dt_ms <= 0:
        return target
    # How much of the full 0->1 range we can cover this tick
    step = dt_ms / ramp_ms
    delta = target - current
    if abs(delta) <= step:
        return target
    return current + (step if delta > 0 else -step)


# ---------------------------------------------------------------------------
# The Mapper
# ---------------------------------------------------------------------------

class Mapper:
    """Reads keyboard + mouse state and drives a virtual Xbox 360 controller.

    Design notes:
      - We poll at 250Hz (4ms tick). This is well within USB HID timing and gives
        smooth stick ramps. ViGEmBus itself batches updates efficiently.
      - We use `keyboard` and `mouse` libraries (low-level OS hooks). They require
        admin rights on Windows because they install global hooks.
      - Mouse-to-rstick: we accumulate mouse delta between ticks, scale it, then
        decay toward zero each tick so the stick recenters when the mouse stops.
        This mimics the way most "mouse-as-stick" tools work (reWASD, Wootility).
    """

    TICK_HZ = 250
    TICK_DT = 1.0 / TICK_HZ  # seconds

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.gamepad = vg.VX360Gamepad()
        self.running = False
        self.enabled = True   # toggleable via hotkey, separate from running

        # State that persists across ticks
        self._keys_down: set[str] = set()
        self._mouse_dx = 0.0
        self._mouse_dy = 0.0
        self._rstick_x = 0.0  # current (possibly decaying) value
        self._rstick_y = 0.0
        self._lstick_x = 0.0  # for ramping
        self._lstick_y = 0.0

        # Locks: keyboard hook + mouse hook fire on their own threads
        self._state_lock = threading.Lock()

        # Hook handles, so we can unhook on stop
        self._kb_hook = None
        self._mouse_hook = None
        self._last_mouse_pos: tuple[int, int] | None = None

        # Observers (the GUI can subscribe to status changes)
        self._observers: list[Callable[[str], None]] = []

    # -- subscription API for the GUI --------------------------------------

    def subscribe(self, callback: Callable[[str], None]) -> None:
        self._observers.append(callback)

    def _notify(self, msg: str) -> None:
        for cb in self._observers:
            try:
                cb(msg)
            except Exception:
                pass

    # -- input hooks -------------------------------------------------------

    def _on_key_event(self, event) -> None:
        """Called by the `keyboard` library on every key event, on its own thread."""
        with self._state_lock:
            if event.event_type == "down":
                self._keys_down.add(event.name)
            elif event.event_type == "up":
                self._keys_down.discard(event.name)

    def _on_mouse_move(self, event) -> None:
        """Called by the `mouse` library on every mouse move."""
        if not self.profile.mouse_as_rstick:
            return
        # `mouse` library gives absolute position. We compute delta ourselves.
        # event has .x and .y attributes for MoveEvent.
        x = getattr(event, "x", None)
        y = getattr(event, "y", None)
        if x is None or y is None:
            return
        with self._state_lock:
            if self._last_mouse_pos is not None:
                dx = x - self._last_mouse_pos[0]
                dy = y - self._last_mouse_pos[1]
                self._mouse_dx += dx
                self._mouse_dy += dy
            self._last_mouse_pos = (x, y)

    # -- the main loop -----------------------------------------------------

    def _tick(self) -> None:
        """Read input state, compute gamepad state, push to ViGEm. Runs every TICK_DT."""
        with self._state_lock:
            keys_down = set(self._keys_down)
            mouse_dx, self._mouse_dx = self._mouse_dx, 0.0
            mouse_dy, self._mouse_dy = self._mouse_dy, 0.0

        if not self.enabled:
            # Release everything so games don't think buttons are stuck.
            self.gamepad.reset()
            self.gamepad.update()
            return

        prof = self.profile

        # ---- 1. Figure out targets that are "pressed" --------------------
        # target_state[target_name] = float in [0,1] (how "much" it's pressed)
        target_state: dict[str, float] = {}

        # walk modifier reduces stick deflection
        walking = (
            prof.walk_modifier is not None
            and prof.walk_modifier in keys_down
        )
        # Make sure the walk modifier itself doesn't trigger its own binding
        # if the user happened to map shift to something. (Edge case.)
        effective_keys = keys_down - (
            {prof.walk_modifier} if prof.walk_modifier and prof.walk_modifier not in prof.bindings else set()
        )

        for key in effective_keys:
            target = prof.bindings.get(key)
            if target is None:
                continue
            # Sticks honor the walk modifier; buttons and triggers don't.
            if target.startswith("LSTICK_") and walking:
                target_state[target] = max(target_state.get(target, 0.0), prof.walk_deflection)
            else:
                target_state[target] = 1.0

        # ---- 2. Compute desired left stick from LSTICK_* targets ----------
        # If both UP and DOWN are pressed (or LEFT and RIGHT), they cancel.
        desired_lx = target_state.get("LSTICK_RIGHT", 0.0) - target_state.get("LSTICK_LEFT", 0.0)
        desired_ly = target_state.get("LSTICK_UP", 0.0) - target_state.get("LSTICK_DOWN", 0.0)

        # Ramp toward desired
        dt_ms = self.TICK_DT * 1000
        self._lstick_x = ramp(self._lstick_x, desired_lx, prof.stick_ramp_ms, dt_ms)
        self._lstick_y = ramp(self._lstick_y, desired_ly, prof.stick_ramp_ms, dt_ms)

        # ---- 3. Compute right stick (mouse + key-based) -------------------
        # Key bindings to right stick (in case someone wants IJKL or something)
        key_rx = target_state.get("RSTICK_RIGHT", 0.0) - target_state.get("RSTICK_LEFT", 0.0)
        key_ry = target_state.get("RSTICK_UP", 0.0) - target_state.get("RSTICK_DOWN", 0.0)

        if prof.mouse_as_rstick:
            # Add mouse delta (scaled). Note: screen Y goes down, stick Y goes up.
            self._rstick_x += mouse_dx / 100.0 * prof.mouse_sensitivity
            self._rstick_y -= mouse_dy / 100.0 * prof.mouse_sensitivity
            # Decay toward zero
            self._rstick_x *= prof.mouse_decay
            self._rstick_y *= prof.mouse_decay
            # Clamp
            self._rstick_x = max(-1.0, min(1.0, self._rstick_x))
            self._rstick_y = max(-1.0, min(1.0, self._rstick_y))
            final_rx = max(-1.0, min(1.0, self._rstick_x + key_rx))
            final_ry = max(-1.0, min(1.0, self._rstick_y + key_ry))
        else:
            final_rx = key_rx
            final_ry = key_ry

        # ---- 4. Push state to virtual gamepad -----------------------------
        self.gamepad.reset()

        # Sticks: vgamepad takes float in [-1, 1]
        self.gamepad.left_joystick_float(x_value_float=self._lstick_x, y_value_float=self._lstick_y)
        self.gamepad.right_joystick_float(x_value_float=final_rx, y_value_float=final_ry)

        # Triggers: vgamepad takes float in [0, 1]
        self.gamepad.left_trigger_float(value_float=target_state.get("LT", 0.0))
        self.gamepad.right_trigger_float(value_float=target_state.get("RT", 0.0))

        # Buttons
        for name, btn in XBOX_BUTTONS.items():
            if target_state.get(name, 0.0) > 0.5:
                self.gamepad.press_button(button=btn)

        self.gamepad.update()

    def _run_loop(self) -> None:
        next_tick = time.perf_counter()
        while self.running:
            self._tick()
            next_tick += self.TICK_DT
            sleep_for = next_tick - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # We fell behind. Skip ahead rather than spiraling.
                next_tick = time.perf_counter()

    # -- start/stop --------------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._kb_hook = keyboard.hook(self._on_key_event, suppress=False)
        self._mouse_hook = mouse.hook(self._on_mouse_move)
        threading.Thread(target=self._run_loop, daemon=True).start()
        self._notify("Mapper started")

    def stop(self) -> None:
        self.running = False
        if self._kb_hook is not None:
            try:
                keyboard.unhook(self._kb_hook)
            except Exception:
                pass
            self._kb_hook = None
        if self._mouse_hook is not None:
            try:
                mouse.unhook(self._mouse_hook)
            except Exception:
                pass
            self._mouse_hook = None
        self.gamepad.reset()
        self.gamepad.update()
        self._notify("Mapper stopped")

    def toggle_enabled(self) -> None:
        self.enabled = not self.enabled
        self._notify(f"Mapper {'enabled' if self.enabled else 'paused'}")

    def set_profile(self, profile: Profile) -> None:
        # Reset stick state when switching profiles, otherwise old deflection persists.
        with self._state_lock:
            self._rstick_x = self._rstick_y = 0.0
            self._lstick_x = self._lstick_y = 0.0
            self._mouse_dx = self._mouse_dy = 0.0
        self.profile = profile
        self._notify(f"Profile switched to: {profile.name}")


# ---------------------------------------------------------------------------
# Profile storage
# ---------------------------------------------------------------------------

PROFILE_DIR = Path.home() / ".keypad-gamepad" / "profiles"


def save_profile(profile: Profile) -> Path:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILE_DIR / f"{profile.name}.json"
    path.write_text(json.dumps(profile.to_dict(), indent=2))
    return path


def load_profile(name: str) -> Profile:
    path = PROFILE_DIR / f"{name}.json"
    return Profile.from_dict(json.loads(path.read_text()))


def list_profiles() -> list[str]:
    if not PROFILE_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILE_DIR.glob("*.json"))


def ensure_defaults_exist() -> None:
    """Write the built-in defaults to disk on first run."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for prof in (Profile.default_fps(), Profile.default_racing()):
        path = PROFILE_DIR / f"{prof.name}.json"
        if not path.exists():
            save_profile(prof)
