"""OSCQuery: advertise ourselves so VRChat sends us its tracking data, and watch for VRChat clients.

VRChat only sends /tracking/vrsystem/* (head and wrist poses) to applications
it discovers over mDNS whose OSCQuery address tree contains those paths. On a
standalone Quest this is the only way to get OSC output to a PC.
"""

import json
import socket
import threading
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import ifaddr
from pythonoscquery.osc_query_service import OSCQueryHTTPHandler, OSCQueryHTTPServer, OSCQueryService
from pythonoscquery.shared.osc_access import OSCAccess
from pythonoscquery.shared.osc_address_space import OSCAddressSpace
from pythonoscquery.shared.osc_path_node import OSCPathNode
from pythonoscquery.shared.oscquery_spec import OSCQueryAttribute
from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

VRSYSTEM_POSE_ADDRESSES = (
    "/tracking/vrsystem/head/pose",
    "/tracking/vrsystem/leftwrist/pose",
    "/tracking/vrsystem/rightwrist/pose",
)
OSC_SERVICE = "_osc._udp.local."
OSCJSON_SERVICE = "_oscjson._tcp.local."
VRCHAT_PREFIX = "VRChat-Client"


def local_ipv4_addresses() -> list[tuple[str, str]]:
    """(adapter name, IPv4 address) for every adapter, to help choose which one to advertise."""
    out = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if ip.is_IPv4:
                out.append((adapter.nice_name, ip.ip))
    return out


def route_ip(towards: str | None) -> str | None:
    """This PC's address on the route towards `towards` (or the default route). Sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect((towards or "192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return None


def build_address_space(tree: str) -> OSCAddressSpace:
    """`typed` mirrors VRChat's own receiver sample; `bare` mirrors SlimeVR (one empty container node)."""
    space = OSCAddressSpace()
    space.add_node(OSCPathNode("/avatar"))  # VRChat then sends avatar parameters (incl. hand gestures)
    if tree == "bare":
        space.add_node(OSCPathNode("/tracking/vrsystem"))
        return space
    for address in VRSYSTEM_POSE_ADDRESSES:
        node = OSCPathNode(address, access=OSCAccess.WRITEONLY_VALUE, value=[0.0] * 6)
        # VRChat's library omits VALUE on write-only endpoints; keep TYPE ("ffffff") only.
        node.attributes[OSCQueryAttribute.VALUE] = None
        space.add_node(node)
    return space


class _RecordingHandler(OSCQueryHTTPHandler):
    def do_GET(self) -> None:
        callback = getattr(self.server, "on_request", None)
        if callback:
            callback(self.client_address[0], self.path)
        super().do_GET()

    def log_message(self, format, *args) -> None:
        pass  # requests are reported through on_request instead of stderr


class OscQueryAdvertiser:
    def __init__(
        self,
        name: str,
        lan_ip: str,
        osc_port: int,
        tree: str = "typed",
        on_request: Callable[[str, str], None] | None = None,
    ):
        space = build_address_space(tree)
        http = OSCQueryHTTPServer(space, None, ("", 0), _RecordingHandler)
        http.on_request = on_request
        self.http_port = http.server_address[1]
        # A fresh name per run (as SlimeVR does): VRChat ignored a re-advertised name with new ports.
        self.name = f"{name}-{self.http_port}"
        self._service = OSCQueryService(space, self.name, self.http_port, osc_port, lan_ip)
        http.host_info = self._service.host_info
        # Pre-create the HTTP server so start() only registers mDNS; this gives us the
        # request hook and an OS-assigned port that is known before advertising.
        self._service.http_server = http
        threading.Thread(target=http.serve_forever, name="oscquery-http", daemon=True).start()

    def start(self) -> None:
        self._service.start()

    def stop(self) -> None:
        self._service.stop()


@dataclass
class ServiceRecord:
    type: str
    name: str
    addresses: list[str] = field(default_factory=list)
    port: int | None = None
    host_info: dict | None = None

    @property
    def is_vrchat(self) -> bool:
        return self.name.startswith(VRCHAT_PREFIX)


class ServiceWatcher:
    """Reports every OSC / OSCQuery service on the LAN, including VRChat clients and ourselves."""

    def __init__(self, on_event: Callable[[str, ServiceRecord, dict | None], None]):
        self._on_event = on_event
        self._lock = threading.Lock()
        self.services: dict[tuple[str, str], ServiceRecord] = {}
        self._resolver = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mdns-resolve")
        self._zc = Zeroconf()
        self._browser = ServiceBrowser(self._zc, [OSC_SERVICE, OSCJSON_SERVICE], handlers=[self._on_change])

    def vrchat_osc_targets(self) -> list[ServiceRecord]:
        with self._lock:
            return [s for s in self.services.values() if s.is_vrchat and s.type == OSC_SERVICE]

    def vrchat_clients(self) -> list[ServiceRecord]:
        with self._lock:
            return [s for s in self.services.values() if s.is_vrchat]

    def close(self) -> None:
        self._browser.cancel()
        self._resolver.shutdown(wait=False, cancel_futures=True)
        self._zc.close()

    def _on_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        # Zeroconf handlers must not block, so resolution happens on a worker thread.
        if state_change is ServiceStateChange.Removed:
            with self._lock:
                record = self.services.pop((service_type, name), ServiceRecord(service_type, name))
            self._on_event("removed", record, None)
        else:
            self._resolver.submit(self._resolve, service_type, name, state_change.name.lower())

    def _resolve(self, service_type: str, name: str, event: str) -> None:
        info = self._zc.get_service_info(service_type, name, timeout=3000)
        record = ServiceRecord(service_type, name)
        if info is not None:
            record.addresses = info.parsed_addresses()
            record.port = info.port
        if record.is_vrchat and service_type == OSCJSON_SERVICE and record.addresses and record.port:
            record.host_info = _fetch_host_info(record.addresses[0], record.port)
        with self._lock:
            self.services[(service_type, name)] = record
        self._on_event(event, record, record.host_info)


def _fetch_host_info(ip: str, port: int) -> dict:
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/?HOST_INFO", timeout=3) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": repr(e)}
