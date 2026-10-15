from qsft.adjust import FIST, FINGER_POINT, NEUTRAL, THUMBS_UP, Bob, GestureAdjuster


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _alternate(adj, clock, hand, n, dt=0.3):
    """n switches between Fist and ThumbsUp, continuing from the hand's current gesture."""
    events = []
    for _ in range(n):
        clock.t += dt
        events.append(adj.on_gesture(hand, FIST if adj._gesture[hand] == THUMBS_UP else THUMBS_UP))
    return [e for e in events if e]


def test_toggle_needs_ten_quick_alternations(tmp_path):
    clock = Clock()
    adj = GestureAdjuster(tmp_path / "o.json", now=clock)
    adj.on_gesture("right", FIST)
    assert _alternate(adj, clock, "right", 9) == []
    assert not adj.active
    assert _alternate(adj, clock, "right", 1) == ["enter"]
    assert adj.active


def test_slow_or_interrupted_sequences_dont_toggle(tmp_path):
    clock = Clock()
    adj = GestureAdjuster(tmp_path / "o.json", now=clock)
    adj.on_gesture("right", FIST)
    assert _alternate(adj, clock, "right", 12, dt=1.0) == []  # only ~8 switches fit in the 8 s window
    clock.t += 10
    _alternate(adj, clock, "right", 6)
    adj.on_gesture("right", FINGER_POINT)  # breaks the alternation
    adj.on_gesture("right", FIST)
    assert _alternate(adj, clock, "right", 6) == []
    assert not adj.active


def test_drag_moves_offset_with_the_hand_and_is_saved(tmp_path):
    clock = Clock()
    adj = GestureAdjuster(tmp_path / "o.json", now=clock)
    adj.start_from(0.20)
    adj.on_gesture("left", FIST)
    _alternate(adj, clock, "left", 10)
    assert adj.active
    adj.on_gesture("left", NEUTRAL)
    adj.on_gesture("right", FIST)
    adj.on_wrist("right", 1.00)
    adj.on_wrist("right", 1.03)
    assert abs(adj.offset - 0.23) < 1e-9
    adj.on_gesture("right", NEUTRAL)  # let go
    adj.on_wrist("right", 1.20)
    assert abs(adj.offset - 0.23) < 1e-9
    # exit: the small drags made while doing the exit sequence are undone
    adj.on_gesture("right", FIST)
    adj.on_wrist("right", 1.20)
    for k in range(10):
        clock.t += 0.3
        adj.on_gesture("right", THUMBS_UP if k % 2 == 0 else FIST)
        adj.on_wrist("right", 1.20 + 0.01 * (k % 2 == 0))
    assert not adj.active
    assert abs(adj.offset - 0.23) < 1e-9
    assert abs(GestureAdjuster(tmp_path / "o.json").offset - 0.23) < 1e-9


def test_idle_exit(tmp_path):
    clock = Clock()
    adj = GestureAdjuster(tmp_path / "o.json", now=clock)
    adj.on_gesture("right", FIST)
    _alternate(adj, clock, "right", 10)
    clock.t += 61
    assert adj.check_idle() == "exit"


def test_bob():
    clock = Clock()
    bob = Bob(now=clock)
    assert bob.lift() == 0.0
    bob.start("enter")
    clock.t = 0.1
    assert bob.lift() > 0
    clock.t = 0.2
    assert bob.lift() == 0.0
    clock.t = 0.35
    assert bob.lift() > 0
