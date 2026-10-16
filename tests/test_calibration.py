import numpy as np
from scipy.spatial.transform import Rotation

from qsft.calibration import VRCHAT_FRACTION_SCALE, Pose, VRChatClock, solve_full


def test_solve_full_recovers_alignment_and_mount():
    rng = np.random.default_rng(0)
    X = Pose(np.array([1.5, 1.6, -2.0]), Rotation.from_euler("y", 140, degrees=True).as_matrix())
    M = Pose(np.array([0.03, 0.02, 0.085]), Rotation.from_euler("YXZ", [-33, 16, 114], degrees=True).as_matrix())
    T = [Pose(rng.normal([1, -1, -3], 0.4), Rotation.from_rotvec(rng.normal(0, 0.6, 3)).as_matrix()) for _ in range(150)]
    H = [X @ t @ M for t in T]
    for h in H:  # 2 mm / ~0.3 degree noise
        h.pos = h.pos + rng.normal(0, 0.002, 3)
        h.rot = h.rot @ Rotation.from_rotvec(rng.normal(0, 0.003, 3)).as_matrix()
    X2, M2, q = solve_full(H, T)
    assert np.linalg.norm(X2.pos - X.pos) < 0.01
    assert np.degrees(Rotation.from_matrix(X2.rot @ X.rot.T).magnitude()) < 0.5
    assert np.linalg.norm(M2.pos - M.pos) < 0.01
    assert q["pos_median_cm"] < 0.5


def test_vrchat_clock_decodes_mis_scaled_fractions():
    """VRChat's fraction field is scaled by 5e9 instead of 2^32, so it wraps early within each second."""
    true = 1792100520.0 + np.arange(0, 3.0, 0.019)
    tags = np.floor(true) + (((true % 1) * VRCHAT_FRACTION_SCALE) % 1)
    clock = VRChatClock()
    decoded = np.array([clock.decode(t) for t in tags])
    assert np.allclose(decoded, true, atol=1e-6)
    assert np.all(np.diff(decoded) > 0)


def _cal(tmp_path, M):
    from qsft.calibration import Calibrator
    cal = Calibrator(tmp_path / "cal.json")
    cal._add_solve(M, {"pos_median_cm": 0.3}, now=0.0)
    return cal


def _mount(arm_cm, yaw_deg=0.0):
    return Pose(np.array([0.0, 0.0, arm_cm / 100]), Rotation.from_euler("y", yaw_deg, degrees=True).as_matrix())


def test_old_solves_fade(tmp_path):
    cal = _cal(tmp_path, _mount(8.0))
    for k in range(4):
        cal._add_solve(_mount(8.0), {"pos_median_cm": 0.3}, now=10.0 * k)
    cal.consider_solve(_mount(9.0), {"pos_median_cm": 0.3}, now=3600.0)  # an hour later (e.g. next session)
    assert abs(np.linalg.norm(cal.M.pos) * 100 - 9.0) < 0.05


def test_small_differences_average_in(tmp_path):
    cal = _cal(tmp_path, _mount(8.0))
    cal.consider_solve(_mount(8.4), {"pos_median_cm": 0.3}, now=1.0)
    small = np.linalg.norm(cal.M.pos) * 100 - 8.0
    cal2 = _cal(tmp_path, _mount(8.0))
    cal2.consider_solve(_mount(9.2), {"pos_median_cm": 0.3}, now=1.0)
    larger = np.linalg.norm(cal2.M.pos) * 100 - 8.0
    assert 0.1 < small < 0.3  # noise-sized: mostly smoothed away
    assert larger / 1.2 > small / 0.4  # bigger (but not mount-change) differences pull proportionally harder


def test_moved_mount_needs_confirmation_then_replaces(tmp_path):
    cal = _cal(tmp_path, _mount(8.0))
    assert cal.consider_solve(_mount(8.0, yaw_deg=20), {"pos_median_cm": 0.3}, now=1.0) is None
    assert np.degrees(Rotation.from_matrix(cal.M.rot).magnitude()) < 0.1  # unchanged until confirmed
    note = cal.consider_solve(_mount(8.1, yaw_deg=20.5), {"pos_median_cm": 0.3}, now=2.0)
    assert note and "changed" in note
    assert abs(np.degrees(Rotation.from_matrix(cal.M.rot).magnitude()) - 20.25) < 0.2


def test_one_off_outlier_solve_is_ignored(tmp_path):
    cal = _cal(tmp_path, _mount(8.0))
    cal.consider_solve(_mount(8.0, yaw_deg=20), {"pos_median_cm": 0.3}, now=1.0)
    cal.consider_solve(_mount(8.1), {"pos_median_cm": 0.3}, now=2.0)
    assert np.degrees(Rotation.from_matrix(cal.M.rot).magnitude()) < 0.1
