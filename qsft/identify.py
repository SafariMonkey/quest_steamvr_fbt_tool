"""Find out which tracker is which: python -m qsft.identify

Shake or twist a tracker to see its serial (and its role in qsft.toml), or turn trackers on and off
one at a time. SteamVR must be running. 'q' + Enter to quit.
"""

import argparse
from pathlib import Path

import numpy as np

from qsft.openvr_io import RAW, OpenVRSession, SteamVRNotRunning
from qsft.util import ConsoleInput, Every, Ticker, load_config, log

ANGULAR_RAD_S = 3.0  # a deliberate shake or twist; resting or worn trackers stay well below
WINDOW_S = 0.5


def config_role(serial: str, cfg: dict) -> str:
    if serial == cfg.get("calibration_tracker"):
        return "calibration tracker"
    if serial in cfg.get("trackers", []):
        return "body tracker"
    return "not in config"


def moving_serials(peak_angular: dict[str, float]) -> list[str]:
    """Serials being shaken, fastest first, from each tracker's peak angular speed (rad/s) over the window."""
    moving = [s for s, w in peak_angular.items() if w >= ANGULAR_RAD_S]
    return sorted(moving, key=lambda s: -peak_angular[s])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m qsft.identify", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("qsft.toml"))
    cfg = load_config(parser.parse_args(argv).config)
    try:
        session = OpenVRSession()
    except SteamVRNotRunning:
        raise SystemExit("SteamVR isn't running. Start SteamVR first.") from None
    console = ConsoleInput()
    ticker, window, refresh = Ticker(60.0), Every(WINDOW_S), Every(2.0)
    trackers: dict[str, int] = {}
    connected: set[str] = set()
    peak: dict[str, float] = {}
    last_moving: list[str] = []

    def update_devices(initial: bool = False) -> None:
        nonlocal trackers, connected
        devices = [d for d in session.devices() if d.device_class == "tracker"]
        trackers = {d.serial: d.index for d in devices}
        now_on = {d.serial for d in devices if d.connected}
        if initial:
            for s in sorted(now_on):
                log(f"On: {s}  ({config_role(s, cfg)})")
        else:
            for s in sorted(now_on - connected):
                log(f"Turned on: {s}  ({config_role(s, cfg)})")
            for s in sorted(connected - now_on):
                log(f"Turned off: {s}  ({config_role(s, cfg)})")
        connected = now_on

    update_devices(initial=True)
    log("Shake or twist a tracker to see its serial, or turn trackers on and off one at a time. 'q' to quit.")
    try:
        while not any(line.lower() in ("q", "quit", "exit") for line in console.lines()):
            changed, quitting = session.poll_events()
            if quitting:
                log("SteamVR is quitting")
                break
            if changed or refresh.due():
                update_devices()
            poses = session.poses(RAW)
            for serial, i in trackers.items():
                w = float(np.linalg.norm(list(poses[i].vAngularVelocity))) if poses[i].bPoseIsValid else 0.0
                peak[serial] = max(peak.get(serial, 0.0), w)
            if window.due():
                moving = moving_serials(peak)
                if moving and moving != last_moving:
                    for s in moving:
                        log(f"Moving: {s}  ({config_role(s, cfg)})")
                last_moving = moving
                peak.clear()
            ticker.wait()
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
