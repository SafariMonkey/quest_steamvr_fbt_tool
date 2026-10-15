"""A stand-in for the Quest's VRChat, for testing the probe without a headset.

It mimics VRChat's documented OSC/OSCQuery behaviour, not its exact implementation:
- advertises itself as VRChat-Client-FAKE (_osc._udp for its tracker input, _oscjson._tcp for HOST_INFO);
- finds OSCQuery services whose address tree contains /tracking/vrsystem, and streams a synthetic
  head pose (6 floats: Unity position, Unity Euler degrees) to them in timestamped bundles;
- prints the OSC tracker messages it receives.

Usage: uv run python tools/fake_vrchat.py [--ip IP] [--rate 72]
Type 'j' + Enter to simulate a tracking-origin jump (recentre), 'g' to play the gesture sequence that
toggles the bridge's height adjust mode (ending in a fist), 'u' to raise the right hand 5 cm, 'o' to open
the right hand, 'q' to quit.
"""

import argparse
import json
import math
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pythonosc.osc_bundle_builder import OscBundleBuilder  # noqa: E402
from pythonosc.osc_message_builder import OscMessageBuilder  # noqa: E402
from pythonoscquery.osc_query_service import OSCQueryService  # noqa: E402
from pythonoscquery.shared.osc_address_space import OSCAddressSpace  # noqa: E402
from pythonoscquery.shared.osc_path_node import OSCPathNode  # noqa: E402
from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf  # noqa: E402

from qsft.osc_io import OscReceiver  # noqa: E402
from qsft.oscquery import OSCJSON_SERVICE, route_ip  # noqa: E402

NAME = "VRChat-Client-FAKE"


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())


class Targets:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_service: dict[str, tuple[str, int]] = {}

    def snapshot(self) -> list[tuple[str, int]]:
        with self.lock:
            return list(self.by_service.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default=route_ip(None) or "127.0.0.1", help="address to advertise")
    parser.add_argument("--rate", type=float, default=72.0, help="head pose rate (Quest Pro VRChat runs at 72-90 fps)")
    args = parser.parse_args()

    received = {"n": 0, "last": {}}

    def on_tracker(msg) -> None:
        received["n"] += 1
        received["last"][msg.address] = msg.args

    rx = OscReceiver(on_tracker, port=0)
    rx.start()
    space = OSCAddressSpace()
    space.add_node(OSCPathNode("/avatar"))
    service = OSCQueryService(space, NAME, _free_port(), rx.port, args.ip)
    service.start()
    print(f"{NAME} advertised on {args.ip}: OSC in udp/{rx.port}, http/{service.http_port}")

    targets = Targets()
    zc = Zeroconf()
    resolving = threading.Lock()

    def on_change(zeroconf, service_type, name, state_change) -> None:
        if name.startswith(NAME):
            return
        if state_change is ServiceStateChange.Removed:
            with targets.lock:
                targets.by_service.pop(name, None)
            return
        threading.Thread(target=resolve, args=(service_type, name), daemon=True).start()

    def resolve(service_type, name) -> None:
        with resolving:
            info = zc.get_service_info(service_type, name, timeout=3000)
            if not info or not info.parsed_addresses():
                return
            base = f"http://{info.parsed_addresses()[0]}:{info.port}"
            try:
                tree = json.dumps(_get_json(base + "/"))
                if "/tracking/vrsystem" not in tree:
                    return
                host = _get_json(base + "/?HOST_INFO")
            except Exception as e:
                print(f"Couldn't query {name}: {e!r}")
                return
            target = (host["OSC_IP"], int(host["OSC_PORT"]))
            with targets.lock:
                targets.by_service[name] = target
            print(f"Sending head/wrist poses to {host.get('NAME')} at {target[0]}:{target[1]}")

    browser = ServiceBrowser(zc, [OSCJSON_SERVICE], handlers=[on_change])
    jump = {"offset": 0.0, "right_y": 0.0}

    def send_gesture(value: int) -> None:
        m = OscMessageBuilder("/avatar/parameters/GestureRight")
        m.add_arg(value)
        for target in targets.snapshot():
            sock.sendto(m.build().dgram, target)

    def gesture_sequence() -> None:
        for k in range(11):  # Fist/ThumbsUp x10 switches, ending on Fist
            send_gesture(1 if k % 2 == 0 else 7)
            time.sleep(0.3)

    def console() -> None:
        for line in sys.stdin:
            cmd = line.strip().lower()
            if cmd == "g":
                threading.Thread(target=gesture_sequence, daemon=True).start()
            elif cmd == "u":
                jump["right_y"] += 0.05
            elif cmd == "o":
                send_gesture(2)
            elif cmd == "j":
                jump["offset"] += 0.5
                print(f"Simulated origin jump: x offset now {jump['offset']} m")
            elif cmd == "q":
                stop.set()

    stop = threading.Event()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    threading.Thread(target=console, daemon=True).start()
    t0 = time.perf_counter()
    next_print = time.monotonic() + 2
    try:
        while not stop.is_set():
            t = time.perf_counter() - t0
            head = [
                0.3 * math.sin(t * 0.5) + jump["offset"], 1.65 + 0.03 * math.sin(t * 1.3), 0.3 * math.cos(t * 0.5),
                15 * math.sin(t * 0.9), 60 * math.sin(t * 0.3), 8 * math.sin(t * 1.1),
            ]
            bb = OscBundleBuilder(time.time())  # VRChat time-tags its tracking bundles
            for address, values in (
                ("/tracking/vrsystem/head/pose", head),
                ("/tracking/vrsystem/leftwrist/pose", [head[0] - 0.3, 1.0, head[2], 0.0, 0.0, 0.0]),
                ("/tracking/vrsystem/rightwrist/pose", [head[0] + 0.3, 1.0 + jump["right_y"], head[2], 0.0, 0.0, 0.0]),
            ):
                m = OscMessageBuilder(address)
                for v in values:
                    m.add_arg(float(v))
                bb.add_content(m.build())
            dgram = bb.build().dgram
            for target in targets.snapshot():
                sock.sendto(dgram, target)
            if time.monotonic() >= next_print:
                next_print += 2
                last = {a: [round(v, 3) for v in vals] for a, vals in received["last"].items()}
                print(f"Tracker messages received: {received['n']}  last: {last}", flush=True)
            time.sleep(1 / args.rate)
    except KeyboardInterrupt:
        pass
    finally:
        browser.cancel()
        zc.close()
        service.stop()
        rx.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    main()
