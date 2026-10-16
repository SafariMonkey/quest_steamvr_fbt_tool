"""The link to VRChat on the Quest: advertise over OSCQuery, receive its head pose, find where to send."""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

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
from qsft.recording import NullRecorder, now_ns
from qsft.util import log

HEAD_ADDRESS = VRSYSTEM_POSE_ADDRESSES[0]
STATS_WINDOW_S = 2.0


@dataclass
class StreamStats:
    count: int = 0
    arrivals: deque = field(default_factory=lambda: deque(maxlen=4096))

    def add(self, t: int) -> None:
        self.count += 1
        self.arrivals.append(t)

    def window(self, now: int) -> tuple[float, float]:
        """(rate in Hz, largest gap in ms) over the last STATS_WINDOW_S, including the gap up to now."""
        start = now - int(STATS_WINDOW_S * 1e9)
        recent = [t for t in self.arrivals if t >= start]
        if not recent:
            return 0.0, (now - self.arrivals[-1]) / 1e6 if self.arrivals else float("inf")
        gaps = np.diff([*recent, now]) if len(recent) > 1 else [now - recent[0]]
        return len(recent) / STATS_WINDOW_S, max(gaps) / 1e6


class QuestLink:
    """Advertises over OSCQuery, receives VRChat's head pose, and watches for VRChat clients."""

    def __init__(self, cfg: dict, on_head: Callable[[ReceivedMessage], None], recorder=None, name: str = "QSFT"):
        self._rec = recorder or NullRecorder()
        self._on_head = on_head
        self._queried_at: dict[str, float] = {}  # VRChat IP -> last OSCQuery request or head pose from it
        self._vrchat_seen_at: dict[str, float] = {}  # VRChat IP -> when its service (re)appeared
        self.quest_ip: str | None = cfg.get("quest_ip") or None
        self.local_ips = {ip for _, ip in local_ipv4_addresses()} | {"127.0.0.1"}
        self.lan_ip = cfg.get("lan_ip") or route_ip(self.quest_ip) or "127.0.0.1"
        self._lock = threading.Lock()
        self._head_stats: dict[str, StreamStats] = {}

        self._receiver = OscReceiver(self._on_message, self._on_bad_packet, port=int(cfg.get("osc_port", 0)))
        self._receiver.start()
        self._advertiser = OscQueryAdvertiser(name, self.lan_ip, self._receiver.port, self._on_http)
        self._advertiser.start()
        log(f"Advertising '{self._advertiser.name}' on {self.lan_ip} (OSC udp/{self._receiver.port})")
        self._rec.write("advertise", name=self._advertiser.name, ip=self.lan_ip, osc_port=self._receiver.port,
                        http_port=self._advertiser.http_port, adapters=local_ipv4_addresses())
        self.watcher = ServiceWatcher(self._on_service)

    def close(self) -> None:
        self.watcher.close()
        self._advertiser.stop()
        self._receiver.stop()

    def is_local(self, ip: str) -> bool:
        return ip in self.local_ips

    def is_quest(self, ip: str) -> bool:
        return ip == self.quest_ip if self.quest_ip else not self.is_local(ip)

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
                    ip = next((h for h in self._head_stats if not self.is_local(h)), None)
        if ip is None:
            return None
        for s in clients:
            if ip in s.addresses:
                if s.type == OSC_SERVICE and s.port:
                    return ip, s.port
                if s.host_info and isinstance(s.host_info.get("OSC_PORT"), int):
                    return ip, s.host_info["OSC_PORT"]
        return ip, 9000  # VRChat's default OSC input port

    def status_lines(self) -> list[str]:
        now = now_ns()
        lines = []
        with self._lock:
            heads = sorted(self._head_stats.items())
        if not heads:
            lines.append("  no head pose from VRChat yet")
        for ip, st in heads:
            rate, gap = st.window(now)
            where = "LOCAL (this PC; ignored)" if not self.is_quest(ip) else ip
            lines.append(f"  head pose from {where}: {rate:5.1f} Hz, max gap {gap:6.0f} ms")
        vrc = self.watcher.vrchat_clients()
        lines.append(
            "  VRChat clients: "
            + (", ".join(sorted({f"{s.name.split('.')[0]}@{s.addresses[0] if s.addresses else '?'}" for s in vrc})) or "none seen")
        )
        now_s = time.monotonic()
        unasked = {ip for ip, seen in self._vrchat_seen_at.items()
                   if not self.is_local(ip) and self._queried_at.get(ip, -1.0) < seen and now_s - seen > 10}
        if unasked:
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
        if msg.address != HEAD_ADDRESS or len(msg.args) != 6:
            return
        self._queried_at[ip] = time.monotonic()  # receiving its head pose proves VRChat found us
        with self._lock:
            st = self._head_stats.get(ip)
            if st is None:
                st = self._head_stats[ip] = StreamStats()
                log(f"Receiving the head pose from {ip}{' (LOCAL - this PC; ignored)' if not self.is_quest(ip) else ''}")
            st.add(msg.t)
        if self.is_quest(ip):
            self._on_head(msg)

    def _on_bad_packet(self, src: tuple[str, int], error: Exception) -> None:
        self._rec.write("osc_error", src=f"{src[0]}:{src[1]}", error=repr(error))

    def _on_http(self, client_ip: str, path: str) -> None:
        if self._queried_at.get(client_ip, -1.0) < self._vrchat_seen_at.get(client_ip, 0.0) or client_ip not in self._queried_at:
            log(f"VRChat at {client_ip} found us{' (LOCAL - this PC)' if self.is_local(client_ip) else ''}")
        self._queried_at[client_ip] = time.monotonic()
        self._rec.write("http", client=client_ip, path=path)

    def _on_service(self, event: str, record: ServiceRecord, host_info: dict | None) -> None:
        self._rec.write(
            "mdns", event=event, type=record.type, name=record.name, addresses=record.addresses,
            port=record.port, host_info=host_info,
        )
        if record.is_vrchat and event == "added":
            log(f"Found {record.name.split('.')[0]} at {record.addresses[0] if record.addresses else '?'}")
            if record.addresses:
                self._vrchat_seen_at[record.addresses[0]] = time.monotonic()
