"""Forward Person B's Lighthouse trackers to the Quest's VRChat, calibrated continuously.

Run: uv run python -m qsft.bridge   (settings come from qsft.toml)

Start order doesn't matter: the bridge waits for SteamVR (and reconnects if it restarts), for VRChat
on the Quest, and for the calibration tracker. While the calibration tracker is on the headset, the
Lighthouse -> Quest alignment is kept up to date from VRChat's head pose. Pause it ('p') or switch the
tracker off to keep the last alignment.
Console: p = pause/resume calibration, m = re-solve the mount offset, q = quit.
"""

import argparse
import time
from pathlib import Path

import numpy as np
import openvr

from qsft.calibration import Calibrator, Pairer, Pose, _yaw
from qsft.coords import flip_handedness, matrix34_to_pos_rot, unity_euler_from_matrix, unity_matrix_from_euler
from qsft.openvr_io import RAW, OpenVRSession, SteamVRNotRunning, pose_record
from qsft.osc_io import OscSender
from qsft.quest_link import QuestLink
from qsft.recording import NullRecorder, Recorder, default_path, now_ns
from qsft.util import ConsoleInput, Every, Ticker, load_config, log

MAX_TRACKERS = 8
STEAMVR_RETRY_S = 2.0
SEND_ERROR_LOG_INTERVAL_S = 30.0


class DeviceMap:
    """Serial -> device index for the trackers we use; refreshed when SteamVR's devices change."""

    def __init__(self, session: OpenVRSession, rec, serials: list[str]):
        self._session = session
        self._rec = rec
        self._serials = serials
        self._last = None
        self.index: dict[str, int] = {}
        self.refresh()

    def refresh(self) -> None:
        devices = self._session.devices()
        snapshot = [d.as_dict() for d in devices]
        if snapshot == self._last:
            return
        self._last = snapshot
        self._rec.write("devices", devices=snapshot)
        found = {d.serial: d.index for d in devices}
        self.index = {s: found[s] for s in self._serials if s in found}
        missing = [s for s in self._serials if s not in found]
        if missing:
            log("Not (yet) present in SteamVR: " + ", ".join(missing))


