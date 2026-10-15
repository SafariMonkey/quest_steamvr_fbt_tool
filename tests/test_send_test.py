import numpy as np

from qsft.coords import unity_matrix_from_euler
from qsft.probe.commands import FOLLOW_DISTANCE_M, SPIN_PERIOD_S, synthetic_tracker_pose
from qsft.probe.session import HeadPose


def _head(pos, euler):
    return HeadPose(0, "1.2.3.4", np.array(pos, float), np.array(euler, float), unity_matrix_from_euler(euler))


def test_follow_places_tracker_in_front_at_head_height():
    head = _head([1.0, 1.7, 2.0], [-30, 90, 10])  # looking right (+X), pitched up, rolled
    pos, rot = synthetic_tracker_pose("follow", None, head, None, None, 0.0)
    assert np.allclose(pos, [1.0 + FOLLOW_DISTANCE_M, 1.7, 2.0])
    assert np.allclose(rot, unity_matrix_from_euler([0, 90, 0]))


def test_spin_is_about_the_tracker_local_axis():
    head = _head([0, 1.7, 0], [0, 90, 0])
    _, rot = synthetic_tracker_pose("follow", "y", head, None, None, SPIN_PERIOD_S / 4)  # peak of the sine
    assert np.allclose(rot, unity_matrix_from_euler([0, 135, 0]))


def test_head_dependent_modes_wait_for_a_head():
    assert synthetic_tracker_pose("follow", None, None, None, None, 0.0) is None
    assert synthetic_tracker_pose("anchor", None, None, None, None, 0.0) is None
    pos, rot = synthetic_tracker_pose("fixed", None, None, None, (0, 1, 0.5), 0.0)
    assert np.allclose(pos, [0, 1, 0.5]) and np.allclose(rot, np.eye(3))
