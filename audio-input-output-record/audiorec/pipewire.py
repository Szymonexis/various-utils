"""Linux / PipeWire backend.

One `pw-record` process owns a private 3-channel capture node
(FL, FR = system output, FC = microphone) started with `--target 0`, so
WirePlumber never routes it; we patch it ourselves with `pw-link`:

    <output device>:monitor_FL/FR  ->  recorder:input_FL/FR
    <input device>:capture_*       ->  recorder:input_FC

Reading a sink's monitor ports and a source's capture ports is purely
passive, and both signals enter one node, so they are sample-aligned for
the whole take.  Pause unlinks the node (it then idles and writes nothing).
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import signal
import subprocess
import time
from typing import TYPE_CHECKING, Callable

from .common import (APP, CAPTURE_BACKOFF_S, RATE, ROLES, child_env, notify, now_iso)

if TYPE_CHECKING:
    from .daemon import Daemon

CHANNEL_MAP = "FL,FR,FC"  # recorder channels: system L, system R, microphone


def make_pdeathsig(sig: int) -> Callable[[], None] | None:
    """preexec_fn that makes a child receive `sig` if this process dies, so
    a crashed/killed daemon still lets pw-record close its file properly."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
    except (OSError, AttributeError):
        return None

    def preexec() -> None:
        prctl(1, int(sig), 0, 0, 0)  # PR_SET_PDEATHSIG

    return preexec


def truthy(v) -> bool:
    return v is True or str(v).lower() == "true"


class Graph:
    """A snapshot of the PipeWire graph from `pw-dump`."""

    def __init__(self, objects: list):
        self.nodes: dict[int, dict] = {}
        self.ports: dict[int, dict] = {}
        self.links: dict[int, dict] = {}
        self.defaults: dict[str, str] = {}
        configured: dict[str, str] = {}
        for o in objects:
            if not isinstance(o, dict):
                continue
            kind, info = o.get("type", ""), o.get("info") or {}
            if kind == "PipeWire:Interface:Node":
                self.nodes[o["id"]] = info.get("props") or {}
            elif kind == "PipeWire:Interface:Port":
                props = dict(info.get("props") or {})
                props["_direction"] = info.get("direction")
                self.ports[o["id"]] = props
            elif kind == "PipeWire:Interface:Link":
                self.links[o["id"]] = info
            elif (kind == "PipeWire:Interface:Metadata"
                  and (o.get("props") or {}).get("metadata.name") == "default"):
                for m in o.get("metadata") or []:
                    key, value = m.get("key", ""), m.get("value")
                    if isinstance(value, str):
                        with contextlib.suppress(ValueError):
                            value = json.loads(value)
                    name = value.get("name") if isinstance(value, dict) else None
                    if not name or m.get("subject", 0) != 0:
                        continue
                    for k in ("sink", "source"):
                        if key == f"default.audio.{k}":
                            self.defaults[k] = name
                        elif key == f"default.configured.audio.{k}":
                            configured[k] = name
        for k, v in configured.items():
            self.defaults.setdefault(k, v)

    def node_id(self, name: str | None) -> int | None:
        if not name:
            return None
        for nid, p in self.nodes.items():
            if p.get("node.name") == name:
                return nid
        return None

    def describe(self, nid: int | None) -> str | None:
        if nid is None:
            return None
        p = self.nodes.get(nid, {})
        return p.get("node.description") or p.get("node.nick") or p.get("node.name")

    def _ports(self, nid: int, direction: str) -> list[tuple[int, dict]]:
        found = [(pid, p) for pid, p in self.ports.items()
                 if p.get("node.id") == nid and p.get("_direction") == direction]
        return sorted(found, key=lambda x: (int(x[1].get("port.id", 0) or 0), x[0]))

    def out_ports(self, nid: int, monitor: bool) -> list[tuple[int, dict]]:
        return [(pid, p) for pid, p in self._ports(nid, "output")
                if truthy(p.get("port.monitor")) == monitor]

    def in_ports(self, nid: int) -> list[tuple[int, dict]]:
        return self._ports(nid, "input")

    def devices(self, kind: str) -> list[tuple[int, dict]]:
        def match(cls: str) -> bool:
            if kind == "sink":
                return cls.startswith("Audio/Sink") or cls == "Audio/Duplex"
            return cls.startswith("Audio/Source") or cls == "Audio/Duplex"

        return sorted(((nid, p) for nid, p in self.nodes.items()
                       if match(str(p.get("media.class", "")))),
                      key=lambda x: str(x[1].get("node.description", "")))


