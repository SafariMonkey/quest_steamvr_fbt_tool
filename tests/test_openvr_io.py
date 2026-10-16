import numpy as np
import openvr
from scipy.spatial.transform import Rotation

from qsft.openvr_io import pose_record


def _pose(rot: np.ndarray, pos, valid=True) -> openvr.TrackedDevicePose_t:
    p = openvr.TrackedDevicePose_t()
    for r in range(3):
        for c in range(3):
            p.mDeviceToAbsoluteTracking.m[r][c] = rot[r, c]
        p.mDeviceToAbsoluteTracking.m[r][3] = pos[r]
    p.vVelocity.v[:] = [0.1, 0.2, 0.3]
    p.vAngularVelocity.v[:] = [1.0, 2.0, 3.0]
    p.eTrackingResult = openvr.TrackingResult_Running_OK
    p.bPoseIsValid = valid
    p.bDeviceIsConnected = True
    return p


def test_valid_pose_record():
    rot = Rotation.from_euler("y", 90, degrees=True)
    rec = pose_record(_pose(rot.as_matrix(), [1.0, 1.5, -2.0]))
    assert rec[:2] == [1, 200]
    assert np.allclose(rec[2:5], [1.0, 1.5, -2.0])
    x, y, z, w = rot.as_quat()
    assert np.allclose(np.abs(np.dot(rec[5:9], [w, x, y, z])), 1.0, atol=1e-5)
    assert np.allclose(rec[9:12], [0.1, 0.2, 0.3], atol=1e-4)
    assert np.allclose(rec[12:15], [1.0, 2.0, 3.0], atol=1e-4)


def test_invalid_pose_record_is_short():
    assert pose_record(_pose(np.eye(3), [0, 0, 0], valid=False)) == [0, 200, 1]
