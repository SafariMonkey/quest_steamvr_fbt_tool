"""Lighthouse -> Quest calibration from a tracker mounted on the headset.

Model, with both spaces in right-handed OpenVR axes: H = X · T · M
  H: the Quest's head pose from VRChat (/tracking/vrsystem/head/pose, converted from Unity axes)
  T: the mounted tracker's pose, SteamVR raw universe
  X: Lighthouse raw -> Quest space (what we apply to the body trackers)
  M: tracker -> head; a property of the physical mount, solved once and saved

Recordings showed M is stable to a few mm / under 1 degree, while X jumps whenever the Quest
re-centres or relocalises. So M is solved once from varied head motion, and X is then
re-estimated continuously from H · M⁻¹ · T⁻¹ while the tracker is on the headset.
"""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

# VRChat's bundle time tags have correct seconds but scale the fraction by 5e9 instead of 2^32.
VRCHAT_FRACTION_SCALE = 5e9 / 2**32


class VRChatClock:
    """Decodes VRChat's time tags and maps them onto our clock (perf_counter ns).

    The mapping uses the lower envelope of (arrival - send): the fastest delivery counts as zero delay.
    """

    WINDOW = 3000  # samples (~1 min at 50 Hz)

    def __init__(self):
        self._last_s = None
        self._last_f = 0.0
        self._wrapped = False
        self._offsets: deque = deque(maxlen=self.WINDOW)

    def decode(self, time_tag: float) -> float:
        s = np.floor(time_tag)
        f = time_tag - s
        if s != self._last_s:
            self._wrapped = False
        elif f < self._last_f - 0.5:
            self._wrapped = True
        self._last_s, self._last_f = s, f
        return s + (f + (1.0 if self._wrapped else 0.0)) / VRCHAT_FRACTION_SCALE

    def local_time(self, time_tag: float | None, arrival_ns: int) -> int:
        if not time_tag:
            return arrival_ns
        send_ns = int(self.decode(time_tag) * 1e9)
        self._offsets.append(arrival_ns - send_ns)
        return send_ns + min(self._offsets)


@dataclass
class Pose:
    pos: np.ndarray
    rot: np.ndarray

    def inv(self) -> "Pose":
        return Pose(-self.rot.T @ self.pos, self.rot.T)

    def __matmul__(self, other: "Pose") -> "Pose":
        return Pose(self.rot @ other.pos + self.pos, self.rot @ other.rot)

    def to_json(self) -> dict:
        return {"pos": self.pos.tolist(), "rot": self.rot.tolist()}

    @classmethod
    def from_json(cls, d: dict) -> "Pose":
        return cls(np.array(d["pos"], float), np.array(d["rot"], float))


def _yaw(rot: np.ndarray) -> float:
    return float(np.arctan2(rot[0, 2], rot[0, 0]))


def _pose_distance(a: "Pose", b: "Pose") -> tuple[float, float]:
    """(cm, degrees) between two poses."""
    return float(np.linalg.norm(a.pos - b.pos) * 100), float(np.degrees(Rotation.from_matrix(a.rot @ b.rot.T).magnitude()))


