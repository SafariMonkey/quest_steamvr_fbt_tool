"""Reading SteamVR devices and poses.

Poses are read in the raw (uncalibrated) universe by default. It is fixed to
the Lighthouse base stations, so the PCVR player's room setup and playspace
moves (which only change the standing/seated zero pose) don't affect it.
"""

from dataclasses import asdict, dataclass

import openvr
from openvr.error_code import TrackedPropertyError

from qsft.coords import matrix34_to_pos_rot, quat_wxyz

RAW = openvr.TrackingUniverseRawAndUncalibrated

_CLASS_NAMES = {
    openvr.TrackedDeviceClass_HMD: "hmd",
    openvr.TrackedDeviceClass_Controller: "controller",
    openvr.TrackedDeviceClass_GenericTracker: "tracker",
    openvr.TrackedDeviceClass_TrackingReference: "base_station",
    openvr.TrackedDeviceClass_DisplayRedirect: "display_redirect",
}

_DEVICE_EVENTS = {
    openvr.VREvent_TrackedDeviceActivated,
    openvr.VREvent_TrackedDeviceDeactivated,
    openvr.VREvent_TrackedDeviceRoleChanged,
}


class SteamVRNotRunning(Exception):
    pass


@dataclass
class DeviceInfo:
    index: int
    serial: str
    device_class: str
    tracking_system: str
    model: str
    manufacturer: str
    controller_type: str
    universe_id: int | None
    connected: bool

    def as_dict(self) -> dict:
        return asdict(self)


class OpenVRSession:
    def __init__(self):
        # Background apps never start SteamVR themselves and don't keep it running.
        try:
            openvr.init(openvr.VRApplication_Background)
        except openvr.error_code.InitError_Init_NoServerForBackgroundApp:
            raise SteamVRNotRunning() from None
        self.system = openvr.VRSystem()
        self._poses = (openvr.TrackedDevicePose_t * openvr.k_unMaxTrackedDeviceCount)()
        self._event = openvr.VREvent_t()

    def close(self) -> None:
        openvr.shutdown()

    def devices(self) -> list[DeviceInfo]:
        out = []
        for i in range(openvr.k_unMaxTrackedDeviceCount):
            cls = self.system.getTrackedDeviceClass(i)
            if cls == openvr.TrackedDeviceClass_Invalid:
                continue
            out.append(
                DeviceInfo(
                    index=i,
                    serial=self._string(i, openvr.Prop_SerialNumber_String),
                    device_class=_CLASS_NAMES.get(cls, str(cls)),
                    tracking_system=self._string(i, openvr.Prop_TrackingSystemName_String),
                    model=self._string(i, openvr.Prop_ModelNumber_String),
                    manufacturer=self._string(i, openvr.Prop_ManufacturerName_String),
                    controller_type=self._string(i, openvr.Prop_ControllerType_String),
                    universe_id=self._uint64(i, openvr.Prop_CurrentUniverseId_Uint64),
                    connected=bool(self.system.isTrackedDeviceConnected(i)),
                )
            )
        return out

    def poses(self, universe=RAW, predict_s: float = 0.0):
        """All device poses (indexed by device index), now or extrapolated ahead. The array is reused between calls."""
        self.system.getDeviceToAbsoluteTrackingPose(universe, predict_s, self._poses)
        return self._poses

    def poll_events(self) -> tuple[bool, bool]:
        """Drain the event queue. Returns (devices_changed, quit_requested)."""
        changed = quit_requested = False
        while self.system.pollNextEvent(self._event):
            if self._event.eventType in _DEVICE_EVENTS:
                changed = True
            elif self._event.eventType == openvr.VREvent_Quit:
                quit_requested = True
        if quit_requested:
            self.system.acknowledgeQuit_Exiting()
        return changed, quit_requested

    def _string(self, i: int, prop) -> str:
        try:
            return self.system.getStringTrackedDeviceProperty(i, prop)
        except TrackedPropertyError:
            return ""

    def _uint64(self, i: int, prop) -> int | None:
        try:
            return int(self.system.getUint64TrackedDeviceProperty(i, prop))
        except TrackedPropertyError:
            return None


def pose_record(pose) -> list:
    """Compact pose for recordings.

    Valid: [1, tracking_result, px, py, pz, qw, qx, qy, qz, vx, vy, vz, wx, wy, wz]
    (OpenVR axes; metres, m/s, rad/s). Invalid: [0, tracking_result, connected].
    """
    if not pose.bPoseIsValid:
        return [0, int(pose.eTrackingResult), int(bool(pose.bDeviceIsConnected))]
    pos, rot = matrix34_to_pos_rot(pose.mDeviceToAbsoluteTracking)
    return [
        1,
        int(pose.eTrackingResult),
        *(round(float(v), 5) for v in pos),
        *(round(float(v), 6) for v in quat_wxyz(rot)),
        *(round(float(pose.vVelocity[k]), 4) for k in range(3)),
        *(round(float(pose.vAngularVelocity[k]), 4) for k in range(3)),
    ]
