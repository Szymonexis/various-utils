"""Command line: toggle / start / pause / resume / stop / status / devices /
finalize / ui.  The same commands drive the hotkey, the UI and scripts."""
from __future__ import annotations

import argparse
import contextlib
import glob
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

from . import common as c
from .common import (APP, FROZEN, IS_WIN, LOCK, META_DIR, MIX_FORMATS, READY_TIMEOUT_S,
                     RECOVERY_SCAN, ROLE_LABEL, ROLES, RT, STATE, STOP_TIMEOUT_S, FileLock,
                     Log, backend_name, control_lock, fmt_clock, human_size, load_config,
                     lock_is_held, log_path_for, make_base, meta_path_for, mix_ext, notify,
                     now_iso, read_json, recent_sessions, release_control_lock,
                     remember_session, send_command, session_dir, session_lock_path,
                     spawn_self, tilde, write_json)


def backend_module(name: str | None = None):
    if (name or backend_name()) == "pipewire":
        from . import pipewire as mod
    else:
        from . import portable as mod
    return mod


def default_device_names() -> dict:
    """What would be recorded right now (for the UI's idle view)."""
    return backend_module().default_device_names()


def status_snapshot() -> dict | None:
    """Live state of the active recording (None when idle), for status/UI."""
    st = read_json(STATE)
    if not st or not lock_is_held():
        return None
    now = time.time()
    started = float(st.get("started") or now)
    since = st.get("paused_since")
    live_pause = now - float(since) if since else 0.0
    recorded = now - started - float(st.get("paused_total") or 0) - live_pause
    return {**st, "recording": True, "recorded_s": max(0.0, recorded),
            "elapsed_s": now - started}


def session_busy(meta_path: Path) -> bool:
    """Is a live process (recorder or finalizer) working on this session?"""
    return lock_is_held(session_lock_path(meta_path))


def recover_interrupted() -> list[Path]:
    """Finish sessions whose recorder/finalizer died (crash, power loss,
    logout during the mixdown): render their mix from what was captured."""
    recovered: list[Path] = []
    if read_json(STATE) and not lock_is_held():
        with contextlib.suppress(OSError):  # Windows: a reader may hold it open
            STATE.unlink(missing_ok=True)
    for meta_path in recent_sessions()[:RECOVERY_SCAN]:
        meta = read_json(meta_path)
        if not meta or meta.get("app") != APP:
            continue
        if meta.get("status") not in ("recording", "stopped", "finalizing"):
            continue
        if session_busy(meta_path):
            continue  # still being handled by a live process
        if meta.get("status") == "recording":
            meta["status"] = "interrupted"
        meta.setdefault("ended", now_iso())
        with contextlib.suppress(OSError):
            write_json(meta_path, meta, durable=True)
        Log(log_path_for(meta_path))(f"recovery: previous {APP} process is gone; finalizing")
        spawn_self(["finalize", str(meta_path), "--recovered"], log_path_for(meta_path))
        recovered.append(meta_path)
    return recovered


