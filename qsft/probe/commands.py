"""Probe commands. Each serves one or more hardware tests in INVESTIGATION.md section 4."""

import math
import time

import numpy as np
from scipy.spatial.transform import Rotation

from qsft.coords import matrix34_to_pos_rot, unity_euler_from_matrix, yaw_matrix
from qsft.openvr_io import RAW, STANDING, OpenVRSession, pose_record
from qsft.osc_io import OscSender
from qsft.probe.session import ConsoleInput, Every, QuestLink, Ticker, log
from qsft.recording import Recorder, now_ns


def configured_serials(serial_args: list[str] | None, cfg: dict) -> list[str]:
    serials = list(serial_args or [])
    if not serials:
        serials = list(cfg.get("trackers", []))
        if cfg.get("calibration_tracker"):
            serials.append(cfg["calibration_tracker"])
    return list(dict.fromkeys(serials))


class DeviceMap:
    """Serial -> device index for the devices we watch; refreshed when SteamVR's devices change."""

    def __init__(self, session: OpenVRSession, rec: Recorder, serials: list[str] | None):
        self._session = session
        self._rec = rec
        self._serials = serials  # None: every generic tracker
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
        if self._serials is None:
            self.index = {d.serial: d.index for d in devices if d.device_class == "tracker"}
        else:
            found = {d.serial: d.index for d in devices}
            self.index = {s: found[s] for s in self._serials if s in found}
            missing = [s for s in self._serials if s not in found]
            if missing:
                log("Not (yet) present in SteamVR: " + ", ".join(missing))
        log("Watching: " + (", ".join(f"{s} (#{i})" for s, i in self.index.items()) or "nothing"))


def _console_marks(console: ConsoleInput, rec: Recorder, handle=None) -> bool:
    """Process typed lines; returns False when the user asked to quit."""
    for line in console.lines():
        if line.lower() in ("q", "quit", "exit"):
            return False
        if handle and handle(line):
            continue
        if line:
            rec.write("mark", text=line)
            log(f"Marked: {line}")
    return True


# ---------------------------------------------------------------- devices (H1)


def cmd_devices(args, cfg: dict, rec: Recorder) -> None:
    session = OpenVRSession()
    try:
        devices = session.devices()
    finally:
        session.close()
    rec.write("devices", devices=[d.as_dict() for d in devices])
    wanted = set(configured_serials(None, cfg))
    print(f"  {'#':>2} {'class':<12} {'serial':<22} {'system':<12} {'model':<26} {'controller type':<26} universe")
    for d in devices:
        flag = "*" if d.serial in wanted else " "
        state = "" if d.connected else "  (disconnected)"
        print(
            f"{flag} {d.index:>2} {d.device_class:<12} {d.serial:<22} {d.tracking_system:<12} "
            f"{d.model:<26} {d.controller_type:<26} {d.universe_id}{state}"
        )
    if wanted:
        print("* = listed in your config")


# ---------------------------------------------------------------- pc-watch (H1)


def _pose_delta(a, b) -> tuple[float, float]:
    """(millimetres, degrees) between two (pos, rot) poses."""
    dpos = float(np.linalg.norm(a[0] - b[0])) * 1000
    cos = (np.trace(a[1].T @ b[1]) - 1) / 2
    return dpos, float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


