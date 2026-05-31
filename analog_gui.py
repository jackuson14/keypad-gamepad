"""
analog_gui.py - control panel for the MonsGeek M1 V5 HE analog gamepad.

A Tkinter app over analog_mapper:
  - ViGEmBus status banner (real pad vs dry-run fallback).
  - Profile selector + New/Save/Delete (analog profiles).
  - Bindings table: key label -> key_index -> calibration max -> gamepad target.
  - In-app discovery/calibration wizard ("Learn key"): press a key, it captures the
    key_index and full-press depth, no hardcoded table needed.
  - Tuning: global dead-zone + button threshold.
  - Live preview: stick dots + trigger bars + pressed buttons, always reflecting your
    current key depths (works whether or not output is running).
  - Start/Stop output, global F8 pause (no admin), optional minimize-to-tray.

No Administrator required: the keyboard is read over HID and F8 uses RegisterHotKey.
"""

from __future__ import annotations

import sys
import threading
import webbrowser

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, font as tkfont

import sv_ttk

import updater
from version import __version__
from hid_protocol import DepthMonitor
from winhotkey import start_hotkey, VK_F8
from analog_mapper import (
    AnalogMapper, AnalogProfile, Keymap,
    XBOX_BUTTONS, SPECIAL_TARGETS, MIN_TICK_HZ, MAX_TICK_HZ,
    ensure_defaults_exist, list_profiles, load_profile, save_profile,
    load_keymap, ANALOG_PROFILE_DIR, get_last_profile, set_last_profile,
)

SORTED_TARGETS = sorted(XBOX_BUTTONS.keys()) + sorted(SPECIAL_TARGETS)

# Sun Valley (sv_ttk) themes the ttk widgets; these cover the few raw-tk bits it
# can't (Canvas, Listbox, Toplevel) so they match the dark window instead of
# rendering as light-grey 2000s-era controls.
SURFACE = "#1c1c1c"      # sv_ttk dark window background
CARD = "#202020"         # slightly raised panel (stick canvas)
GRID = "#3a3f4a"         # stick guide lines / outline
ACCENT = "#57a6ff"       # stick dot / highlights (Win11-ish blue)
TEXT = "#fafafa"
MUTED = "#9aa0a6"
OK_BG, OK_FG = "#16301c", "#5fd06f"      # "pad attached" banner
WARN_BG, WARN_FG = "#3a1d1d", "#ff8a80"  # "dry-run / not attached" banner