def create_session(directory: Path, name: str | None, timestamp: bool,
                   meta: dict) -> tuple[str, Path]:
    """Reserve a unique base name in `directory` by creating its metadata file
    exclusively; returns (base, meta_path)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / META_DIR).mkdir(exist_ok=True)
    first = make_base(name, timestamp)
    for i in range(1, 1000):
        base = first if i == 1 else f"{first}-{i}"
        if any(directory.glob(glob.escape(base) + ".*")):
            continue
        meta_path = meta_path_for(directory, base)
        try:
            fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        write_json(meta_path, {**meta, "base": base}, durable=True)
        remember_session(meta_path)
        return base, meta_path
    raise SystemExit(f"{APP}: could not find a free file name for {first!r} in {directory}")


def do_start(args) -> int:
    if lock_is_held():
        print("Already recording.")
        show_status()
        return 0
    cfg = load_config()
    out_dir = Path(args.output_dir or cfg["output_dir"]).expanduser().resolve()
    name = cfg["name"] if args.name is None else args.name
    timestamp = cfg["timestamp"] if args.timestamp is None else args.timestamp
    mix_format = args.mix_format or cfg["mix_format"]
    monitor = cfg["monitor_mic"] if args.monitor_mic is None else args.monitor_mic

    for m in recover_interrupted():
        print(f"Finishing an interrupted recording in the background: {m.stem}")
    bname = backend_name()
    mod = backend_module(bname)
    mod.check_tools(monitor)
    from .finalize import ffmpeg_works

    if not ffmpeg_works():
        print(f"{APP}: warning: ffmpeg is not usable; the lossless tracks will be recorded "
              "but no mix is rendered (install ffmpeg or `pip install imageio-ffmpeg`).",
              file=sys.stderr)
    if args.no_system and args.no_mic:
        raise SystemExit(f"{APP}: nothing to record (--no-system and --no-mic)")
    if monitor and args.no_mic:
        raise SystemExit(f"{APP}: --monitor-mic needs the microphone")
    sink, source = mod.resolve_targets(None if args.no_system else args.sink,
                                       None if args.no_mic else args.source)

    def mode(off: bool, pinned: str | None) -> str:
        return "off" if off else ("pinned" if pinned else "default")

    settings = {
        "sink_mode": mode(args.no_system, sink), "sink": sink,
        "source_mode": mode(args.no_mic, source), "source": source,
        "monitor_mic": bool(monitor), "mix_format": mix_format,
        "bitrate": args.bitrate, "bits": args.bits, "mic_gain_db": args.mic_gain,
        "system_gain_db": args.system_gain, "split_tracks": bool(args.split_tracks),
        "max_duration_s": args.max_duration, "notify": not args.no_notify,
    }
    meta = {
        "app": APP, "version": 3, "backend": bname, "created": now_iso(),
        "status": "starting",
        "channels": {"FL": "system left", "FR": "system right", "FC": "microphone"},
        "settings": settings, "parts": [],
    }
    try:
        base, meta_path = create_session(out_dir, name, timestamp, meta)
    except OSError as e:
        raise SystemExit(f"{APP}: cannot write to {out_dir}: {e}")

    if args.foreground:
        from .daemon import Daemon

        return Daemon(meta_path, on_locked=release_control_lock, foreground=True).run()

    log_path = log_path_for(meta_path)
    proc = spawn_self(["_daemon", str(meta_path)], log_path)
    deadline = time.time() + READY_TIMEOUT_S
    ready = None
    while time.time() < deadline:
        ready = (read_json(meta_path) or {}).get("ready")
        if ready or proc.poll() is not None:
            ready = ready or (read_json(meta_path) or {}).get("ready")
            break
        time.sleep(0.05)
    if not ready:
        alive = proc.poll() is None
        tail = ""
        with contextlib.suppress(OSError):
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-8:])
        text = ("the recorder did not confirm in time (it may still be starting)" if alive
                else "the recorder exited during startup")
        notify("Recording failed to start", text, icon="dialog-error", urgency="critical",
               enabled=settings["notify"])
        print(f"{APP}: {text}. Log ({tilde(log_path)}):\n{tail}", file=sys.stderr)
        return 1
    if not ready.get("ok"):
        print(f"{APP}: {ready.get('message')}", file=sys.stderr)
        return 1
    print(f"● Recording → {tilde(out_dir)}{os.sep}{base}.{mix_ext(mix_format)}"
          f"  (+ {base}.tracks.flac)")
    for line in str(ready.get("message", "")).splitlines():
        print(f"  {line}")
    print(f"  pause with: {APP} pause    stop with: {APP} stop   (or trigger {APP} again)")
    return 0


def do_pause(want: bool | None) -> int:
    """want=True pause, False resume, None toggle."""
    snap = status_snapshot()
    if not snap:
        print("Not recording.")
        return 1
    paused = bool(snap.get("paused"))
    target = (not paused) if want is None else want
    if target == paused:
        print("Already paused." if paused else "Not paused.")
        return 0
    reply = send_command("pause" if target else "resume")
    if not reply or not reply.get("ok") or bool(reply.get("paused")) != target:
        print(f"{APP}: the recorder did not confirm the {'pause' if target else 'resume'}"
              + (f" ({reply.get('error')})" if reply and reply.get("error") else ""),
              file=sys.stderr)
        return 1
    print(("▮▮ Paused at " if target else "● Resumed at ")
          + fmt_clock(float(reply.get("recorded_s") or 0)))
    return 0


def do_stop(args) -> int:
    if not lock_is_held():
        recovered = recover_interrupted()
        if recovered:
            print("No live recorder; finishing interrupted recording(s): "
                  + ", ".join(m.stem for m in recovered))
            return 0
        print("Not recording.")
        return 1
    snap = status_snapshot() or {}
    reply = send_command("stop")
    if not reply or not reply.get("ok"):
        pid = snap.get("pid")  # unreachable over IPC: fall back to a signal
        if not pid:
            print(f"{APP}: a recording is active but cannot be reached (see {tilde(RT)}).",
                  file=sys.stderr)
            return 1
        with contextlib.suppress(OSError):
            os.kill(int(pid), signal.SIGTERM)  # Windows: TerminateProcess (tracks survive)
    deadline = time.time() + STOP_TIMEOUT_S
    while lock_is_held():
        if time.time() > deadline:
            print(f"{APP}: the recorder is taking long to stop; it will finish on its own.",
                  file=sys.stderr)
            return 1
        time.sleep(0.1)
    meta_path = Path(snap["meta"]) if snap.get("meta") else None
    where = f" Saving {tilde(snap['dir'])}{os.sep}{snap['base']}.*" if snap.get("dir") else ""
    print(f"■ Stopped after {fmt_clock(snap.get('recorded_s', 0))}.{where}")
    if not getattr(args, "wait", False) or not meta_path:
        print("  the mix is rendered in the background (a notification follows).")
        return 0
    time.sleep(0.2)
    while session_busy(meta_path):
        time.sleep(0.2)
    meta = read_json(meta_path)
    if meta and meta.get("status") == "done":
        out = session_dir(meta_path) / meta["mix"] if meta.get("mix") else session_dir(meta_path)
        print(f"  saved: {tilde(out)}")
        return 0
    print(f"  finalize status: {(meta or {}).get('status', 'unknown')} "
          f"(see {tilde(log_path_for(meta_path))})")
    return 1


def show_status() -> int:
    snap = status_snapshot()
    if not snap:
        print("Not recording.")
        return 1
    d = Path(snap["dir"])
    size = 0
    with contextlib.suppress(OSError):
        pattern = glob.escape(snap["base"])
        size = sum(p.stat().st_size for p in
                   [*d.glob(pattern + ".tracks*.flac"), *(d / META_DIR).glob(pattern + ".s*.flac")])
    free = None
    with contextlib.suppress(OSError):
        free = shutil.disk_usage(d).free
    state = "paused   " if snap.get("paused") else "recording"
    print(f"{'▮▮' if snap.get('paused') else '●'} {state}  {fmt_clock(snap['recorded_s'])}   "
          f"{tilde(d)}{os.sep}{snap['base']}.*")
    for role in ROLES:
        if not (snap.get("enabled") or {}).get(role, True):
            print(f"  {ROLE_LABEL[role]:<13} off")
            continue
        found = (snap.get("found") or {}).get(role)
        linked = (snap.get("linked") or {}).get(role)
        tag = ("paused" if snap.get("paused") else "ok" if linked else
               "not linked" if found else "waiting for device")
        print(f"  {ROLE_LABEL[role]:<13} {found or '-'}  [{tag}]")
    print(f"  {'Mic monitor':<13} {'on' if snap.get('monitor_mic') else 'off'}")
    print(f"  {'Size':<13} {human_size(size)}"
          + (f"   (free: {human_size(free)})" if free is not None else "")
          + (f"   part {snap.get('part')}" if (snap.get("part") or 1) > 1 else ""))
    return 0


def cmd_devices(args) -> int:
    if getattr(args, "json", False):
        import json

        print(json.dumps(default_device_names()))
        return 0
    for line in backend_module().list_devices():
        print(line)
    return 0


def find_meta(arg: str) -> Path:
    """Accept a session's metadata file, any of its audio files, or its base path."""
    p = Path(arg).expanduser().resolve()
    if p.suffix == ".json" and p.parent.name == META_DIR:
        return p
    name = p.name
    for suffix in (".tracks.flac", ".system.flac", ".mic.flac"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    else:
        name = re.sub(r"\.tracks\.part\d+\.flac$", "", name)
        if name == p.name and p.suffix.lstrip(".") in set(mix_ext(f) for f in MIX_FORMATS):
            name = p.stem
    return meta_path_for(p.parent, name)


def cmd_finalize(args) -> int:
    from .finalize import finalize

    meta_path = find_meta(args.file)
    d = session_dir(meta_path)
    base = meta_path.stem
    if not meta_path.exists() and not list(d.glob(glob.escape(base) + ".tracks*.flac")):
        raise SystemExit(f"{APP}: no recording named {base!r} in {d}")
    meta = read_json(meta_path) or {}
    if args.mix_format == "none" and not args.split_tracks:
        raise SystemExit(f"{APP}: nothing to do with --mix-format none")
    lock = FileLock(session_lock_path(meta_path))
    if not lock.try_acquire():
        if args.recovered:
            return 0  # another recoverer got there first
        raise SystemExit(f"{APP}: that recording is still running or its mix is being "
                         "rendered; try again when it is done")
    try:
        log = Log(log_path_for(meta_path))
        result = finalize(meta_path, log, mix_format=args.mix_format,
                          split=True if args.split_tracks else None, announce=args.recovered)
    finally:
        lock.release()
    if args.recovered and result.get("ok"):
        notify("Interrupted recording recovered", tilde(result.get("mix") or d),
               icon="document-save", enabled=(meta.get("settings") or {}).get("notify", True))
    if not args.recovered:
        print(result.get("mix") or result.get("message"))
    return 0 if result.get("ok") else 1


def cmd_ui(_args) -> int:
    from .ui import main as ui_main

    return ui_main()


# --------------------------------------------------------------------------
# argument parsing


def build_parser() -> argparse.ArgumentParser:
    opts = argparse.ArgumentParser(add_help=False)
    g = opts.add_argument_group("recording options (defaults come from the UI settings)")
    g.add_argument("--output-dir", help="folder to save into")
    g.add_argument("--name", help="file name / prefix, e.g. 'standup'")
    g.add_argument("--timestamp", action=argparse.BooleanOptionalAction, default=None,
                   help="append date & time to the name (default: on)")
    g.add_argument("--sink", help="record this output (Windows: a speaker; macOS: the "
                   "loopback input, e.g. BlackHole) instead of the default")
    g.add_argument("--source", help="record this input instead of the default microphone")
    g.add_argument("--no-system", action="store_true", help="do not record system output")
    g.add_argument("--no-mic", action="store_true", help="do not record the microphone")
    g.add_argument("--monitor-mic", action=argparse.BooleanOptionalAction, default=None,
                   help="also play your mic into the headphones while recording "
                        "(software sidetone)")
    g.add_argument("--mix-format", choices=MIX_FORMATS, default=None,
                   help="format of the stereo mix rendered on stop (default: opus)")
    g.add_argument("--bitrate", help="bitrate for opus/mp3/m4a mixes, e.g. 96k")
    g.add_argument("--bits", type=int, choices=(16, 24), default=16,
                   help="bit depth of the lossless master (default: 16)")
    g.add_argument("--mic-gain", type=float, default=0.0, metavar="DB",
                   help="mic level in the mix, in dB (default: 0)")
    g.add_argument("--system-gain", type=float, default=0.0, metavar="DB",
                   help="system audio level in the mix, in dB (default: 0)")
    g.add_argument("--split-tracks", action="store_true",
                   help="also write <name>.system.flac and <name>.mic.flac on stop")
    g.add_argument("--max-duration", type=c.parse_duration, metavar="DUR",
                   help="stop automatically after e.g. 90m or 4h of recorded audio")
    g.add_argument("--no-notify", action="store_true", help="no desktop notifications")
    g.add_argument("--foreground", action="store_true",
                   help="record in this process (Ctrl+C stops) instead of in the background")

    from . import __doc__ as pkg_doc

    p = argparse.ArgumentParser(prog=APP, description=(pkg_doc or "").split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=("Run without a command to open the window."
                                        if FROZEN else
                                        "Run without a command to toggle recording."))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("toggle", parents=[opts], help="start if idle, stop if recording")
    sub.add_parser("start", parents=[opts], help="start recording")
    pa = sub.add_parser("pause", help="pause the recording (nothing is written while paused)")
    pa.add_argument("--toggle", action="store_true", help="resume if already paused")
    sub.add_parser("resume", help="resume a paused recording")
    s = sub.add_parser("stop", help="stop recording")
    s.add_argument("--wait", action="store_true", help="wait until the mix is written")
    sub.add_parser("status", help="show the active recording")
    dv = sub.add_parser("devices", help="list outputs and inputs")
    dv.add_argument("--json", action="store_true", help="default devices as JSON (for the UI)")
    sub.add_parser("ui", help="open the recorder window")
    f = sub.add_parser("finalize", help="(re)build the mix of a recording")
    f.add_argument("file", help="the recording's .tracks.flac (or its mix / metadata file)")
    f.add_argument("--mix-format", choices=MIX_FORMATS, default=None)
    f.add_argument("--split-tracks", action="store_true")
    f.add_argument("--recovered", action="store_true", help=argparse.SUPPRESS)
    d = sub.add_parser("_daemon")
    d.add_argument("meta")
    return p


def main(argv: list[str]) -> int:
    if not argv:
        argv = ["ui"] if FROZEN else ["toggle"]
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["toggle", *argv]
    args = build_parser().parse_args(argv)
    if args.cmd == "_daemon":
        from .daemon import Daemon

        return Daemon(Path(args.meta)).run()
    if args.cmd == "finalize":
        return cmd_finalize(args)
    if args.cmd == "status":
        return show_status()
    if args.cmd == "devices":
        return cmd_devices(args)
    if args.cmd == "ui":
        return cmd_ui(args)
    with control_lock():
        if args.cmd == "start":
            return do_start(args)
        if args.cmd == "stop":
            return do_stop(args)
        if args.cmd == "pause":
            return do_pause(None if args.toggle else True)
        if args.cmd == "resume":
            return do_pause(False)
        # toggle
        if lock_is_held():
            return do_stop(args)
        return do_start(args)


def _alert(text: str) -> None:
    """Without a terminal (hotkey, UI, windowed build) stderr is invisible:
    also show a notification."""
    try:
        tty = bool(sys.stderr and sys.stderr.isatty())
    except (AttributeError, ValueError):
        tty = False
    if not tty and os.environ.get("AUDIO_REC_QUIET_ERRORS") != "1":
        notify(f"{APP}: error", text, icon="dialog-error", urgency="critical")


def _fix_stdio() -> None:
    """Windowed builds / pythonw have no stdout/stderr/stdin at all: send them
    to a log (many libraries write to sys.stderr unconditionally)."""
    if sys.stdout is None or sys.stderr is None:
        try:
            logdir = c.state_dir() / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            f = open(logdir / f"{APP}-{os.getpid()}.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            f = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f
    if sys.stdin is None:
        sys.stdin = open(os.devnull, encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):  # Windows pipes default to cp1252 + strict
        with contextlib.suppress(Exception):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")


def selftest() -> int:
    """`--selftest` (CI / after install): load every native dependency."""
    import tkinter

    from .finalize import find_ffmpeg, ffmpeg_works

    problems = []
    tcl = tkinter.Tcl().eval("info patchlevel")
    if backend_name() == "portable":
        try:
            import numpy  # noqa: F401
            import soundcard  # noqa: F401
            import soundfile

            formats = soundfile.available_formats()
            if "FLAC" not in formats:
                problems.append("soundfile without FLAC")
        except Exception as e:
            problems.append(f"audio libraries: {e}")
    if not ffmpeg_works():
        problems.append(f"ffmpeg not usable ({find_ffmpeg()})")
    print(f"{APP} selftest: backend={backend_name()} tcl/tk={tcl} ffmpeg={find_ffmpeg()}"
          + ("" if not problems else "\nPROBLEMS: " + "; ".join(problems)))
    return 1 if problems else 0


def run() -> None:
    _fix_stdio()
    if sys.argv[1:2] == ["--selftest"]:
        sys.exit(selftest())
    try:
        code = main(sys.argv[1:])
    except KeyboardInterrupt:
        code = 130
    except SystemExit as e:
        if isinstance(e.code, str):
            _alert(e.code)
        raise
    except Exception as e:
        # log and exit: re-raising in a windowed build would leave a modal
        # traceback dialog behind and a background worker that never exits
        import traceback

        traceback.print_exc()
        _alert(f"{type(e).__name__}: {e}")
        code = 1
    sys.exit(code)
