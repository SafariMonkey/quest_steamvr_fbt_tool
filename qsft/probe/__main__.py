"""Command line: python -m qsft.probe <command> --help"""

import argparse
import sys
from pathlib import Path

from qsft.probe import commands
from qsft.probe.session import load_config, log
from qsft.recording import NullRecorder, Recorder, default_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m qsft.probe", description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("qsft.toml"), help="config file (default: qsft.toml)")
    parser.add_argument("--out", type=Path, help="recording path (default: recordings/<time>-<command>.jsonl.gz)")
    parser.add_argument("--no-record", action="store_true", help="don't write a recording")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_link_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--quest-ip", help="the Quest's IP address (default: discovered via mDNS)")
        p.add_argument("--lan-ip", help="this PC's LAN address to advertise (default: the route to the Quest)")
        p.add_argument("--osc-port", type=int, help="UDP port to receive VRChat's OSC on (default: any free port)")
        p.add_argument("--tree", choices=("typed", "bare"), default="typed",
                       help="OSCQuery tree style: typed endpoints like VRChat's sample, or a bare node like SlimeVR")

    sub.add_parser("devices", help="H1: list SteamVR devices, serials and tracking universes")
    sub.add_parser("identify", help="setup: shake a tracker (or turn it on/off) to see which serial it is")

    p = sub.add_parser("pc-watch", help="H1: show whether still trackers move in raw vs standing space")
    p.add_argument("--serial", action="append", help="tracker serial to watch (repeatable; default: config, else all trackers)")
    p.add_argument("--rate", type=float, default=20.0, help="poll rate in Hz (default 20)")

    p = sub.add_parser("listen", help="H2/H3: advertise over OSCQuery and log what VRChat sends")
    add_link_options(p)
    p.add_argument("--status-interval", type=float, default=2.0, help="seconds between status prints")

    p = sub.add_parser("send-test", help="H4: send a synthetic OSC tracker to the Quest")
    add_link_options(p)
    p.add_argument("--mode", choices=("follow", "anchor", "wrist", "fixed"), default="follow")
    p.add_argument("--rate", type=float, default=90.0, help="send rate in Hz (default 90)")
    p.add_argument("--no-bundle", action="store_true", help="send position and rotation as separate packets")
    p.add_argument("--fixed-pos", type=float, nargs=3, default=(0.0, 1.0, 0.5), metavar=("X", "Y", "Z"),
                   help="position for --mode fixed, Unity axes (default 0 1 0.5)")

    p = sub.add_parser("record", help="H5: record Lighthouse trackers (raw) and VRChat's head pose together")
    add_link_options(p)
    p.add_argument("--serial", action="append", help="tracker serial (repeatable; default: config trackers + calibration_tracker)")
    p.add_argument("--rate", type=float, default=120.0, help="Lighthouse poll rate in Hz (default 120)")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    for key in ("quest_ip", "lan_ip", "osc_port"):
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value

    command = args.command.replace("-", "_")
    rec_args = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
    rec = NullRecorder() if args.no_record else Recorder(args.out or default_path(args.command), args.command, {**rec_args, "config": cfg})
    if rec.path:
        log(f"Recording to {rec.path}")
    try:
        getattr(commands, f"cmd_{command}")(args, cfg, rec)
    except KeyboardInterrupt:
        pass
    finally:
        rec.close()
        if rec.path:
            log(f"Recording saved: {rec.path}")


if __name__ == "__main__":
    sys.exit(main())
