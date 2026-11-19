"""The background recorder process: OS-independent supervision around a
capture backend (PipeWire on Linux, the portable one on Windows/macOS).

Control: the CLI/UI talk to it over a localhost socket (pause / resume /
stop; port + secret token in STATE), POSIX signals still stop it (logout,
Ctrl+C).  Liveness: it holds LOCK while recording and the per-session lock
until the mix is written, so nobody ever needs to inspect PIDs."""
from __future__ import annotations

import contextlib
import json
import select
import shutil
import signal
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

from .common import (DISK_STOP_BYTES, DISK_WARN_BYTES, IS_WIN, LOCK, POLL_S, ROLE_LABEL,
                     ROLES, STATE, ControlServer, FileLock, KeepAwake, Log, fmt_clock, human_size,
                     log_path_for, notify, now_iso, read_json, session_dir, session_lock_path,
                     tilde, write_json)
from .finalize import finalize


def make_backend(name: str, d: "Daemon"):
    if name == "pipewire":
        from .pipewire import PipeWireBackend

        return PipeWireBackend(d)
    from .portable import PortableBackend

    return PortableBackend(d)


class Daemon:
    def __init__(self, meta_path: Path, on_locked: Callable[[], None] | None = None,
                 foreground: bool = False):
        self.meta_path = meta_path
        self.dir = session_dir(meta_path)
        self.meta = read_json(meta_path) or {}
        self.settings = self.meta.get("settings") or {}
        self.base = self.meta.get("base") or meta_path.stem
        self.notify_on = bool(self.settings.get("notify", True))
        self.log = Log(log_path_for(meta_path))
        self.on_locked = on_locked
        self.foreground = foreground
        self.ready_sent = False
        self.stop_reason: str | None = None
        self.signals = 0
        self.pause_request: bool | None = None
        self.paused = False
        self.paused_since: float | None = None
        self.paused_total = 0.0
        self.missing_polls = {"system": 0, "mic": 0}
        self.announced_missing = {"system": False, "mic": False}
        self.disk_warned = False
        self.started = time.time()
        self.last_state: dict = {}
        self.last_state_write = 0.0
        self.ipc: ControlServer | None = None
        self.wake_r, self.wake_w = socket.socketpair()
        self.wake_r.setblocking(False)
        self.wake_w.setblocking(False)
        self.backend = make_backend(self.meta.get("backend", "pipewire"), self)

    # ---- plumbing -------------------------------------------------------

    def _on_signal(self, signum, _frame) -> None:
        self.signals += 1
        if self.stop_reason is None:
            self.stop_reason = signal.Signals(signum).name

    def install_signals(self) -> None:
        with contextlib.suppress(ValueError, OSError):  # sockets work on Windows too
            signal.set_wakeup_fd(self.wake_w.fileno(), warn_on_full_buffer=False)
        names = ("SIGINT", "SIGBREAK") if IS_WIN else ("SIGTERM", "SIGINT", "SIGHUP")
        for name in names:
            sig = getattr(signal, name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, self._on_signal)

    def wait(self, seconds: float) -> None:
        """Sleep up to `seconds`, waking early for signals and commands."""
        deadline = time.monotonic() + seconds
        while not self.stop_reason:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            socks = [self.wake_r] + ([self.ipc.sock] if self.ipc else [])
            try:
                r, _, _ = select.select(socks, [], [], remaining)
            except (OSError, ValueError):
                time.sleep(min(remaining, 0.2))
                continue
            if self.wake_r in r:
                with contextlib.suppress(OSError):
                    while self.wake_r.recv(512):
                        pass
                return
            if self.ipc and self.ipc.sock in r:
                try:
                    self.ipc.handle(self.on_command)
                except Exception as e:  # a bad client must never stop the recording
                    self.log(f"ipc: {e!r}")
                return

    def on_command(self, cmd: str) -> dict:
        if cmd == "stop":
            if self.stop_reason is None:
                self.stop_reason = "stop-command"
            return {"ok": True}
        if cmd in ("pause", "resume"):
            self.pause_request = cmd == "pause"
            self.apply_pause()
            return {"ok": True, "paused": self.paused, "recorded_s": self.recorded_s()}
        if cmd == "status":
            return {"ok": True, **self.state_dict()}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def save_meta(self, **kv) -> None:
        self.meta.update(kv)
        with contextlib.suppress(OSError):
            write_json(self.meta_path, self.meta, durable=True)

    def ready(self, ok: bool, message: str) -> None:
        """Tell the starting CLI how it went (it polls the session metadata)."""
        if self.ready_sent:
            return
        self.ready_sent = True
        self.save_meta(ready={"ok": ok, "message": message, "found": dict(self.backend.found)})
        if self.foreground:
            with contextlib.suppress(AttributeError, OSError):
                print(message, file=sys.stderr if not ok else sys.stdout, flush=True)

    def enabled(self, role: str) -> bool:
        key = "sink" if role == "system" else "source"
        return self.settings.get(f"{key}_mode", "default") != "off"

    def recorded_s(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        live_pause = now - self.paused_since if self.paused_since is not None else 0.0
        return max(0.0, now - self.started - self.paused_total - live_pause)

    def state_dict(self, status: str = "recording") -> dict:
        import os

        return {
            "pid": os.getpid(), "status": status, "meta": str(self.meta_path),
            "dir": str(self.dir), "base": self.base, "started": self.started,
            "paused": self.paused, "paused_since": self.paused_since,
            "paused_total": self.paused_total, "part": getattr(self.backend, "part", 1),
            "backend": self.backend.name,
            "found": dict(self.backend.found), "linked": dict(self.backend.linked),
            "enabled": {r: self.enabled(r) for r in ROLES},
            "monitor_mic": bool(self.settings.get("monitor_mic")),
            "mix_format": self.settings.get("mix_format", "opus"),
            "port": self.ipc.port if self.ipc else None,
            "token": self.ipc.token if self.ipc else None,
        }

    def write_state(self, status: str = "recording", force: bool = False) -> None:
        state = self.state_dict(status)
        if force or state != self.last_state or time.time() - self.last_state_write > 30:
            with contextlib.suppress(OSError):
                write_json(STATE, {**state, "updated": time.time()})
            self.last_state, self.last_state_write = state, time.time()

    # ---- pause / devices / limits ---------------------------------------

    def apply_pause(self) -> None:
        want, self.pause_request = self.pause_request, None
        if want is None or want == self.paused:
            return
        now = time.time()
        pauses = self.meta.setdefault("pauses", [])
        if want:
            self.paused, self.paused_since = True, now
            pauses.append({"paused": now_iso(), "at_s": round(self.recorded_s(now), 1)})
            self.log(f"pause at {fmt_clock(self.recorded_s(now))}")
        else:
            self.paused_total += now - (self.paused_since or now)
            self.paused, self.paused_since = False, None
            if pauses:
                pauses[-1]["resumed"] = now_iso()
            self.missing_polls = {"system": 0, "mic": 0}
            self.log("resume")
        self.save_meta()
        self.backend.on_pause()
        self.write_state(force=True)
        notify("Recording paused" if want else "Recording resumed",
               f"{fmt_clock(self.recorded_s())} recorded so far",
               icon="media-playback-pause" if want else "media-record",
               enabled=self.notify_on)

    def watch_devices(self) -> None:
        """Tell the user (once) when an input disappears and when it returns."""
        if self.paused:
            return
        found, linked = self.backend.found, self.backend.linked
        for role in ROLES:
            if not self.enabled(role):
                continue
            if linked[role]:
                if self.announced_missing[role]:
                    notify(f"{ROLE_LABEL[role]} reconnected", found[role] or "",
                           icon="audio-card", enabled=self.notify_on)
                self.missing_polls[role] = 0
                self.announced_missing[role] = False
            else:
                self.missing_polls[role] += 1
                if self.missing_polls[role] == 2 and not self.announced_missing[role]:
                    self.announced_missing[role] = True
                    notify(f"{ROLE_LABEL[role]} unavailable",
                           "Still recording; it will be re-attached automatically when it "
                           "comes back.", icon="dialog-warning", enabled=self.notify_on)

    def check_limits(self) -> None:
        try:
            free = shutil.disk_usage(self.dir).free
        except OSError:
            free = None
        if free is not None and free < DISK_STOP_BYTES:
            self.log(f"disk: only {human_size(free)} free -> stopping")
            notify("Recording stopped: disk almost full", f"{human_size(free)} left on "
                   f"{tilde(self.dir)}", icon="dialog-error", urgency="critical",
                   enabled=self.notify_on)
            self.stop_reason = "disk-full"
        elif free is not None and free < DISK_WARN_BYTES and not self.disk_warned:
            self.disk_warned = True
            self.log(f"disk: low space ({human_size(free)} free)")
            notify("Disk space is low", f"{human_size(free)} left; the recording stops "
                   f"automatically at {human_size(DISK_STOP_BYTES)}.", icon="dialog-warning",
                   enabled=self.notify_on)
        limit = self.settings.get("max_duration_s")
        if limit and self.recorded_s() >= limit and not self.stop_reason:
            self.log("max duration reached -> stopping")
            self.stop_reason = "max-duration"

    # ---- main -----------------------------------------------------------

    def run(self) -> int:
        import os

        self.install_signals()  # before anyone can find us
        rec_lock = FileLock(LOCK)
        if not rec_lock.acquire(timeout=2):  # tolerate a concurrent `status` probe
            self.ready(False, "another recording is already running")
            return 1
        session_lock = FileLock(session_lock_path(self.meta_path))
        if not session_lock.try_acquire():
            rec_lock.release()
            self.ready(False, "this session is already owned by another process")
            return 1
        if self.on_locked:  # --foreground: let stop/pause/toggle in from now on
            self.on_locked()
        self.ipc = ControlServer()
        awake = KeepAwake()
        awake.start()
        self.log(f"session: start (pid {os.getpid()}, backend {self.backend.name}) "
                 f"settings={json.dumps(self.settings)}")
        self.save_meta(status="recording", pid=os.getpid(), started=now_iso())
        failed = False
        try:
            self.write_state(force=True)
            self.backend.start()
            self.write_state(force=True)
            for role in ROLES:  # startup message covers an absent device
                if self.enabled(role) and not self.backend.linked[role]:
                    self.announced_missing[role] = True
            parts = [f"{ROLE_LABEL[r]}: {self.backend.found[r] or 'waiting for device'}"
                     for r in ROLES if self.enabled(r)]
            self.ready(True, "\n".join(parts))
            notify("Recording started", "\n".join(parts), icon="media-record",
                   enabled=self.notify_on)
            self.loop()
        except Exception as e:
            failed = True
            self.log("error: " + "".join(traceback.format_exception(e)).rstrip())
            self.ready(False, f"recording failed: {e}")
            notify("Recording failed", f"{e}\nSee {tilde(log_path_for(self.meta_path))}",
                   icon="dialog-error", urgency="critical", enabled=self.notify_on)
        finally:
            with contextlib.suppress(Exception):
                self.backend.stop()
            if self.paused_since is not None:
                self.paused_total += time.time() - self.paused_since
                self.paused_since = None
            recorded = self.recorded_s()
            reason = self.stop_reason or ("error" if failed else "unknown")
            self.log(f"session: stopped after {fmt_clock(recorded)} recorded "
                     f"(reason: {reason})")
            try:
                self.save_meta(status="stopped", ended=now_iso(), stop_reason=reason,
                               recorded_s=round(recorded, 1),
                               elapsed_s=round(time.time() - self.started, 1))
                with contextlib.suppress(OSError):  # Windows: the UI may be reading it
                    STATE.unlink(missing_ok=True)
            finally:
                self.ipc.close()
                awake.stop()
                rec_lock.release()  # recording is over: a new one may start now

        try:
            if failed and not (self.meta.get("parts") or self.meta.get("segments")):
                self.save_meta(status="failed")
                return 1
            if recorded > 900:
                notify("Recording stopped", f"{fmt_clock(recorded)} captured; saving…",
                       icon="media-playback-stop", enabled=self.notify_on)
            signals_at_stop = self.signals
            result = finalize(self.meta_path, self.log, announce=True,
                              should_abort=lambda: self.signals > signals_at_stop)
            if result.get("ok"):
                where = result.get("mix") or result.get("tracks") or str(self.dir)
                notify("Recording saved", f"{fmt_clock(recorded)} → {tilde(where)}",
                       icon="document-save", enabled=self.notify_on)
            return 0 if result.get("ok") and not failed else 1
        finally:
            session_lock.release()

    def loop(self) -> None:
        while not self.stop_reason:
            self.wait(POLL_S)
            if self.stop_reason:
                break
            self.apply_pause()
            self.check_limits()
            if self.stop_reason:
                break
            self.backend.poll()
            self.watch_devices()
            self.write_state()
