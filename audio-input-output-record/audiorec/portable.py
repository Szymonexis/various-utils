"""Portable backend: Windows (WASAPI loopback), macOS (a loopback input
device such as BlackHole) and any Linux with PulseAudio/pipewire-pulse.

Built on the `soundcard` package (WASAPI / CoreAudio / PulseAudio).  The
system output and the microphone are two independent device streams with
their own clocks, so instead of mixing them live we:

  1. capture each into its own raw FLAC (in .audio-rec/), plus a coarse
     timing log (wall-clock time vs. frames received), one *segment* per
     continuous stretch (a pause, or a device restart, starts a new one);
  2. on stop, per segment: fit each stream's real clock (start offset and
     rate, by least squares over the timing log), keep the longer stream
     as the reference and resample the other onto its clock with a
     windowed-sinc interpolator, then write the same 3-channel master the
     Linux backend produces (L, R = system, C = mic) -- so voice and
     system audio stay aligned even after hours of clock drift.

Passthrough: loopback capture is passive (Windows records what the
output plays; on macOS the Multi-Output Device keeps your headphones in
the chain), and the microphone is opened in shared mode, so other apps
keep using it.
"""
from __future__ import annotations

import contextlib
import glob
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .common import (APP, DISK_STOP_BYTES, IS_MAC, IS_WIN, META_DIR, RATE, ROLES, Log,
                     human_size, notify, now_iso, read_json, session_dir, write_json)

if TYPE_CHECKING:
    from .daemon import Daemon

CHUNK = RATE // 20  # frames per record() call (50 ms)
STALL_S = 4.0  # a stream that delivers nothing this long is restarted
TIMING_EVERY_S = 0.0  # log every chunk (~20 rows/s, ~2 MB/h)
FIRST_DATA_WAIT_S = 2.0
# macOS inputs that carry system audio (an Aggregate Device is often a mic
# combination, so it is only used when chosen explicitly with --sink)
LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "vb-cable", "cable output")
PIP_HINT = ("pip install -r requirements.txt  (soundcard, soundfile, numpy, "
            "imageio-ffmpeg)")


def _libs():
    try:
        import numpy  # noqa: F401
        import soundcard  # noqa: F401
        import soundfile  # noqa: F401
    except Exception as e:  # ImportError, or a missing native library
        raise SystemExit(f"{APP}: the portable backend needs soundcard, soundfile and "
                         f"numpy ({e}). Install them with:\n  {PIP_HINT}")
    import numpy as np
    import soundcard as sc
    import soundfile as sf

    return np, sc, sf


# --------------------------------------------------------------------------
# devices


def _name(dev) -> str:
    return str(getattr(dev, "name", dev))


def _match(devices: list, spec: str):
    """Exact id, exact name, or a unique case-insensitive name substring."""
    for dv in devices:
        if spec in (str(getattr(dv, "id", "")), _name(dv)):
            return dv
    hits = [dv for dv in devices if spec.lower() in _name(dv).lower()]
    return hits[0] if len(hits) == 1 else None


def _is_loop(dev) -> bool:
    return any(h in _name(dev).lower() for h in LOOPBACK_HINTS)


def _loopback_inputs(sc) -> list:
    return [m for m in sc.all_microphones() if _is_loop(m)]


def find_system(spec: str | None):
    """(capture device, playback device or None, label) for system audio."""
    _, sc, _ = _libs()
    if IS_MAC:  # no loopback API: record a loopback *input* device (BlackHole)
        mics = sc.all_microphones()
        dev = _match(mics, spec) if spec else next(iter(_loopback_inputs(sc)), None)
        return dev, None, (_name(dev) if dev else None)
    speakers = sc.all_speakers()
    spk = _match(speakers, spec) if spec else sc.default_speaker()
    if spk is None:
        return None, None, None
    return _loopback_for(sc, spk), spk, _name(spk)


def _loopback_for(sc, spk):
    """The loopback 'microphone' of a speaker.  Windows: same WASAPI id;
    PulseAudio: '<sink>.monitor' named 'Monitor of <sink description>'."""
    try:
        loops = [m for m in sc.all_microphones(include_loopback=True)
                 if getattr(m, "isloopback", False)]
    except Exception:
        return None
    sid = str(getattr(spk, "id", ""))
    for want in (sid, sid + ".monitor"):
        for m in loops:
            if str(getattr(m, "id", "")) == want:
                return m
    named = [m for m in loops if _name(spk) and _name(spk) in _name(m)]
    return named[0] if len(named) == 1 else None


