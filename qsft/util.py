"""Small helpers: config, console input, logging and loop timing."""

import queue
import sys
import threading
import time
import tomllib
from pathlib import Path


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    return data.get("bridge") or data.get("probe", {})  # [probe] was the section name on the probe branch


def log(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


class ConsoleInput:
    """Lines typed into the console, read on a background thread."""

    def __init__(self):
        self._lines: queue.SimpleQueue[str] = queue.SimpleQueue()
        threading.Thread(target=self._run, name="console", daemon=True).start()

    def lines(self) -> list[str]:
        out = []
        while not self._lines.empty():
            out.append(self._lines.get())
        return out

    def _run(self) -> None:
        for line in sys.stdin:
            self._lines.put(line.strip())


class Every:
    def __init__(self, seconds: float):
        self._interval = seconds
        self._next = time.monotonic() + seconds

    def due(self) -> bool:
        now = time.monotonic()
        if now < self._next:
            return False
        self._next = now + self._interval
        return True


class Ticker:
    """Fixed-rate loop timing that doesn't accumulate drift."""

    def __init__(self, rate_hz: float):
        self._period = 1.0 / rate_hz
        self._next = time.perf_counter()

    def wait(self) -> None:
        self._next += self._period
        delay = self._next - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        elif delay < -self._period:
            self._next = time.perf_counter()  # fell behind; don't try to catch up
