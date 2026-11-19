"""Shared, OS-independent plumbing: paths, config, naming, locks, IPC,
process spawning and notifications.  Works on Linux, macOS and Windows."""
from __future__ import annotations

import contextlib
import datetime as dt
import glob
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

APP = "audio-rec"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
FROZEN = bool(getattr(sys, "frozen", False))  # PyInstaller build
PKG_DIR = Path(__file__).resolve().parent
ENTRY = PKG_DIR.parent / "audio_rec.py"  # the script entry point (not frozen)

RATE = 48000
POLL_S = float(os.environ.get("AUDIO_REC_POLL", "2"))
DISK_WARN_BYTES = 2 * 1024**3
DISK_STOP_BYTES = 300 * 1024**2
READY_TIMEOUT_S = 20
STOP_TIMEOUT_S = 30
RECENT_KEEP = 50  # sessions remembered for crash recovery
RECOVERY_SCAN = 25
MIX_FORMATS = ("opus", "flac", "mp3", "m4a", "wav", "none")
DEFAULT_BITRATE = {"opus": "128k", "mp3": "192k", "m4a": "192k"}
CAPTURE_BACKOFF_S = (0.5, 1, 2, 5, 10, 30)
ROLES = ("system", "mic")
ROLE_LABEL = {"system": "System audio", "mic": "Microphone"}
META_DIR = ".audio-rec"

# Windows process creation flags (subprocess exposes them only on Windows)
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


# --------------------------------------------------------------------------
# per-OS locations


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else Path.home() / fallback


