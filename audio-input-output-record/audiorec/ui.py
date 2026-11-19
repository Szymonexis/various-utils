"""audio-rec UI: a small window with Start / Pause / Stop, a folder picker and
a file-name (prefix) field.

It is only a remote control for the same background recorder the hotkey uses
(audio_rec.py), so you can close the window while recording, start from the
hotkey and stop here, etc.  The folder / name / format chosen here are saved
as the defaults for the hotkey too.

Run it with:  ./audio-rec ui   (or: python audio_rec.py ui)
"""
from __future__ import annotations

import contextlib
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, ttk
except ImportError:  # pragma: no cover - depends on the Python build
    sys.exit("audio-rec ui: tkinter is missing. NixOS: start it through ./audio-rec ui "
             "(shell.nix provides it); Linux: install python3-tk; Windows/macOS: use the "
             "python.org installer (it includes Tk).")

from . import common as ar
from .cli import session_busy, status_snapshot

POLL_MS = 500
DEVICE_REFRESH_S = 5.0
COLORS = {"idle": "#9e9e9e", "recording": "#d32f2f", "paused": "#f39c12", "saving": "#1e88e5"}
FORMATS = [f for f in ar.MIX_FORMATS if f != "none"]
WRAP = 520  # px: keeps long names/paths from widening the window


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Audio Recorder")
        root.resizable(False, False)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        cfg = ar.load_config()
        self.dir_var = tk.StringVar(value=cfg["output_dir"])
        self.name_var = tk.StringVar(value=cfg["name"])
        self.ts_var = tk.BooleanVar(value=cfg["timestamp"])
        self.fmt_var = tk.StringVar(value=cfg["mix_format"] if cfg["mix_format"] in FORMATS
                                    else "opus")
        self.mon_var = tk.BooleanVar(value=cfg["monitor_mic"])

        self.results: queue.Queue = queue.Queue()
        self.busy = False
        self.snap: dict | None = None
        self.last_meta: Path | None = None  # session we last saw recording
        self.reported_meta: Path | None = None
        self.idle_found: dict = {"system": None, "mic": None}
        self.devices_at = 0.0
        self.devices_busy = False
        self.devices_known = False
        self.fs: dict = {}
        self.fs_busy = False
        self.fs_at = 0.0
        self.save_job: str | None = None

        self._build()
        for v in (self.dir_var, self.name_var, self.ts_var, self.fmt_var, self.mon_var):
            v.trace_add("write", lambda *_: self._settings_changed())
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._tick()

    # ---- layout ---------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        frm = ttk.Frame(self.root, padding=12)
        frm.grid(sticky="nsew")

        head = ttk.Frame(frm)
        head.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        self.dot = tk.Canvas(head, width=18, height=18, highlightthickness=0,
                             bg=ttk.Style().lookup("TFrame", "background") or None)
        self.dot_id = self.dot.create_oval(2, 2, 16, 16, fill=COLORS["idle"], outline="")
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self.state_lbl = ttk.Label(head, text="Idle", width=22)
        self.state_lbl.grid(row=0, column=1, sticky="w")
        big = tkfont.nametofont("TkDefaultFont").copy()
        big.configure(size=max(18, int(big.cget("size") * 2)), weight="bold")
        self.timer_lbl = ttk.Label(head, text="0:00:00", font=big)
        self.timer_lbl.grid(row=0, column=2, sticky="e")
        head.columnconfigure(1, weight=1)

        self.sys_lbl = ttk.Label(frm, text="Output: -", wraplength=WRAP)
        self.sys_lbl.grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        self.mic_lbl = ttk.Label(frm, text="Mic: -", wraplength=WRAP)
        self.mic_lbl.grid(row=2, column=0, columnspan=3, sticky="w", **pad)

        ttk.Separator(frm).grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)

        ttk.Label(frm, text="Save to").grid(row=4, column=0, sticky="w", **pad)
        self.dir_entry = ttk.Entry(frm, textvariable=self.dir_var, width=42)
        self.dir_entry.grid(row=4, column=1, sticky="ew", **pad)
        self.browse_btn = ttk.Button(frm, text="Browse…", command=self._browse)
        self.browse_btn.grid(row=4, column=2, sticky="ew", **pad)

        ttk.Label(frm, text="Name").grid(row=5, column=0, sticky="w", **pad)
        self.name_entry = ttk.Entry(frm, textvariable=self.name_var, width=42)
        self.name_entry.grid(row=5, column=1, sticky="ew", **pad)
        self.ts_chk = ttk.Checkbutton(frm, text="+ date & time", variable=self.ts_var)
        self.ts_chk.grid(row=5, column=2, sticky="w", **pad)

        ttk.Label(frm, text="Format").grid(row=6, column=0, sticky="w", **pad)
        opts = ttk.Frame(frm)
        opts.grid(row=6, column=1, columnspan=2, sticky="w")
        self.fmt_box = ttk.Combobox(opts, textvariable=self.fmt_var, values=FORMATS,
                                    state="readonly", width=7)
        self.fmt_box.grid(row=0, column=0, padx=(6, 12), pady=3)
        self.mon_ok = not (ar.IS_MAC and ar.backend_name() == "portable")
        self.mon_chk = ttk.Checkbutton(
            opts, text="hear my mic in headphones" if self.mon_ok
            else "hear my mic (not on macOS)", variable=self.mon_var)
        if not self.mon_ok:
            self.mon_var.set(False)
        self.mon_chk.grid(row=0, column=1, sticky="w")

        self.preview_lbl = ttk.Label(frm, text="", foreground="#666", wraplength=WRAP)
        self.preview_lbl.grid(row=7, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 0))

        ttk.Separator(frm).grid(row=8, column=0, columnspan=3, sticky="ew", pady=8)

        btns = ttk.Frame(frm)
        btns.grid(row=9, column=0, columnspan=3, sticky="ew")
        self.start_btn = ttk.Button(btns, text="● Start", command=self._start)
        self.pause_btn = ttk.Button(btns, text="▮▮ Pause", command=self._pause)
        self.stop_btn = ttk.Button(btns, text="■ Stop", command=self._stop)
        self.open_btn = ttk.Button(btns, text="Open folder", command=self._open_folder)
        for i, b in enumerate((self.start_btn, self.pause_btn, self.stop_btn, self.open_btn)):
            b.grid(row=0, column=i, padx=4, sticky="ew")
            btns.columnconfigure(i, weight=1)

        self.msg_lbl = ttk.Label(frm, text="Closing this window does not stop a recording.",
                                 foreground="#666", wraplength=WRAP, justify="left")
        self.msg_lbl.grid(row=10, column=0, columnspan=3, sticky="w", padx=6, pady=(8, 0))
        frm.columnconfigure(1, weight=1)

    # ---- settings -------------------------------------------------------

    def _settings(self) -> dict:
        return {"output_dir": self.dir_var.get().strip(), "name": self.name_var.get().strip(),
                "timestamp": bool(self.ts_var.get()), "mix_format": self.fmt_var.get(),
                "monitor_mic": bool(self.mon_var.get())}

    def _settings_changed(self) -> None:
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(600, self._save_settings)
        self._update_preview()

    def _save_settings(self) -> None:
        self.save_job = None
        s = self._settings()
        if s["output_dir"]:
            try:
                ar.save_config(s)
            except OSError as e:
                self._message(f"Could not save settings: {e}", error=True)

    def _update_preview(self) -> None:
        if self.snap:
            base = self.snap["base"]
            self.preview_lbl.configure(text=f"Recording: {base}."
                                       f"{ar.mix_ext(self.snap.get('mix_format') or 'opus')}"
                                       f"  +  {base}.tracks.flac")
            return
        s = self._settings()
        if not s["output_dir"]:
            self.preview_lbl.configure(text="Choose a folder to save into.")
            return
        key = (s["output_dir"], s["name"], s["timestamp"])
        # the exact (collision-checked) name comes from the background poller;
        # until then show what the name would be without a collision
        base = self.fs.get("base") if self.fs.get("for") == key else None
        base = base or ar.make_base(s["name"], s["timestamp"])
        self._kick_fs_poll()
        when = "   (date & time of start)" if s["timestamp"] else ""
        self.preview_lbl.configure(
            text=f"Saves: {base}.{ar.mix_ext(s['mix_format'])}  +  {base}.tracks.flac{when}")

    def _kick_fs_poll(self) -> None:
        """Folder look-ups (name collisions, the last session's metadata) run
        in a worker: a slow or offline network folder must not freeze Tk."""
        if self.fs_busy or time.time() - self.fs_at < 1.0:
            return
        self.fs_busy, self.fs_at = True, time.time()
        s, last = self._settings(), self.last_meta

        def work() -> None:
            out = {"for": (s["output_dir"], s["name"], s["timestamp"]), "last": last,
                   "base": None, "meta": None, "busy": False}
            with contextlib.suppress(Exception):
                if s["output_dir"]:
                    out["base"] = ar.plan_base(Path(s["output_dir"]).expanduser(),
                                               s["name"], s["timestamp"])
            if last is not None:
                with contextlib.suppress(Exception):
                    out["meta"] = ar.read_json(last)
                    out["busy"] = session_busy(last)
            self.results.put(("fs", 0, out, ""))

        threading.Thread(target=work, daemon=True).start()

    # ---- actions --------------------------------------------------------

    def _run(self, args: list[str], label: str) -> None:
        """Run an audio_rec.py command off the UI thread."""
        if self.busy:
            return
        self.busy = True
        self._refresh_buttons()

        def work() -> None:
            try:
                r = ar.run_self(args)  # errors come back here and are shown inline
                self.results.put((label, r.returncode, r.stdout, r.stderr))
            except Exception as e:
                self.results.put((label, 1, "", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _start(self) -> None:
        s = self._settings()
        if not s["output_dir"]:
            self._message("Choose a folder first.", error=True)
            return
        self._save_settings()
        args = ["start", "--output-dir", s["output_dir"], "--mix-format", s["mix_format"],
                "--timestamp" if s["timestamp"] else "--no-timestamp",
                "--monitor-mic" if s["monitor_mic"] else "--no-monitor-mic"]
        if s["name"]:
            args += ["--name", s["name"]]
        self._message("Starting…")
        self._run(args, "start")

    def _pause(self) -> None:
        if self.snap:
            self._run(["resume" if self.snap.get("paused") else "pause"], "pause")

    def _stop(self) -> None:
        if self.snap:
            self._message("Stopping…")
            self._run(["stop"], "stop")

    def _browse(self) -> None:
        start = str(Path(self.dir_var.get() or ar.default_output_root()).expanduser())
        if shutil.which("kdialog"):  # native KDE picker, run off the UI thread
            def work() -> None:
                try:
                    r = subprocess.run(["kdialog", "--title", "Save recordings to",
                                        "--getexistingdirectory", start],
                                       stdin=subprocess.DEVNULL, capture_output=True,
                                       text=True, env=ar.child_env())
                    self.results.put(("browse", r.returncode, r.stdout.strip(), r.stderr))
                except OSError as e:
                    self.results.put(("browse", 1, "", str(e)))

            threading.Thread(target=work, daemon=True).start()
            return
        chosen = filedialog.askdirectory(parent=self.root, initialdir=start,
                                         title="Save recordings to", mustexist=False)
        if chosen:
            self.dir_var.set(chosen)

    def _open_folder(self) -> None:
        if self.snap:
            target = Path(self.snap["dir"])
        else:
            target = Path(self.dir_var.get() or ar.default_output_root()).expanduser()
        try:
            ar.open_folder(target)
        except OSError as e:
            self._message(f"Could not open the folder: {e}", error=True)

    def _close(self) -> None:
        if self.save_job:
            self.root.after_cancel(self.save_job)
            self._save_settings()
        self.root.destroy()

    # ---- polling --------------------------------------------------------

    def _message(self, text: str, error: bool = False) -> None:
        self.msg_lbl.configure(text=text, foreground="#c62828" if error else "#444")

    def _handle_result(self, label: str, rc: int, out, err: str) -> None:
        if label == "fs":
            self.fs, self.fs_busy = out, False
            return
        if label == "devices":
            self.devices_busy = False
            try:
                found = json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}
            except (ValueError, IndexError):
                found = {}
            self.idle_found = {"system": found.get("system"), "mic": found.get("mic")}
            self.devices_known = True
            if not self.snap:
                self._show_idle_devices()
            return
        if label == "browse":
            if rc == 0 and out:
                self.dir_var.set(out)
            return
        self.busy = False
        if rc != 0:
            detail = (err or out).strip().splitlines()
            self._message(f"{label} failed: " + (detail[-1] if detail else f"exit {rc}"),
                          error=True)
        elif label == "start":
            self._message("Recording. You can close this window; use the hotkey or "
                          "reopen it to stop.")
        elif label == "stop":
            self._message("Stopped. Saving the mix…")
        elif label == "pause":
            self._message("Paused: nothing is recorded until you resume." if "Paused" in out
                          else "Resumed: recording continues in the same file.")

    def _tick(self) -> None:
        try:
            while True:
                self._handle_result(*self.results.get_nowait())
        except queue.Empty:
            pass
        try:
            self.snap = status_snapshot()
        except Exception as e:  # never let a transient error kill the UI loop
            self.snap = None
            self._message(f"status error: {e}", error=True)
        self._render()
        self.root.after(POLL_MS, self._tick)

    def _render(self) -> None:
        snap = self.snap
        if snap:
            self.last_meta = Path(snap["meta"])
            state = "paused" if snap.get("paused") else "recording"
            self.state_lbl.configure(text="Paused" if state == "paused" else "Recording")
            self.timer_lbl.configure(text=ar.fmt_clock(snap.get("recorded_s", 0)))
            found, linked = snap.get("found") or {}, snap.get("linked") or {}
            enabled = snap.get("enabled") or {}
            for role, lbl, title in (("system", self.sys_lbl, "Output"),
                                     ("mic", self.mic_lbl, "Mic")):
                if not enabled.get(role, True):
                    lbl.configure(text=f"{title}: off")
                    continue
                tag = ("paused" if snap.get("paused") else
                       "ok" if linked.get(role) else
                       "not linked" if found.get(role) else "waiting for device")
                lbl.configure(text=f"{title}: {found.get(role) or '-'}   [{tag}]")
        else:
            state = "idle"
            fresh = self.fs.get("last") == self.last_meta
            meta = self.fs.get("meta") if (self.last_meta and fresh) else None
            status = (meta or {}).get("status")
            working = bool(meta) and bool(self.fs.get("busy"))
            self._kick_fs_poll()
            if status in ("stopped", "finalizing") and working:
                state = "saving"
                self.state_lbl.configure(text="Saving…")
            else:
                self.state_lbl.configure(text="Idle")
                if meta and self.reported_meta != self.last_meta and status:
                    self.reported_meta = self.last_meta
                    self._report_finished(self.last_meta, meta)
            self._refresh_idle_devices()
        self.dot.itemconfigure(self.dot_id, fill=COLORS[state])
        for w in (self.dir_entry, self.name_entry, self.browse_btn, self.ts_chk, self.mon_chk):
            w.configure(state="disabled" if snap or (w is self.mon_chk and not self.mon_ok)
                        else "normal")
        self.fmt_box.configure(state="disabled" if snap else "readonly")
        self._refresh_buttons()
        self._update_preview()

    def _report_finished(self, meta_path: Path, meta: dict) -> None:
        status, d = meta.get("status"), meta_path.parent.parent
        if status == "done":
            out = d / meta["mix"] if meta.get("mix") else d
            self._message(f"Saved {out.name}  (in {ar.tilde(out.parent)})")
        elif status == "empty":
            self._message("Nothing was captured in the last recording.", error=True)
        elif status == "stopped":  # e.g. mix postponed for lack of disk space
            self._message(f"Mix not built yet: {meta.get('error') or 'interrupted'}. "
                          "It is retried on the next start; the lossless tracks are in "
                          f"{ar.tilde(d)}", error=True)
        elif status in ("failed", "interrupted"):
            self._message(f"Last recording: {status} ({meta.get('error') or 'see log'}); "
                          f"the lossless tracks are in {ar.tilde(d)}", error=True)

    def _refresh_idle_devices(self) -> None:
        """Ask a helper process which devices would be recorded.  Never load
        the audio libraries into this (Tk) process: on Windows soundcard
        switches the thread to multi-threaded COM, which hangs Tk's folder
        dialog (soundcard issue #182)."""
        if self.devices_busy or time.time() - self.devices_at < DEVICE_REFRESH_S:
            return
        self.devices_at = time.time()
        self.devices_busy = True

        def work() -> None:
            try:
                r = ar.run_self(["devices", "--json"], timeout=30)
                self.results.put(("devices", r.returncode, r.stdout, r.stderr))
            except Exception as e:
                self.results.put(("devices", 1, "", str(e)))

        threading.Thread(target=work, daemon=True).start()
        self._show_idle_devices()

    def _show_idle_devices(self) -> None:
        unknown = "…" if not self.devices_known else "not found"
        self.sys_lbl.configure(text=f"Output: {self.idle_found['system'] or unknown}")
        self.mic_lbl.configure(text=f"Mic: {self.idle_found['mic'] or unknown}")

    def _refresh_buttons(self) -> None:
        snap, busy = self.snap, self.busy
        self.start_btn.configure(state="disabled" if (snap or busy) else "normal")
        self.stop_btn.configure(state="normal" if (snap and not busy) else "disabled")
        self.pause_btn.configure(state="normal" if (snap and not busy) else "disabled",
                                 text="▶ Resume" if snap and snap.get("paused") else "▮▮ Pause")


def main() -> int:
    try:
        root = tk.Tk(className="audio-rec")
    except tk.TclError as e:
        print(f"audio-rec ui: cannot open a window ({e})", file=sys.stderr)
        return 1
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
