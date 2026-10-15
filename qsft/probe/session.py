"""Pieces shared by the probe commands: config, console input, and the link to the Quest's VRChat."""

import queue
import sys
import threading
import time
import tomllib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from qsft.coords import unity_matrix_from_euler
from qsft.osc_io import OscReceiver, ReceivedMessage
from qsft.oscquery import (
    OSC_SERVICE,
    VRSYSTEM_POSE_ADDRESSES,
    OscQueryAdvertiser,
    ServiceRecord,
    ServiceWatcher,
    local_ipv4_addresses,
    route_ip,
)
from qsft.recording import Recorder, now_ns

HEAD_ADDRESS = VRSYSTEM_POSE_ADDRESSES[0]
STATS_WINDOW_S = 2.0


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f).get("probe", {})


def log(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


class ConsoleInput:
    """Lines typed into the console, read on a background thread."""

    def __init__(self):
        self._lines: queue.SimpleQueue[str] = queue.SimpleQueue()
        threading.Thread(target=self._run, name="console", daemon=True).start()

    def lines(self) -> list[str]:
        out = []
        while not self._lines.empty():
            out.append(self._lines.get())
        return out

    def _run(self) -> None:
        for line in sys.stdin:
            self._lines.put(line.strip())


class Every:
    def __init__(self, seconds: float):
        self._interval = seconds
        self._next = time.monotonic() + seconds

    def due(self) -> bool:
        now = time.monotonic()
        if now < self._next:
            return False
        self._next = now + self._interval
        return True


class Ticker:
    """Fixed-rate loop timing that doesn't accumulate drift."""

    def __init__(self, rate_hz: float):
        self.set_rate(rate_hz)

    def set_rate(self, rate_hz: float) -> None:
        self._period = 1.0 / rate_hz
        self._next = time.perf_counter()

    def wait(self) -> None:
        self._next += self._period
        delay = self._next - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        elif delay < -self._period:
            self._next = time.perf_counter()  # fell behind; don't try to catch up


@dataclass
class HeadPose:
    t: int
    src_ip: str
    pos: np.ndarray  # Unity axes, metres
    euler: np.ndarray  # Unity Euler degrees (x, y, z)
    rot: np.ndarray  # Unity axes


@dataclass
class StreamStats:
    count: int = 0
    arrivals: deque = field(default_factory=lambda: deque(maxlen=4096))
    last_args: tuple = ()
    last_bundle_time: float | None = None

    def add(self, msg: ReceivedMessage) -> None:
        self.count += 1
        self.arrivals.append(msg.t)
        self.last_args = msg.args
        self.last_bundle_time = msg.bundle_time

    def window(self, now: int) -> tuple[float, float]:
        """(rate in Hz, largest gap in ms) over the last STATS_WINDOW_S, including the gap up to now."""
        start = now - int(STATS_WINDOW_S * 1e9)
        recent = [t for t in self.arrivals if t >= start]
        if not recent:
            return 0.0, (now - self.arrivals[-1]) / 1e6 if self.arrivals else float("inf")
        gaps = np.diff([*recent, now]) if len(recent) > 1 else [now - recent[0]]
        return len(recent) / STATS_WINDOW_S, max(gaps) / 1e6


class QuestLink:
    """Advertises over OSCQuery, receives VRChat's tracking output, and watches for VRChat clients."""

    # A head can't move this fast; faster changes are tracking-origin jumps (recentre, relocalisation).
    JUMP_SPEED_M_S = 3.0
    JUMP_MIN_M = 0.05
    JUMP_ANGULAR_DEG_S = 720.0
    JUMP_MIN_DEG = 20.0

    def __init__(
        self, recorder: Recorder, cfg: dict, tree: str = "typed", name: str = "QSFT-Probe",
        on_head: Callable[[ReceivedMessage], None] | None = None,
    ):
        self._rec = recorder
        self._on_head_cb = on_head
        self._started = time.monotonic()
        self._queried_by: set[str] = set()
        self.quest_ip: str | None = cfg.get("quest_ip") or None
        self.local_ips = {ip for _, ip in local_ipv4_addresses()} | {"127.0.0.1"}
        self.lan_ip = cfg.get("lan_ip") or route_ip(self.quest_ip) or "127.0.0.1"
        self._lock = threading.Lock()
        self.stats: dict[tuple[str, str], StreamStats] = {}
        self.heads: dict[str, HeadPose] = {}
        self._http_seen: dict[tuple[str, str], int] = {}

        adapters = local_ipv4_addresses()
        recorder.write("net", lan_ip=self.lan_ip, quest_ip=self.quest_ip, adapters=adapters)
        log("Network adapters: " + ", ".join(f"{adapter}={ip}" for adapter, ip in adapters))

        self._receiver = OscReceiver(self._on_message, self._on_bad_packet, port=int(cfg.get("osc_port", 0)))
        self._receiver.start()
        self._advertiser = OscQueryAdvertiser(name, self.lan_ip, self._receiver.port, tree, self._on_http)
        self._advertiser.start()
        name = self._advertiser.name
        log(
            f"Advertising '{name}' on {self.lan_ip}: OSC udp/{self._receiver.port}, "
            f"OSCQuery http/{self._advertiser.http_port} (tree: {tree})"
        )
        recorder.write(
            "advertise", name=name, ip=self.lan_ip, osc_port=self._receiver.port,
            http_port=self._advertiser.http_port, tree=tree,
        )
        self.watcher = ServiceWatcher(self._on_service)

    def close(self) -> None:
        self.watcher.close()
        self._advertiser.stop()
        self._receiver.stop()

    def is_local(self, ip: str) -> bool:
        return ip in self.local_ips

    def quest_target(self) -> tuple[str, int] | None:
        """The Quest's VRChat OSC input.

        IP: configured, else a VRChat client on another machine (mDNS), else wherever head poses come from.
        Port: from its _osc._udp record or HOST_INFO (the Quest only advertised _oscjson), else 9000.
        HOST_INFO's OSC_IP is the Quest's own 127.0.0.1, so it's never used.
        """
        clients = [s for s in self.watcher.vrchat_clients() if s.addresses]
        ip = self.quest_ip
        if ip is None:
            remote = [s for s in clients if not self.is_local(s.addresses[0])]
            if remote:
                ip = remote[0].addresses[0]
            else:
                with self._lock:
                    ip = next((h for h in self.heads if not self.is_local(h)), None)
        if ip is None:
            return None
        for s in clients:
            if ip in s.addresses:
                if s.type == OSC_SERVICE and s.port:
                    return ip, s.port
                if s.host_info and isinstance(s.host_info.get("OSC_PORT"), int):
                    return ip, s.host_info["OSC_PORT"]
        return ip, 9000  # VRChat's default OSC input port

    def is_quest(self, ip: str) -> bool:
        return ip == self.quest_ip if self.quest_ip else not self.is_local(ip)

    def head(self) -> HeadPose | None:
        """Latest head pose from the Quest (configured IP, else any non-local sender)."""
        with self._lock:
            for ip, pose in self.heads.items():
                if (self.quest_ip and ip == self.quest_ip) or (not self.quest_ip and not self.is_local(ip)):
                    return pose
        return None

    def status_lines(self) -> list[str]:
        now = now_ns()
        lines = []
        with self._lock:
            items = sorted(self.stats.items())
        if not items:
            lines.append("  no OSC received yet")
        for (ip, address), st in items:
            rate, gap = st.window(now)
            where = "LOCAL (this PC)" if self.is_local(ip) else ip
            values = ", ".join(f"{v:.3f}" if isinstance(v, float) else repr(v) for v in st.last_args)
            lines.append(f"  {where} {address}: {rate:5.1f} Hz, max gap {gap:6.0f} ms, n={st.count}  [{values}]")
        vrc = self.watcher.vrchat_clients()
        lines.append(
            "  VRChat clients: "
            + (", ".join(sorted({f"{s.name.split('.')[0]}@{s.addresses[0] if s.addresses else '?'}" for s in vrc})) or "none seen")
        )
        unasked = {s.addresses[0] for s in vrc if s.addresses and not self.is_local(s.addresses[0])} - self._queried_by
        if unasked and time.monotonic() - self._started > 10:
            lines.append(
                f"  HINT: VRChat at {', '.join(sorted(unasked))} hasn't queried us. In VRChat, turn "
                "'Allow Sending Head and Wrist VR Tracking OSC Data' off and on again (or restart VRChat)."
            )
        return lines

    def _on_message(self, msg: ReceivedMessage) -> None:
        ip = msg.src[0]
        self._rec.write(
            "osc", t=msg.t, src=f"{ip}:{msg.src[1]}", a=msg.address,
            v=[round(v, 6) if isinstance(v, float) else v for v in msg.args], bt=msg.bundle_time,
        )
        with self._lock:
            st = self.stats.get((ip, msg.address))
            if st is None:
                st = self.stats[(ip, msg.address)] = StreamStats()
                log(f"First {msg.address} from {ip}{' (LOCAL - this PC)' if self.is_local(ip) else ''}")
            st.add(msg)
        if msg.address == HEAD_ADDRESS and len(msg.args) == 6:
            self._on_head(msg)
            if self._on_head_cb and self.is_quest(ip):
                self._on_head_cb(msg)

    def _on_head(self, msg: ReceivedMessage) -> None:
        ip = msg.src[0]
        pose = HeadPose(
            msg.t, ip, np.array(msg.args[:3], dtype=float), np.array(msg.args[3:], dtype=float),
            unity_matrix_from_euler(msg.args[3:]),
        )
        with self._lock:
            prev = self.heads.get(ip)
            self.heads[ip] = pose
        if prev is None:
            return
        dt = max((pose.t - prev.t) / 1e9, 1e-4)
        dpos = float(np.linalg.norm(pose.pos - prev.pos))
        cos = (np.trace(prev.rot.T @ pose.rot) - 1) / 2
        dang = float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))
        if (dpos > self.JUMP_MIN_M and dpos / dt > self.JUMP_SPEED_M_S) or (
            dang > self.JUMP_MIN_DEG and dang / dt > self.JUMP_ANGULAR_DEG_S
        ):
            self._rec.write("jump", t=pose.t, src=ip, dt_ms=round(dt * 1e3, 1), dpos_m=round(dpos, 4), dang_deg=round(dang, 2))
            log(f"Head pose jump from {ip}: {dpos * 100:.1f} cm, {dang:.1f} deg in {dt * 1e3:.0f} ms")

    def _on_bad_packet(self, src: tuple[str, int], error: Exception) -> None:
        self._rec.write("osc_error", src=f"{src[0]}:{src[1]}", error=repr(error))

    def _on_http(self, client_ip: str, path: str) -> None:
        self._queried_by.add(client_ip)
        self._rec.write("http", client=client_ip, path=path)
        key = (client_ip, path)
        self._http_seen[key] = self._http_seen.get(key, 0) + 1
        if self._http_seen[key] == 1:
            log(f"OSCQuery request from {client_ip}{' (LOCAL - this PC)' if self.is_local(client_ip) else ''}: GET {path}")

    def _on_service(self, event: str, record: ServiceRecord, host_info: dict | None) -> None:
        self._rec.write(
            "mdns", event=event, type=record.type, name=record.name, addresses=record.addresses,
            port=record.port, host_info=host_info,
        )
        if event == "updated":
            return
        addr = f"{record.addresses[0]}:{record.port}" if record.addresses else "?"
        log(f"mDNS {event}: {record.name} {addr}" + (f" HOST_INFO={host_info}" if host_info else ""))
