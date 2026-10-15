"""Replay a probe/bridge recording through the live calibration code.

Usage: uv run python tools/replay.py <recording.jsonl.gz> [calibration tracker serial]
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scipy.spatial.transform import Rotation  # noqa: E402

from qsft.calibration import Calibrator, Pairer, Pose, _yaw  # noqa: E402
from qsft.coords import flip_handedness, unity_matrix_from_euler  # noqa: E402
from qsft.oscquery import local_ipv4_addresses  # noqa: E402
from qsft.recording import read_recording  # noqa: E402

HEAD = "/tracking/vrsystem/head/pose"


def head_pose(args) -> Pose:
    pos, rot = flip_handedness(np.array(args[:3], float), unity_matrix_from_euler(args[3:6]))
    return Pose(pos, rot)


def main() -> None:
    path = Path(sys.argv[1])
    records = read_recording(path)
    hdr = next(records)
    serial = sys.argv[2] if len(sys.argv) > 2 else hdr["args"]["config"]["calibration_tracker"]
    local = {ip for _, ip in local_ipv4_addresses()} | {"127.0.0.1"}
    t0 = hdr["t"]
    cal = Calibrator(Path(tempfile.mkdtemp()) / "calibration.json")
    pairer = Pairer()
    next_print = 0.0
    for r in records:
        if r["k"] == "lh":
            v = r["d"].get(serial)
            if v is None:
                continue
            ok = v[0] == 1 and v[1] == 200
            pose = Pose(np.array(v[2:5]), Rotation.from_quat([v[6], v[7], v[8], v[5]]).as_matrix()) if ok else None
            pairer.history.add(r["t"], pose, float(np.linalg.norm(v[12:15])) if ok else 0.0)
        elif r["k"] == "osc" and r["a"] == HEAD and r["src"].split(":")[0] not in local:
            pairer.add_head(r["t"], r["bt"], head_pose(r["v"]))
        else:
            continue
        for t, head, tracker, w_head, w_tracker in pairer.ready():
            msg = cal.add(t, head, tracker, w_head, w_tracker)
            ts = (r["t"] - t0) / 1e9
            if msg:
                print(f"{ts:7.2f}s {msg}")
            if ts >= next_print:
                next_print = ts + 5
                x = cal.X
                xs = f"X yaw {np.degrees(_yaw(x.rot)):7.2f} pos {np.round(x.pos, 3)}" if x else "X none"
                print(f"{ts:7.2f}s [{cal.state}] {xs}")


if __name__ == "__main__":
    main()
