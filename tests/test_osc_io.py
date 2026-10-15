import json
import queue
import socket
import time

import numpy as np
from pythonosc.osc_bundle_builder import OscBundleBuilder
from pythonosc.osc_message_builder import OscMessageBuilder

from qsft.osc_io import OscReceiver, OscSender, parse_datagram
from qsft.oscquery import build_address_space


def _receiver():
    got: queue.SimpleQueue = queue.SimpleQueue()
    rx = OscReceiver(got.put, bind_ip="127.0.0.1")
    rx.start()
    return rx, got


def test_bundle_and_plain_messages_round_trip():
    rx, got = _receiver()
    tx = OscSender("127.0.0.1", rx.port)
    try:
        tx.send([("/tracking/trackers/1/position", [np.float64(0.5), 1.0, -2.0]), ("/tracking/trackers/1/rotation", [0, 90, 0])])
        tx.send([("/x", [1.25])], bundle=False)
        msgs = [got.get(timeout=2) for _ in range(3)]
    finally:
        tx.close()
        rx.stop()
    assert [m.address for m in msgs] == ["/tracking/trackers/1/position", "/tracking/trackers/1/rotation", "/x"]
    assert msgs[0].args == (0.5, 1.0, -2.0)
    assert msgs[0].bundle_time == 0  # IMMEDIATELY
    assert msgs[2].bundle_time is None
    assert msgs[0].src[0] == "127.0.0.1"


def test_future_time_tag_is_recorded_not_waited_for():
    """python-osc's dispatcher would sleep until this time tag; we must not."""
    future = time.time() + 30
    bb = OscBundleBuilder(future)
    m = OscMessageBuilder("/tracking/vrsystem/head/pose")
    for v in (0.0, 1.6, 0.0, 10.0, 20.0, 0.0):
        m.add_arg(v)
    bb.add_content(m.build())
    rx, got = _receiver()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(bb.build().dgram, ("127.0.0.1", rx.port))
        msg = got.get(timeout=2)
    finally:
        rx.stop()
    assert abs(msg.bundle_time - future) < 1e-3
    assert len(msg.args) == 6


def test_nested_bundles():
    inner = OscBundleBuilder(123.0)
    inner.add_content(OscMessageBuilder("/a").build())
    outer = OscBundleBuilder(0)
    outer.add_content(inner.build())
    outer.add_content(OscMessageBuilder("/b").build())
    parsed = [(t, m.address) for t, m in parse_datagram(outer.build().dgram)]
    assert sorted(a for _, a in parsed) == ["/a", "/b"]


def test_typed_tree_looks_like_vrchats_sample():
    tree = json.loads(build_address_space("typed").root_node.to_json())
    head = tree["CONTENTS"]["tracking"]["CONTENTS"]["vrsystem"]["CONTENTS"]["head"]["CONTENTS"]["pose"]
    assert head["FULL_PATH"] == "/tracking/vrsystem/head/pose"
    assert head["TYPE"] == "ffffff"
    assert head["ACCESS"] == 2
    assert "VALUE" not in head


def test_bare_tree_is_a_single_container():
    tree = json.loads(build_address_space("bare").root_node.to_json())
    vrsystem = tree["CONTENTS"]["tracking"]["CONTENTS"]["vrsystem"]
    assert vrsystem["FULL_PATH"] == "/tracking/vrsystem"
    assert "CONTENTS" not in vrsystem