def find_mic(spec: str | None):
    _, sc, _ = _libs()
    if spec:
        dev = _match(sc.all_microphones(), spec)
    else:
        try:
            dev = sc.default_microphone()
        except Exception:
            dev = None
        if IS_MAC and dev is not None and _is_loop(dev):
            # BlackHole as the default input is not a microphone: take a real one
            # (soundcard objects have no __eq__, so compare by name, not identity)
            dev = next((m for m in sc.all_microphones() if not _is_loop(m)), None)
    return dev, (_name(dev) if dev else None)


def check_tools(monitor: bool) -> None:
    _libs()
    if IS_MAC and monitor:
        # Whether the monitor reaches BlackHole depends on the output setup, so
        # the mix could lose or double the voice.  Use the headset's sidetone.
        raise SystemExit(f"{APP}: 'hear my mic' (--monitor-mic) is not available on "
                         "macOS; see INSTALL.md")
    if IS_MAC and not _loopback_inputs(_libs()[1]):
        print(f"{APP}: note: no loopback device (e.g. BlackHole 2ch) found, so only the "
              "microphone can be recorded. See INSTALL.md, 'macOS'.")


def resolve_targets(sink_spec: str | None, source_spec: str | None) -> tuple[str | None, str | None]:
    sink = source = None
    if sink_spec:
        dev, spk, _ = find_system(sink_spec)
        if dev is None:
            raise SystemExit(f"{APP}: no output matches {sink_spec!r}; see `{APP} devices`")
        sink = _name(spk or dev) if IS_MAC else str(getattr(spk or dev, "id", _name(spk or dev)))
    if source_spec:
        dev, _ = find_mic(source_spec)
        if dev is None:
            raise SystemExit(f"{APP}: no input matches {source_spec!r}; see `{APP} devices`")
        source = _name(dev) if IS_MAC else str(getattr(dev, "id", _name(dev)))
    return sink, source


def default_device_names() -> dict:
    _, _, sys_label = find_system(None)
    _, mic_label = find_mic(None)
    if IS_MAC and not sys_label:
        sys_label = None
    return {"system": sys_label, "mic": mic_label}


def list_devices() -> list[str]:
    _, sc, _ = _libs()
    lines = []
    if IS_MAC:
        loops = {_name(m) for m in _loopback_inputs(sc)}
        lines.append("System audio sources (loopback inputs; install BlackHole if empty):")
        lines += [f"    {n}" for n in sorted(loops)] or ["    (none)"]
    else:
        default = _name(sc.default_speaker())
        lines.append("Outputs (their sound is recorded via loopback):")
        for s in sc.all_speakers():
            lines.append(f"  {'*' if _name(s) == default else ' '} {_name(s)}")
    default_mic = None
    with contextlib.suppress(Exception):
        default_mic = _name(sc.default_microphone())
    lines.append("Inputs:")
    for m in sc.all_microphones():
        lines.append(f"  {'*' if _name(m) == default_mic else ' '} {_name(m)}")
    lines.append("(* = current default; pass a name or part of it to --sink/--source)")
    return lines


# --------------------------------------------------------------------------
# capture


def _com_init_thread() -> None:
    """Windows: every audio thread joins the multi-threaded COM apartment
    itself (relying on an implicit MTA breaks if anything else initialised
    COM first, e.g. in a frozen build)."""
    if IS_WIN:
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED


def _to_channels(np, data, channels: int):
    """Device-native capture -> stereo (system) or mono (mic).  Capturing
    at the device's own channel count avoids soundcard's broken single-
    channel WASAPI streams.  Surround outputs (WAVE order FL FR FC LFE then
    rear/side pairs) are folded down instead of losing dialogue in FC."""
    if data.ndim == 1:
        data = data[:, None]
    have = data.shape[1]
    if channels == 1:
        return data.mean(axis=1, keepdims=True, dtype="float32") if have > 1 else data
    if have == 1:
        return np.repeat(data, 2, axis=1)
    if have < 6:
        return data[:, :2]
    c = 0.7071 * data[:, 2]
    left = data[:, 0] + c + 0.7071 * data[:, 4::2].sum(axis=1)
    right = data[:, 1] + c + 0.7071 * data[:, 5::2].sum(axis=1)
    return (np.stack([left, right], axis=1) * 0.5).astype("float32")


