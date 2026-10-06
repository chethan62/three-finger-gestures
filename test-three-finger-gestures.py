#!/usr/bin/env python3
"""Self-check for three-finger-gestures.py: pure logic + the event parser."""
import importlib.util
import os
import pathlib

spec = importlib.util.spec_from_file_location(
    "tfg", pathlib.Path(__file__).with_name("three-finger-gestures.py"))
tfg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tfg)

SX, SY = 1224.0, 804.0   # this machine's touchpad spans

# --- classify: clear swipes on each axis, both directions ------------------
assert tfg.classify(0, -300, SX, SY) == "up"
assert tfg.classify(0, 300, SX, SY) == "down"
assert tfg.classify(-300, 0, SX, SY) == "left"
assert tfg.classify(300, 0, SX, SY) == "right"

# threshold is a fraction of each axis' own span
assert tfg.classify(int(SX * 0.20), 0, SX, SY) == "right"
assert tfg.classify(int(SX * 0.12), 0, SX, SY) is None
assert tfg.classify(0, int(SY * 0.12), SX, SY) is None

# non-square pad: equal raw movement in X and Y is NOT ambiguous -- Y wins,
# because 200/804 is a larger fraction of that axis than 200/1224
assert tfg.classify(200, 200, SX, SY) == "down"

# passes the threshold but neither axis dominates -> nothing
assert tfg.classify(245, 177, SX, SY) is None

# --- delta: only slots in both, so an early lift doesn't skew it -----------
start = {0: [1000, 500], 1: [1200, 500], 2: [1400, 500]}
after = {0: [1600, 100], 1: [1800, 100]}   # slot 2 lifted
assert tfg.delta(start, after) == (600.0, -400.0)
assert tfg.delta({0: [0, 0]}, {}) == (0.0, 0.0)

# --- parser: real evdev tuples through the protocol-B state machine --------
E = tfg.EV_ABS
SYN = (tfg.EV_SYN, tfg.SYN_REPORT, 0)


def run(events, span=(SX, SY)):
    det = tfg.SwipeDetector(*span)
    out = []
    for ev in events:
        r = det.feed(*ev)
        if r is not None:
            out.append((r[0], round(r[1]), round(r[2])))
    return out


def place(slots, xs, y):
    """MT protocol B: set slot, tracking id, x, y for each finger."""
    ev = []
    for slot, x in zip(slots, xs):
        ev += [(E, tfg.ABS_MT_SLOT, slot),
               (E, tfg.ABS_MT_TRACKING_ID, 100 + slot),
               (E, tfg.ABS_MT_POSITION_X, x),
               (E, tfg.ABS_MT_POSITION_Y, y)]
    return ev + [SYN]


def move(slots, xs, y):
    ev = []
    for slot, x in zip(slots, xs):
        ev += [(E, tfg.ABS_MT_SLOT, slot),
               (E, tfg.ABS_MT_POSITION_X, x),
               (E, tfg.ABS_MT_POSITION_Y, y)]
    return ev + [SYN]


def lift(*slots):
    """Lift fingers one at a time, as a real touchpad does: a frame per
    contact, so the gesture completes on the FIRST lift with the remaining
    contacts still holding their last positions."""
    ev = []
    for slot in slots:
        ev += [(E, tfg.ABS_MT_SLOT, slot), (E, tfg.ABS_MT_TRACKING_ID, -1), SYN]
    return ev


# three fingers down, dragged 300 down the pad, one lifts -> one 'down'
events = (place([0, 1, 2], [400, 600, 800], 400)
          + move([0, 1, 2], [400, 600, 800], 700)
          + lift(0))
assert run(events) == [("down", 0, 300)], run(events)

# same but dragged left -> 'left'
events = (place([0, 1, 2], [400, 600, 800], 400)
          + move([0, 1, 2], [100, 300, 500], 400)
          + lift(2))
assert run(events) == [("left", -300, 0)], run(events)

# three-finger tap (no movement) -> completed gesture, but no direction
events = place([0, 1, 2], [400, 600, 800], 400) + lift(0)
assert run(events) == [(None, 0, 0)], run(events)

# two fingers never completes a gesture
events = place([0, 1], [400, 600], 400) + lift(0, 1)
assert run(events) == [], run(events)

# --- four fingers must NEVER leak into the 3-finger actions ------------------
# 4-finger swipes belong to KWin (Overview / Desktop Grid). A 4-finger gesture
# whose fingers lift unevenly drops to 3 contacts, and one whose fingers land
# sequentially passes through 3 -- both must be rejected, not adopted.
events = (place([0, 1, 2, 3], [300, 500, 700, 900], 400)   # four land together
          + lift(3)                                          # ...one lifts first
          + move([0, 1, 2], [300, 500, 700], 700)            # drag
          + lift(0, 1, 2))