def cmd_pc_watch(args, cfg: dict, rec: Recorder) -> None:
    session = OpenVRSession()
    dmap = DeviceMap(session, rec, configured_serials(args.serial, cfg) or None)
    console = ConsoleInput()
    ticker, status, refresh = Ticker(args.rate), Every(1.0), Every(5.0)
    baseline: dict[tuple[str, str], tuple] = {}
    latest: dict[tuple[str, str], tuple] = {}
    last_r2s = None
    log("Keep the watched trackers still. Raw poses should not change when the PCVR playspace moves; standing poses will.")
    log("Type 'reset' to re-take the baseline, a note to mark the recording, or 'q' to quit.")

    def handle(line: str) -> bool:
        if line.lower() == "reset":
            baseline.clear()
            rec.write("mark", text="reset baseline")
            log("Baseline reset")
            return True
        return False

    try:
        while _console_marks(console, rec, handle):
            changed, quitting = session.poll_events()
            if quitting:
                log("SteamVR is quitting")
                break
            if changed or refresh.due():
                dmap.refresh()
            t = now_ns()
            for universe, name in ((RAW, "raw"), (STANDING, "std")):
                poses = session.poses(universe)
                records = {}
                for serial, i in dmap.index.items():
                    records[serial] = pose_record(poses[i])
                    if poses[i].bPoseIsValid:
                        latest[(serial, name)] = matrix34_to_pos_rot(poses[i].mDeviceToAbsoluteTracking)
                        baseline.setdefault((serial, name), latest[(serial, name)])
                rec.write("lh", t=t, u=name, d=records)
            r2s = session.raw_to_standing()
            if last_r2s is None or not np.allclose(r2s, last_r2s, atol=1e-5):
                rec.write("r2s", m=np.round(r2s, 6).tolist())
                if last_r2s is not None:
                    log("Standing-universe offset changed (PCVR playspace moved)")
                last_r2s = r2s
            if status.due():
                for serial in dmap.index:
                    parts = []
                    for name in ("raw", "std"):
                        key = (serial, name)
                        if key in latest:
                            mm, deg = _pose_delta(latest[key], baseline[key])
                            parts.append(f"{name} moved {mm:8.1f} mm {deg:6.2f} deg")
                        else:
                            parts.append(f"{name} no valid pose")
                    print(f"  {serial:<22} " + " | ".join(parts), flush=True)
            ticker.wait()
    finally:
        session.close()


# ---------------------------------------------------------------- listen (H2, H3)


def cmd_listen(args, cfg: dict, rec: Recorder) -> None:
    link = QuestLink(rec, cfg, tree=args.tree)
    console = ConsoleInput()
    status = Every(args.status_interval)
    log("Waiting for VRChat. On the Quest: enable OSC, and 'Allow Sending Head and Wrist VR Tracking OSC Data'.")
    log("Type a note and press Enter to mark the recording (e.g. 'recentre'), or 'q' to quit.")
    try:
        while _console_marks(console, rec):
            time.sleep(0.05)
            if status.due():
                for line in link.status_lines():
                    print(line, flush=True)
    finally:
        link.close()


# ---------------------------------------------------------------- send-test (H4)

SPIN_AMPLITUDE_DEG = 45.0
SPIN_PERIOD_S = 4.0
FOLLOW_DISTANCE_M = 0.4
ANCHOR_DISTANCE_M = 0.5


def synthetic_tracker_pose(mode: str, spin: str | None, head, anchor, fixed_pos, t_s: float):
    """Pose (Unity position, rotation matrix) of the synthetic tracker, or None if it needs a head pose we lack."""
    if mode == "fixed":
        pos, base = np.asarray(fixed_pos, dtype=float), np.eye(3)
    elif mode == "anchor":
        if anchor is None:
            return None
        pos, base = anchor
    else:
        if head is None:
            return None
        base = yaw_matrix(head.rot)
        pos = head.pos + base @ np.array([0.0, 0.0, FOLLOW_DISTANCE_M])
    rot = base
    if spin:
        angle = SPIN_AMPLITUDE_DEG * math.sin(2 * math.pi * t_s / SPIN_PERIOD_S)
        rot = base @ Rotation.from_euler(spin, angle, degrees=True).as_matrix()
    return pos, rot