def yaw_only(rot: np.ndarray) -> np.ndarray:
    """Nearest rotation about +Y (both spaces are gravity-aligned)."""
    yaw = np.arctan2(rot[0, 2] - rot[2, 0], rot[0, 0] + rot[2, 2])
    c, s = np.cos(yaw), np.sin(yaw)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def solve_full(H: list[Pose], T: list[Pose]) -> tuple[Pose, Pose, dict]:
    """Solve X and M from paired samples with varied orientation. Returns (X, M, quality)."""
    Hr = np.array([h.rot for h in H]); Hp = np.array([h.pos for h in H])
    Tr = np.array([t.rot for t in T]); Tp = np.array([t.pos for t in T])
    n = len(H)
    rng = np.random.default_rng(0)
    i, j = rng.integers(0, n, 20000), rng.integers(0, n, 20000)
    aH = Rotation.from_matrix(Hr[i] @ np.transpose(Hr[j], (0, 2, 1))).as_rotvec()
    aT = Rotation.from_matrix(Tr[i] @ np.transpose(Tr[j], (0, 2, 1))).as_rotvec()
    nH, nT = np.linalg.norm(aH, axis=1), np.linalg.norm(aT, axis=1)
    keep = (nH > 0.3) & (nT > 0.3) & (nH < 2.8)
    if keep.sum() < 50:
        raise ValueError("not enough rotation")
    # Delta-rotation axes: aH = RX aT, independent of the mount (as in OpenVR Space Calibrator).
    RX = yaw_only(Rotation.align_vectors(aH[keep], aT[keep])[0].as_matrix())
    # h = RX RT m + RX t + p, linear in (m, p).
    A = np.zeros((3 * n, 6)); b = np.zeros(3 * n)
    for k in range(n):
        A[3 * k:3 * k + 3, :3] = RX @ Tr[k]
        A[3 * k:3 * k + 3, 3:] = np.eye(3)
        b[3 * k:3 * k + 3] = Hp[k] - RX @ Tp[k]
    m, p = np.linalg.lstsq(A, b, rcond=None)[0].reshape(2, 3)
    RM = Rotation.from_matrix(np.transpose(Tr, (0, 2, 1)) @ RX.T @ Hr).mean().as_matrix()

    def unpack(x):
        c, s = np.cos(x[0]), np.sin(x[0])
        rx = np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])
        return rx, x[1:4], Rotation.from_rotvec(x[4:7]).as_matrix(), x[7:10]

    def residuals(x):
        rx, px, rm, mx = unpack(x)
        pred = np.einsum("ij,njk,k->ni", rx, Tr, mx) + Tp @ rx.T + px
        ang = Rotation.from_matrix(np.transpose(rx @ Tr @ rm, (0, 2, 1)) @ Hr).as_rotvec()
        return np.concatenate([(pred - Hp).ravel(), 0.1 * ang.ravel()])

    yaw0 = np.arctan2(RX[0, 2], RX[0, 0])
    x0 = np.concatenate([[yaw0], p, Rotation.from_matrix(RM).as_rotvec(), m])
    x = least_squares(residuals, x0, loss="huber", f_scale=0.02).x
    rx, px, rm, mx = unpack(x)
    r = residuals(x)
    perr = np.linalg.norm(r[: 3 * n].reshape(n, 3), axis=1)
    aerr = np.linalg.norm(r[3 * n:].reshape(n, 3), axis=1) / 0.1
    quality = {
        "samples": n,
        "pos_median_cm": float(np.median(perr) * 100),
        "pos_p90_cm": float(np.percentile(perr, 90) * 100),
        "angle_median_deg": float(np.degrees(np.median(aerr))),
        "lever_arm_cm": float(np.linalg.norm(mx) * 100),
    }
    return Pose(px, rx), Pose(mx, rm), quality


