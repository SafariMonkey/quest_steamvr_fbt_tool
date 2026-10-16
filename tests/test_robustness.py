"""Bring-up robustness: things missing at startup, or restarting, must not wedge the bridge."""

import numpy as np
from scipy.spatial.transform import Rotation

from qsft.calibration import Calibrator, Pairer, Pose, VRChatClock
from qsft.osc_io import OscSender


def test_unpairable_head_samples_dont_pile_up():
    """Calibration tracker off: head samples arrive but can never be paired; keep only a few seconds."""
    pairer = Pairer()
    for k in range(50 * 60):  # a minute of head poses at 50 Hz, no tracker history at all
        pairer.add_head(k * 20_000_000, None, Pose(np.zeros(3), np.eye(3)))
    assert len(pairer._pending) <= 50 * Pairer.MAX_PENDING_S + 1
    assert list(pairer.ready()) == []


def test_clock_offsets_reset_after_a_gap():
    """VRChat restarted: the old arrival-vs-send offsets mustn't keep being used."""
    clock = VRChatClock()
    for k in range(100):  # old instance: delivered 1.0 s after its time tags (by our clock)
        clock.local_time(1000.0 + k * 0.02, int((1001.0 + k * 0.02) * 1e9))
    t = clock.local_time(2000.0, int(2005.0e9))  # new instance after a 1000 s gap, offset now 5 s
    assert t == int(2005.0e9)  # mapped with its own (fastest) offset, not the stale 1 s one


def test_send_failure_is_reported_not_raised(monkeypatch):
    sender = OscSender("127.0.0.1", 9)

    def refuse(*args):
        raise ConnectionResetError(10054, "port unreachable")

    monkeypatch.setattr(sender, "_sock", type("S", (), {"sendto": staticmethod(refuse), "close": lambda self: None})())
    assert sender.send([("/x", [1.0])]) is False
    assert isinstance(sender.last_error, ConnectionResetError)


def test_last_runs_alignment_is_replaced_by_this_sessions(tmp_path):
    """A saved alignment is usually stale (the Quest re-localises); the first fresh estimate replaces it."""
    path = tmp_path / "cal.json"
    stale = Pose(np.array([5.0, 1.6, -3.0]), Rotation.from_euler("y", 10, degrees=True).as_matrix())
    mount = Pose(np.array([0.0, 0.0, 0.085]), np.eye(3))
    seed = Calibrator(path)
    seed.M, seed.X = mount, stale
    seed.save()
    cal = Calibrator(path)
    assert cal.x_from_last_run
    true_x = Pose(np.array([1.0, 1.65, 2.0]), Rotation.from_euler("y", 140, degrees=True).as_matrix())
    rng = np.random.default_rng(0)
    for _ in range(Calibrator.WINDOW):
        tracker = Pose(rng.normal([1, -1, -3], 0.2), Rotation.from_rotvec(rng.normal(0, 0.3, 3)).as_matrix())
        cal._track(true_x @ tracker @ mount, tracker)
    assert not cal.x_from_last_run
    assert np.linalg.norm(cal.X.pos - true_x.pos) < 1e-6
