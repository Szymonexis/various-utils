"""Render the stereo mix (and optional split tracks) from a session's
3-channel master parts with ffmpeg.  Shared by every backend."""
from __future__ import annotations

import contextlib
import glob
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .common import (BELOW_NORMAL_PRIORITY_CLASS, CREATE_NO_WINDOW, DEFAULT_BITRATE,
                     DISK_STOP_BYTES, IS_WIN, RATE, Log, child_env, human_size,
                     log_path_for, notify, now_iso, read_json, session_dir, tilde, write_json)


def find_ffmpeg() -> str | None:
    """ffmpeg from PATH (Linux: shell.nix; Windows/macOS: winget/brew), else
    the binary shipped by the `imageio-ffmpeg` pip package."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    # apps started from Finder do not get Homebrew's PATH
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and os.path.exists(exe) else None
    except Exception:  # not installed, or no binary for this platform
        return None


def _popen_kwargs(low_priority: bool = False) -> dict:
    if IS_WIN:
        flags = CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY_CLASS if low_priority else 0)
        return {"creationflags": flags}
    if low_priority:
        return {"preexec_fn": lambda: os.nice(10)}
    return {}


def ffmpeg_works(ffmpeg: str | None = None) -> bool:
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return False
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        return subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True,
                              timeout=15, env=child_env(), **_popen_kwargs()).returncode == 0
    return False


def ffmpeg_encoders(ffmpeg: str) -> set[str]:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=20, env=child_env(),
                           **_popen_kwargs())
    except (OSError, subprocess.SubprocessError):
        return set()
    return {m.group(1) for m in re.finditer(r"^\s*A\S*\s+(\S+)", r.stdout, re.M)}


def encoder_args(fmt: str, bitrate: str | None, bits: int, encoders: set[str],
                 log: Log) -> tuple[list[str], str]:
    """ffmpeg codec arguments and file extension for the mix."""
    if fmt == "opus":
        br = bitrate or DEFAULT_BITRATE["opus"]
        if "libopus" in encoders:
            return ["-c:a", "libopus", "-b:a", br, "-vbr", "on"], "opus"
        if "opus" in encoders:
            return ["-c:a", "opus", "-strict", "-2", "-b:a", br], "opus"
        log("no opus encoder in ffmpeg; writing the mix as FLAC instead")
        fmt = "flac"
    if fmt == "mp3":
        if "libmp3lame" in encoders:
            return ["-c:a", "libmp3lame", "-b:a", bitrate or DEFAULT_BITRATE["mp3"]], "mp3"
        log("no mp3 encoder in ffmpeg; writing the mix as FLAC instead")
        fmt = "flac"
    if fmt == "m4a":
        return ["-c:a", "aac", "-b:a", bitrate or DEFAULT_BITRATE["m4a"],
                "-movflags", "+faststart"], "m4a"
    if fmt == "wav":  # RF64 kicks in automatically past 4 GiB
        return ["-c:a", "pcm_s24le" if bits == 24 else "pcm_s16le", "-rf64", "auto"], "wav"
    return flac_args(bits), "flac"


def flac_args(bits: int) -> list[str]:
    if bits == 24:
        return ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    return ["-c:a", "flac", "-sample_fmt", "s16"]


def probe_part(path: Path) -> tuple[bool, str]:
    """Is this a readable 3-channel master?  soundfile if installed (the
    portable setup; imageio-ffmpeg ships no ffprobe), else ffprobe."""
    try:
        import soundfile  # type: ignore

        info = soundfile.info(str(path))
        return info.channels == 3, f"{info.channels} channels"
    except ImportError:
        pass
    except Exception as e:  # unreadable
        return False, str(e)
    if not shutil.which("ffprobe"):
        return True, "not probed"
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=60, env=child_env(), **_popen_kwargs())
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return False, (r.stderr.strip() or "unreadable")
    return out.split(",")[0] == "3", f"{out} channels"


def estimate_output_bytes(parts: list[Path], fmt: str, split: bool, settings: dict,
                          meta: dict) -> int:
    """Rough upper bound of what finalize will write."""
    tracks = sum(p.stat().st_size for p in parts)
    seconds = float(meta.get("recorded_s") or 0) or tracks / 60_000  # FLAC ~60 kB/s worst case
    need = tracks if split else 0
    if fmt in DEFAULT_BITRATE:
        br = settings.get("bitrate") or DEFAULT_BITRATE[fmt]
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", str(br))
        bps = float(m.group(1)) * {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}[m.group(2)] \
            if m else 192e3
        need += int(seconds * bps / 8 * 1.1)
    elif fmt == "wav":
        need += int(seconds * RATE * 2 * (3 if int(settings.get("bits", 16)) == 24 else 2))
    elif fmt != "none":
        need += tracks
    return need


def db_to_gain(db: float) -> str:
    return f"{10 ** (db / 20):.6f}"


def finalize(meta_path: Path, log: Log, *, mix_format: str | None = None,
             split: bool | None = None, should_abort: Callable[[], bool] = lambda: False,
             announce: bool = True) -> dict:
    """Build `<base>.<ext>` (stereo mix) and optionally `<base>.system.flac` /
    `<base>.mic.flac` from the 3-channel parts.  Never touches the parts.
    The caller must hold the session lock."""
    meta = read_json(meta_path) or {}
    d = session_dir(meta_path)
    settings = meta.get("settings") or {}
    base = meta.get("base") or meta_path.stem
    fmt = mix_format or settings.get("mix_format") or "opus"
    split = settings.get("split_tracks", False) if split is None else split
    notify_on = settings.get("notify", True) and announce
    bits = int(settings.get("bits", 16))

    def save(**kv) -> None:
        current = read_json(meta_path) or meta
        current.update(kv)
        with contextlib.suppress(OSError):
            write_json(meta_path, current, durable=True)

    save(status="finalizing", finalizer_pid=os.getpid())
    if meta.get("backend") == "portable":
        # separate system/mic captures -> one aligned 3-channel master first
        from .portable import build_tracks

        built = build_tracks(meta_path, log, should_abort)
        if not built.get("ok"):
            save(status="stopped" if built.get("retry") else "failed",
                 error=built.get("message"))
            if notify_on and built.get("message") != "interrupted":
                notify("Recording not finished yet", f"{built.get('message')}. The raw "
                       "captures are safe" + ("; it is retried on the next start."
                                              if built.get("retry") else "."),
                       icon="dialog-warning", urgency="critical")
            return {"ok": False, "message": built.get("message"), "mix": None}
        meta = read_json(meta_path) or meta

    names = [p.get("file") for p in meta.get("parts") or [] if p.get("file")]
    if not names:  # metadata lost/stale: fall back to what is on disk
        names = sorted(p.name for p in d.glob(glob.escape(base) + ".tracks*.flac")
                       if ".incomplete-" not in p.name)
    parts: list[Path] = []
    for name in names:
        if should_abort():
            save(status="stopped", error="interrupted")
            return {"ok": False, "message": "interrupted", "mix": None}
        path = d / name
        if not path.exists() or path.stat().st_size == 0:
            continue
        ok, info = probe_part(path)
        if ok:
            parts.append(path)
        else:
            log(f"finalize: skipping {name}: {info}")
    if not parts:
        log("finalize: nothing was recorded")
        save(status="empty", finalized=now_iso())
        if notify_on:
            notify("Recording was empty", f"Nothing was captured for {base}",
                   icon="dialog-warning")
        return {"ok": False, "message": "nothing recorded", "mix": None}

    tracks_hint = parts[0] if len(parts) == 1 else d
    if fmt == "none" and not split:
        save(status="done", finalized=now_iso(), mix=None)
        return {"ok": True, "message": "no mix requested", "mix": None, "tracks": str(tracks_hint)}
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("finalize: ffmpeg not found; the 3-channel master is kept as-is")
        save(status="failed", error="ffmpeg not found")
        if notify_on:
            notify("Recording saved without mix", "ffmpeg is missing; master tracks: "
                   + tilde(tracks_hint), icon="dialog-warning")
        return {"ok": False, "message": "ffmpeg not found", "mix": None}

    need = estimate_output_bytes(parts, fmt, split, settings, meta)
    try:
        free = shutil.disk_usage(d).free
    except OSError:
        free = None
    if free is not None and free - need < DISK_STOP_BYTES // 2:
        why = (f"not enough disk space for the mix (needs ~{human_size(need)}, "
               f"{human_size(free)} free)")
        log(f"finalize: {why}; it will be retried on the next start")
        save(status="stopped", error=why)  # stays recoverable
        if notify_on:
            notify("Mix postponed: disk almost full", f"Free up space, then run "
                   f"`audio-rec finalize {tilde(parts[0])}`. The lossless tracks are safe.",
                   icon="dialog-warning", urgency="critical")
        return {"ok": False, "message": why, "mix": None}

    encoders = ffmpeg_encoders(ffmpeg)
    gs = db_to_gain(float(settings.get("system_gain_db", 0)))
    gm = db_to_gain(float(settings.get("mic_gain_db", 0)))
    n = len(parts)
    inputs: list[str] = []
    for p in parts:
        inputs += ["-i", str(p)]
    labels = (["mix"] if fmt != "none" else []) + (["sys", "mic"] if split else [])
    fc = ("".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[src];" if n > 1
          else "[0:a]anull[src];")
    fc += f"[src]asplit={len(labels)}" + "".join(f"[x{i}]" for i in range(len(labels)))
    outputs: list[str] = []
    final: list[tuple[Path, Path]] = []  # (temp, final)

    def add_output(label: str, chain: str, codec: list[str], ext: str, suffix: str) -> None:
        nonlocal fc
        fc += f";[x{labels.index(label)}]{chain}[o_{label}]"
        target = d / f"{base}{suffix}.{ext}"
        temp = d / f"{base}{suffix}.incomplete-{os.getpid()}.{ext}"
        outputs.extend(["-map", f"[o_{label}]", *codec,
                        "-metadata", f"title={base}{suffix}", str(temp)])
        final.append((temp, target))

    if "mix" in labels:
        monitored = meta.get("monitor_recorded")
        if monitored is None:  # sessions recorded before this flag existed
            monitored = settings.get("monitor_mic") and settings.get("sink_mode") != "off"
        if monitored:
            # The mic monitor played into the recorded output, so the system
            # channels already contain the voice as you heard it.
            pan = f"pan=stereo|FL={gs}*c0|FR={gs}*c1"
        else:
            pan = f"pan=stereo|FL={gs}*c0+{gm}*c2|FR={gs}*c1+{gm}*c2"
        codec, ext = encoder_args(fmt, settings.get("bitrate"), bits, encoders, log)
        # float first: summing in the decoder's integer format would clip
        # before the limiter ever sees the peaks
        add_output("mix", f"aformat=sample_fmts=dbl,{pan},alimiter=limit=0.97:level=0:latency=1",
                   codec, ext, "")
    if split:
        add_output("sys", "pan=stereo|FL=c0|FR=c1", flac_args(bits), "flac", ".system")
        add_output("mic", "pan=mono|c0=c2", flac_args(bits), "flac", ".mic")

    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           *inputs, "-filter_complex", fc, *outputs]
    log(f"finalize: rendering {', '.join(t.name for _, t in final)} from {n} part(s)")
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log.f, stderr=log.f,
                            env=child_env(), **_popen_kwargs(low_priority=True))
    aborted = False
    while proc.poll() is None:
        if should_abort():
            aborted = True
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break
        time.sleep(0.25)
    if not aborted and proc.returncode != 0 and should_abort():
        aborted = True  # ffmpeg itself got the logout/shutdown SIGTERM
    if aborted or proc.returncode != 0:
        for temp, _ in final:
            temp.unlink(missing_ok=True)
        why = "interrupted" if aborted else f"ffmpeg exited with {proc.returncode}"
        log(f"finalize: failed ({why}); the 3-channel master is untouched")
        save(status="failed" if not aborted else "stopped", error=why)
        if notify_on and not aborted:
            notify("Could not build the mix", f"{why}; see {tilde(log_path_for(meta_path))}. "
                   "The lossless tracks are safe.", icon="dialog-error", urgency="critical")
        return {"ok": False, "message": why, "mix": None}
    for temp, target in final:
        os.replace(temp, target)
    mix = final[0][1] if "mix" in labels else None
    log(f"finalize: done in {time.time() - t0:.1f}s -> {', '.join(t.name for _, t in final)}")
    save(status="done", finalized=now_iso(), mix=mix.name if mix else None, error=None)
    return {"ok": True, "message": "done", "mix": str(mix) if mix else None,
            "outputs": [str(t) for _, t in final]}
