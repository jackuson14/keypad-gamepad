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

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from hid_protocol import DepthMonitor
from winhotkey import start_hotkey, VK_F8
from analog_mapper import (
    AnalogMapper, AnalogProfile, Keymap,
    XBOX_BUTTONS, SPECIAL_TARGETS,
    ensure_defaults_exist, list_profiles, load_profile, save_profile,
    load_keymap, ANALOG_PROFILE_DIR,
)

SORTED_TARGETS = sorted(XBOX_BUTTONS.keys()) + sorted(SPECIAL_TARGETS)

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
        root.geometry("760x720")

        ensure_defaults_exist()
        self.profile: AnalogProfile = load_profile("analog_fps") if "analog_fps" in list_profiles() \
            else AnalogProfile.default_fps()
        self.keymap = load_keymap()

        # One shared monitor for live preview + wizards + the engine.
        self.monitor = DepthMonitor()
        self.monitor_ok = False
        self.mapper: AnalogMapper | None = None
        self.tray = None
        self._hotkey_stop = False

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
                text="  ViGEmBus not attached - DRY-RUN (live preview only). "
                     "Install/upgrade ViGEmBus 1.22.0 for real output.",
                background="#f3d6d6", foreground="#7a1f1f")
        else:
            self.vigem_banner.config(
                text="  Virtual Xbox 360 pad attached via ViGEmBus.",
                background="#d6f3da", foreground="#1f5a2a")
        if self.mapper.unresolved_labels:
            self._set_status(f"Bindings with no learned key: {self.mapper.unresolved_labels} "
                             f"- use 'Learn key' to teach them.")

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        self.vigem_banner = tk.Label(self.root, text="  (checking ViGEmBus...)",
                                     anchor="w", relief="groove")
        self.vigem_banner.pack(fill="x")

        # Profile row
        top = ttk.Frame(self.root); top.pack(fill="x", **pad)
        ttk.Label(top, text="Profile:").pack(side="left")
        self.profile_var = tk.StringVar(value=self.profile.name)
        self.profile_combo = ttk.Combobox(top, textvariable=self.profile_var,
                                          values=list_profiles(), state="readonly", width=22)
        self.profile_combo.pack(side="left", padx=4)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda e: self._load_selected_profile())
        ttk.Button(top, text="New...", command=self._new_profile).pack(side="left", padx=2)
        ttk.Button(top, text="Save", command=self._save_current_profile).pack(side="left", padx=2)
        ttk.Button(top, text="Delete", command=self._delete_current_profile).pack(side="left", padx=2)
        ttk.Button(top, text="Reconnect kbd", command=self._reconnect).pack(side="right", padx=2)

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
        bcol = ttk.Frame(bframe); bcol.pack(side="left", fill="y", padx=4)
        ttk.Button(bcol, text="Add...", command=self._add_binding).pack(fill="x", pady=2)
        ttk.Button(bcol, text="Edit target...", command=self._edit_binding).pack(fill="x", pady=2)
        ttk.Button(bcol, text="Remove", command=self._remove_binding).pack(fill="x", pady=2)
        ttk.Separator(bcol, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(bcol, text="Learn key...", command=self._learn_key).pack(fill="x", pady=2)
        ttk.Button(bcol, text="Calibrate sel.", command=self._calibrate_selected).pack(fill="x", pady=2)

        # Tuning
        tune = ttk.LabelFrame(self.root, text="Tuning"); tune.pack(fill="x", **pad)
        dz_row = ttk.Frame(tune); dz_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(dz_row, text="Dead-zone (depth):", width=20).pack(side="left")
        self.dz_var = tk.IntVar(value=self.profile.dead_zone)
        ttk.Scale(dz_row, from_=0, to=400, variable=self.dz_var,
                  command=lambda v: self._on_tune_changed()).pack(side="left", fill="x", expand=True)
        self.dz_label = ttk.Label(dz_row, width=6); self.dz_label.pack(side="left")
        bt_row = ttk.Frame(tune); bt_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(bt_row, text="Button threshold:", width=20).pack(side="left")
        self.bt_var = tk.DoubleVar(value=self.profile.button_threshold)
        ttk.Scale(bt_row, from_=0.1, to=1.0, variable=self.bt_var,
                  command=lambda v: self._on_tune_changed()).pack(side="left", fill="x", expand=True)
        self.bt_label = ttk.Label(bt_row, width=6); self.bt_label.pack(side="left")

        # Live preview
        live = ttk.LabelFrame(self.root, text="Live output preview"); live.pack(fill="x", **pad)
        sticks = ttk.Frame(live); sticks.pack(side="left", padx=8, pady=4)
        self.lcanvas = tk.Canvas(sticks, width=90, height=90, bg="#1d1f24", highlightthickness=0)
        self.lcanvas.grid(row=0, column=0, padx=6); ttk.Label(sticks, text="L stick").grid(row=1, column=0)
        self.rcanvas = tk.Canvas(sticks, width=90, height=90, bg="#1d1f24", highlightthickness=0)
        self.rcanvas.grid(row=0, column=1, padx=6); ttk.Label(sticks, text="R stick").grid(row=1, column=1)
        trig = ttk.Frame(live); trig.pack(side="left", padx=12, fill="x", expand=True)
        ttk.Label(trig, text="LT").grid(row=0, column=0, sticky="w")
        self.lt_bar = ttk.Progressbar(trig, length=160, maximum=1.0); self.lt_bar.grid(row=0, column=1, padx=6, pady=3)
        ttk.Label(trig, text="RT").grid(row=1, column=0, sticky="w")
        self.rt_bar = ttk.Progressbar(trig, length=160, maximum=1.0); self.rt_bar.grid(row=1, column=1, padx=6, pady=3)
        self.btn_label = ttk.Label(trig, text="buttons: -"); self.btn_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=4)

        # Control bar
        ctrl = ttk.Frame(self.root); ctrl.pack(fill="x", **pad)
        self.start_btn = ttk.Button(ctrl, text="Start output", command=self.toggle_running)
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(ctrl, text="Pause (F8)", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(ctrl, textvariable=self.status_var, foreground="#555").pack(side="left", padx=8)

        self._update_tune_labels()

    # ------------------------------------------------------------------ helpers

    def _set_status(self, msg: str) -> None:
        if hasattr(self, "status_var"):
            self.status_var.set(msg)

    def _update_tune_labels(self) -> None:
        self.dz_label.config(text=f"{int(self.dz_var.get())}")
        self.bt_label.config(text=f"{self.bt_var.get():.2f}")

    def _on_tune_changed(self) -> None:
        self.profile.dead_zone = int(self.dz_var.get())
        self.profile.button_threshold = float(self.bt_var.get())
        if self.mapper is not None:
            self.mapper.profile = self.profile
            self.mapper._build_runtime()
        self._update_tune_labels()

    def _sync_tuning_from_profile(self) -> None:
        self.dz_var.set(self.profile.dead_zone)
        self.bt_var.set(self.profile.button_threshold)
        self._update_tune_labels()

    def _refresh_bindings_table(self) -> None:
        for it in self.tree.get_children():
            self.tree.delete(it)
        for label, target in sorted(self.profile.bindings.items()):
            ki = self.keymap.by_label.get(label)
            mx = self.keymap.calibration.get(ki, {}).get("max", "-") if ki is not None else "-"
            self.tree.insert("", "end", values=(label, ki if ki is not None else "(unlearned)", mx, target))

    # ------------------------------------------------------------------ profiles

    def _load_selected_profile(self) -> None:
        name = self.profile_var.get()
        try:
            self.profile = load_profile(name)
        except FileNotFoundError:
            messagebox.showerror("Error", f"Profile '{name}' not found"); return
        if self.mapper is not None:
            self.mapper.set_profile(self.profile)
        self._sync_tuning_from_profile()
        self._refresh_bindings_table()
        self._set_status(f"Loaded profile '{name}'")

    def _save_current_profile(self) -> None:
        self._on_tune_changed()
        save_profile(self.profile)
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
        dlg = tk.Toplevel(self.root); dlg.title("Capture key"); dlg.geometry("420x150")
        dlg.transient(self.root); dlg.grab_set()
        ttk.Label(dlg, text=prompt, wraplength=400).pack(pady=8)
        live = ttk.Label(dlg, text="(waiting for a key press...)", foreground="#555")
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
        dlg = tk.Toplevel(self.root); dlg.title("Pick"); dlg.geometry("300x420")
        dlg.transient(self.root); dlg.grab_set()
        ttk.Label(dlg, text=prompt, wraplength=280).pack(pady=6)
        lb = tk.Listbox(dlg, height=18)
        for it in items:
            lb.insert("end", it)
        lb.pack(fill="both", expand=True, padx=8, pady=4)
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

    # ------------------------------------------------------------------ live preview loop

    def _draw_stick(self, canvas: tk.Canvas, x: float, y: float) -> None:
        canvas.delete("all")
        w = int(canvas["width"]); h = int(canvas["height"])
        cx, cy = w / 2, h / 2
        canvas.create_oval(4, 4, w - 4, h - 4, outline="#3a3f4a")
        canvas.create_line(cx, 6, cx, h - 6, fill="#2a2e36")
        canvas.create_line(6, cy, w - 6, cy, fill="#2a2e36")
        px = cx + x * (w / 2 - 8)
        py = cy - y * (h / 2 - 8)
        canvas.create_oval(px - 6, py - 6, px + 6, py + 6, fill="#4fa3ff", outline="")

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
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
