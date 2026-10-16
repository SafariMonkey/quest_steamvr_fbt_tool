"""Session recordings: gzip-compressed JSON lines, one event per line.

Every record has "k" (kind) and "t" (time.perf_counter_ns() when it was
created). The header record pairs that clock with wall-clock time. The file is
flushed regularly, so a recording cut short by a crash is still readable.
"""

import gzip
import json
import platform
import queue
import socket
import sys
import threading
import time
import zlib
from collections.abc import Iterator
from pathlib import Path

FORMAT_VERSION = 1

now_ns = time.perf_counter_ns


def default_path(command: str, directory: Path = Path("recordings")) -> Path:
    return directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{command}.jsonl.gz"


class Recorder:
    """Thread-safe writer; records are queued and written by a background thread."""

    _FLUSH_INTERVAL_S = 1.0

    def __init__(self, path: Path, command: str, args: dict):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = gzip.open(path, "wt", encoding="utf-8", compresslevel=6)
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()
        self.write(
            "hdr",
            version=FORMAT_VERSION,
            command=command,
            args=args,
            wall_ns=time.time_ns(),
            host=socket.gethostname(),
            python=sys.version.split()[0],
            platform=platform.platform(),
        )

    def write(self, kind: str, t: int | None = None, **fields) -> None:
        self._queue.put({"k": kind, "t": now_ns() if t is None else t, **fields})

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join()
        self._file.close()

    def _run(self) -> None:
        last_flush = time.monotonic()
        while True:
            try:
                record = self._queue.get(timeout=self._FLUSH_INTERVAL_S)
            except queue.Empty:
                record = ...
            if record is None:
                return
            if record is not ...:
                self._file.write(json.dumps(record, separators=(",", ":")))
                self._file.write("\n")
            if time.monotonic() - last_flush >= self._FLUSH_INTERVAL_S:
                self._file.flush()
                last_flush = time.monotonic()


class NullRecorder:
    path = None

    def write(self, kind: str, t: int | None = None, **fields) -> None:
        pass

    def close(self) -> None:
        pass


def read_recording(path: Path) -> Iterator[dict]:
    """Yield records, stopping quietly at a truncated end."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        try:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    return
        except (EOFError, zlib.error, gzip.BadGzipFile):
            return
