"""
GUI for keypad-gamepad.

Uses Tkinter (ships with Python — no extra install) for a small control panel:
  - profile selector
  - editable bindings table (key -> gamepad target)
  - sliders for stick ramp, mouse sensitivity, walk deflection
  - start/stop and pause toggle
  - global hotkey F8 to pause/resume without alt-tabbing out of a game
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import threading

import keyboard as kb_lib  # for global hotkey

from mapper import (
    Mapper, Profile, ALL_TARGETS, XBOX_BUTTONS, SPECIAL_TARGETS,
    save_profile, load_profile, list_profiles, ensure_defaults_exist,
)


SORTED_TARGETS = sorted(XBOX_BUTTONS.keys()) + sorted(SPECIAL_TARGETS)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("keypad-gamepad — MonsGeek M1 V5 gamepad mode")
        root.geometry("620x640")

        ensure_defaults_exist()
        self.profile: Profile = load_profile("default_fps")
        self.mapper = Mapper(self.profile)
        self.mapper.subscribe(self._on_status)

        self._build_ui()
        self._refresh_bindings_table()

        # Register global hotkey: F8 toggles pause/resume.
        # We do this even before "Start" is clicked, so user knows hotkey works
        # once they've started the mapper.
        kb_lib.add_hotkey("f8", self.toggle_pause)

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # --- Top: profile selector + load/save/new buttons ---
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Profile:").pack(side="left")
        self.profile_var = tk.StringVar(value=self.profile.name)
        self.profile_combo = ttk.Combobox(
            top, textvariable=self.profile_var,
            values=list_profiles(), state="readonly", width=22,
        )
        self.profile_combo.pack(side="left", padx=4)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda e: self._load_selected_profile())

        ttk.Button(top, text="New…", command=self._new_profile).pack(side="left", padx=2)
        ttk.Button(top, text="Save", command=self._save_current_profile).pack(side="left", padx=2)
        ttk.Button(top, text="Delete", command=self._delete_current_profile).pack(side="left", padx=2)

        # --- Bindings table ---
        bindings_frame = ttk.LabelFrame(self.root, text="Key bindings")
        bindings_frame.pack(fill="both", expand=True, **pad)

        cols = ("key", "target")
        self.tree = ttk.Treeview(bindings_frame, columns=cols, show="headings", height=14)
        self.tree.heading("key", text="Keyboard key")
        self.tree.heading("target", text="Gamepad target")
        self.tree.column("key", width=200)
        self.tree.column("target", width=300)
        self.tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<Double-1>", lambda e: self._edit_binding())

        scrollbar = ttk.Scrollbar(bindings_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side="left", fill="y")

        btn_col = ttk.Frame(bindings_frame)
        btn_col.pack(side="left", fill="y", padx=4)
        ttk.Button(btn_col, text="Add…", command=self._add_binding).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="Edit…", command=self._edit_binding).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="Remove", command=self._remove_binding).pack(fill="x", pady=2)

        # --- Tuning sliders ---
        tune = ttk.LabelFrame(self.root, text="Tuning")
        tune.pack(fill="x", **pad)

        # Stick ramp
        ramp_row = ttk.Frame(tune)
        ramp_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(ramp_row, text="Stick ramp (ms):", width=22).pack(side="left")
        self.ramp_var = tk.IntVar(value=self.profile.stick_ramp_ms)
        self.ramp_scale = ttk.Scale(
            ramp_row, from_=0, to=500, variable=self.ramp_var,
            command=lambda v: self._on_tune_changed(),
        )
        self.ramp_scale.pack(side="left", fill="x", expand=True)
        self.ramp_label = ttk.Label(ramp_row, width=6)
        self.ramp_label.pack(side="left")

        # Walk deflection
        walk_row = ttk.Frame(tune)
        walk_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(walk_row, text="Walk deflection (%):", width=22).pack(side="left")
        self.walk_var = tk.IntVar(value=int(self.profile.walk_deflection * 100))
        self.walk_scale = ttk.Scale(
            walk_row, from_=10, to=100, variable=self.walk_var,
            command=lambda v: self._on_tune_changed(),
        )
        self.walk_scale.pack(side="left", fill="x", expand=True)
        self.walk_label = ttk.Label(walk_row, width=6)
        self.walk_label.pack(side="left")

        # Mouse sensitivity
        mouse_row = ttk.Frame(tune)
        mouse_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(mouse_row, text="Mouse sensitivity:", width=22).pack(side="left")
        self.mouse_var = tk.DoubleVar(value=self.profile.mouse_sensitivity)
        self.mouse_scale = ttk.Scale(
            mouse_row, from_=1.0, to=30.0, variable=self.mouse_var,
            command=lambda v: self._on_tune_changed(),
        )
        self.mouse_scale.pack(side="left", fill="x", expand=True)
        self.mouse_label = ttk.Label(mouse_row, width=6)
        self.mouse_label.pack(side="left")

        # Mouse-as-rstick checkbox + walk modifier entry
        opts_row = ttk.Frame(tune)
        opts_row.pack(fill="x", padx=8, pady=4)
        self.mouse_enabled_var = tk.BooleanVar(value=self.profile.mouse_as_rstick)
        ttk.Checkbutton(
            opts_row, text="Use mouse as right stick",
            variable=self.mouse_enabled_var,
            command=self._on_tune_changed,
        ).pack(side="left")

        ttk.Label(opts_row, text="    Walk modifier:").pack(side="left")
        self.walk_mod_var = tk.StringVar(value=self.profile.walk_modifier or "")
        ttk.Entry(opts_row, textvariable=self.walk_mod_var, width=10).pack(side="left", padx=4)
        self.walk_mod_var.trace_add("write", lambda *a: self._on_tune_changed())

        # --- Control bar at bottom ---
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", **pad)

        self.start_btn = ttk.Button(ctrl, text="Start mapper", command=self.toggle_running)
        self.start_btn.pack(side="left")

        self.pause_btn = ttk.Button(ctrl, text="Pause (F8)", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="Idle. Make sure ViGEmBus driver is installed.")
        ttk.Label(ctrl, textvariable=self.status_var, foreground="#555").pack(side="left", padx=8)

        self._update_tune_labels()

    # ------------------------------------------------------------------ helpers

    def _update_tune_labels(self) -> None:
        self.ramp_label.config(text=f"{self.ramp_var.get()}")
        self.walk_label.config(text=f"{self.walk_var.get()}%")
        self.mouse_label.config(text=f"{self.mouse_var.get():.1f}")

    def _on_tune_changed(self) -> None:
        self.profile.stick_ramp_ms = int(self.ramp_var.get())
        self.profile.walk_deflection = self.walk_var.get() / 100.0
        self.profile.mouse_sensitivity = float(self.mouse_var.get())
        self.profile.mouse_as_rstick = self.mouse_enabled_var.get()
        self.profile.walk_modifier = self.walk_mod_var.get().strip() or None
        # Push the mutated profile to the mapper so changes take effect live
        self.mapper.profile = self.profile
        self._update_tune_labels()

    def _refresh_bindings_table(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for key, target in sorted(self.profile.bindings.items()):
            self.tree.insert("", "end", values=(key, target))

    # ------------------------------------------------------------------ profile actions

    def _load_selected_profile(self) -> None:
        name = self.profile_var.get()
        try:
            self.profile = load_profile(name)
        except FileNotFoundError:
            messagebox.showerror("Error", f"Profile '{name}' not found")
            return
        self.mapper.set_profile(self.profile)
        # Sync UI to loaded profile
        self.ramp_var.set(self.profile.stick_ramp_ms)
        self.walk_var.set(int(self.profile.walk_deflection * 100))
        self.mouse_var.set(self.profile.mouse_sensitivity)
        self.mouse_enabled_var.set(self.profile.mouse_as_rstick)
        self.walk_mod_var.set(self.profile.walk_modifier or "")
        self._update_tune_labels()
        self._refresh_bindings_table()

    def _save_current_profile(self) -> None:
        self._on_tune_changed()  # make sure tuning values are pulled in
        save_profile(self.profile)
        self.profile_combo["values"] = list_profiles()
        self.status_var.set(f"Saved profile '{self.profile.name}'")

    def _new_profile(self) -> None:
        name = simpledialog.askstring("New profile", "Name for new profile:")
        if not name:
            return
        if name in list_profiles():
            messagebox.showerror("Error", f"Profile '{name}' already exists")
            return
        # Start from a copy of the current profile but with no bindings
        new_prof = Profile(name=name)
        save_profile(new_prof)
        self.profile_combo["values"] = list_profiles()
        self.profile_var.set(name)
        self._load_selected_profile()

    def _delete_current_profile(self) -> None:
        from mapper import PROFILE_DIR
        name = self.profile_var.get()
        if name in ("default_fps", "default_racing"):
            messagebox.showinfo("Nope", "Built-in defaults can't be deleted (they'll just regenerate).")
            return
        if not messagebox.askyesno("Confirm", f"Delete profile '{name}'?"):
            return
        (PROFILE_DIR / f"{name}.json").unlink(missing_ok=True)
        self.profile_combo["values"] = list_profiles()
        self.profile_var.set("default_fps")
        self._load_selected_profile()

    # ------------------------------------------------------------------ binding actions

    def _add_binding(self) -> None:
        # Capture a key, then ask for target
        captured = self._capture_key("Press the keyboard key to bind…")
        if captured is None:
            return
        target = self._pick_target("Map to which gamepad target?")
        if target is None:
            return
        self.profile.bindings[captured] = target
        self._refresh_bindings_table()

    def _edit_binding(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        key = self.tree.item(sel[0])["values"][0]
        target = self._pick_target(f"Change target for '{key}':", current=self.profile.bindings.get(str(key)))
        if target is None:
            return
        self.profile.bindings[str(key)] = target
        self._refresh_bindings_table()

    def _remove_binding(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        key = str(self.tree.item(sel[0])["values"][0])
        self.profile.bindings.pop(key, None)
        self._refresh_bindings_table()

    def _capture_key(self, prompt: str) -> str | None:
        """Pop up a modal that captures the next keypress."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Key capture")
        dlg.geometry("360x100")
        dlg.transient(self.root)
        dlg.grab_set()
        ttk.Label(dlg, text=prompt).pack(pady=10)
        status = ttk.Label(dlg, text="(waiting…)", foreground="#888")
        status.pack()

        result: dict[str, str | None] = {"key": None}

        def on_event(event):
            if event.event_type == "down":
                result["key"] = event.name
                dlg.after(0, dlg.destroy)

        hook = kb_lib.hook(on_event)
        self.root.wait_window(dlg)
        kb_lib.unhook(hook)
        return result["key"]

    def _pick_target(self, prompt: str, current: str | None = None) -> str | None:
        """Modal listbox of all gamepad targets."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Pick target")
        dlg.geometry("300x420")
        dlg.transient(self.root)
        dlg.grab_set()
        ttk.Label(dlg, text=prompt).pack(pady=6)

        lb = tk.Listbox(dlg, height=20)
        for t in SORTED_TARGETS:
            lb.insert("end", t)
        lb.pack(fill="both", expand=True, padx=8, pady=4)
        if current and current in SORTED_TARGETS:
            idx = SORTED_TARGETS.index(current)
            lb.selection_set(idx)
            lb.see(idx)

        chosen: dict[str, str | None] = {"v": None}

        def ok():
            sel = lb.curselection()
            if sel:
                chosen["v"] = SORTED_TARGETS[sel[0]]
            dlg.destroy()

        ttk.Button(dlg, text="OK", command=ok).pack(pady=4)
        lb.bind("<Double-1>", lambda e: ok())
        self.root.wait_window(dlg)
        return chosen["v"]

    # ------------------------------------------------------------------ control

    def toggle_running(self) -> None:
        if self.mapper.running:
            self.mapper.stop()
            self.start_btn.config(text="Start mapper")
            self.pause_btn.config(state="disabled")
        else:
            self._on_tune_changed()  # commit any pending tuning
            self.mapper.start()
            self.start_btn.config(text="Stop mapper")
            self.pause_btn.config(state="normal")

    def toggle_pause(self) -> None:
        if not self.mapper.running:
            return
        self.mapper.toggle_enabled()
        self.pause_btn.config(text="Resume (F8)" if not self.mapper.enabled else "Pause (F8)")

    def _on_status(self, msg: str) -> None:
        # Called from mapper thread - marshal back to GUI thread
        self.root.after(0, lambda: self.status_var.set(msg))

    def _on_close(self) -> None:
        try:
            self.mapper.stop()
        except Exception:
            pass
        try:
            kb_lib.unhook_all()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
