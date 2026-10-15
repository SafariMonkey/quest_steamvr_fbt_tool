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