class Tooltip:
    """Lightweight hover tooltip for any widget (Tk has none built in).

    Shows a small dark popup after a short hover delay; hides on leave/click.
    Styled to match the sv_ttk dark theme (CARD fill, thin GRID border)."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 450,
                 wraplength: int = 340) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self.wraplength = wraplength
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self) -> None:
        if self._tip is not None or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        self._tip.configure(bg=GRID)  # 1px border via the outer bg showing through
        tk.Label(self._tip, text=self.text, justify="left", bg=CARD, fg=TEXT,
                 wraplength=self.wraplength, font=("Segoe UI", 9),
                 padx=10, pady=7, bd=0).pack(padx=1, pady=1)

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

# Optional system-tray support; degrade gracefully if not installed.
try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("keypad-gamepad ANALOG - M1 V5 HE")
        root.geometry("820x760")
        root.minsize(760, 700)
        root.configure(bg=SURFACE)

        ensure_defaults_exist()
        self.profile: AnalogProfile = self._load_initial_profile()
        self.keymap = load_keymap()

        # One shared monitor for live preview + wizards + the engine.
        self.monitor = DepthMonitor()
        self.monitor_ok = False
        self.mapper: AnalogMapper | None = None
        self.tray = None
        self._hotkey_stop = False
        self._tooltips: list[Tooltip] = []   # keep refs alive

        self._build_ui()
        self._start_monitor()
        self._create_mapper()
        self._refresh_bindings_table()
        self._sync_tuning_from_profile()

        # Global F8 = pause/resume output (marshalled back to the Tk thread).
        start_hotkey(VK_F8, lambda: self.root.after(0, self.toggle_pause),
                     lambda: self._hotkey_stop)

        if _HAS_TRAY:
            self._setup_tray()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_preview()

    # ------------------------------------------------------------------ setup

    def _start_monitor(self) -> None:
        try:
            self.monitor.start()
            self.monitor_ok = True
            dev = self.monitor.device
            if dev is not None:
                self.root.title(f"keypad-gamepad ANALOG - {dev.name}")
                note = "" if dev.verified else "  (unverified board - let us know if it works!)"
                self._set_status(f"Connected: {dev.name} [{dev.id_str}]{note}")
        except Exception as e:
            self.monitor_ok = False
            self._set_status(f"No HE keyboard found: {e}. Plug one in, then click 'Reconnect kbd'.")

    def _create_mapper(self) -> None:
        # Attaches a virtual pad now (neutral); the tick loop only runs once started.
        self.mapper = AnalogMapper(self.profile, self.keymap, dry_run=False, monitor=self.monitor)
        if self.mapper.dry_run:
            self.vigem_banner.config(
                text="  ●  ViGEmBus not attached — DRY-RUN (live preview only). "
                     "Install/upgrade ViGEmBus 1.22.0 for real output.",
                background=WARN_BG, foreground=WARN_FG)
        else:
            self.vigem_banner.config(
                text="  ●  Virtual Xbox 360 pad attached via ViGEmBus.",
                background=OK_BG, foreground=OK_FG)
        if self.mapper.unresolved_labels:
            self._set_status(f"Bindings with no learned key: {self.mapper.unresolved_labels} "
                             f"- use 'Learn key' to teach them.")

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        pad = {"padx": 16, "pady": 6}

        # Title strip
        header = ttk.Frame(self.root); header.pack(fill="x", padx=16, pady=(14, 2))
        ttk.Label(header, text="keypad-gamepad", font=("Segoe UI Semibold", 16)).pack(side="left")
        ttk.Label(header, text="ANALOG", font=("Segoe UI", 11),
                  foreground=ACCENT).pack(side="left", padx=(8, 0), pady=(5, 0))
        # Version + update check, anchored right (packed right-first so the button is rightmost).
        self.update_btn = ttk.Button(header, text="Check for updates",
                                     command=self._on_check_for_updates)
        self.update_btn.pack(side="right", pady=(2, 0))
        ttk.Label(header, text=f"v{__version__}", foreground=MUTED).pack(
            side="right", padx=(0, 10), pady=(8, 0))
        self._add_tip(self.update_btn,
                      "Check GitHub for a newer release. If one exists, the new "
                      "keypad-gamepad-analog.exe is downloaded to your Downloads folder "
                      "(it won't replace this running copy automatically).")

        # ViGEmBus status pill (flat, dark; coloured in _create_mapper)
        self.vigem_banner = tk.Label(self.root, text="  Checking ViGEmBus…",
                                     anchor="w", relief="flat", bg=SURFACE, fg=MUTED,
                                     font=("Segoe UI", 10), padx=12, pady=8)
        self.vigem_banner.pack(fill="x", padx=16, pady=(4, 2))

        # Profile row
        top = ttk.Frame(self.root); top.pack(fill="x", **pad)
        ttk.Label(top, text="Profile").pack(side="left")
        self.profile_var = tk.StringVar(value=self.profile.name)
        self.profile_combo = ttk.Combobox(top, textvariable=self.profile_var,
                                          values=list_profiles(), state="readonly", width=22)
        self.profile_combo.pack(side="left", padx=4)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda e: self._load_selected_profile())
        b_new = ttk.Button(top, text="New...", command=self._new_profile); b_new.pack(side="left", padx=2)
        b_save = ttk.Button(top, text="Save", command=self._save_current_profile); b_save.pack(side="left", padx=2)
        b_del = ttk.Button(top, text="Delete", command=self._delete_current_profile); b_del.pack(side="left", padx=2)
        b_recon = ttk.Button(top, text="Reconnect kbd", command=self._reconnect); b_recon.pack(side="right", padx=2)
        self._add_tip(self.profile_combo, "The active profile: one set of key→gamepad bindings plus "
                      "its dead-zone and button-threshold tuning. Switch profiles or create new ones here.")
        self._add_tip(b_new, "Create a new, empty profile.")
        self._add_tip(b_save, "Save the current bindings and tuning into this profile.")
        self._add_tip(b_del, "Delete the selected profile. The built-in analog_fps / analog_racing "
                      "defaults regenerate and can't be deleted.")
        self._add_tip(b_recon, "Re-scan and re-open the keyboard — use after replugging it, or if it "
                      "wasn't detected when the app launched.")

        # Bindings table
        bframe = ttk.LabelFrame(self.root, text="Key bindings  (label -> key_index -> max -> target)")
        bframe.pack(fill="both", expand=True, **pad)
        cols = ("label", "index", "max", "target")
        self.tree = ttk.Treeview(bframe, columns=cols, show="headings", height=10)
        for c, w in (("label", 110), ("index", 80), ("max", 80), ("target", 200)):
            self.tree.heading(c, text=c.capitalize()); self.tree.column(c, width=w)
        self.tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<Double-1>", lambda e: self._edit_binding())
        sb = ttk.Scrollbar(bframe, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=sb.set); sb.pack(side="left", fill="y")
        self._add_tip(self.tree, "Each row maps a physical key to a gamepad control. Columns: key "
                      "label · hardware index · full-press depth (max) · gamepad target. "
                      "Double-click a row to change its target.")
        bcol = ttk.Frame(bframe); bcol.pack(side="left", fill="y", padx=4)
        b_add = ttk.Button(bcol, text="Add...", command=self._add_binding); b_add.pack(fill="x", pady=2)
        b_edit = ttk.Button(bcol, text="Edit target...", command=self._edit_binding); b_edit.pack(fill="x", pady=2)
        b_rem = ttk.Button(bcol, text="Remove", command=self._remove_binding); b_rem.pack(fill="x", pady=2)
        ttk.Separator(bcol, orient="horizontal").pack(fill="x", pady=6)
        b_learn = ttk.Button(bcol, text="Learn key...", command=self._learn_key); b_learn.pack(fill="x", pady=2)
        b_cal = ttk.Button(bcol, text="Calibrate sel.", command=self._calibrate_selected); b_cal.pack(fill="x", pady=2)
        self._add_tip(b_add, "Map one of your learned keys to a gamepad control.")
        self._add_tip(b_edit, "Change which gamepad control the selected key drives.")
        self._add_tip(b_rem, "Remove the selected binding.")
        self._add_tip(b_learn, "Press a key and the app records its hardware index and full-press "
                      "depth — no hardcoded layout, so switch reorderings don't matter.")
        self._add_tip(b_cal, "Re-measure the selected key's full-press depth so its analog range "
                      "stays accurate.")

        # Tuning
        tune = ttk.LabelFrame(self.root, text="Tuning"); tune.pack(fill="x", **pad)

        DZ_TIP = ("Dead-zone — how far a key must travel before it registers at all. The "
                  "keyboard reports depth 0 (released) to ~720 (fully pressed); anything "
                  "shallower than this reads as zero output. Raise it to stop resting fingers "
                  "or sensor jitter from drifting your stick; lower it for a hair trigger.")
        dz_row = ttk.Frame(tune); dz_row.pack(fill="x", padx=8, pady=4)
        dz_lbl = ttk.Label(dz_row, text="Dead-zone (depth):", width=20); dz_lbl.pack(side="left")
        self.dz_var = tk.IntVar(value=self.profile.dead_zone)
        dz_scale = ttk.Scale(dz_row, from_=0, to=400, variable=self.dz_var,
                             command=lambda v: self._on_tune_changed())
        dz_scale.pack(side="left", fill="x", expand=True)
        self.dz_label = ttk.Label(dz_row, width=6); self.dz_label.pack(side="left")
        self._add_tip(dz_lbl, DZ_TIP); self._add_tip(dz_scale, DZ_TIP)

        BT_TIP = ("Button threshold — for keys mapped to buttons (A, B, X, Y, bumpers…), how "
                  "far down the press must go to count as 'pressed'. 0.50 = halfway. Lower = a "
                  "lighter tap fires the button; higher = press more firmly. Keys mapped to "
                  "sticks or triggers ignore this — they stay fully proportional.")
        bt_row = ttk.Frame(tune); bt_row.pack(fill="x", padx=8, pady=4)
        bt_lbl = ttk.Label(bt_row, text="Button threshold:", width=20); bt_lbl.pack(side="left")
        self.bt_var = tk.DoubleVar(value=self.profile.button_threshold)
        bt_scale = ttk.Scale(bt_row, from_=0.1, to=1.0, variable=self.bt_var,
                             command=lambda v: self._on_tune_changed())
        bt_scale.pack(side="left", fill="x", expand=True)
        self.bt_label = ttk.Label(bt_row, width=6); self.bt_label.pack(side="left")
        self._add_tip(bt_lbl, BT_TIP); self._add_tip(bt_scale, BT_TIP)

        HZ_TIP = ("Output rate — how often the virtual Xbox pad is updated, in Hz. Default 1000 "
                  "(every 1 ms). Higher feels marginally smoother, but XInput/Xbox controllers and "
                  "most games only poll at 250–1000 Hz, so 1000 is the practical ceiling. NOTE: this "
                  "is NOT the keyboard's 8K key-polling rate — that's a separate keyboard-only path "
                  "this app doesn't use. Higher rates use a little more CPU.")
        hz_row = ttk.Frame(tune); hz_row.pack(fill="x", padx=8, pady=4)
        hz_lbl = ttk.Label(hz_row, text="Output rate (Hz):", width=20); hz_lbl.pack(side="left")
        self.hz_var = tk.IntVar(value=self.profile.tick_hz)
        hz_spin = ttk.Spinbox(hz_row, from_=MIN_TICK_HZ, to=MAX_TICK_HZ, increment=50,
                              textvariable=self.hz_var, width=8, command=self._on_tune_changed)
        hz_spin.pack(side="left")
        hz_spin.bind("<Return>", lambda e: self._on_tune_changed())
        hz_spin.bind("<FocusOut>", lambda e: self._on_tune_changed())
        ttk.Label(hz_row, text=f"  (virtual pad update rate, {MIN_TICK_HZ}–{MAX_TICK_HZ}; "
                  "not the keyboard's 8K key-polling)", foreground=MUTED).pack(side="left", padx=4)
        self._add_tip(hz_lbl, HZ_TIP); self._add_tip(hz_spin, HZ_TIP)

        # Live preview
        live = ttk.LabelFrame(self.root, text="Live output preview"); live.pack(fill="x", **pad)
        sticks = ttk.Frame(live); sticks.pack(side="left", padx=8, pady=4)
        self.lcanvas = tk.Canvas(sticks, width=96, height=96, bg=CARD, highlightthickness=0)
        self.lcanvas.grid(row=0, column=0, padx=6); ttk.Label(sticks, text="L stick").grid(row=1, column=0)
        self.rcanvas = tk.Canvas(sticks, width=96, height=96, bg=CARD, highlightthickness=0)
        self.rcanvas.grid(row=0, column=1, padx=6); ttk.Label(sticks, text="R stick").grid(row=1, column=1)
        trig = ttk.Frame(live); trig.pack(side="left", padx=12, fill="x", expand=True)
        ttk.Label(trig, text="LT").grid(row=0, column=0, sticky="w")
        self.lt_bar = ttk.Progressbar(trig, length=160, maximum=1.0); self.lt_bar.grid(row=0, column=1, padx=6, pady=3)
        ttk.Label(trig, text="RT").grid(row=1, column=0, sticky="w")
        self.rt_bar = ttk.Progressbar(trig, length=160, maximum=1.0); self.rt_bar.grid(row=1, column=1, padx=6, pady=3)
        self.btn_label = ttk.Label(trig, text="buttons: -"); self.btn_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        preview_tip = ("Live readout of the gamepad output your key presses produce — the dots are "
                       "the sticks, the bars are the triggers (LT/RT). Moves whether or not output "
                       "is running, so you can test bindings safely.")
        self._add_tip(self.lcanvas, preview_tip); self._add_tip(self.rcanvas, preview_tip)

        # Control bar: status on the left, primary action anchored bottom-right.
        ctrl = ttk.Frame(self.root); ctrl.pack(fill="x", padx=16, pady=(6, 14))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(ctrl, textvariable=self.status_var, foreground=MUTED).pack(side="left", padx=(0, 8))
        # Packed right-first so Start (primary) sits rightmost, Pause just to its left.
        self.start_btn = ttk.Button(ctrl, text="Start output", command=self.toggle_running,
                                    style="Accent.TButton")
        self.start_btn.pack(side="right")
        self.pause_btn = ttk.Button(ctrl, text="Pause (F8)", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="right", padx=6)
        self._add_tip(self.start_btn, "Start sending gamepad output to games. Click again to stop. "
                      "Needs ViGEmBus installed for real output.")
        self._add_tip(self.pause_btn, "Pause or resume output without stopping. Works globally — "
                      "even from inside a game — via the F8 hotkey.")

        self._update_tune_labels()

    # ------------------------------------------------------------------ helpers

    def _add_tip(self, widget: tk.Widget, text: str) -> None:
        self._tooltips.append(Tooltip(widget, text))

    def _set_status(self, msg: str) -> None:
        if hasattr(self, "status_var"):
            self.status_var.set(msg)

    def _update_tune_labels(self) -> None:
        self.dz_label.config(text=f"{int(self.dz_var.get())}")
        self.bt_label.config(text=f"{self.bt_var.get():.2f}")

    def _on_tune_changed(self) -> None:
        self.profile.dead_zone = int(self.dz_var.get())
        self.profile.button_threshold = float(self.bt_var.get())
        try:
            hz = int(self.hz_var.get())
            clamped = max(MIN_TICK_HZ, min(MAX_TICK_HZ, hz))
            self.profile.tick_hz = clamped
            if clamped != hz:
                self.hz_var.set(clamped)  # reflect an out-of-range typed value
        except (tk.TclError, ValueError):
            pass  # mid-edit / empty spinbox; keep the last good value
        if self.mapper is not None:
            self.mapper.profile = self.profile
            self.mapper._build_runtime()
        self._update_tune_labels()

    def _sync_tuning_from_profile(self) -> None:
        self.dz_var.set(self.profile.dead_zone)
        self.bt_var.set(self.profile.button_threshold)
        self.hz_var.set(self.profile.tick_hz)
        self._update_tune_labels()

    def _refresh_bindings_table(self) -> None:
        for it in self.tree.get_children():
            self.tree.delete(it)
        for label, target in sorted(self.profile.bindings.items()):
            ki = self.keymap.by_label.get(label)
            mx = self.keymap.calibration.get(ki, {}).get("max", "-") if ki is not None else "-"
            self.tree.insert("", "end", values=(label, ki if ki is not None else "(unlearned)", mx, target))

    # ------------------------------------------------------------------ profiles

    def _load_initial_profile(self) -> AnalogProfile:
        """Restore the last-used profile across launches; fall back to a default."""
        names = list_profiles()
        last = get_last_profile()
        if last and last in names:
            try:
                return load_profile(last)
            except Exception:
                pass
        if "analog_fps" in names:
            return load_profile("analog_fps")
        return AnalogProfile.default_fps()

    def _load_selected_profile(self) -> None:
        name = self.profile_var.get()
        try:
            self.profile = load_profile(name)
        except FileNotFoundError:
            messagebox.showerror("Error", f"Profile '{name}' not found"); return
        if self.mapper is not None:
            self.mapper.set_profile(self.profile)
        set_last_profile(name)
        self._sync_tuning_from_profile()
        self._refresh_bindings_table()
        self._set_status(f"Loaded profile '{name}'")

    def _save_current_profile(self) -> None:
        self._on_tune_changed()
        save_profile(self.profile)
        set_last_profile(self.profile.name)
        self.profile_combo["values"] = list_profiles()
        self._set_status(f"Saved '{self.profile.name}'")

    def _new_profile(self) -> None:
        name = simpledialog.askstring("New profile", "Name:")
        if not name:
            return
        if name in list_profiles():
            messagebox.showerror("Error", f"'{name}' already exists"); return
        self.profile = AnalogProfile(name=name)
        save_profile(self.profile)
        self.profile_combo["values"] = list_profiles()
        self.profile_var.set(name)
        self._load_selected_profile()

    def _delete_current_profile(self) -> None:
        name = self.profile_var.get()
        if name in ("analog_fps", "analog_racing"):
            messagebox.showinfo("Nope", "Built-in defaults regenerate; can't delete."); return
        if not messagebox.askyesno("Confirm", f"Delete '{name}'?"):
            return
        (ANALOG_PROFILE_DIR / f"{name}.json").unlink(missing_ok=True)
        self.profile_combo["values"] = list_profiles()
        self.profile_var.set("analog_fps")
        self._load_selected_profile()

    # ------------------------------------------------------------------ bindings

    def _selected_label(self) -> str | None:
        sel = self.tree.selection()
        return str(self.tree.item(sel[0])["values"][0]) if sel else None

    def _add_binding(self) -> None:
        known = sorted(self.keymap.by_label.keys())
        label = self._pick_from_list("Pick a learned key (or Cancel then 'Learn key'):", known) if known else None
        if label is None:
            if known:
                return
            messagebox.showinfo("No keys learned", "Use 'Learn key...' first to teach a key.")
            return
        target = self._pick_from_list("Map to which gamepad target?", SORTED_TARGETS)
        if target is None:
            return
        self.profile.bindings[label] = target
        self._refresh_bindings_table()

    def _edit_binding(self) -> None:
        label = self._selected_label()
        if not label:
            return
        target = self._pick_from_list(f"Target for '{label}':", SORTED_TARGETS,
                                      current=self.profile.bindings.get(label))
        if target is None:
            return
        self.profile.bindings[label] = target
        if self.mapper is not None:
            self.mapper._build_runtime()
        self._refresh_bindings_table()

    def _remove_binding(self) -> None:
        label = self._selected_label()
        if not label:
            return
        self.profile.bindings.pop(label, None)
        if self.mapper is not None:
            self.mapper._build_runtime()
        self._refresh_bindings_table()

    # ------------------------------------------------------------------ discovery / calibration

    def _learn_key(self) -> None:
        if not self.monitor_ok:
            messagebox.showerror("No keyboard", "Keyboard not connected. Click 'Reconnect kbd'."); return
        captured = self._capture_key_press("Press and FULLY hold the key to learn, then release.")
        if not captured or captured[0] is None:
            return
        idx, peak = captured
        label = simpledialog.askstring("Label", f"key_index={idx} (full-press depth {peak}).\n"
                                                 f"Name this key (e.g. w, space, shift):")
        if not label:
            return
        self.keymap.learn(label.strip(), idx, peak)
        try:
            self.keymap.save()
        except Exception as e:
            messagebox.showwarning("Save failed", f"Could not save keymap: {e}")
        if self.mapper is not None:
            self.mapper._build_runtime()
        self._refresh_bindings_table()
        self._set_status(f"Learned '{label.strip()}' -> key_index {idx} (max {peak})")

    def _calibrate_selected(self) -> None:
        label = self._selected_label()
        if not label or label not in self.keymap.by_label:
            messagebox.showinfo("Calibrate", "Select a learned binding row first."); return
        if not self.monitor_ok:
            messagebox.showerror("No keyboard", "Keyboard not connected."); return
        captured = self._capture_key_press(f"Press '{label}' FULLY down, then release.")
        if not captured or captured[1] <= 0:
            return
        idx = self.keymap.by_label[label]
        self.keymap.calibration[idx] = {"max": captured[1]}
        try:
            self.keymap.save()
        except Exception:
            pass
        if self.mapper is not None:
            self.mapper._build_runtime()
        self._refresh_bindings_table()
        self._set_status(f"Calibrated '{label}' max={captured[1]}")

    def _capture_key_press(self, prompt: str) -> tuple[int | None, int] | None:
        """Modal: watch the live depth stream and capture the strongest-pressed key."""
        dlg = tk.Toplevel(self.root); dlg.title("Capture key"); dlg.geometry("440x170")
        dlg.transient(self.root); dlg.grab_set(); dlg.configure(bg=SURFACE)
        ttk.Label(dlg, text=prompt, wraplength=400).pack(pady=(14, 8), padx=14)
        live = ttk.Label(dlg, text="(waiting for a key press…)", foreground=MUTED)
        live.pack(pady=4)
        st = {"best_idx": None, "best_peak": 0, "done": False, "result": None}

        def poll():
            if st["done"]:
                return
            snap = self.monitor.snapshot()
            if snap:
                idx = max(snap, key=lambda k: snap[k])
                d = snap[idx]
                if d > st["best_peak"]:
                    st["best_peak"] = d; st["best_idx"] = idx
            live.config(text=f"strongest key so far: index={st['best_idx']}  peak depth={st['best_peak']}")
            dlg.after(40, poll)

        def capture():
            st["result"] = (st["best_idx"], st["best_peak"]); st["done"] = True; dlg.destroy()

        def cancel():
            st["done"] = True; dlg.destroy()

        row = ttk.Frame(dlg); row.pack(pady=8)
        ttk.Button(row, text="Capture", command=capture).pack(side="left", padx=4)
        ttk.Button(row, text="Cancel", command=cancel).pack(side="left", padx=4)
        poll()
        self.root.wait_window(dlg)
        return st["result"]

    def _pick_from_list(self, prompt: str, items: list[str], current: str | None = None) -> str | None:
        dlg = tk.Toplevel(self.root); dlg.title("Pick"); dlg.geometry("320x440")
        dlg.transient(self.root); dlg.grab_set(); dlg.configure(bg=SURFACE)
        ttk.Label(dlg, text=prompt, wraplength=290).pack(pady=(12, 6), padx=12)
        lb = tk.Listbox(dlg, height=18, bg=CARD, fg=TEXT, borderwidth=0,
                        highlightthickness=0, relief="flat", activestyle="none",
                        selectbackground=ACCENT, selectforeground="#10243a",
                        font=("Segoe UI", 10))
        for it in items:
            lb.insert("end", it)
        lb.pack(fill="both", expand=True, padx=12, pady=4)
        if current and current in items:
            i = items.index(current); lb.selection_set(i); lb.see(i)
        chosen = {"v": None}

        def ok():
            sel = lb.curselection()
            if sel:
                chosen["v"] = items[sel[0]]
            dlg.destroy()

        ttk.Button(dlg, text="OK", command=ok).pack(pady=4)
        lb.bind("<Double-1>", lambda e: ok())
        self.root.wait_window(dlg)
        return chosen["v"]

    # ------------------------------------------------------------------ control

    def toggle_running(self) -> None:
        if self.mapper is None:
            return
        if self.mapper.running:
            self.mapper.stop()
            self.start_btn.config(text="Start output")
            self.pause_btn.config(state="disabled")
            self._set_status("Output stopped (still listening for preview).")
        else:
            self._on_tune_changed()
            self.mapper.start()
            self.start_btn.config(text="Stop output")
            self.pause_btn.config(state="normal")
            self._set_status("Output running. F8 pauses globally.")

    def toggle_pause(self) -> None:
        if self.mapper is None or not self.mapper.running:
            return
        self.mapper.toggle_enabled()
        paused = not self.mapper.enabled
        self.pause_btn.config(text="Resume (F8)" if paused else "Pause (F8)")
        self._set_status("PAUSED (F8 to resume)." if paused else "Output running.")

    def _reconnect(self) -> None:
        try:
            self.monitor.stop()
        except Exception:
            pass
        self.monitor = DepthMonitor()
        self._start_monitor()
        # Re-point the mapper at the new monitor.
        if self.mapper is not None:
            self.mapper.monitor = self.monitor
        self._set_status("Reconnected." if self.monitor_ok else "Still no keyboard.")

    # ------------------------------------------------------------------ updates

    def _on_check_for_updates(self) -> None:
        """Query GitHub on a worker thread; never block the UI."""
        self.update_btn.config(state="disabled")
        self._set_status("Checking for updates…")

        def work() -> None:
            info = updater.check_for_update(__version__)
            self.root.after(0, lambda: self._on_check_result(info))

        threading.Thread(target=work, daemon=True).start()

    def _on_check_result(self, info: "updater.UpdateInfo") -> None:
        if not self.root.winfo_exists():
            return
        self.update_btn.config(state="normal")

        if info.error:
            self._set_status("Update check failed.")
            messagebox.showwarning("Check for updates", info.error)
            return

        if not info.is_update_available:
            self._set_status(f"Up to date (v{info.current_version}).")
            messagebox.showinfo(
                "Check for updates",
                f"You're on the latest version (v{info.current_version}).")
            return

        self._set_status(f"Update available: v{info.latest_version}.")

        # Running from source: an .exe download is meaningless — point at git/releases.
        if not getattr(sys, "frozen", False):
            if messagebox.askyesno(
                    "Update available",
                    f"v{info.latest_version} is available (you have v{info.current_version}).\n\n"
                    "You're running from source — update with 'git pull'.\n\n"
                    "Open the releases page?"):
                webbrowser.open(info.release_url)
            return

        # Frozen build but no .exe attached to the release: fall back to the web page.
        if not info.asset_url:
            if messagebox.askyesno(
                    "Update available",
                    f"v{info.latest_version} is available, but no .exe was attached to the "
                    "release yet.\n\nOpen the releases page?"):
                webbrowser.open(info.release_url)
            return

        notes = (info.notes or "").strip()
        if len(notes) > 1200:
            notes = notes[:1200].rstrip() + "\n…"
        msg = f"v{info.latest_version} is available (you have v{info.current_version})."
        if notes:
            msg += f"\n\n{notes}"
        msg += "\n\nDownload the new keypad-gamepad-analog.exe now?"
        if messagebox.askyesno("Update available", msg):
            self._start_download(info)

    def _start_download(self, info: "updater.UpdateInfo") -> None:
        self.update_btn.config(state="disabled")
        dlg = tk.Toplevel(self.root); dlg.title("Downloading update")
        dlg.geometry("440x150"); dlg.transient(self.root); dlg.configure(bg=SURFACE)
        ttk.Label(dlg, text=f"Downloading keypad-gamepad-analog v{info.latest_version}…",
                  wraplength=400).pack(pady=(16, 8), padx=16)
        bar = ttk.Progressbar(dlg, length=380, mode="determinate", maximum=100)
        bar.pack(pady=6, padx=16)
        pct = ttk.Label(dlg, text="", foreground=MUTED); pct.pack()
        state = {"indeterminate": False}
        filename = f"keypad-gamepad-analog-v{info.latest_version}.exe"

        def progress(done: int, total: int | None) -> None:
            def upd() -> None:
                if not dlg.winfo_exists():
                    return
                if total:
                    bar["value"] = done * 100 / total
                    pct.config(text=f"{done // 1024:,} / {total // 1024:,} KB")
                else:
                    if not state["indeterminate"]:
                        state["indeterminate"] = True
                        bar.config(mode="indeterminate"); bar.start(15)
                    pct.config(text=f"{done // 1024:,} KB")
            self.root.after(0, upd)

        def work() -> None:
            try:
                path = updater.download_asset(
                    info.asset_url, updater.default_download_dir(),
                    filename=filename, progress_cb=progress)
            except Exception as e:  # noqa: BLE001 - report any download failure to the user
                self.root.after(0, lambda: self._download_failed(dlg, e))
                return
            self.root.after(0, lambda: self._download_done(dlg, path))

        threading.Thread(target=work, daemon=True).start()

    def _download_failed(self, dlg: tk.Toplevel, err: Exception) -> None:
        if dlg.winfo_exists():
            dlg.destroy()
        self.update_btn.config(state="normal")
        self._set_status("Download failed.")
        messagebox.showerror("Download failed", f"Could not download the update:\n{err}")

    def _download_done(self, dlg: tk.Toplevel, path) -> None:
        if dlg.winfo_exists():
            dlg.destroy()
        self.update_btn.config(state="normal")
        self._set_status(f"Downloaded to {path}")
        if messagebox.askyesno(
                "Update downloaded",
                f"Saved to:\n{path}\n\nClose this app and run the new file to update.\n\n"
                "Show it in Explorer now?"):
            updater.reveal_in_explorer(path)

    # ------------------------------------------------------------------ live preview loop

    def _draw_stick(self, canvas: tk.Canvas, x: float, y: float) -> None:
        canvas.delete("all")
        w = int(canvas["width"]); h = int(canvas["height"])
        cx, cy = w / 2, h / 2
        canvas.create_oval(5, 5, w - 5, h - 5, outline=GRID, width=1)
        canvas.create_line(cx, 7, cx, h - 7, fill=GRID)
        canvas.create_line(7, cy, w - 7, cy, fill=GRID)
        px = cx + x * (w / 2 - 9)
        py = cy - y * (h / 2 - 9)
        # faint trail from centre to the dot, then the accent dot
        if abs(x) > 0.01 or abs(y) > 0.01:
            canvas.create_line(cx, cy, px, py, fill=ACCENT, width=2)
        canvas.create_oval(px - 7, py - 7, px + 7, py + 7, fill=ACCENT, outline="")

    def _tick_preview(self) -> None:
        try:
            if self.mapper is not None and self.monitor_ok:
                st = self.mapper.compute_state(self.monitor.snapshot())
                self._draw_stick(self.lcanvas, st["lx"], st["ly"])
                self._draw_stick(self.rcanvas, st["rx"], st["ry"])
                self.lt_bar["value"] = st["lt"]
                self.rt_bar["value"] = st["rt"]
                self.btn_label.config(text=f"buttons: {', '.join(st['buttons']) or '-'}")
            else:
                self._draw_stick(self.lcanvas, 0, 0)
                self._draw_stick(self.rcanvas, 0, 0)
        except Exception:
            pass
        self.root.after(50, self._tick_preview)

    # ------------------------------------------------------------------ tray + close

    def _setup_tray(self) -> None:
        img = Image.new("RGB", (64, 64), "#1d1f24")
        d = ImageDraw.Draw(img)
        d.ellipse((10, 22, 54, 50), fill="#4fa3ff")
        d.text((26, 28), "G", fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self.root.after(0, self._show_window), default=True),
            pystray.MenuItem("Start/Stop output", lambda: self.root.after(0, self.toggle_running)),
            pystray.MenuItem("Pause/Resume (F8)", lambda: self.root.after(0, self.toggle_pause)),
            pystray.MenuItem("Quit", lambda: self.root.after(0, self._really_quit)),
        )
        self.tray = pystray.Icon("keypad-gamepad", img, "keypad-gamepad ANALOG", menu)
        import threading
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _show_window(self) -> None:
        self.root.deiconify(); self.root.lift()

    def _on_close(self) -> None:
        # With a tray, the window close button hides to tray; otherwise it quits.
        if self.tray is not None:
            self.root.withdraw()
            self._set_status("Minimized to tray.")
        else:
            self._really_quit()

    def _really_quit(self) -> None:
        self._hotkey_stop = True
        try:
            if self.mapper is not None:
                self.mapper.stop()
        except Exception:
            pass
        try:
            self.monitor.stop()
        except Exception:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    sv_ttk.set_theme("dark")  # Windows 11-style dark theme for all ttk widgets
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
