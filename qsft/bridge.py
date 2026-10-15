"""Forward Person B's Lighthouse trackers to the Quest's VRChat, calibrated continuously.

Run: uv run python -m qsft.bridge   (settings come from qsft.toml)

While the calibration tracker is on the headset, the Lighthouse -> Quest alignment is kept up to
date from VRChat's head pose. Pause it ('p') or switch the tracker off to keep the last alignment.
Console: p = pause/resume calibration, m = re-solve the mount offset, q = quit; any other text
marks the recording.
"""

import argparse
from pathlib import Path

import numpy as np
import openvr

from qsft.calibration import Calibrator, Pairer, Pose, _yaw
from qsft.coords import flip_handedness, matrix34_to_pos_rot, unity_euler_from_matrix, unity_matrix_from_euler
from qsft.openvr_io import RAW, OpenVRSession, pose_record
from qsft.osc_io import OscSender
from qsft.probe.commands import DeviceMap
from qsft.probe.session import ConsoleInput, Every, QuestLink, Ticker, load_config, log
from qsft.recording import NullRecorder, Recorder, default_path, now_ns

MAX_TRACKERS = 8
MAX_FLOOR_LIFT_M = 0.5  # larger floor corrections are almost certainly a missing/odd SteamVR room setup


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m qsft.bridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("qsft.toml"))
    parser.add_argument("--calibration-file", type=Path, default=Path("calibration.json"))
    parser.add_argument("--out", type=Path, help="recording path (default: recordings/<time>-bridge.jsonl.gz)")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--rate", type=float, default=90.0, help="poll/send rate in Hz (default 90)")
    parser.add_argument("--predict-ms", type=float, help="extrapolate forwarded trackers this far ahead (default 30)")
    parser.add_argument("--head-lead-ms", type=float, help="how far VRChat's head pose leads real time (default 40)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    trackers = list(cfg.get("trackers", []))[:MAX_TRACKERS]
    calib_serial = cfg.get("calibration_tracker")
    if not trackers or not calib_serial:
        raise SystemExit(f"{args.config} needs 'trackers' and 'calibration_tracker'")
    predict_s = (args.predict_ms if args.predict_ms is not None else cfg.get("predict_ms", 30.0)) / 1000
    head_lead_ms = args.head_lead_ms if args.head_lead_ms is not None else cfg.get("head_lead_ms", 40.0)

    rec = NullRecorder() if args.no_record else Recorder(
        args.out or default_path("bridge"), "bridge", {**{k: str(v) for k, v in vars(args).items()}, "config": cfg})
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

    # Height: VRChat draws incoming OSC trackers ~21 cm below the coordinates it reports poses in (measured
    # with send-test wrist mode: constant when crouching and standing, so an offset, not a scale). That
    # equals the height of the reported space's y = 0 above the real floor, and reported eye height + 21 cm
    # matches real eye height: VRChat draws with its floor at the real floor, but reports ~21 cm higher.
    # So trackers are lifted to put the real floor (SteamVR's, which matched the trackers' real heights)
    # at y = 0. SteamVR's floor is read once, in raw space, so later playspace moves by the PCVR player
    # can't shift it.
    r2s = session.raw_to_standing()
    floor_raw_y = float(cfg.get("steamvr_floor_raw_y", -r2s[1][3]))
    align_floor = bool(cfg.get("align_floor", True))
    if align_floor and np.allclose(r2s, np.eye(4)[:3], atol=1e-4) and "steamvr_floor_raw_y" not in cfg:
        log("SteamVR has no room setup (standing pose is identity); floor alignment disabled")
        align_floor = False
    height_offset = float(cfg.get("height_offset_m", 0.0))
    rec.write("floor", floor_raw_y=floor_raw_y, align=align_floor, height_offset=height_offset)
    if align_floor:
        log(f"Aligning SteamVR's floor (raw y {floor_raw_y:.3f}) with VRChat's floor")
    floor_warned = False

    link = QuestLink(rec, cfg, name="QSFT", on_head=on_head)
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
                elif line:
                    log(f"Marked: {line}")
                rec.write("mark", text=line)

            changed, quitting = session.poll_events()
            if quitting:
                log("SteamVR is quitting")
                return
            if changed or refresh.due():
                dmap.refresh()

            # Unpredicted poses: recorded, and used for calibration.
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

            lift = height_offset
            if align_floor and cal.X is not None:
                floor_lift = -(cal.X.pos[1] + floor_raw_y)  # X is yaw-only, so the floor's y is just shifted
                if abs(floor_lift) <= MAX_FLOOR_LIFT_M:
                    lift += floor_lift
                elif not floor_warned:
                    floor_warned = True
                    log(f"Floor alignment would move trackers {floor_lift:+.2f} m; ignoring it "
                        "(check SteamVR's room setup, or set align_floor = false)")
            if cal.X is not None and sender:
                predicted = session.poses(RAW, predict_s) if predict_s else poses
                messages, tx = [], {}
                for n, serial in enumerate(trackers, 1):
                    i = dmap.index.get(serial)
                    if i is None or not predicted[i].bPoseIsValid:
                        continue
                    q = cal.X @ Pose(*matrix34_to_pos_rot(predicted[i].mDeviceToAbsoluteTracking))
                    pos_u, rot_u = flip_handedness(q.pos, q.rot)
                    pos_u[1] += lift
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
                print(f"  calibration: {cal.state} | alignment {xs} | mount {ms} | height lift {lift:+.2f} m", flush=True)
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
                    if "wrist" not in line:
                        print(line, flush=True)
            ticker.wait()
    finally:
        if sender:
            sender.close()
        link.close()


if __name__ == "__main__":
    main()