class _Capture(threading.Thread):
    """Reads one device in a loop and hands chunks to the backend."""

    GAP_S = 0.05  # a persistent jump of the delivery lag this big = samples lost

    def __init__(self, backend: "PortableBackend", role: str, device, channels: int, gen: int):
        super().__init__(name=f"capture-{role}", daemon=True)
        self.b, self.role, self.device, self.channels, self.gen = backend, role, device, channels, gen
        self.stop_evt = threading.Event()
        self.error: Exception | None = None
        self.last_data = 0.0
        self.started = time.monotonic()
        self.heard = False  # any non-zero sample yet (macOS: zeros = no permission)
        self.gap = False  # samples were dropped: the clock fit needs a new segment

    def run(self) -> None:
        _com_init_thread()
        np = self.b.np
        kwargs = {"samplerate": RATE, "channels": None}  # device-native channel count
        if IS_WIN:
            kwargs["blocksize"] = RATE // 2  # 0.5 s WASAPI buffer: tolerate GIL hiccups
        t_open = None
        frames = 0
        win_start, win_min, prev_min = 0.0, float("inf"), None
        try:
            with self.device.recorder(**kwargs) as rec:
                while not self.stop_evt.is_set():
                    data = _to_channels(np, rec.record(numframes=CHUNK), self.channels)
                    now = time.monotonic()
                    self.last_data = now
                    if not self.heard and np.any(data):
                        self.heard = True
                    # Lag = wall time - audio time.  Lost samples (WASAPI "data
                    # discontinuity", an overrun) shift it for good; scheduling
                    # jitter does not move its per-window minimum.
                    frames += len(data)
                    if t_open is None:
                        t_open, win_start = now - len(data) / RATE, now
                    lag = (now - t_open) - frames / RATE
                    win_min = min(win_min, lag)
                    if now - win_start >= 2.0:
                        if prev_min is not None and win_min - prev_min > self.GAP_S:
                            self.gap = True
                        prev_min, win_min, win_start = win_min, float("inf"), now
                    self.b.on_chunk(self.role, self.gen, now, data)
        except Exception as e:  # device unplugged / invalidated
            self.error = e


class _Player(threading.Thread):
    """Plays the mic monitor from a queue, or silence (WASAPI keepalive)."""

    def __init__(self, speaker, source: "queue.Queue | None"):
        super().__init__(name="monitor" if source is not None else "keepalive", daemon=True)
        self.speaker, self.source = speaker, source
        self.speaker_id = str(getattr(speaker, "id", ""))
        self.stop_evt = threading.Event()
        self.error: Exception | None = None

    def run(self) -> None:
        _com_init_thread()
        np = _libs()[0]
        silence = np.zeros((CHUNK, 1), dtype="float32")  # tiled to every channel
        # device-native channel count (single-channel WASAPI streams are broken)
        kwargs = {"samplerate": RATE, "channels": None}
        if IS_WIN:
            kwargs["blocksize"] = RATE // 10  # 100 ms buffer > the 50 ms chunks
        try:
            with self.speaker.player(**kwargs) as p:
                primed = False
                while not self.stop_evt.is_set():
                    if self.source is None:
                        p.play(silence)
                        continue
                    if not primed:  # jitter buffer: start with two chunks queued
                        if self.source.qsize() < 2:
                            time.sleep(0.01)
                            continue
                        primed = True
                    try:
                        data = self.source.get(timeout=0.2)
                    except queue.Empty:
                        primed = False
                        continue
                    while self.source.qsize() > 2:  # keep the sidetone within ~150 ms
                        with contextlib.suppress(queue.Empty):
                            data = self.source.get_nowait()
                    p.play(data)
        except Exception as e:
            self.error = e