def _win_dir(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    if IS_WIN:
        return _win_dir("APPDATA", "AppData/Roaming") / APP
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def state_dir() -> Path:
    if IS_WIN:
        return _win_dir("LOCALAPPDATA", "AppData/Local") / APP
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def runtime_dir() -> Path:
    if IS_WIN:
        d = _win_dir("LOCALAPPDATA", "AppData/Local") / APP / "run"
    elif IS_MAC:  # not $TMPDIR: it differs between GUI, Terminal, ssh and cron
        d = Path.home() / "Library" / "Application Support" / APP / "run"
    else:
        base = os.environ.get("XDG_RUNTIME_DIR")
        d = (Path(base) / APP if base and os.path.isdir(base)
             else Path(f"/tmp/{APP}-{os.getuid()}"))
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


RT = runtime_dir()
LOCK = RT / "recording.lock"  # held by the daemon while it records
CTL = RT / "control.lock"  # serialises start/stop/toggle/pause commands
STATE = RT / "state.json"  # live status of the active recording (+ IPC port)
CONFIG_PATH = config_dir() / "config.json"
RECENT_PATH = state_dir() / "recent.json"


def music_dir() -> Path:
    """XDG_MUSIC_DIR from ~/.config/user-dirs.dirs on Linux, else ~/Music."""
    home = Path.home()
    if IS_LINUX:
        cfg = _xdg("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs"
        try:
            for line in cfg.read_text(encoding="utf-8").splitlines():
                m = re.match(r'\s*XDG_MUSIC_DIR\s*=\s*"(.*)"\s*$', line)
                if m:
                    value = m.group(1).replace("$HOME", str(home))
                    if value and Path(value) != home:
                        return Path(value)
        except OSError:
            pass
    return home / "Music"


def default_output_root() -> Path:
    env = os.environ.get("AUDIO_REC_DIR")
    return Path(env).expanduser().resolve() if env else music_dir() / "Recordings"


# --------------------------------------------------------------------------
# small helpers


def child_env() -> dict:
    """Environment for spawned tools.  On Linux a foreign LD_LIBRARY_PATH
    breaks Nix-built binaries (seen: ffmpeg dies with a glibc mismatch from
    an injected alsa-lib); nothing we spawn ever needs it."""
    env = dict(os.environ)
    if not IS_WIN:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def no_window_flags() -> int:
    """creationflags that keep console tools from flashing a window on Windows."""
    return CREATE_NO_WINDOW if IS_WIN else 0


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def fmt_clock(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def tilde(p: Path | str) -> str:
    s, home = str(p), str(Path.home())
    if IS_WIN:
        return s
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def read_json(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: dict, durable: bool = False) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        if durable:
            f.flush()
            os.fsync(f.fileno())
    for attempt in range(20):  # Windows: a reader may briefly hold the target open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not IS_WIN or attempt == 19:
                raise
            time.sleep(0.05)


def parse_duration(text: str) -> int:
    """'90' (seconds), '45m', '3h', '1h30m', '2h15m10s' -> seconds."""
    import argparse

    t = text.strip().lower()
    if t.isdigit():
        return int(t)
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m(?:in)?)?\s*(?:(\d+)s)?", t)
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (try 90m, 3h, 1h30m)")
    h, mi, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


class Log:
    """Timestamped append-only log."""

    def __init__(self, path: Path | None):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", buffering=1, encoding="utf-8") if path else sys.stderr

    def __call__(self, msg: str) -> None:
        try:
            self.f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        except (OSError, ValueError, AttributeError):  # stderr is None in windowed builds
            pass


# --------------------------------------------------------------------------
# config and naming


CONFIG_DEFAULTS = {"output_dir": "", "name": "", "timestamp": True,
                   "mix_format": "opus", "monitor_mic": False}


def load_config() -> dict:
    """Defaults shared by the UI and the hotkey; env vars override the file."""
    cfg = dict(CONFIG_DEFAULTS)
    for k, v in (read_json(CONFIG_PATH) or {}).items():
        if k in cfg and isinstance(v, type(CONFIG_DEFAULTS[k])):
            cfg[k] = v
    if os.environ.get("AUDIO_REC_DIR"):
        cfg["output_dir"] = os.environ["AUDIO_REC_DIR"]
    if os.environ.get("AUDIO_REC_FORMAT") in MIX_FORMATS:
        cfg["mix_format"] = os.environ["AUDIO_REC_FORMAT"]
    if os.environ.get("AUDIO_REC_MONITOR_MIC") in ("0", "1"):
        cfg["monitor_mic"] = os.environ["AUDIO_REC_MONITOR_MIC"] == "1"
    if cfg["mix_format"] not in MIX_FORMATS:
        cfg["mix_format"] = "opus"
    if not cfg["output_dir"]:
        cfg["output_dir"] = str(default_output_root())
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {k: cfg.get(k, v) for k, v in CONFIG_DEFAULTS.items()}
    write_json(CONFIG_PATH, data)


def clean_name(name: str | None) -> str:
    """Make a user-typed name safe as a file-name prefix on every OS."""
    n = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "-", name or "")
    n = re.sub(r"\s+", " ", n).strip(" .-")
    if IS_WIN and re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", n):
        n = f"_{n}"  # reserved device names
    return n[:100]


def make_base(name: str | None, timestamp: bool, when: dt.datetime | None = None) -> str:
    n = clean_name(name)
    if n and not timestamp:
        return n
    stamp = (when or dt.datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    return f"{n}_{stamp}" if n else stamp


def meta_path_for(directory: Path, base: str) -> Path:
    return directory / META_DIR / f"{base}.json"


def session_dir(meta_path: Path) -> Path:
    """The folder holding the audio files of the session described by meta_path."""
    return meta_path.parent.parent


def log_path_for(meta_path: Path) -> Path:
    return meta_path.with_suffix(".log")


def session_lock_path(meta_path: Path) -> Path:
    """Held by whichever process owns a session (recorder, then finalizer).
    Kept on the local runtime dir: network folders may not support locks."""
    import hashlib

    key = hashlib.sha1(os.path.normcase(str(meta_path)).encode("utf-8")).hexdigest()
    return RT / "sessions" / f"{key}.lock"


def base_taken(directory: Path, base: str) -> bool:
    if meta_path_for(directory, base).exists():
        return True
    return any(directory.glob(glob.escape(base) + ".*"))


def plan_base(directory: Path, name: str | None, timestamp: bool) -> str:
    """The base name a recording started now would get (for previews)."""
    base = make_base(name, timestamp)
    candidate, i = base, 1
    while base_taken(directory, candidate) and i < 1000:
        i += 1
        candidate = f"{base}-{i}"
    return candidate


def mix_ext(fmt: str) -> str:
    return {"opus": "opus", "mp3": "mp3", "m4a": "m4a", "wav": "wav"}.get(fmt, "flac")


def remember_session(meta_path: Path) -> None:
    """Index of recent sessions (persists across reboots) for crash recovery."""
    with contextlib.suppress(OSError):
        RECENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        items = [p for p in (read_json(RECENT_PATH) or {}).get("sessions", [])
                 if isinstance(p, str) and p != str(meta_path)]
        write_json(RECENT_PATH, {"sessions": [str(meta_path), *items][:RECENT_KEEP]})


def recent_sessions() -> list[Path]:
    items = (read_json(RECENT_PATH) or {}).get("sessions", [])
    return [Path(p) for p in items if isinstance(p, str)]


# --------------------------------------------------------------------------
# inter-process locks (released by the OS when the holder dies)


class FileLock:
    """Exclusive, non-blocking lock on a file: fcntl.flock on Linux/macOS,
    msvcrt.locking on Windows (on a byte far past the content, so the file
    itself stays readable for other processes)."""

    WIN_OFFSET = 1 << 20

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if IS_WIN:
                import msvcrt

                os.lseek(fd, self.WIN_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def acquire(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while not self.try_acquire():
            if time.time() > deadline:
                return False
            time.sleep(0.05)
        return True

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if IS_WIN:
                import msvcrt

                with contextlib.suppress(OSError):
                    os.lseek(fd, self.WIN_OFFSET, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def lock_is_held(path: Path = LOCK) -> bool:
    """True if some live process holds the lock at `path`."""
    probe = FileLock(path)
    if probe.try_acquire():
        probe.release()
        return False
    return True


_CTL_LOCKS: list[FileLock] = []


def release_control_lock() -> None:
    """Let other commands run (used once a --foreground recorder is up)."""
    for lk in list(_CTL_LOCKS):
        lk.release()


@contextlib.contextmanager
def control_lock(timeout: float = 60):
    lk = FileLock(CTL)
    if not lk.acquire(timeout):
        raise SystemExit(f"{APP}: another {APP} command is still busy")
    _CTL_LOCKS.append(lk)
    try:
        yield
    finally:
        _CTL_LOCKS.remove(lk)
        lk.release()


# --------------------------------------------------------------------------
# IPC: the daemon listens on 127.0.0.1 (port + secret token in STATE)


class ControlServer:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.token = secrets.token_hex(16)

    def handle(self, on_command: Callable[[str], dict]) -> None:
        """Serve every pending connection (called when the socket is readable)."""
        while True:
            try:
                conn, _ = self.sock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2.0)
                    data = b""
                    while b"\n" not in data and len(data) < 8192:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    req = json.loads(data.decode("utf-8", "replace").splitlines()[0])
                    tok = req.get("token") if isinstance(req, dict) else None
                    if not (isinstance(tok, str) and hmac.compare_digest(
                            tok.encode("utf-8", "replace"), self.token.encode("ascii"))):
                        reply = {"ok": False, "error": "bad token"}
                    else:
                        reply = on_command(str(req.get("cmd", "")))
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                except Exception:  # anything a local client sends is untrusted
                    continue

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


def send_command(cmd: str, timeout: float = 5.0) -> dict | None:
    """Ask the running daemon to do something; None if it cannot be reached."""
    st = read_json(STATE) or {}
    port, token = st.get("port"), st.get("token")
    if not port or not token:
        return None
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps({"token": token, "cmd": cmd}) + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode("utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------
# re-invoking ourselves (script or frozen build) in the background


def self_cmd(*args: str) -> list[str]:
    if FROZEN:  # the PyInstaller executable (inside the .app on macOS)
        return [sys.executable, *args]
    exe = sys.executable
    if IS_WIN and Path(exe).name.lower() == "pythonw.exe":
        console = Path(exe).with_name("python.exe")  # capture output for the UI
        if console.exists():
            exe = str(console)
    return [exe, str(ENTRY), *args]


def spawn_self(args: list[str], log_path: Path | None) -> subprocess.Popen:
    """Start `audio-rec <args>` fully detached from the caller (it must
    survive the hotkey helper, the UI or the terminal going away)."""
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(log_path, "a") if log_path else subprocess.DEVNULL
    cmd = self_cmd(*args)
    # a frozen child must not reuse the parent's unpacked runtime: it outlives it
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    env.pop("AUDIO_REC_QUIET_ERRORS", None)  # only the UI's own command shows errors inline
    try:
        if IS_WIN:
            # CREATE_NO_WINDOW gives python.exe a hidden console that ffmpeg
            # inherits (no window flashes); never combine with DETACHED_PROCESS.
            base = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
                try:
                    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out,
                                            stderr=out, creationflags=flags, env=env,
                                            cwd=str(Path.home()), close_fds=True)
                except OSError:  # the job object may forbid breaking away
                    if flags == base:
                        raise
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                start_new_session=True, cwd="/", env=env)
    finally:
        if log_path:
            out.close()


def run_self(args: list[str], timeout: float = 90) -> subprocess.CompletedProcess:
    """Run an audio-rec command and capture its output (used by the UI)."""
    env = {**os.environ, "AUDIO_REC_QUIET_ERRORS": "1"}  # the UI shows errors inline
    env["PYTHONIOENCODING"] = "utf-8:replace"
    return subprocess.run(self_cmd(*args), stdin=subprocess.DEVNULL, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env,
                          creationflags=no_window_flags())


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    opener = "open" if IS_MAC else (shutil.which("xdg-open") or shutil.which("kde-open"))
    if opener:
        subprocess.Popen([opener, str(path)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=child_env(), start_new_session=True)


class KeepAwake:
    """Stop the machine from idle-sleeping during a long recording (lid
    close / power button still win).  Windows: SetThreadExecutionState;
    macOS: caffeinate; Linux: systemd-inhibit.  All released when we exit."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.win = False

    def start(self) -> None:
        try:
            if IS_WIN:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
                self.win = True
                return
            pid = str(os.getpid())
            if IS_MAC and os.path.exists("/usr/bin/caffeinate"):
                cmd = ["/usr/bin/caffeinate", "-i", "-w", pid]
            elif shutil.which("systemd-inhibit") and shutil.which("tail"):
                cmd = ["systemd-inhibit", "--what=idle:sleep", f"--who={APP}",
                       "--why=Recording audio", "--mode=block",
                       "tail", f"--pid={pid}", "-f", "/dev/null"]
            else:
                return
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         env=child_env(), start_new_session=True)
        except Exception:
            pass

    def stop(self) -> None:
        if self.win:
            with contextlib.suppress(Exception):
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            self.win = False
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            with contextlib.suppress(Exception):
                p.terminate()
                p.wait(3)


# --------------------------------------------------------------------------
# notifications


_WIN_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:AUDIO_REC_TITLE)) > $null
$n.Item(1).AppendChild($t.CreateTextNode($env:AUDIO_REC_BODY)) > $null
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(summary: str, body: str = "", *, icon: str = "audio-input-microphone",
           urgency: str = "normal", enabled: bool = True) -> None:
    """Best-effort desktop notification; never raises, never blocks for long.
    Linux: notify-send, else busctl (always present with systemd).
    macOS: osascript.  Windows: a PowerShell toast (fire and forget)."""
    if not enabled or os.environ.get("AUDIO_REC_NOTIFY", "1") == "0":
        return
    timeout_ms = 10000 if urgency == "critical" else 5000
    try:
        if IS_WIN:
            env = {**os.environ, "AUDIO_REC_TITLE": summary, "AUDIO_REC_BODY": body or " "}
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle",
                              "Hidden", "-Command", _WIN_TOAST],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env,
                             creationflags=CREATE_NO_WINDOW)
            return
        if IS_MAC:  # texts via argv: no AppleScript quoting issues
            subprocess.Popen(["osascript", "-e", "on run argv",
                              "-e", "display notification (item 3 of argv) with title "
                                    "(item 1 of argv) subtitle (item 2 of argv)",
                              "-e", "end run", APP, summary, body or " "],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        if shutil.which("notify-send"):
            cmd = ["notify-send", "-a", APP, "-i", icon, "-u", urgency,
                   "-t", str(timeout_ms), summary, body]
        elif shutil.which("busctl"):
            hints = ["1", "urgency", "y", "2"] if urgency == "critical" else ["0"]
            cmd = ["busctl", "--user", "call", "org.freedesktop.Notifications",
                   "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                   "Notify", "susssasa{sv}i", APP, "0", icon, summary, body, "0",
                   *hints, str(timeout_ms)]
        else:
            return
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=child_env(), start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------------
# backend selection


def backend_name() -> str:
    """'pipewire' (Linux, best: sample-locked, passive patching) or
    'portable' (Windows WASAPI loopback / macOS loopback device, also any
    Linux with PulseAudio) -- override with AUDIO_REC_BACKEND."""
    forced = os.environ.get("AUDIO_REC_BACKEND")
    if forced in ("pipewire", "portable"):
        return forced
    if IS_LINUX and shutil.which("pw-record") and shutil.which("pw-dump"):
        return "pipewire"
    return "portable"