def cmd_send_test(args, cfg: dict, rec: Recorder) -> None:
    link = QuestLink(rec, cfg, tree=args.tree)
    console = ConsoleInput()
    state = {"mode": args.mode, "spin": None, "rate": args.rate, "bundle": not args.no_bundle, "anchor": None}
    ticker, status = Ticker(state["rate"]), Every(2.0)
    sender, target = None, None
    t0 = time.perf_counter()

    def take_anchor() -> None:
        head = link.head()
        if head is None:
            log("No head pose yet; can't anchor")
            return
        base = yaw_matrix(head.rot)
        state["anchor"] = (head.pos + base @ np.array([0.0, 0.0, ANCHOR_DISTANCE_M]), base)
        log(f"Anchored {ANCHOR_DISTANCE_M} m in front of the head at {np.round(state['anchor'][0], 3)}")

    def handle(line: str) -> bool:
        cmd = line.lower().split()
        if not cmd:
            return False
        if cmd[0] in ("f", "follow"):
            state["mode"] = "follow"
        elif cmd[0] in ("a", "anchor"):
            state["mode"] = "anchor"
            take_anchor()
        elif cmd[0] == "fixed":
            state["mode"] = "fixed"
        elif cmd[0] in ("x", "y", "z"):
            state["spin"] = cmd[0]
        elif cmd[0] in ("0", "nospin"):
            state["spin"] = None
        elif cmd[0] == "rate":
            try:
                rate = float(cmd[1])
                assert rate > 0
            except (IndexError, ValueError, AssertionError):
                log("Usage: rate <Hz>")
                return True
            state["rate"] = rate
            ticker.set_rate(rate)
        elif cmd[0] == "bundle":
            state["bundle"] = not state["bundle"]
        else:
            return False
        rec.write("mark", text=f"send-test: {line}")
        log(f"mode={state['mode']} spin={state['spin']} rate={state['rate']} Hz bundle={state['bundle']}")
        return True

    log("Commands: f(ollow) | a(nchor) | fixed | x / y / z (spin about that axis) | 0 (no spin) | rate N | bundle | q")
    if state["mode"] == "anchor":
        take_anchor()
    try:
        while _console_marks(console, rec, handle):
            new_target = link.quest_target()
            if new_target != target:
                if sender:
                    sender.close()
                target = new_target
                sender = OscSender(*target) if target else None
                rec.write("target", ip=target[0] if target else None, port=target[1] if target else None)
                log(f"Sending to {target[0]}:{target[1]}" if target else "No Quest target yet (set quest_ip, or wait for mDNS)")
            if state["mode"] == "anchor" and state["anchor"] is None:
                take_anchor()
            pose = synthetic_tracker_pose(
                state["mode"], state["spin"], link.head(), state["anchor"], args.fixed_pos, time.perf_counter() - t0
            )
            if pose is not None and sender:
                pos, rot = pose
                euler = unity_euler_from_matrix(rot)
                sender.send(
                    [("/tracking/trackers/1/position", pos.tolist()), ("/tracking/trackers/1/rotation", euler.tolist())],
                    bundle=state["bundle"],
                )
                rec.write("tx", p=np.round(pos, 5).tolist(), e=np.round(euler, 3).tolist(), bundle=state["bundle"])
            if status.due():
                for line in link.status_lines():
                    print(line, flush=True)
                waiting = "" if pose is not None else "  (waiting for a head pose)"
                print(f"  sending: mode={state['mode']} spin={state['spin']} rate={state['rate']} Hz "
                      f"bundle={state['bundle']} target={target}{waiting}", flush=True)
            ticker.wait()
    finally:
        if sender:
            sender.close()
        link.close()


# ---------------------------------------------------------------- record (H5)


def cmd_record(args, cfg: dict, rec: Recorder) -> None:
    serials = configured_serials(args.serial, cfg)
    if not serials:
        raise SystemExit("record needs trackers: pass --serial, or list them in the config file")
    session = OpenVRSession()
    try:
        dmap = DeviceMap(session, rec, serials)
        link = QuestLink(rec, cfg, tree=args.tree)
    except BaseException:
        session.close()
        raise
    console = ConsoleInput()
    ticker, status, refresh, r2s_check = Ticker(args.rate), Every(2.0), Every(5.0), Every(1.0)
    valid = {s: 0 for s in serials}
    polls = 0
    last_r2s = None
    log(f"Recording {', '.join(serials)} at {args.rate:g} Hz in the raw universe, plus everything VRChat sends.")
    log("Type a note and press Enter to mark the recording (e.g. 'recentre', 'tracker off'), or 'q' to quit.")
    try:
        while _console_marks(console, rec):
            changed, quitting = session.poll_events()
            if quitting:
                log("SteamVR is quitting")
                break
            if changed or refresh.due():
                dmap.refresh()
            t = now_ns()
            poses = session.poses(RAW)
            records = {s: pose_record(poses[i]) for s, i in dmap.index.items()}
            rec.write("lh", t=t, u="raw", d=records)
            polls += 1
            for s, r in records.items():
                valid[s] += r[0]
            if r2s_check.due():
                r2s = session.raw_to_standing()
                if last_r2s is None or not np.allclose(r2s, last_r2s, atol=1e-5):
                    rec.write("r2s", m=np.round(r2s, 6).tolist())
                    last_r2s = r2s
            if status.due():
                for s in serials:
                    pct = 100 * valid[s] / polls if polls else 0
                    where = f"#{dmap.index[s]}" if s in dmap.index else "absent"
                    print(f"  {s:<22} {where:>6}  valid {pct:5.1f}% of {polls} polls", flush=True)
                for line in link.status_lines():
                    print(line, flush=True)
                valid = {s: 0 for s in serials}
                polls = 0
            ticker.wait()
    finally:
        link.close()
        session.close()