class PortableBackend:
    name = "portable"

    def __init__(self, d: "Daemon"):
        import warnings

        warnings.filterwarnings("ignore", message="data discontinuity")
        self.d = d
        self.np, self.sc, self.sf = _libs()
        self.found: dict[str, str | None] = {"system": None, "mic": None}
        self.linked: dict[str, bool] = {"system": False, "mic": False}
        self.part = 0  # current segment number
        self.seg: int | None = None  # None = drop incoming audio (paused)
        # capture generation per role: chunks from threads we replaced are ignored
        self.gens = {"system": 0, "mic": 0}
        self.caps: dict[str, _Capture | None] = {"system": None, "mic": None}
        self.devices: dict[str, object] = {}
        self.speaker = None  # the playback device behind the system capture
        self.keepalive: _Player | None = None
        self.monitor: _Player | None = None
        self.monitor_q: queue.Queue = queue.Queue(maxsize=8)
        self.q: queue.Queue = queue.Queue()
        self.writer = threading.Thread(target=self._writer, name="writer", daemon=True)
        self.writer_error: Exception | None = None
        self.restarts = 0
        self.last_restart = 0.0
        self.silence_warned = False
        self.player_failures = 0
        self.monitor_fed_always = True

    # ---- lifecycle ------------------------------------------------------

    def enabled(self, role: str) -> bool:
        return self.d.enabled(role)

    def start(self) -> None:
        self.writer.start()
        self.new_segment("start")
        self.open_devices()
        if self.d.settings.get("monitor_mic"):
            self.d.save_meta(monitor_recorded=self.monitor_fed_always)
        deadline = time.monotonic() + FIRST_DATA_WAIT_S
        while time.monotonic() < deadline and not all(
                self.caps[r] is None or self.caps[r].last_data for r in ROLES):
            time.sleep(0.05)
        self.update_status()

    def open_devices(self, roles=ROLES) -> None:
        d = self.d
        for role in roles:
            self.gens[role] += 1
            old = self.caps.get(role)
            if old:
                old.stop_evt.set()
            self.caps[role] = None
            if not self.enabled(role):
                continue
            key = "sink" if role == "system" else "source"
            spec = d.settings.get(key) if d.settings.get(f"{key}_mode") == "pinned" else None
            try:
                if role == "system":
                    dev, spk, label = find_system(spec)
                    self.speaker = spk
                    channels = 2
                else:
                    dev, label = find_mic(spec)
                    channels = 1
            except Exception as e:
                d.log(f"{role}: device lookup failed: {e}")
                dev, label = None, None
            self.found[role] = label
            self.devices[role] = dev
            if dev is None:
                continue
            cap = _Capture(self, role, dev, channels, self.gens[role])
            cap.start()
            self.caps[role] = cap
            d.log(f"{role}: {label} (capturing)")
        self.ensure_players()

    def _spec(self, role: str) -> str | None:
        key = "sink" if role == "system" else "source"
        return self.d.settings.get(key) if self.d.settings.get(f"{key}_mode") == "pinned" else None

    def lookup(self, role: str):
        """The device this role should record now (None if absent)."""
        try:
            return find_system(self._spec(role))[0] if role == "system" \
                else find_mic(self._spec(role))[0]
        except Exception:
            return None

    def ensure_players(self) -> None:
        """Keepalive (Windows: a silent stream keeps loopback packets coming
        while nothing plays) and mic monitor; (re)start only what is missing,
        failed or bound to a speaker we no longer record."""
        d = self.d
        want_keep = IS_WIN and self.speaker is not None and self.caps.get("system") is not None
        mon_spk = None
        if d.settings.get("monitor_mic") and not IS_MAC and self.caps.get("mic") is not None:
            mon_spk = self.speaker
            if mon_spk is None:
                with contextlib.suppress(Exception):
                    mon_spk = self.sc.default_speaker()
        for attr, want_spk, source in (("keepalive", self.speaker if want_keep else None, None),
                                       ("monitor", mon_spk, self.monitor_q)):
            p = getattr(self, attr)
            healthy = (p is not None and p.is_alive() and not p.error
                       and p.speaker_id == str(getattr(want_spk, "id", "")))
            if want_spk is None or not healthy:
                if p is not None:
                    if p.error:
                        d.log(f"{attr}: {p.error}")
                        self.player_failures += 1
                    p.stop_evt.set()
                setattr(self, attr, None)
            if want_spk is not None and not healthy and self.player_failures <= 20:
                if source is not None:
                    while not source.empty():  # no stale audio from the last stream
                        with contextlib.suppress(queue.Empty):
                            source.get_nowait()
                player = _Player(want_spk, source)
                player.start()
                setattr(self, attr, player)
        # The monitor plays into the output whose loopback is recorded, so the
        # system channels carry the voice; finalize must then not add the mic
        # channel again (echo).  If that ever stops being true, keep the mic.
        if d.settings.get("monitor_mic"):
            fed = (self.monitor is not None and self.speaker is not None
                   and self.caps.get("system") is not None
                   and self.monitor.speaker_id == str(getattr(self.speaker, "id", "")))
            if not fed and self.monitor_fed_always:
                self.monitor_fed_always = False
                d.save_meta(monitor_recorded=False)

    def on_chunk(self, role: str, gen: int, t: float, data) -> None:
        if gen != self.gens[role]:
            return  # from a capture thread we already replaced
        seg = self.seg
        if seg is not None:
            self.q.put((seg, role, t, data))
        if role == "mic" and self.monitor is not None:
            with contextlib.suppress(queue.Full):
                self.monitor_q.put_nowait(data)

    def update_status(self) -> None:
        now = time.monotonic()
        for role in ROLES:
            cap = self.caps.get(role)
            self.linked[role] = bool(cap and cap.is_alive() and cap.last_data
                                     and now - cap.last_data < STALL_S)

    def poll(self) -> None:
        d = self.d
        if self.writer_error:
            raise RuntimeError(f"writing the recording failed: {self.writer_error}")
        now = time.monotonic()
        broken = []
        for role in ROLES:
            if not self.enabled(role):
                continue
            cap = self.caps.get(role)
            if cap is None:  # absent: only reopen once it is back
                if self.lookup(role) is not None:
                    broken.append(role)
            elif cap.error or not cap.is_alive():
                d.log(f"{role}: capture stopped ({cap.error or 'thread ended'})")
                broken.append(role)
            elif now - (cap.last_data or cap.started) > (STALL_S if cap.last_data
                                                          else 2 * STALL_S):
                d.log(f"{role}: no audio for {STALL_S:.0f}s; reopening the device")
                broken.append(role)
            elif self.default_changed(role):
                d.log(f"{role}: default device changed")
                broken.append(role)
        if broken and now - self.last_restart > min(30.0, 2.0 * (1 + self.restarts)):
            self.last_restart = now
            self.restarts += 1
            if not d.paused:
                self.new_segment("restart")  # a stream restart breaks continuity
            self.open_devices(broken)
        elif not broken:
            self.restarts = 0
        gaps = [r for r in ROLES if self.caps.get(r) is not None and self.caps[r].gap]
        if gaps:
            for r in gaps:
                self.caps[r].gap = False
            d.log(f"{', '.join(gaps)}: samples were dropped; starting a new segment")
            if not d.paused:
                self.new_segment("discontinuity")  # one clock fit per continuous stretch
        self.ensure_players()
        mic = self.caps.get("mic")
        if (mic and not mic.heard and not self.silence_warned and mic.last_data
                and now - mic.started > 10):
            self.silence_warned = True
            hint = (" On macOS, allow Microphone access for this app (or the terminal "
                    "running it) in System Settings > Privacy & Security." if IS_MAC else
                    " Is it muted?")
            d.log("mic: only digital silence so far")
            self.found["mic"] = f"{self.found['mic']} (pure silence: muted or no permission?)"
            notify("Microphone records pure silence", "Recording continues." + hint,
                   icon="dialog-warning", enabled=d.notify_on)
        self.update_status()

    def default_changed(self, role: str) -> bool:
        """Would we pick a different device now than the one we record?"""
        key = "sink" if role == "system" else "source"
        if self.d.settings.get(f"{key}_mode") != "default":
            return False
        try:
            if role == "system":
                if IS_MAC:
                    return False  # the loopback input does not follow the output
                current = find_system(None)[1]
                return (current is not None and self.speaker is not None
                        and str(current.id) != str(self.speaker.id))
            current = find_mic(None)[0]
            dev = self.devices.get("mic")
            return (current is not None and dev is not None
                    and str(current.id) != str(getattr(dev, "id", "")))
        except Exception:
            return False

    def on_pause(self) -> None:
        if self.d.paused:
            self.seg = None  # captures keep running; chunks are dropped
            self.q.put(("close",))
        else:
            self.new_segment("resume")

    def stop(self) -> None:
        self.seg = None
        for cap in self.caps.values():
            if cap:
                cap.stop_evt.set()
        for p in (self.keepalive, self.monitor):
            if p:
                p.stop_evt.set()
        for cap in self.caps.values():
            if cap:
                cap.join(timeout=3)
        self.q.put(("stop",))
        self.writer.join(timeout=30)
        self.d.save_meta()

    # ---- segments and the writer thread ----------------------------------

    def new_segment(self, reason: str) -> None:
        d = self.d
        self.part += 1
        stem = f"{d.base}.s{self.part:03d}"
        seg = {"index": self.part, "reason": reason, "started": now_iso(),
               "files": {"system": f"{stem}.system.flac", "mic": f"{stem}.mic.flac"},
               "timing": f"{stem}.timing.csv"}
        d.meta.setdefault("segments", []).append(seg)
        d.save_meta()
        self.q.put(("close",))
        self.seg = self.part

    def _writer(self) -> None:
        """Owns every file handle: raw FLACs and timing logs."""
        d = self.d
        raw_dir = d.dir / META_DIR
        subtype = "PCM_24" if int(d.settings.get("bits", 16)) == 24 else "PCM_16"
        files: dict[str, object] = {}
        frames: dict[str, int] = {}
        last_row: dict[str, float] = {}
        timing = None
        current = None
        done: set[int] = set()  # closed segments: late chunks for them are dropped

        def close_all() -> None:
            nonlocal timing, current
            if current is not None:
                done.add(current)
            for f in files.values():
                with contextlib.suppress(Exception):
                    f.close()
            files.clear()
            frames.clear()
            last_row.clear()
            if timing:
                with contextlib.suppress(Exception):
                    timing.close()
            timing, current = None, None

        while True:
            item = self.q.get()
            if item[0] == "stop":
                close_all()
                return
            if item[0] == "close":
                close_all()
                continue
            seg, role, t, data = item
            if seg in done:
                continue
            try:
                if seg != current:
                    close_all()
                    current = seg
                    stem = f"{d.base}.s{seg:03d}"
                    timing = open(raw_dir / f"{stem}.timing.csv", "a", encoding="utf-8")
                if role not in files:
                    files[role] = self.sf.SoundFile(
                        str(raw_dir / f"{d.base}.s{seg:03d}.{role}.flac"), "w", RATE,
                        data.shape[1], format="FLAC", subtype=subtype)
                    frames[role] = 0
                files[role].write(data)
                frames[role] += len(data)
                if t - last_row.get(role, -1e9) >= TIMING_EVERY_S:
                    last_row[role] = t
                    timing.write(f"{role},{t:.6f},{frames[role]}\n")
                    timing.flush()
            except Exception as e:  # disk full, permission... stop the session
                self.writer_error = e
                d.log(f"writer: {e}")
                close_all()
                return