assert run(events) == [], f"4-finger leak (uneven lift): {run(events)}"

events = (place([0, 1, 2], [300, 500, 700], 400)            # three land
          + place([3], [900], 400)                          # then a fourth
          + move([0, 1, 2, 3], [300, 500, 700, 900], 700)
          + lift(0, 1, 2, 3))
assert run(events) == [], f"4-finger leak (sequential land): {run(events)}"

# --- one gesture per physical touch (regression: Overview stutter) ---------
# A finger re-touching mid-touch must NOT arm a second gesture: before the
# guard, one slow swipe reported 2-3 completions with real deltas -> 2-3
# toggles -> the Overview open/close flicker the user saw.
events = (place([0, 1, 2], [400, 600, 800], 400)
          + move([0, 1, 2], [400, 600, 800], 700)
          + lift(1)                                   # completes -> 'down'
          + place([1], [600], 700)                    # re-touch, others still down
          + move([0, 1, 2], [400, 600, 800], 1150)    # real movement again
          + lift(2))                                  # completes a SECOND time
got = run(events)
assert len(got) == 1, f"re-armed inside one touch: {got}"
assert got[0][0] == "down"

# ...but a genuine second touch (full lift between) DOES fire again
events = (place([0, 1, 2], [400, 600, 800], 400)
          + move([0, 1, 2], [400, 600, 800], 700)
          + lift(0, 1, 2)                             # full lift
          + place([0, 1, 2], [400, 600, 800], 400)
          + move([0, 1, 2], [400, 600, 800], 100)
          + lift(0, 1, 2))
got = run(events)
assert [g[0] for g in got] == ["down", "up"], f"second touch lost: {got}"

# --- Overview open: left/right send arrow keys so the selection moves -------
assert tfg.overview_key("right", overview=True) == 106   # KEY_RIGHT
assert tfg.overview_key("left", overview=True) == 105    # KEY_LEFT
# outside the grid, left/right fall through to the app switcher
assert tfg.overview_key("right", overview=False) is None
assert tfg.overview_key("left", overview=False) is None
# all four directions navigate the grid while it is open (it is 2-dimensional,
# so horizontal alone can only reach one row of windows)
assert tfg.overview_key("up", overview=True) == 103      # KEY_UP
assert tfg.overview_key("down", overview=True) == 108    # KEY_DOWN
# outside the grid they are ordinary KWin actions again
assert tfg.overview_key("up", overview=False) is None
assert tfg.overview_key("down", overview=False) is None

# --- mid-drag stepping: the Overview selection follows the fingers ---------
def feed_all(det, evs):
    out = []
    for e in evs:
        r = det.feed(*e)
        if r is not None:
            out.append(r)
    return out


xs = [300, 500, 700]
det = tfg.SwipeDetector(SX, SY)
d = int(det.step_units[0] * 1.2)    # a comfortable margin over one step

# two full steps of rightward travel -> two steps, in order
steps = []
det = tfg.SwipeDetector(SX, SY, on_step=steps.append)
feed_all(det, (place([0, 1, 2], xs, 400)
               + move([0, 1, 2], [x + d for x in xs], 400)
               + move([0, 1, 2], [x + 2 * d for x in xs], 400)
               + lift(0)))
assert steps == ["right", "right"], steps

# leftward travel steps the other way
steps = []
det = tfg.SwipeDetector(SX, SY, on_step=steps.append)
feed_all(det, place([0, 1, 2], xs, 400)
         + move([0, 1, 2], [x - d for x in xs], 400)
         + lift(0))
assert steps == ["left"], steps

# vertical travel steps the rows -- the grid is 2-dimensional, and the Y axis
# has its own span so it needs its own step size
dy = int(tfg.SwipeDetector(SX, SY).step_units[1] * 1.2)
for sign, want in ((-1, "up"), (+1, "down")):
    steps = []
    det = tfg.SwipeDetector(SX, SY, on_step=steps.append)
    feed_all(det, place([0, 1, 2], xs, 400)
             + move([0, 1, 2], xs, 400 + sign * dy)
             + lift(0))
    assert steps == [want], (want, steps)

# a short drag crosses no step, and the gesture still completes normally
steps = []
det = tfg.SwipeDetector(SX, SY, on_step=steps.append)
out = feed_all(det, place([0, 1, 2], xs, 400)
               + move([0, 1, 2], [x + int(det.step_units[0] * 0.5) for x in xs], 400)
               + lift(0))
assert steps == [] and out and out[0][0] == "right", (steps, out)

# --- live device: autodetect + real span read on this machine --------------
dev = tfg.find_touchpad()
assert dev and "event" in dev, dev
sx, sy = tfg.spans(os.open(dev, os.O_RDONLY))
assert sx > 100 and sy > 100, (sx, sy)

print(f"all checks passed ({dev}, span {sx:.0f}x{sy:.0f})")