class Bridge:
    """Everything that outlives a SteamVR session: the VRChat link, calibration, sender, console."""

    def __init__(self, args, cfg: dict, rec):
        self.args, self.rec = args, rec
        self.trackers = list(cfg.get("trackers", []))[:MAX_TRACKERS]
        self.calib_serial = cfg.get("calibration_tracker")
        self.predict_s = (args.predict_ms if args.predict_ms is not None else cfg.get("predict_ms", 30.0)) / 1000
        head_lead_ms = args.head_lead_ms if args.head_lead_ms is not None else cfg.get("head_lead_ms", 40.0)
        self.pairer = Pairer(head_lead_ms)
        self.cal = Calibrator(args.calibration_file)
        if self.cal.M:
            log(f"Loaded the mount offset from {args.calibration_file}")
        if self.cal.X:
            log("Loaded the last run's alignment; it's replaced once the calibration tracker is on the headset")
        self.link = QuestLink(cfg, self._on_head, rec)
        self.console = ConsoleInput()
        self.sender, self.target = None, None
        self._last_send_error_log = -1e9
        log(f"Forwarding {', '.join(f'{s} as tracker {n}' for n, s in enumerate(self.trackers, 1))}; "
            f"calibrating with {self.calib_serial}")
        log("Commands: p = pause/resume calibration, m = re-solve mount offset, q = quit")

    def close(self) -> None:
        if self.sender:
            self.sender.close()
        self.link.close()

    def _on_head(self, msg) -> None:
        pos, rot = flip_handedness(np.array(msg.args[:3], float), unity_matrix_from_euler(msg.args[3:6]))
        self.pairer.add_head(msg.t, msg.bundle_time, Pose(pos, rot))

    def handle_console(self) -> bool:
        """Process typed lines; returns False when the user asked to quit."""
        for line in self.console.lines():
            cmd = line.lower()
            if cmd in ("q", "quit", "exit"):
                return False
            if cmd == "p":
                self.cal.paused = not self.cal.paused
                log("Calibration paused (alignment kept)" if self.cal.paused else "Calibration resumed")
            elif cmd == "m":
                self.cal.reset_mount()
                log("Re-solving the mount offset: move your head around (turn, nod, tilt)")
            elif line and self.rec.path:
                log(f"Marked: {line}")
            self.rec.write("mark", text=line)
        return True

    def wait_for_steamvr(self) -> OpenVRSession | None:
        """Keep the VRChat side running while waiting for SteamVR. None if the user quits meanwhile."""
        announced = False
        while True:
            try:
                return OpenVRSession()
            except SteamVRNotRunning:
                if not announced:
                    log("Waiting for SteamVR to start ('q' to quit)")
                    announced = True
            deadline = time.monotonic() + STEAMVR_RETRY_S
            while time.monotonic() < deadline:
                if not self.handle_console():
                    return None
                time.sleep(0.1)

    def update_target(self) -> None:
        new_target = self.link.quest_target()
        if new_target == self.target:
            return
        if self.sender:
            self.sender.close()
        self.target = new_target
        self.sender = OscSender(*self.target) if self.target else None
        self.rec.write("target", ip=self.target[0] if self.target else None, port=self.target[1] if self.target else None)
        if self.target:
            log(f"Sending trackers to {self.target[0]}:{self.target[1]}")

    def run_session(self, session: OpenVRSession) -> str:
        """Run until SteamVR quits ('steamvr') or the user quits ('quit')."""
        dmap = DeviceMap(session, self.rec, [*self.trackers, self.calib_serial])
        ticker, status, refresh = Ticker(self.args.rate), Every(2.0), Every(5.0)
        sent = 0
        cal, pairer = self.cal, self.pairer
        while True:
            if not self.handle_console():
                return "quit"
            changed, quitting = session.poll_events()
            if quitting:
                return "steamvr"
            if changed or refresh.due():
                dmap.refresh()

            # Unpredicted poses: used for calibration (and recorded, if recording).
            t = now_ns()
            poses = session.poses(RAW)
            self.rec.write("lh", t=t, u="raw", d={s: pose_record(poses[i]) for s, i in dmap.index.items()})
            ci = dmap.index.get(self.calib_serial)
            calib_ok = False
            if ci is not None:
                p = poses[ci]
                calib_ok = bool(p.bPoseIsValid and p.eTrackingResult == openvr.TrackingResult_Running_OK)
                pose = Pose(*matrix34_to_pos_rot(p.mDeviceToAbsoluteTracking)) if calib_ok else None
                pairer.history.add(t, pose, float(np.linalg.norm(list(p.vAngularVelocity))) if calib_ok else 0.0)
            for th, head, tr, w_head, w_tracker in pairer.ready():
                note = cal.add(th, head, tr, w_head, w_tracker)
                if note:
                    log(note)
                    self.rec.write("cal", text=note, X=cal.X.to_json() if cal.X else None,
                                   M=cal.M.to_json() if cal.M else None)

            self.update_target()
            if cal.X is not None and self.sender:
                predicted = session.poses(RAW, self.predict_s) if self.predict_s else poses
                messages, tx = [], {}
                for n, serial in enumerate(self.trackers, 1):
                    i = dmap.index.get(serial)
                    if i is None or not predicted[i].bPoseIsValid:
                        continue
                    q = cal.X @ Pose(*matrix34_to_pos_rot(predicted[i].mDeviceToAbsoluteTracking))
                    pos_u, rot_u = flip_handedness(q.pos, q.rot)
                    euler = unity_euler_from_matrix(rot_u)
                    messages += [(f"/tracking/trackers/{n}/position", pos_u.tolist()),
                                 (f"/tracking/trackers/{n}/rotation", euler.tolist())]
                    tx[serial] = [*np.round(pos_u, 4).tolist(), *np.round(euler, 2).tolist()]
                if messages:
                    if self.sender.send(messages):
                        self.rec.write("tx", d=tx)
                        sent += 1
                    elif time.monotonic() - self._last_send_error_log > SEND_ERROR_LOG_INTERVAL_S:
                        self._last_send_error_log = time.monotonic()
                        log(f"Can't send to {self.target[0]}:{self.target[1]} ({self.sender.last_error!r}); "
                            "retrying (is VRChat running on the Quest?)")

            if status.due():
                self.print_status(dmap, poses, calib_ok, ci is not None, sent / 2)
                sent = 0
            ticker.wait()

    def print_status(self, dmap: DeviceMap, poses, calib_ok: bool, calib_present: bool, send_rate: float) -> None:
        cal = self.cal
        x = cal.X
        if x is None:
            xs = "none yet"
        else:
            xs = f"yaw {np.degrees(_yaw(x.rot)):.1f} deg, offset {np.round(x.pos, 2).tolist()} m"
            if cal.x_from_last_run:
                xs += " (from the last run; unconfirmed)"
        ms = f"lever arm {np.linalg.norm(cal.M.pos) * 100:.1f} cm" if cal.M else "not solved"
        print(f"  calibration: {cal.state} | alignment {xs} | mount {ms}", flush=True)
        if not calib_ok:
            why = "isn't tracking (off, or out of view)" if calib_present else "isn't in SteamVR yet"
            print(f"  calibration tracker {self.calib_serial} {why}", flush=True)
        states = []
        for serial in self.trackers:
            i = dmap.index.get(serial)
            states.append(f"{serial} {'ok' if i is not None and poses[i].bPoseIsValid else '--'}")
        print(f"  trackers: {', '.join(states)}", flush=True)
        if cal.X is None:
            print("  not sending yet: waiting for the first alignment (calibration tracker on the headset, "
                  "head pose from VRChat)", flush=True)
        elif not self.sender:
            print("  not sending yet: no Quest found (set quest_ip in qsft.toml, or wait for mDNS)", flush=True)
        else:
            print(f"  sending to {self.target[0]}:{self.target[1]} at {send_rate:.0f} Hz", flush=True)
        for line in self.link.status_lines():
            print(line, flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m qsft.bridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("qsft.toml"))
    parser.add_argument("--calibration-file", type=Path, default=Path("calibration.json"))
    parser.add_argument("--rate", type=float, default=90.0, help="poll/send rate in Hz (default 90)")
    parser.add_argument("--predict-ms", type=float, help="extrapolate forwarded trackers this far ahead (default 30)")
    parser.add_argument("--head-lead-ms", type=float, help="how far VRChat's head pose leads real time (default 40)")
    parser.add_argument("--record", action="store_true",
                        help="record the session to recordings/ (for diagnosing problems; any typed text marks it)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.get("trackers") or not cfg.get("calibration_tracker"):
        raise SystemExit(f"{args.config} needs 'trackers' and 'calibration_tracker' (python -m qsft.identify helps)")
    rec = Recorder(default_path("bridge"), "bridge", {**{k: str(v) for k, v in vars(args).items()}, "config": cfg}) \
        if args.record else NullRecorder()
    if rec.path:
        log(f"Recording to {rec.path}")
    bridge = Bridge(args, cfg, rec)
    try:
        while True:
            session = bridge.wait_for_steamvr()
            if session is None:
                break
            log("Connected to SteamVR")
            try:
                outcome = bridge.run_session(session)
            finally:
                session.close()
            if outcome == "quit":
                break
            log("SteamVR closed")
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        rec.close()
        if rec.path:
            log(f"Recording saved: {rec.path}")


if __name__ == "__main__":
    main()