# --------------------------------------------------------------------------
# on stop: align the segments into the 3-channel master


def _fit(rows: list[tuple[float, int]]) -> tuple[float, float]:
    """Clock model t = a + n / r of one stream from its (arrival time, frames)
    timing rows -> (a, r).

    * rate: least squares, but the deviation from nominal is shrunk by its
      own standard error (short or jittery segments -> ~nominal), and
      anything beyond +-1000 ppm is treated as a broken log, not a clock
      (real crystals are within ~+-100 ppm);
    * offset: lower envelope of t - n/r, because delivery delays are always
      positive (a chunk can arrive late, never early)."""
    np = _libs()[0]
    if not rows:
        return 0.0, float(RATE)
    t = np.array([r[0] for r in rows], dtype="float64")
    n = np.array([r[1] for r in rows], dtype="float64")
    r = float(RATE)
    if len(rows) >= 10 and n[-1] - n[0] > 10 * RATE:
        keep = t - t[0] > 2.0  # skip the start-up burst (pre-filled buffers)
        if keep.sum() >= 10:
            tk, nk = t[keep], n[keep]
            slope, intercept = np.polyfit(nk, tk, 1)
            resid = tk - (intercept + slope * nk)
            spread = np.sqrt(np.sum((nk - nk.mean()) ** 2))
            se = np.sqrt(np.sum(resid ** 2) / max(1, len(tk) - 2)) / spread if spread else 1.0
            if slope > 0:
                dev = (1.0 / (slope * RATE) - 1) * 1e6  # ppm
                dev_se = se / slope * 1e6
                if abs(dev) <= 1000:
                    dev *= dev * dev / (dev * dev + dev_se * dev_se)  # shrink if unsure
                    r = RATE * (1 + dev * 1e-6)
    a = float(np.percentile(t - n / r, 5))
    return a, r


