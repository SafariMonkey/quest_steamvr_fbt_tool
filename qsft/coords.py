"""Pose conversions between OpenVR and VRChat (Unity) conventions.

OpenVR: right-handed, +X right, +Y up, -Z forward, metres.
VRChat/Unity: left-handed, +X right, +Y up, +Z forward, metres. Euler angles are
in degrees and applied Z, then X, then Y about fixed axes (Unity's
Quaternion.Euler), i.e. R = Ry @ Rx @ Rz.

Switching handedness is the reflection S = diag(1, 1, -1), so p_unity = S p and
R_unity = S R S. S is its own inverse, so the same functions convert back.
"""

import numpy as np
from scipy.spatial.transform import Rotation

_S = np.diag([1.0, 1.0, -1.0])


def matrix34_to_pos_rot(m) -> tuple[np.ndarray, np.ndarray]:
    """Split an OpenVR HmdMatrix34_t (or any 3x4 nested sequence) into position and rotation."""
    a = np.array([[m[r][c] for c in range(4)] for r in range(3)], dtype=float)
    return a[:, 3].copy(), a[:, :3].copy()


def flip_handedness(pos: np.ndarray, rot: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Convert a pose between OpenVR and Unity axes (either direction)."""
    return _S @ pos, _S @ rot @ _S


def unity_euler_from_matrix(rot: np.ndarray) -> np.ndarray:
    """Unity Euler angles (x, y, z) in degrees for a rotation matrix in Unity axes."""
    y, x, z = Rotation.from_matrix(rot).as_euler("YXZ", degrees=True)
    return np.array([x, y, z])


def unity_matrix_from_euler(euler) -> np.ndarray:
    """Rotation matrix in Unity axes for Unity Euler angles (x, y, z) in degrees."""
    x, y, z = euler
    return Rotation.from_euler("YXZ", [y, x, z], degrees=True).as_matrix()


def quat_wxyz(rot: np.ndarray) -> np.ndarray:
    x, y, z, w = Rotation.from_matrix(rot).as_quat()
    return np.array([w, x, y, z])


def yaw_matrix(rot: np.ndarray) -> np.ndarray:
    """The rotation about +Y only (heading) of a rotation matrix, in either axis convention."""
    forward = rot[:, 2]
    yaw = np.arctan2(forward[0], forward[2])
    c, s = np.cos(yaw), np.sin(yaw)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])
