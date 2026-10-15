"""In-headset height adjustment of the forwarded trackers, driven by VRChat hand gestures.

VRChat draws incoming OSC trackers at a vertical offset from the coordinates it reports poses in
(~21 cm in testing), and doesn't tell us that offset. This lets Person B set it by eye, in VR:

- Enter/leave adjust mode: on one hand, alternate Fist and ThumbsUp 10 times within 8 s (hold grip
  and trigger, lift the thumb off the controller and put it back). Deliberate enough not to happen
  by accident.
- In adjust mode: make a fist and move that hand up/down to drag the whole tracker space with it.
  A constant offset moves trackers exactly as much as the hand, so this is direct manipulation.
- The result is saved and used from then on.

Gestures arrive as VRChat's built-in avatar parameters /avatar/parameters/GestureLeft|GestureRight.

Known limitation: Fist vs ThumbsUp depends on the controller's capacitive thumb sensing, which often
fails on worn controllers. Fine for native Quest Pro controllers for now; not a long-term trigger.
"""

import json
import time
from collections import deque
from pathlib import Path

# VRChat gesture parameter values
NEUTRAL, FIST, HAND_OPEN, FINGER_POINT, VICTORY, ROCK_N_ROLL, HAND_GUN, THUMBS_UP = range(8)


class GestureAdjuster:
    TOGGLE_GESTURES = (FIST, THUMBS_UP)
    TOGGLE_SWITCHES = 10
    TOGGLE_WINDOW_S = 8.0
    DRAG_GESTURE = FIST
    IDLE_EXIT_S = 60.0

    def __init__(self, path: Path, now=time.monotonic):
        self.path = path
        self._now = now
        self.offset: float | None = None  # metres added to forwarded tracker heights; None = not set
        self.active = False
        self._gesture = {"left": NEUTRAL, "right": NEUTRAL}
        self._switches = {"left": deque(), "right": deque()}
        self._offset_before_sequence: float | None = None
        self._drag: tuple[str, float, float] | None = None  # (hand, wrist y at start, offset at start)
        self._wrist_y: dict[str, float] = {}
        self._last_activity = 0.0
        if path.exists():
            self.offset = json.loads(path.read_text()).get("y")

    def save(self) -> None:
        self.path.write_text(json.dumps({"y": self.offset}))

    def on_gesture(self, hand: str, value: int) -> str | None:
        """Feed a gesture parameter change. Returns 'enter' or 'exit' when adjust mode toggles."""
        previous, self._gesture[hand] = self._gesture[hand], value
        if value == previous:
            return None
        switches = self._switches[hand]
        if value in self.TOGGLE_GESTURES and previous in self.TOGGLE_GESTURES:
            now = self._now()
            if not switches:
                self._offset_before_sequence = self.offset
            switches.append(now)
            while switches and switches[0] < now - self.TOGGLE_WINDOW_S:
                switches.popleft()
            if len(switches) >= self.TOGGLE_SWITCHES:
                self._switches = {"left": deque(), "right": deque()}
                return self._toggle()
        elif value not in self.TOGGLE_GESTURES:
            switches.clear()  # the sequence must be an unbroken alternation
        if hand == (self._drag[0] if self._drag else None) and value != self.DRAG_GESTURE:
            self._drag = None
        return None

    def on_wrist(self, hand: str, y: float) -> None:
        """Feed the reported wrist height; drives dragging while in adjust mode."""
        self._wrist_y[hand] = y
        if not self.active:
            return
        if self._drag is None and self._gesture[hand] == self.DRAG_GESTURE:
            self._drag = (hand, y, self.offset or 0.0)
        if self._drag and self._drag[0] == hand:
            self.offset = self._drag[2] + (y - self._drag[1])
            self._last_activity = self._now()

    def check_idle(self) -> str | None:
        if self.active and self._now() - self._last_activity > self.IDLE_EXIT_S:
            return self._toggle()
        return None

    def start_from(self, offset: float) -> None:
        """Initial offset to adjust from when nothing is saved yet."""
        if self.offset is None:
            self.offset = offset

    def nudge(self, delta: float) -> None:
        self.offset = (self.offset or 0.0) + delta
        self.save()

    def _toggle(self) -> str:
        self.active = not self.active
        self._drag = None
        if self.active:
            self._last_activity = self._now()
            return "enter"
        # Undo any dragging that happened while performing the exit sequence itself.
        if self._offset_before_sequence is not None:
            self.offset = self._offset_before_sequence
        self.save()
        return "exit"


class Countdown:
    """A delayed action, so it can be started at the PC and happen once you're in the play space.

    tick() returns "tick" at 3, 2 and 1 seconds to go, "fire" when due, else None.
    """

    def __init__(self, now=time.monotonic):
        self._now = now
        self._due = None
        self._ticks: list[float] = []

    @property
    def pending(self) -> bool:
        return self._due is not None

    def start(self, seconds: float) -> None:
        self._due = self._now() + seconds
        self._ticks = [self._due - s for s in (3, 2, 1) if s < seconds]

    def tick(self) -> str | None:
        if self._due is None:
            return None
        now = self._now()
        if now >= self._due:
            self._due = None
            return "fire"
        if self._ticks and now >= self._ticks[0]:
            self._ticks.pop(0)
            return "tick"
        return None


class Bob:
    """Short up/down bobs of the forwarded trackers, as visible feedback in VR."""

    HEIGHT_M = 0.05
    PATTERNS = {
        "enter": ((0.0, 0.15), (0.3, 0.45)),
        "exit": ((0.0, 0.4),),
        "tick": ((0.0, 0.1),),
        "ok": ((0.0, 0.15), (0.3, 0.45)),
        "fail": ((0.0, 0.8),),
    }

    def __init__(self, now=time.monotonic):
        self._now = now
        self._start = None
        self._pattern = ()

    def start(self, kind: str) -> None:
        self._start, self._pattern = self._now(), self.PATTERNS[kind]

    def lift(self) -> float:
        if self._start is None:
            return 0.0
        t = self._now() - self._start
        return self.HEIGHT_M if any(a <= t < b for a, b in self._pattern) else 0.0
