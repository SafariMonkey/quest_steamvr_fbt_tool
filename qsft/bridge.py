"""Forward Person B's Lighthouse trackers to the Quest's VRChat, calibrated continuously.

Run: uv run python -m qsft.bridge   (settings come from qsft.toml)

While the calibration tracker is on the headset, the Lighthouse -> Quest alignment is kept up to
date from VRChat's head pose. Pause it ('p') or switch the tracker off to keep the last alignment.
Console: p = pause/resume calibration, m = re-solve the mount offset, q = quit.
"""

import argparse
from pathlib import Path

import numpy as np
import openvr

from qsft.calibration import Calibrator, Pairer, Pose, _yaw
from qsft.coords import flip_handedness, matrix34_to_pos_rot, unity_euler_from_matrix, unity_matrix_from_euler
from qsft.openvr_io import RAW, OpenVRSession, pose_record
from qsft.osc_io import OscSender
from qsft.quest_link import QuestLink
from qsft.recording import NullRecorder, Recorder, default_path, now_ns
from qsft.util import ConsoleInput, Every, Ticker, load_config, log

MAX_TRACKERS = 8


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
    trackers = list(cfg.get("trackers", []))[:MAX_TRACKERS]
    calib_serial = cfg.get("calibration_tracker")
    if not trackers or not calib_serial:
        raise SystemExit(f"{args.config} needs 'trackers' and 'calibration_tracker' (python -m qsft.identify helps)")
    predict_s = (args.predict_ms if args.predict_ms is not None else cfg.get("predict_ms", 30.0)) / 1000
    head_lead_ms = args.head_lead_ms if args.head_lead_ms is not None else cfg.get("head_lead_ms", 40.0)

    rec = Recorder(default_path("bridge"), "bridge", {**{k: str(v) for k, v in vars(args).items()}, "config": cfg}) \
        if args.record else NullRecorder()
    if rec.path:
        log(f"Recording to {rec.path}")
    session = OpenVRSession()
    try:
        run(session, rec, cfg, trackers, calib_serial, predict_s, head_lead_ms, args)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
        rec.close()
        if rec.path:
            log(f"Recording saved: {rec.path}")


def run(session, rec, cfg, trackers, calib_serial, predict_s, head_lead_ms, args) -> None:
    dmap = DeviceMap(session, rec, [*trackers, calib_serial])
    pairer = Pairer(head_lead_ms)
    cal = Calibrator(args.calibration_file)
    if cal.M:
        log(f"Loaded the mount offset from {args.calibration_file}")
    if cal.X:
        log("Loaded the last alignment; it's refreshed once the calibration tracker is on the headset")

    def on_head(msg) -> None:
        pos, rot = flip_handedness(np.array(msg.args[:3], float), unity_matrix_from_euler(msg.args[3:6]))
        pairer.add_head(msg.t, msg.bundle_time, Pose(pos, rot))

    link = QuestLink(cfg, on_head, rec)
    console = ConsoleInput()
    ticker, status, refresh = Ticker(args.rate), Every(2.0), Every(5.0)
    sender, target = None, None
    sent = 0
    log(f"Forwarding {', '.join(f'{s} as tracker {n}' for n, s in enumerate(trackers, 1))}; "
        f"calibrating with {calib_serial}")
    log("Commands: p = pause/resume calibration, m = re-solve mount offset, q = quit")
    try:
        while True:
            for line in console.lines():
                cmd = line.lower()
                if cmd in ("q", "quit", "exit"):
                    return
                if cmd == "p":
                    cal.paused = not cal.paused
                    log("Calibration paused (alignment kept)" if cal.paused else "Calibration resumed")
                elif cmd == "m":
                    cal.reset_mount()
                    log("Re-solving the mount offset: move your head around (turn, nod, tilt)")
                elif line and rec.path:
                    log(f"Marked: {line}")
                rec.write("mark", text=line)

            changed, quitting = session.poll_events()
            if quitting:
                log("SteamVR is quitting")
                return
            if changed or refresh.due():
                dmap.refresh()

            # Unpredicted poses: used for calibration (and recorded, if recording).
            t = now_ns()
            poses = session.poses(RAW)
            rec.write("lh", t=t, u="raw", d={s: pose_record(poses[i]) for s, i in dmap.index.items()})
            ci = dmap.index.get(calib_serial)
            if ci is not None:
                p = poses[ci]
                ok = p.bPoseIsValid and p.eTrackingResult == openvr.TrackingResult_Running_OK
                pose = Pose(*matrix34_to_pos_rot(p.mDeviceToAbsoluteTracking)) if ok else None
                pairer.history.add(t, pose, float(np.linalg.norm(list(p.vAngularVelocity))) if ok else 0.0)
            for th, head, tr, w_head, w_tracker in pairer.ready():
                note = cal.add(th, head, tr, w_head, w_tracker)
                if note:
                    log(note)
                    rec.write("cal", text=note, X=cal.X.to_json() if cal.X else None,
                              M=cal.M.to_json() if cal.M else None)

            new_target = link.quest_target()
            if new_target != target:
                if sender:
                    sender.close()
                target = new_target
                sender = OscSender(*target) if target else None
                rec.write("target", ip=target[0] if target else None, port=target[1] if target else None)
                if target:
                    log(f"Sending trackers to {target[0]}:{target[1]}")

            if cal.X is not None and sender:
                predicted = session.poses(RAW, predict_s) if predict_s else poses
                messages, tx = [], {}
                for n, serial in enumerate(trackers, 1):
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
                    sender.send(messages)
                    rec.write("tx", d=tx)
                    sent += 1

            if status.due():
                x = cal.X
                xs = f"yaw {np.degrees(_yaw(x.rot)):.1f} deg, offset {np.round(x.pos, 2).tolist()} m" if x else "none yet"
                ms = f"lever arm {np.linalg.norm(cal.M.pos) * 100:.1f} cm" if cal.M else "not solved"
                print(f"  calibration: {cal.state} | alignment {xs} | mount {ms}", flush=True)
                states = []
                for serial in trackers:
                    i = dmap.index.get(serial)
                    states.append(f"{serial} {'ok' if i is not None and poses[i].bPoseIsValid else '--'}")
                print(f"  trackers: {', '.join(states)}", flush=True)
                if cal.X is None:
                    print("  not sending yet: waiting for the first calibration", flush=True)
                elif not sender:
                    print("  not sending yet: no Quest found (set quest_ip in qsft.toml, or wait for mDNS)", flush=True)
                else:
                    print(f"  sending to {target[0]}:{target[1]} at {sent / 2:.0f} Hz", flush=True)
                sent = 0
                for line in link.status_lines():
                    print(line, flush=True)
            ticker.wait()
    finally:
        if sender:
            sender.close()
        link.close()


if __name__ == "__main__":
    main()