class _Reader:
    """Sequential, zero-padded access to a raw capture by frame index."""

    def __init__(self, path: Path, channels: int):
        np, _, sf = _libs()
        self.np = np
        self.f = sf.SoundFile(str(path)) if path and path.exists() else None
        self.channels = channels
        self.buf = np.zeros((0, channels), dtype="float32")
        self.base = 0
        self.eof = self.f is None
        # A crash-truncated FLAC reports 2**63-1 frames and libsndfile drops
        # the last partial block of a read at its broken end: read small.
        self.step = 4096 if self.f is not None and self.f.frames >= 2 ** 62 else 0

    def _skip(self, n: int) -> None:
        """Advance the (empty-buffer) file position by n frames."""
        try:
            self.f.seek(self.base + n)
            self.base += n
            return
        except Exception:
            pass
        while n > 0 and not self.eof:
            block = self.f.read(min(n, 1 << 20), dtype="float32", always_2d=True)
            if not len(block):
                self.eof = True
                break
            n -= len(block)
            self.base += len(block)

    def get(self, i0: int, i1: int):
        """Frames [i0, i1); zeros outside the recording.  Calls must not move
        backwards.  Invariant: file position == base + len(buf)."""
        np = self.np
        if i0 > self.base:  # drop what we no longer need
            drop = min(i0 - self.base, len(self.buf))
            self.buf = self.buf[drop:]
            self.base += drop
            if not len(self.buf) and not self.eof and i0 > self.base:
                self._skip(i0 - self.base)
        while not self.eof and self.base + len(self.buf) < i1:
            want = self.step or max(i1 - (self.base + len(self.buf)), 65536)
            try:
                block = self.f.read(want, dtype="float32", always_2d=True)
            except Exception:  # truncated by a crash: use what we have
                block = np.zeros((0, self.channels), dtype="float32")
            if not len(block):
                self.eof = True
                break
            self.buf = np.concatenate([self.buf, block[:, :self.channels]])
        out = np.zeros((i1 - i0, self.channels), dtype="float32")
        lo, hi = max(i0, self.base), min(i1, self.base + len(self.buf))
        if hi > lo:
            out[lo - i0:hi - i0] = self.buf[lo - self.base:hi - self.base]
        return out

    def close(self) -> None:
        if self.f is not None:
            self.f.close()