class Calibrator:
    """Pairs head samples with the mounted tracker, solves M once, then tracks X.

    Outliers are handled at three levels: implausible jumps between consecutive samples (the Quest's
    tracking origin moving) reset what was collected; per-window estimates drop samples far from the
    median and are skipped when the window disagrees with itself; the full solve uses a robust loss
    and is rejected (dropping the older half of its samples) if the fit is poor.
    """

    # Mount (M) solve
    MOUNT_MIN_SAMPLES = 100
    MOUNT_MAX_SAMPLES = 300
    MOUNT_SOLVE_EVERY = 25
    MOUNT_MAX_ANGULAR_SPEED = 3.0  # rad/s
    DIVERSITY_DEG = 2.0  # keep a sample only if its orientation differs this much from all kept ones
    MAX_GOOD_POS_CM = 2.0
    MAX_GOOD_ANGLE_DEG = 3.0
    # Refining M later: keep re-solving from fresh samples; M is a weighted mean of recent good solves.
    # Weights favour good fits (a mediocre first solve is soon outvoted) and recent solves (the tracker
    # may be rotated or re-tightened on its mount, between or during sessions).
    REFINE_EVERY = 50
    REFINE_MIN_SAMPLES = 150
    REFINE_MAX_POS_CM = 0.6
    REFINE_MAX_ANGLE_DEG = 1.0
    MAX_SOLVES = 20
    SOLVE_HALF_LIFE_S = 300.0
    # A good solve this far from M, confirmed by the next good solve, means the mount itself changed.
    MOUNT_CHANGE_CM, MOUNT_CHANGE_DEG = 1.5, 3.0
    # Below that, a solve pulls harder the further it is from M (one-euro style): noise-sized
    # differences are smoothed heavily, real small shifts are followed quickly.
    SOLVE_NOISE_CM, SOLVE_NOISE_DEG = 0.7, 1.0
    MOUNT_AGREE_CM, MOUNT_AGREE_DEG = 1.0, 1.5
    SAMPLE_MAX_AGE_S = 60.0  # mount samples older than this are dropped (slow drift would mix spaces)
    # Continuous X
    MAX_ANGULAR_SPEED = 1.5  # rad/s; faster samples are too timing-sensitive
    WINDOW = 50  # samples (~1 s)
    OUTLIER_CM, OUTLIER_DEG = 5.0, 3.0
    SNAP_CM, SNAP_DEG = 8.0, 4.0  # a change this big, confirmed by two windows, is an origin jump: snap
    CONFIRM_CM, CONFIRM_DEG = 3.0, 2.0
    SMOOTH_PER_SAMPLE = 0.05
    # Jumps: head and tracker are rigidly joined, so between consecutive samples they turn by the same
    # angle and (lever arm aside) move by the same distance.
    JUMP_MAX_DT_S = 0.1
    JUMP_DEG = 15.0  # real jumps were 30+ degrees; fast motion alone gives up to ~7
    JUMP_CM = 20.0
    # Attachment: a tracker on the headset turns exactly as fast as the head does.
    ATTACH_WINDOW = 100  # samples (~2 s)
    ATTACH_MOVING_RAD_S = 0.25
    ATTACH_ON_MISMATCH = 0.3  # hysteresis: attach below this, detach above the next
    ATTACH_OFF_MISMATCH = 0.5

    def __init__(self, path: Path):
        self.path = path
        self.M: Pose | None = None
        self.X: Pose | None = None
        self.quality: dict = {}
        self.paused = False
        self.attached = False
        self._speeds: deque = deque(maxlen=self.ATTACH_WINDOW)
        self._H: deque = deque(maxlen=self.MOUNT_MAX_SAMPLES)
        self._T: deque = deque(maxlen=self.MOUNT_MAX_SAMPLES)
        self._ts: deque = deque(maxlen=self.MOUNT_MAX_SAMPLES)
        self._new_since_solve = 0
        self._window: deque = deque(maxlen=self.WINDOW)
        self._prev: tuple[int, Pose, Pose] | None = None
        self._candidate: Pose | None = None
        self._solves: list[tuple[Pose, float, float]] = []  # (M, fit weight, wall time)
        self._pending_change: Pose | None = None
        self._wall_offset = time.time() - time.perf_counter_ns() / 1e9  # sample times are perf_counter ns
        self.load()

    def load(self) -> None:
        if self.path.exists():
            d = json.loads(self.path.read_text())
            self.M = Pose.from_json(d["M"]) if d.get("M") else None
            self.X = Pose.from_json(d["X"]) if d.get("X") else None
            self.quality = d.get("quality", {})
            self._solves = [(Pose.from_json(m), m["weight"], m.get("time", 0.0)) for m in d.get("mount_solves", [])]

    def save(self) -> None:
        self.path.write_text(json.dumps({
            "M": self.M.to_json() if self.M else None,
            "X": self.X.to_json() if self.X else None,
            "quality": self.quality,
            "mount_solves": [{**m.to_json(), "weight": w, "time": ts} for m, w, ts in self._solves],
        }, indent=1))

    def reset_mount(self) -> None:
        self.M = None
        self._solves.clear()
        self._pending_change = None
        self._clear_samples()

    def _add_solve(self, M: Pose, q: dict, now: float) -> None:
        """Fold a good solve into M: mean of recent solves weighted by fit, recency and (new solve) surprise."""
        weight = 1.0 / (q["pos_median_cm"] ** 2 + 0.05)
        if self.M is not None and self._solves:
            dpos, dang = _pose_distance(M, self.M)
            weight *= 1 + (dpos / self.SOLVE_NOISE_CM) ** 2 + (dang / self.SOLVE_NOISE_DEG) ** 2
        self._solves = [*self._solves, (M, weight, now)][-self.MAX_SOLVES:]
        w = np.array([fit * 0.5 ** ((now - ts) / self.SOLVE_HALF_LIFE_S) for _, fit, ts in self._solves])
        pos = np.sum([m.pos * wt for (m, _, _), wt in zip(self._solves, w)], axis=0) / w.sum()
        rot = Rotation.from_matrix(np.array([m.rot for m, _, _ in self._solves])).mean(weights=w).as_matrix()
        self.M = Pose(pos, rot)

    def consider_solve(self, M: Pose, q: dict, now: float) -> str | None:
        """Take a good refinement solve into account; detects a physically changed mount."""
        dpos, dang = _pose_distance(M, self.M)
        if dpos > self.MOUNT_CHANGE_CM or dang > self.MOUNT_CHANGE_DEG:
            pending, self._pending_change = self._pending_change, M
            if pending is None or any(d > a for d, a in zip(_pose_distance(M, pending), (self.MOUNT_AGREE_CM, self.MOUNT_AGREE_DEG))):
                return None  # wait for the next solve to confirm
            self._solves.clear()
            self._add_solve(pending, q, now)
            self._add_solve(M, q, now)
            self._pending_change = None
            return (f"mount offset changed by {dpos:.1f} cm / {dang:.1f} deg (tracker moved on its mount?); "
                    f"now lever arm {np.linalg.norm(self.M.pos) * 100:.1f} cm")
        self._pending_change = None
        before = self.M
        self._add_solve(M, q, now)
        dpos, dang = _pose_distance(self.M, before)
        if dpos < 0.3 and dang < 0.3:
            return None
        return (f"mount offset refined ({len(self._solves)} solves): moved {dpos:.1f} cm / {dang:.1f} deg, "
                f"lever arm {np.linalg.norm(self.M.pos) * 100:.1f} cm")

    def _clear_samples(self) -> None:
        self._H.clear(); self._T.clear(); self._ts.clear()
        self._new_since_solve = 0

    @property
    def state(self) -> str:
        if self.paused:
            return "paused"
        if not self.attached:
            return "waiting: move your head (calibration tracker must be on the headset)"
        if self.M is None:
            return (f"solving mount offset ({len(self._H)}/{self.MOUNT_MIN_SAMPLES} varied samples; "
                    "keep turning, nodding and tilting)")
        return "tracking"

    def add(self, t: int, head: Pose, tracker: Pose, w_head: float, w_tracker: float) -> str | None:
        """Feed one paired sample. Returns a message when something notable happens."""
        notes = [self._check_jump(t, head, tracker), self._update_attachment(w_head, w_tracker)]
        if not self.paused and self.attached:
            if self.M is None:
                if max(w_head, w_tracker) <= self.MOUNT_MAX_ANGULAR_SPEED:
                    notes.append(self._collect(t, head, tracker))
            else:
                if max(w_head, w_tracker) <= self.MOUNT_MAX_ANGULAR_SPEED:
                    notes.append(self._refine(t, head, tracker))
                if max(w_head, w_tracker) <= self.MAX_ANGULAR_SPEED:
                    notes.append(self._track(head, tracker))
        notes = [n for n in notes if n]
        return "; ".join(notes) if notes else None

    def _check_jump(self, t: int, head: Pose, tracker: Pose) -> str | None:
        prev, self._prev = self._prev, (t, head, tracker)
        if prev is None or (t - prev[0]) / 1e9 > self.JUMP_MAX_DT_S:
            return None
        _, ph, pt = prev
        dang_h = np.degrees(Rotation.from_matrix(ph.rot.T @ head.rot).magnitude())
        dang_t = np.degrees(Rotation.from_matrix(pt.rot.T @ tracker.rot).magnitude())
        dpos_h = np.linalg.norm(head.pos - ph.pos) * 100
        dpos_t = np.linalg.norm(tracker.pos - pt.pos) * 100
        if abs(dang_h - dang_t) < self.JUMP_DEG and abs(dpos_h - dpos_t) < self.JUMP_CM:
            return None
        # The Quest's tracking origin moved: samples from before are in the old space.
        self._clear_samples()
        self._window.clear()
        return f"Quest tracking jumped ({dpos_h - dpos_t:+.0f} cm, {dang_h - dang_t:+.0f} deg vs the tracker)"

    def _update_attachment(self, w_head: float, w_tracker: float) -> str | None:
        self._speeds.append((w_head, w_tracker))
        if len(self._speeds) < self.ATTACH_WINDOW // 4:
            return None
        w = np.array(self._speeds)
        typical = np.median(w.max(axis=1))
        if typical < self.ATTACH_MOVING_RAD_S:
            return None  # both still: no evidence either way
        mismatch = np.median(np.abs(w[:, 0] - w[:, 1])) / typical
        attached = mismatch < (self.ATTACH_OFF_MISMATCH if self.attached else self.ATTACH_ON_MISMATCH)
        if attached == self.attached:
            return None
        self.attached = attached
        self._window.clear()
        self._clear_samples()
        if attached:
            return "calibration tracker is moving with the headset"
        return "calibration tracker isn't moving with the headset; calibration frozen"

    def _track(self, head: Pose, tracker: Pose) -> str | None:
        x = head @ self.M.inv() @ tracker.inv()
        self._window.append(Pose(x.pos, yaw_only(x.rot)))
        if len(self._window) < self.WINDOW // 2:
            return None
        est = self._window_estimate()
        if est is None:
            return None
        if self.X is None:
            self.X = est
            self.save()
            return "calibrated"
        dpos, dang = _pose_distance(est, self.X)
        if dpos > self.SNAP_CM or dang > self.SNAP_DEG:
            # Only snap when two consecutive (non-overlapping) windows agree on the new place.
            candidate, self._candidate = self._candidate, est
            self._window.clear()
            if candidate is None or any(d > c for d, c in zip(_pose_distance(est, candidate), (self.CONFIRM_CM, self.CONFIRM_DEG))):
                return None
            self.X = est
            self._candidate = None
            self._clear_samples()  # samples from before are in the old space
            self.save()
            return f"Quest tracking space moved {dpos:.0f} cm / {dang:.0f} deg; re-aligned"
        self._candidate = None
        a = self.SMOOTH_PER_SAMPLE
        yaw_old, yaw_est = _yaw(self.X.rot), _yaw(est.rot)
        yaw = yaw_old + a * np.angle(np.exp(1j * (yaw_est - yaw_old)))
        self.X = Pose(self.X.pos * (1 - a) + est.pos * a, Rotation.from_rotvec([0, yaw, 0]).as_matrix())
        return None

    def _window_estimate(self) -> Pose | None:
        """Robust mean of the window: drop samples far from the median; None if the window disagrees with itself."""
        pos = np.array([p.pos for p in self._window])
        yaws = np.array([_yaw(p.rot) for p in self._window])
        med_pos = np.median(pos, axis=0)
        mean_yaw = np.angle(np.mean(np.exp(1j * yaws)))
        med_yaw = yaws[np.argmin(np.abs(np.angle(np.exp(1j * (yaws - mean_yaw)))))]  # nearest actual sample
        dyaw = np.abs(np.angle(np.exp(1j * (yaws - med_yaw))))
        keep = (np.linalg.norm(pos - med_pos, axis=1) < self.OUTLIER_CM / 100) & (dyaw < np.radians(self.OUTLIER_DEG))
        if keep.sum() < 0.6 * len(yaws):
            return None  # e.g. mid-jump: wait for a consistent window
        yaw = med_yaw + np.angle(np.mean(np.exp(1j * (yaws[keep] - med_yaw))))
        return Pose(pos[keep].mean(axis=0), Rotation.from_rotvec([0, yaw, 0]).as_matrix())

    def _add_diverse(self, t: int, head: Pose, tracker: Pose) -> bool:
        """Keep the sample if its orientation differs enough from every kept one."""
        while self._ts and self._ts[0] < t - self.SAMPLE_MAX_AGE_S * 1e9:
            self._H.popleft(); self._T.popleft(); self._ts.popleft()
        if self._T:
            # cos(angle between rotations) = (trace(A^T B) - 1) / 2, for all kept samples at once
            traces = np.einsum("nij,ij->n", np.array([t.rot for t in self._T]), tracker.rot)
            if np.max(traces) > 1 + 2 * np.cos(np.radians(self.DIVERSITY_DEG)):
                return False
        self._H.append(head); self._T.append(tracker); self._ts.append(t)
        self._new_since_solve += 1
        return True

    def _refine(self, t: int, head: Pose, tracker: Pose) -> str | None:
        """Re-solve M from recent varied samples; adopt it only if it clearly beats the current M on them."""
        if not self._add_diverse(t, head, tracker):
            return None
        if len(self._H) < self.REFINE_MIN_SAMPLES or self._new_since_solve < self.REFINE_EVERY:
            return None
        self._new_since_solve = 0
        H, T = list(self._H), list(self._T)
        self._clear_samples()  # next solve uses fresh samples
        try:
            _, M, q = solve_full(H, T)
        except ValueError:
            return None
        if q["pos_median_cm"] > self.REFINE_MAX_POS_CM or q["angle_median_deg"] > self.REFINE_MAX_ANGLE_DEG:
            return None
        note = self.consider_solve(M, q, self._wall_offset + t / 1e9)
        self.quality = q
        self.save()
        return note

    def _collect(self, t: int, head: Pose, tracker: Pose) -> str | None:
        if not self._add_diverse(t, head, tracker):
            return None
        if len(self._H) < self.MOUNT_MIN_SAMPLES or self._new_since_solve < self.MOUNT_SOLVE_EVERY:
            return None
        self._new_since_solve = 0
        try:
            X, M, q = solve_full(list(self._H), list(self._T))
        except ValueError:
            return None
        if q["pos_median_cm"] > self.MAX_GOOD_POS_CM or q["angle_median_deg"] > self.MAX_GOOD_ANGLE_DEG:
            for _ in range(len(self._H) // 2):  # probably mixed spaces; drop the older half
                self._H.popleft(); self._T.popleft(); self._ts.popleft()
            return f"mount solve rejected ({q['pos_median_cm']:.1f} cm, {q['angle_median_deg']:.1f} deg); collecting more"
        self._solves.clear()
        self._add_solve(M, q, self._wall_offset + t / 1e9)
        self.X, self.quality = X, q
        self._clear_samples()
        self.save()
        return (f"mount offset solved: lever arm {q['lever_arm_cm']:.1f} cm, fit {q['pos_median_cm']:.2f} cm / "
                f"{q['angle_median_deg']:.2f} deg over {q['samples']} samples")


class TrackerHistory:
    """Recent poses of the mounted tracker, for looking up its pose at a head sample's time."""

    MAX_GAP_NS = 30_000_000

    def __init__(self, seconds: float = 3.0):
        self._buf: deque = deque()
        self._span = int(seconds * 1e9)

    @property
    def latest_t(self) -> int | None:
        return self._buf[-1][0] if self._buf else None

    def add(self, t: int, pose: Pose | None, angular_speed: float) -> None:
        q = Rotation.from_matrix(pose.rot).as_quat() if pose else None
        self._buf.append((t, pose.pos if pose else None, q, angular_speed))
        while self._buf and self._buf[0][0] < t - self._span:
            self._buf.popleft()

    def at(self, t: int) -> tuple[Pose, float] | None:
        buf = self._buf
        if not buf or t < buf[0][0] or t > buf[-1][0]:
            return None
        lo, hi = 0, len(buf) - 1
        while hi - lo > 1:  # binary search for the samples either side of t
            mid = (lo + hi) // 2
            if buf[mid][0] <= t:
                lo = mid
            else:
                hi = mid
        (t0, p0, q0, w0), (t1, p1, q1, w1) = buf[lo], buf[hi]
        if p0 is None or p1 is None or t1 - t0 > self.MAX_GAP_NS:
            return None
        a = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        if np.dot(q0, q1) < 0:
            q1 = -q1
        q = (1 - a) * q0 + a * q1
        return Pose((1 - a) * p0 + a * p1, Rotation.from_quat(q / np.linalg.norm(q)).as_matrix()), (1 - a) * w0 + a * w1


class Pairer:
    """Matches each head sample with the mounted tracker's pose at the same moment.

    VRChat's head pose is a predicted (display-time) pose, so it matches the tracker's pose a little
    *after* its send time: HEAD_LEAD_MS, measured at about 40 ms from the first recording.
    """

    MAX_EXTRA_DELAY_MS = 15.0  # head samples delivered later than this beyond the fastest are skipped

    def __init__(self, head_lead_ms: float = 40.0):
        self.lead_ns = int(head_lead_ms * 1e6)
        self.clock = VRChatClock()
        self.history = TrackerHistory()
        self._pending: deque = deque()
        self._lock = threading.Lock()
        self._prev_head: tuple[int, Pose] | None = None

    def add_head(self, arrival_ns: int, bundle_time: float | None, head: Pose) -> None:
        """Called for every head sample, in arrival order (any thread)."""
        t = self.clock.local_time(bundle_time, arrival_ns)
        with self._lock:
            self._pending.append((t, arrival_ns - t, head))

    def ready(self):
        """Yield (time, head, tracker, head angular speed, tracker angular speed) once the tracker pose is known."""
        latest = self.history.latest_t
        while True:
            with self._lock:
                if not self._pending or latest is None or self._pending[0][0] + self.lead_ns > latest:
                    return
                t, extra_delay, head = self._pending.popleft()
            prev, self._prev_head = self._prev_head, (t, head)
            if extra_delay > self.MAX_EXTRA_DELAY_MS * 1e6 or prev is None or t <= prev[0]:
                continue
            w_head = Rotation.from_matrix(head.rot @ prev[1].rot.T).magnitude() / ((t - prev[0]) / 1e9)
            found = self.history.at(t + self.lead_ns)
            if found is not None:
                yield t, head, found[0], float(w_head), float(found[1])
