import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from qsft.coords import (
    flip_handedness,
    matrix34_to_pos_rot,
    quat_wxyz,
    unity_euler_from_matrix,
    unity_matrix_from_euler,
    yaw_matrix,
)


def ovr(axis: str, deg: float) -> np.ndarray:
    return Rotation.from_euler(axis, deg, degrees=True).as_matrix()


@pytest.mark.parametrize(
    ("ovr_rot", "unity_euler", "meaning"),
    [
        (ovr("y", 90), (0, -90, 0), "turn left: Unity yaw is positive to the right"),
        (ovr("x", 45), (-45, 0, 0), "nose up: Unity pitch is positive nose-down"),
        (ovr("z", 30), (0, 0, 30), "tilt head left"),
        (ovr("y", 180), (0, 180, 0), "face backwards"),
    ],
)
def test_known_orientations(ovr_rot, unity_euler, meaning):
    _, rot_u = flip_handedness(np.zeros(3), ovr_rot)
    got = unity_euler_from_matrix(rot_u)
    # Compare as rotations so 180 vs -180 doesn't matter.
    assert np.allclose(unity_matrix_from_euler(got), unity_matrix_from_euler(unity_euler), atol=1e-9), meaning


def test_directions_survive_conversion():
    """OpenVR forward (-Z) must become Unity forward (+Z); right and up are unchanged."""
    rng = np.random.default_rng(1)
    for r in Rotation.random(50, random_state=rng).as_matrix():
        _, ru = flip_handedness(np.zeros(3), r)
        fwd_ovr = r @ [0, 0, -1]
        fwd_unity = ru @ [0, 0, 1]
        assert np.allclose(fwd_unity, [fwd_ovr[0], fwd_ovr[1], -fwd_ovr[2]])
        assert np.isclose(np.linalg.det(ru), 1.0)


def test_unity_euler_matches_quaternion_euler_definition():
    """Quaternion.Euler(x, y, z) = qY * qX * qZ (applied Z, then X, then Y)."""
    rng = np.random.default_rng(2)
    for x, y, z in rng.uniform(-180, 180, size=(100, 3)):
        q = (
            Rotation.from_euler("y", y, degrees=True)
            * Rotation.from_euler("x", x, degrees=True)
            * Rotation.from_euler("z", z, degrees=True)
        )
        assert np.allclose(unity_matrix_from_euler((x, y, z)), q.as_matrix())
        assert np.allclose(unity_matrix_from_euler(unity_euler_from_matrix(q.as_matrix())), q.as_matrix())


def test_matches_slimevr_quaternion_flip():
    """SlimeVR converts Unity <-> its right-handed space with q -> (w, -x, -y, z) and p -> (x, y, -z)."""
    rng = np.random.default_rng(3)
    for r in Rotation.random(50, random_state=rng).as_matrix():
        _, ru = flip_handedness(np.zeros(3), r)
        w, x, y, z = quat_wxyz(r)
        expected = Rotation.from_quat([-x, -y, z, w]).as_matrix()
        assert np.allclose(ru, expected)
    assert np.allclose(flip_handedness(np.array([1.0, 2.0, 3.0]), np.eye(3))[0], [1, 2, -3])


def test_matrix34_split():
    m = [[1, 0, 0, 0.1], [0, 1, 0, 0.2], [0, 0, 1, 0.3]]
    pos, rot = matrix34_to_pos_rot(m)
    assert np.allclose(pos, [0.1, 0.2, 0.3])
    assert np.allclose(rot, np.eye(3))


def test_yaw_matrix_keeps_heading_only():
    r = unity_matrix_from_euler((30, 70, -20))
    assert np.allclose(yaw_matrix(r), unity_matrix_from_euler((0, 70, 0)))