_TAPS = 16
_PHASES = 512
_KERNEL: list = []


def _kernel_table():
    """Kaiser-windowed sinc weights for _PHASES+1 fractional offsets
    (polyphase table, built once) -> ((_PHASES+1, 16) weights, tap offsets)."""
    if not _KERNEL:
        np = _libs()[0]
        j = np.arange(-_TAPS // 2 + 1, _TAPS // 2 + 1)  # -7 .. 8
        frac = np.arange(_PHASES + 1) / _PHASES
        x = j[None, :] - frac[:, None]
        half = _TAPS / 2
        w = np.sinc(x) * np.i0(7.0 * np.sqrt(np.clip(1 - (x / half) ** 2, 0, 1))) / np.i0(7.0)
        _KERNEL.extend([(w / w.sum(axis=1, keepdims=True)).astype("float32"), j])
    return _KERNEL[0], _KERNEL[1]


def _resample(reader: _Reader, pos):
    """Values of the stream at fractional frame positions `pos` (monotonic)."""
    np = _libs()[0]
    table, j = _kernel_table()
    base = np.floor(pos).astype("int64")
    w = table[np.rint((pos - base) * _PHASES).astype("int64")]
    lo, hi = int(base[0] + j[0]), int(base[-1] + j[-1]) + 1
    src = reader.get(lo, hi)
    idx = (base - lo)[:, None] + j[None, :]  # 0 .. hi-lo-1
    out = np.empty((len(pos), reader.channels), dtype="float32")
    for ch in range(reader.channels):
        out[:, ch] = np.einsum("ij,ij->i", src[:, ch][idx], w)
    return out


def _render_segment(seg: dict, raw_dir: Path, out, log: Log,
                    should_abort: Callable[[], bool]) -> int | None:
    """Append one aligned segment to `out`; returns frames written (None = aborted)."""
    np, _, sf = _libs()
    rows: dict[str, list] = {"system": [], "mic": []}
    with contextlib.suppress(OSError):
        for line in (raw_dir / seg["timing"]).read_text(encoding="utf-8").splitlines():
            parts = line.split(",")
            if len(parts) == 3 and parts[0] in rows:
                with contextlib.suppress(ValueError):
                    rows[parts[0]].append((float(parts[1]), int(parts[2])))
    streams = {}
    for role, ch in (("system", 2), ("mic", 1)):
        path = raw_dir / seg["files"][role]
        if not path.exists() or path.stat().st_size == 0:
            continue
        frames = 0
        with contextlib.suppress(Exception):
            frames = sf.info(str(path)).frames
        logged = rows[role][-1][1] if rows[role] else 0
        if frames >= 2 ** 62 or frames <= 0:  # crash-truncated FLAC: length unknown
            frames = logged
        else:
            frames = max(frames, logged)
        if frames <= 0:
            continue
        a, r = _fit(rows[role])
        streams[role] = {"reader": _Reader(path, ch), "a": a, "r": r, "M": frames}
    if not streams:
        return 0
    ref_role = max(streams, key=lambda k: streams[k]["M"])
    ref = streams[ref_role]
    t0 = min(s["a"] for s in streams.values())
    if max(s["a"] for s in streams.values()) - t0 > 120:
        log(f"align: segment {seg['index']}: implausible start offsets; aligning starts")
        for s in streams.values():
            s["a"], s["r"] = ref["a"], ref["r"]
        t0 = ref["a"]
    k_ref0 = int(round((ref["a"] - t0) * ref["r"]))
    total = max(int(np.ceil(k_ref0 + (s["a"] + s["M"] / s["r"] - ref["a"]) * ref["r"]))
                for s in streams.values())
    for role, s in streams.items():
        ratio = s["r"] / ref["r"]
        drift = abs(ratio - 1) * total
        s["exact"] = role == ref_role or drift < 0.5
        s["offset"] = (ref["a"] - s["a"]) * s["r"] - k_ref0 * ratio  # m(k) = k*ratio + offset
        s["ratio"] = ratio
        log(f"align: segment {seg['index']} {role}: start {s['a'] - t0:+.3f}s, clock "
            f"{(s['r'] / RATE - 1) * 1e6:+.1f} ppm, {s['M'] / RATE:.1f}s"
            + ("" if role == ref_role else f", resampled x{ratio:.7f}" if not s["exact"]
               else ", shifted"))
    block = RATE * 2
    written = 0
    for k0 in range(0, total, block):
        if should_abort():
            return None
        n = min(block, total - k0)
        frame = np.zeros((n, 3), dtype="float32")
        k = np.arange(k0, k0 + n, dtype="float64")
        for role, s in streams.items():
            if role == ref_role:
                data = s["reader"].get(k0 - k_ref0, k0 - k_ref0 + n)
            elif s["exact"]:
                shift = int(round(s["offset"]))
                data = s["reader"].get(k0 + shift, k0 + shift + n)
            else:
                data = _resample(s["reader"], k * s["ratio"] + s["offset"])
            if role == "system":
                frame[:, 0:2] = data
            else:
                frame[:, 2] = data[:, 0]
        out.write(frame)
        written += n
    for s in streams.values():
        s["reader"].close()
    return written


def build_tracks(meta_path: Path, log: Log, should_abort: Callable[[], bool]) -> dict:
    """Raw per-device captures -> `<base>.tracks.flac` (idempotent)."""
    np, _, sf = _libs()
    meta = read_json(meta_path) or {}
    d = session_dir(meta_path)
    base = meta.get("base") or meta_path.stem
    raw_dir = d / META_DIR
    out_path = d / f"{base}.tracks.flac"
    if meta.get("tracks_built") and out_path.exists():
        return {"ok": True}
    segs = meta.get("segments") or []
    if not segs:  # metadata lost: rebuild the list from the files
        for tpath in sorted(raw_dir.glob(glob.escape(base) + ".s*.timing.csv")):
            stem = tpath.name[: -len(".timing.csv")]
            segs.append({"index": len(segs) + 1, "timing": tpath.name,
                         "files": {"system": f"{stem}.system.flac", "mic": f"{stem}.mic.flac"}})
    bits = int((meta.get("settings") or {}).get("bits", 16))
    for old in d.glob(glob.escape(base) + ".tracks.incomplete-*.flac"):
        old.unlink(missing_ok=True)  # left over by a crashed finalize (we hold the lock)
    need = sum(p.stat().st_size for seg in segs for r in ("system", "mic")
               if (p := raw_dir / seg["files"][r]).exists())
    with contextlib.suppress(OSError):
        free = shutil.disk_usage(d).free
        if free - need < DISK_STOP_BYTES // 2:
            why = (f"not enough disk space to align the tracks (needs ~{human_size(need)}, "
                   f"{human_size(free)} free)")
            log(f"align: {why}")
            return {"ok": False, "retry": True, "message": why}
    tmp = d / f"{base}.tracks.incomplete-{os.getpid()}.flac"
    log(f"align: building {out_path.name} from {len(segs)} segment(s)")
    t_start = time.time()
    total = 0
    try:
        with sf.SoundFile(str(tmp), "w", RATE, 3, format="FLAC",
                          subtype="PCM_24" if bits == 24 else "PCM_16") as out:
            for seg in segs:
                n = _render_segment(seg, raw_dir, out, log, should_abort)
                if n is None:
                    raise InterruptedError
                total += n
    except InterruptedError:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "retry": True, "message": "interrupted"}
    except Exception as e:
        tmp.unlink(missing_ok=True)
        log(f"align: failed: {e!r}")
        return {"ok": False, "retry": False, "message": f"aligning the tracks failed: {e}"}
    meta = read_json(meta_path) or meta
    if total == 0:
        tmp.unlink(missing_ok=True)
        meta["parts"] = []
    else:
        os.replace(tmp, out_path)
        meta["parts"] = [{"file": out_path.name}]
        log(f"align: {total / RATE:.1f}s written in {time.time() - t_start:.1f}s")
    meta["tracks_built"] = now_iso()
    write_json(meta_path, meta, durable=True)
    return {"ok": True}