def pw_graph(timeout: float = 10) -> Graph:
    r = subprocess.run(["pw-dump", "-N"], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=timeout, env=child_env())
    if r.returncode != 0:
        raise RuntimeError(f"pw-dump failed: {r.stderr.strip() or r.returncode}")
    data = json.loads(r.stdout or "[]")
    if not isinstance(data, list):
        raise RuntimeError("unexpected pw-dump output")
    return Graph(data)


def pw_link(out_port: int, in_port: int) -> tuple[bool, str]:
    r = subprocess.run(["pw-link", str(out_port), str(in_port)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=5, env=child_env())
    msg = (r.stderr or r.stdout).strip()
    return r.returncode == 0 or "exists" in msg.lower(), msg


def pw_unlink(link_id: int) -> None:
    subprocess.run(["pw-link", "-d", str(link_id)], stdin=subprocess.DEVNULL,
                   capture_output=True, text=True, timeout=5, env=child_env())


def plan_links(g: Graph, rec_in: dict[str, int], sink: str | None,
               source: str | None) -> tuple[dict[tuple[int, int], str], dict[str, str | None]]:
    """Desired links {(out_port, in_port): role} into the recorder, plus the
    description of the device found for each role (None if absent)."""
    want: dict[tuple[int, int], str] = {}
    found: dict[str, str | None] = {"system": None, "mic": None}

    sid = g.node_id(sink)
    if sid is not None:
        mon = g.out_ports(sid, monitor=True)
        by_ch = {p.get("audio.channel"): pid for pid, p in mon}
        if "FL" in by_ch and "FR" in by_ch:
            pairs = [(by_ch["FL"], "FL"), (by_ch["FR"], "FR")]
        elif len(mon) == 1:  # mono output: feed both recorder channels
            pairs = [(mon[0][0], "FL"), (mon[0][0], "FR")]
        elif len(mon) >= 2:
            pairs = [(mon[0][0], "FL"), (mon[1][0], "FR")]
        else:
            pairs = []
        for out, ch in pairs:
            if ch in rec_in:
                want[(out, rec_in[ch])] = "system"
        if pairs:
            found["system"] = g.describe(sid)
            extra = sorted(str(p.get("audio.channel")) for _, p in mon
                           if p.get("audio.channel") not in ("FL", "FR"))
            if "FL" in by_ch and extra:
                found["system"] += f" (front L/R only; not recorded: {' '.join(extra)})"

    nid = g.node_id(source)
    if nid is not None and "FC" in rec_in:
        # A sink chosen as "input" (e.g. "Monitor of ...") is read via its
        # monitor; virtual sources (Audio/Source/Virtual) only expose
        # monitor-flagged capture ports, so fall back to those.
        is_sink = str(g.nodes[nid].get("media.class", "")).startswith("Audio/Sink")
        outs = g.out_ports(nid, monitor=is_sink) or g.out_ports(nid, monitor=not is_sink)
        if outs:
            by_ch = {p.get("audio.channel"): pid for pid, p in outs}
            out = next((by_ch[c] for c in ("MONO", "FL", "FC") if c in by_ch), outs[0][0])
            want[(out, rec_in["FC"])] = "mic"
            found["mic"] = g.describe(nid)
    return want, found


def resolve_device(g: Graph, spec: str, kind: str) -> str:
    """Accept a node.name, id/serial, or a unique case-insensitive substring
    of the description; return the node.name."""
    cands = g.devices(kind)
    for nid, p in cands:
        if spec in (p.get("node.name"), str(nid), str(p.get("object.serial"))):
            return p["node.name"]
    s = spec.lower()
    hits = [p for _, p in cands if s in " ".join(
        str(p.get(k, "")) for k in ("node.description", "node.nick", "node.name")).lower()]
    if len(hits) == 1:
        return hits[0]["node.name"]
    listing = "\n".join(f"  {p.get('node.description')}  [{p.get('node.name')}]" for _, p in cands)
    why = "matches several" if hits else "matches no"
    raise SystemExit(f"{APP}: {spec!r} {why} {kind}s. Available:\n{listing or '  (none)'}")


# --------------------------------------------------------------------------
# backend API used by the CLI


def check_tools(monitor: bool) -> None:
    need = ["pw-record", "pw-link", "pw-dump"] + (["pw-loopback"] if monitor else [])
    missing = [t for t in need if not shutil.which(t)]
    if missing:
        raise SystemExit(f"{APP}: missing {', '.join(missing)} (PipeWire tools). On NixOS run "
                         "through ./audio-rec (uses shell.nix); elsewhere install PipeWire's "
                         "command-line tools or set AUDIO_REC_BACKEND=portable.")


def resolve_targets(sink_spec: str | None, source_spec: str | None) -> tuple[str | None, str | None]:
    try:
        g = pw_graph()
    except Exception as e:
        raise SystemExit(f"{APP}: cannot talk to PipeWire: {e}")
    return (resolve_device(g, sink_spec, "sink") if sink_spec else None,
            resolve_device(g, source_spec, "source") if source_spec else None)


def default_device_names() -> dict:
    g = pw_graph(timeout=3)
    return {"system": g.describe(g.node_id(g.defaults.get("sink"))),
            "mic": g.describe(g.node_id(g.defaults.get("source")))}


def list_devices() -> list[str]:
    g = pw_graph()
    lines = []
    for kind, title in (("sink", "Outputs (recorded via their monitor)"), ("source", "Inputs")):
        lines.append(f"{title}:")
        for _, p in g.devices(kind):
            mark = "*" if p.get("node.name") == g.defaults.get(kind) else " "
            lines.append(f"  {mark} {p.get('node.description') or p.get('node.name')}")
            lines.append(f"      {p.get('node.name')}")
    lines.append("(* = current default; pass a name or part of the description to "
                 "--sink/--source)")
    return lines


# --------------------------------------------------------------------------
# the capture side of the daemon


class PipeWireBackend:
    name = "pipewire"

    def __init__(self, d: "Daemon"):
        self.d = d
        self.proc: subprocess.Popen | None = None
        self.part = 0
        self.part_started = 0.0
        self.capture_failures = 0
        self.node_name = ""
        self.ours: set[tuple[int, int]] = set()
        self.loopback: subprocess.Popen | None = None
        self.loopback_failures = 0
        self.found: dict[str, str | None] = {"system": None, "mic": None}
        self.linked: dict[str, bool] = {"system": False, "mic": False}

    # ---- lifecycle (called by the daemon) -------------------------------

    def start(self) -> None:
        self.start_capture()
        if self.d.settings.get("monitor_mic"):
            self.start_loopback()
        # the loopback plays into the very output we record: its voice is in L/R
        self.d.save_meta(monitor_recorded=bool(self.d.settings.get("monitor_mic"))
                         and self.d.settings.get("sink_mode") != "off")

    def poll(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            if self.proc is not None:
                self.on_capture_exit()
            else:
                with contextlib.suppress(Exception):
                    self.start_capture()
            return
        try:
            self.reconcile(pw_graph())
        except Exception as e:
            self.d.log(f"graph: {e}")
        self.check_loopback()

    def on_pause(self) -> None:
        try:
            self.reconcile(pw_graph())
        except Exception as e:
            self.d.log(f"graph: {e}")

    def stop(self) -> None:
        self.stop_loopback()
        self.stop_capture()

    # ---- capture process ------------------------------------------------

    def target(self, role: str, g: Graph) -> str | None:
        key = "sink" if role == "system" else "source"
        mode = self.d.settings.get(f"{key}_mode", "default")
        if mode == "off":
            return None
        if mode == "pinned":
            return self.d.settings.get(key)
        return g.defaults.get(key)

    def start_capture(self) -> None:
        d = self.d
        self.part += 1
        name = (f"{d.base}.tracks.flac" if self.part == 1
                else f"{d.base}.tracks.part{self.part}.flac")
        self.node_name = f"{APP}.{os.getpid()}.{self.part}"
        props = {
            "node.name": self.node_name,
            "node.description": "audio-rec recorder",
            "media.name": f"audio-rec: {d.base}",
            "application.name": APP,
            "application.icon-name": "media-record",
            "node.autoconnect": False,
            # Only run while linked to a device: unlinked (paused, or every
            # device gone) the recorder is not scheduled, so nothing -- not
            # even silence -- is written and resume continues seamlessly.
            "node.want-driver": False,
            # keep WirePlumber from restoring a saved volume/mute onto us
            "state.restore-props": False,
            "state.restore-target": False,
        }
        cmd = ["pw-record", "--target", "0", "--rate", str(RATE), "--channels", "3",
               "--channel-map", CHANNEL_MAP,
               "--format", "s24" if int(d.settings.get("bits", 16)) == 24 else "s16",
               "-P", json.dumps(props), str(d.dir / name)]
        d.log(f"capture: part {self.part} -> {name}")
        # own session: a Ctrl+C in --foreground reaches only us, and we stop
        # pw-record in order; PDEATHSIG still stops it if we die.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=d.log.f,
                                     stderr=d.log.f, env=child_env(),
                                     start_new_session=True,
                                     preexec_fn=make_pdeathsig(signal.SIGINT))
        self.part_started = time.time()
        self.ours.clear()
        self.linked = {"system": False, "mic": False}
        d.meta.setdefault("parts", []).append({"file": name, "started": now_iso()})
        d.save_meta()
        self.wait_for_node()

    def wait_for_node(self, timeout: float = 6) -> None:
        """Link as soon as the recorder's ports exist, so the first moments
        after the trigger are captured."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.d.stop_reason:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"pw-record exited immediately (rc={self.proc.returncode})")
            with contextlib.suppress(Exception):
                g = pw_graph()
                rec = g.node_id(self.node_name)
                if rec is not None and len(g.in_ports(rec)) >= 3:
                    self.reconcile(g)
                    return
            time.sleep(0.05)
        if not self.d.stop_reason:
            raise RuntimeError("the recorder node did not appear in PipeWire")

    def close_part(self) -> None:
        d = self.d
        parts = d.meta.get("parts") or []
        if not parts:
            return
        last = parts[-1]
        last.setdefault("ended", now_iso())
        path = d.dir / last["file"]
        if not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            parts.pop()
        d.save_meta()

    def stop_capture(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)  # pw-record finalises the FLAC header
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        if p:
            self.close_part()
        self.proc = None

    def on_capture_exit(self) -> None:
        d = self.d
        assert self.proc is not None
        rc, ran = self.proc.returncode, time.time() - self.part_started
        d.log(f"capture: pw-record exited unexpectedly (rc={rc}) after {ran:.0f}s")
        self.proc = None
        self.close_part()
        if ran > 60:
            self.capture_failures = 0
        self.capture_failures += 1
        if self.capture_failures == 1:
            notify("Recording interrupted", "Capture stopped unexpectedly; restarting into a "
                   "new part file.", icon="dialog-warning", enabled=d.notify_on)
        delay = CAPTURE_BACKOFF_S[min(self.capture_failures - 1, len(CAPTURE_BACKOFF_S) - 1)]
        d.wait(delay)
        if d.stop_reason:
            return
        try:
            self.start_capture()
        except Exception as e:  # PipeWire may be restarting; try again next poll
            d.log(f"capture: restart failed: {e}")

    # ---- links ----------------------------------------------------------

    def reconcile(self, g: Graph) -> None:
        d = self.d
        rec = g.node_id(self.node_name)
        if rec is None:
            return
        rec_in = {p.get("audio.channel"): pid for pid, p in g.in_ports(rec)}
        want, found = plan_links(g, rec_in, self.target("system", g), self.target("mic", g))
        if d.paused:  # paused = recorder unlinked: it idles and writes nothing
            want = {}
        existing = {(l.get("output-port-id"), l.get("input-port-id")): lid
                    for lid, l in g.links.items() if l.get("input-node-id") == rec}
        for key in list(self.ours):  # only ever remove links we created
            if key not in existing:
                self.ours.discard(key)
            elif key not in want:
                pw_unlink(existing[key])
                self.ours.discard(key)
        ok_keys = set()
        for key in want:
            if key in existing:
                self.ours.add(key)
                ok_keys.add(key)
                continue
            ok, msg = pw_link(*key)
            if ok:
                self.ours.add(key)
                ok_keys.add(key)
            else:
                d.log(f"link: {key[0]} -> {key[1]} failed: {msg}")
        for role in ROLES:
            keys = [k for k, r in want.items() if r == role]
            linked = bool(keys) and all(k in ok_keys for k in keys)
            if not d.paused and (found[role] != self.found[role]
                                 or linked != self.linked[role]):
                state = "linked" if linked else ("not linked" if found[role] else "unavailable")
                d.log(f"{role}: {found[role] or '-'} ({state})")
            self.found[role], self.linked[role] = found[role], linked

    # ---- mic monitor ----------------------------------------------------

    def start_loopback(self) -> None:
        d = self.d
        # state.restore-*=false: WirePlumber must not remember (and later
        # restore) a volume, mute or target for these helper streams
        quiet = {"state.restore-props": False, "state.restore-target": False}
        cap = {"node.description": "audio-rec mic monitor (in)", "audio.position": ["MONO"],
               **quiet}
        play = {"node.description": "Mic monitor (audio-rec)", "audio.position": ["MONO"],
                **quiet}
        if d.settings.get("source_mode") == "pinned":
            cap["target.object"] = d.settings.get("source")
        if d.settings.get("sink_mode") == "pinned":
            play["target.object"] = d.settings.get("sink")
        cmd = ["pw-loopback", "-n", f"{APP}-monitor", "-c", "1", "-m", "[ MONO ]",
               "-l", "10", "--capture-props", json.dumps(cap),
               "--playback-props", json.dumps(play)]
        d.log("monitor: starting mic -> headphones loopback")
        self.loopback = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=d.log.f,
                                         stderr=d.log.f, env=child_env(),
                                         start_new_session=True,
                                         preexec_fn=make_pdeathsig(signal.SIGTERM))

    def check_loopback(self) -> None:
        d = self.d
        if not d.settings.get("monitor_mic") or d.stop_reason:
            return
        if self.loopback is None or self.loopback.poll() is not None:
            if self.loopback is not None:
                self.loopback_failures += 1
                d.log(f"monitor: pw-loopback exited (rc={self.loopback.returncode})")
                self.loopback = None
                if self.loopback_failures == 21:
                    d.log("monitor: giving up on the mic monitor (recording continues)")
                    notify("Mic monitor stopped", "It kept failing; the recording itself "
                           "continues.", icon="dialog-warning", enabled=d.notify_on)
            if self.loopback_failures > 20:
                return
            with contextlib.suppress(OSError):
                self.start_loopback()

    def stop_loopback(self) -> None:
        p, self.loopback = self.loopback, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
