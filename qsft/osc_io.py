"""OSC over UDP: a raw receiver that keeps bundle time tags, and a sender.

The receiver parses packets itself rather than using python-osc's dispatcher.
The dispatcher sleeps until a bundle's time tag, and the Quest's clock isn't
ours. It also replaces past time tags with the arrival time, which loses the
sender-side timing we want to record.
"""

import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from pythonosc.osc_bundle import OscBundle
from pythonosc.osc_bundle_builder import IMMEDIATELY, OscBundleBuilder
from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder

from qsft.recording import now_ns


@dataclass(frozen=True)
class ReceivedMessage:
    t: int  # perf_counter_ns at arrival
    src: tuple[str, int]
    address: str
    args: tuple
    # Bundle time tag in seconds since the Unix epoch, by the sender's clock. 0 means
    # "immediately"; None means the message wasn't in a bundle.
    bundle_time: float | None


def parse_datagram(dgram: bytes) -> Iterator[tuple[float | None, OscMessage]]:
    if OscBundle.dgram_is_bundle(dgram):
        yield from _walk_bundle(OscBundle(dgram))
    elif OscMessage.dgram_is_message(dgram):
        yield None, OscMessage(dgram)


def _walk_bundle(bundle: OscBundle) -> Iterator[tuple[float | None, OscMessage]]:
    for content in bundle:
        if isinstance(content, OscMessage):
            yield bundle.timestamp, content
        else:
            yield from _walk_bundle(content)


class OscReceiver:
    def __init__(
        self,
        on_message: Callable[[ReceivedMessage], None],
        on_error: Callable[[tuple[str, int], Exception], None] | None = None,
        bind_ip: str = "0.0.0.0",
        port: int = 0,
    ):
        self._on_message = on_message
        self._on_error = on_error
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((bind_ip, port))
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="osc-receiver", daemon=True)

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        self._sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                dgram, src = self._sock.recvfrom(65535)
            except TimeoutError:
                continue
            except ConnectionResetError:
                # Windows reports ICMP port-unreachable from earlier sends on this socket; harmless.
                continue
            t = now_ns()
            try:
                for bundle_time, msg in parse_datagram(dgram):
                    self._on_message(ReceivedMessage(t, src, msg.address, tuple(msg.params), bundle_time))
            except Exception as e:  # malformed packets must not kill the receiver
                if self._on_error:
                    self._on_error(src, e)


class OscSender:
    def __init__(self, ip: str, port: int):
        self.target = (ip, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, messages: list[tuple[str, list[float]]], bundle: bool = True) -> None:
        built = []
        for address, args in messages:
            b = OscMessageBuilder(address=address)
            for a in args:
                b.add_arg(float(a))  # python-osc rejects numpy floats; VRChat expects 32-bit floats
            built.append(b.build())
        if bundle:
            bb = OscBundleBuilder(IMMEDIATELY)
            for m in built:
                bb.add_content(m)
            self._sock.sendto(bb.build().dgram, self.target)
        else:
            for m in built:
                self._sock.sendto(m.dgram, self.target)

    def close(self) -> None:
        self._sock.close()
