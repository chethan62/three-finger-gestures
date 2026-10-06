#!/usr/bin/env python3
"""Three-finger touchpad swipes -> KWin shortcuts (Windows 11 style).

Reads the touchpad directly from /dev/input (user must be in the `input`
group) and fires KWin global shortcuts by name over KGlobalAccel's DBus
interface.  No libinput CLI, no root; the only external tool is ydotool, used
for the Overview's arrow keys (no KWin action exists for those).

    swipe up    -> Overview                    (Task View)
    swipe down  -> Show Desktop
    swipe left  -> previous app | move the Overview selection, while it is open
    swipe right -> next app     | move the Overview selection, while it is open

While the Overview is open, ALL FOUR directions move its selection AS THE
FINGERS MOVE (one window per STEP_FRACTION of that axis' span), not once per
completed swipe. The grid is 2-dimensional, so vertical swipes are what reach
the other rows. No keyboard is involved -- the swipe sends the arrow keys.
Consequence: an upward swipe no longer closes the grid; use KWin's own
4-finger up, Escape, or click a window.

Thresholds are derived from the device's own ABS axis spans at startup, so
the defaults work on any touchpad.  Run with --debug to print every gesture.

Tuning
    SWIPE_FRACTION (default 0.15, near the top of the file) is the main knob:
    the fraction of the pad's axis span that counts as a swipe. Raise it if
    jitter fires gestures, lower it if deliberate swipes don't register.
    STEP_FRACTION (0.35) is how far the fingers travel per Overview step --
    lower it if the selection should move further per swipe.

        systemctl --user stop three-finger-gestures
        python3 ~/.local/share/three-finger-gestures/three-finger-gestures.py --debug
        ~/.local/share/three-finger-gestures/install.sh    # reapplies + verifies

    This daemon owns ONLY what KWin does not bind. KWin 6 hardcodes 4-finger
    up = Overview, 4-finger down = Desktop Grid, and 3/4-finger left/right =
    switch virtual desktop. 3-finger up/down is unbound -- that is the gap
    this fills. Never map a direction KWin already owns: both actors fire and
    the result is a stutter (visible as Overview opening then flickering).

Manage -- the installer owns install, verify and removal
    ~/.local/share/three-finger-gestures/install.sh              install + verify
    ~/.local/share/three-finger-gestures/install.sh --verify     verify only
    ~/.local/share/three-finger-gestures/install.sh --uninstall  remove the service
    ~/.local/share/three-finger-gestures/install.sh --purge      remove service + sources
    python3 ~/.local/share/three-finger-gestures/test-three-finger-gestures.py
"""
import fcntl
import glob
import os
import struct
import subprocess
import sys
import time

EV_SYN, EV_ABS = 0, 3
SYN_REPORT = 0
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39

EVENT = struct.Struct("llHHi")      # input_event, 24 bytes on 64-bit Linux
ABSINFO = struct.Struct("iiiiii")   # input_absinfo

# ponytail: the one tuning knob. Fraction of the pad's axis span that counts
# as a swipe. Raise if jitter fires gestures, lower if swipes feel stiff.
SWIPE_FRACTION = 0.15
DOMINANCE = 1.3     # winning axis must beat the other by this factor
COOLDOWN = 0.5      # seconds of dead time after a gesture fires
# Horizontal travel (fraction of the pad width) per Overview selection step
# while the fingers are still down -- this is what makes the highlight move
# WITH the swipe instead of one window per completed swipe.
STEP_FRACTION = 0.35

ACTIONS = {
    "up": ("kwin", "Overview"),
    "down": ("kwin", "Show Desktop"),
    "left": ("kwin", "Walk Through Windows"),
    "right": ("kwin", "Walk Through Windows (Reverse)"),
}

# While KWin's Overview is open, ALL FOUR directions move its SELECTION with
# arrow keys. The grid is 2-dimensional, so horizontal alone can only reach one
# row of windows -- vertical swipes navigate the rows.
#
# Measured on this box using the accent-highlight position as the metric:
#   * arrow keys shift the highlight and reverse each other cleanly
#     (RIGHT,RIGHT,LEFT landed back on exactly the RIGHT-only state);
#   * moving the POINTER changes nothing -- the highlight does not follow it;
#   * Cycle Overview is a 3-state cycle (grid -> desktop layout -> CLOSED),
#     so it shut the grid on the third press. Rejected.
# KWin exposes no "select next window" KGlobalAccel action, so the keys are
# injected via ydotool -- the swipe sends them, so no keyboard is involved.
# Consequence: while the grid is open, an upward swipe no longer closes it.
# Use KWin's own 4-finger up, Escape, or click a window.
OVERVIEW_KEYS = {"up": 103, "down": 108, "left": 105, "right": 106}
# KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT


def _eviocgabs(code):
    # _IOR('E', 0x40 + code, struct input_absinfo)
    return (2 << 30) | (ABSINFO.size << 16) | (ord("E") << 8) | (0x40 + code)


def spans(fd, default=(1000.0, 700.0)):
    """Axis spans (max-min) for X and Y, read from the device via ioctl."""
    try:
        out = []
        for code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
            _v, lo, hi, _fz, _fl, _res = ABSINFO.unpack(
                fcntl.ioctl(fd, _eviocgabs(code), b"\0" * ABSINFO.size))
            out.append(float(hi - lo))
        return out[0], out[1]
    except OSError:
        return default


def find_touchpad():
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            with open(f"/sys/class/input/{os.path.basename(path)}/device/name") as fh:
                if "touchpad" in fh.read().lower():
                    return path
        except OSError:
            continue
    return None


def classify(dx, dy, span_x, span_y, fraction=SWIPE_FRACTION):
    """Map a completed swipe's delta to a direction, or None.

    Deltas are normalised by the pad's spans first, so a wide pad's X axis
    isn't unfairly favoured and the threshold means the same on both axes.
    """
    nx, ny = dx / span_x, dy / span_y
    if max(abs(nx), abs(ny)) < fraction:
        return None
    if abs(nx) >= abs(ny) * DOMINANCE:
        return "right" if nx > 0 else "left"
    if abs(ny) >= abs(nx) * DOMINANCE:
        return "down" if ny > 0 else "up"  # touchpad Y grows downward
    return None


def delta(start, slots):
    """Mean per-slot delta for slots present in both dicts (robust to a
    finger lifting early, which would otherwise shift the centroid)."""
    dxs = [slots[k][0] - v[0] for k, v in start.items() if k in slots]
    dys = [slots[k][1] - v[1] for k, v in start.items() if k in slots]
    if not dxs:
        return 0.0, 0.0
    return sum(dxs) / len(dxs), sum(dys) / len(dys)


class SwipeDetector:
    """Multitouch-protocol-B state machine: raw (type, code, value) evdev
    tuples in, a completed gesture out.

    feed() returns None until a three-finger contact ENDS, then returns
    (direction, dx, dy) where direction is a key of ACTIONS or None for a
    three-finger tap.  While three fingers are down it also calls on_step()
    each time the fingers travel another step-width horizontally, so a caller
    can act on the movement before the gesture finishes.
    """

    def __init__(self, span_x, span_y, fraction=SWIPE_FRACTION, on_step=None,
                 step_fraction=STEP_FRACTION):
        self.span_x, self.span_y, self.fraction = span_x, span_y, fraction
        self.slots = {}   # slot -> [x, y] for each finger currently down
        self.start = None
        self.slot = 0
        self.armed = True   # only a full lift (0 contacts) re-arms a gesture
        self.on_step = on_step
        # per-axis, because pads are not square: the same fraction of each
        # axis' OWN span is what makes a step feel equal in both directions
        self.step_units = (max(1.0, span_x * step_fraction),
                           max(1.0, span_y * step_fraction))
        self.step_anchor = None     # (x, y) at the last step, or None

    def feed(self, typ, code, val):
        if typ == EV_ABS:
            if code == ABS_MT_SLOT:
                self.slot = val
            elif code == ABS_MT_TRACKING_ID:
                if val == -1:
                    self.slots.pop(self.slot, None)
                else:
                    self.slots[self.slot] = [0, 0]
            elif code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
                self.slots.setdefault(self.slot, [0, 0])
                self.slots[self.slot][0 if code == ABS_MT_POSITION_X else 1] = val
            return None
        if typ == EV_SYN and code == SYN_REPORT:
            return self._report()
        return None

    def _mean_xy(self):
        if not self.slots:
            return 0.0, 0.0
        n = len(self.slots)
        return (sum(v[0] for v in self.slots.values()) / n,
                sum(v[1] for v in self.slots.values()) / n)

    def _maybe_step(self):
        """Call on_step once per step-width of travel since the last call, per
        axis, so the Overview selection follows the fingers in both directions."""
        if self.on_step is None or self.step_anchor is None:
            return
        ax, ay = self.step_anchor
        mx, my = self._mean_xy()
        sx, sy = self.step_units
        while abs(mx - ax) >= sx:
            sign = 1.0 if mx > ax else -1.0
            ax += sign * sx
            self.on_step("right" if sign > 0 else "left")
        while abs(my - ay) >= sy:
            sign = 1.0 if my > ay else -1.0
            ay += sign * sy
            self.on_step("down" if sign > 0 else "up")  # touchpad Y grows downward
        self.step_anchor = (ax, ay)

    def _report(self):
        """A gesture completes when the contact count drops below three.

        Re-arming requires a full lift, so one physical touch yields at most
        one gesture -- otherwise a slow swipe reports 2-3 completions and
        fires that many times (the Overview open/close stutter).

        Four contacts invalidate the gesture outright: 4-finger swipes are
        KWin's (Overview / Desktop Grid), and such a gesture passes through --
        or drops to -- exactly 3 contacts when fingers land or lift unevenly.
        Adopting it there fires our action on KWin's gesture, which is the same
        two-actors-on-one-swipe bug we removed from the direction map.
        """
        n = len(self.slots)
        if n > 3:
            self.start = None
            self.step_anchor = None
            self.armed = False      # a full lift is required before arming again
            return None
        if n < 3 and self.start is not None:
            dx, dy = delta(self.start, self.slots)
            self.start = None
            self.step_anchor = None
            self.armed = n == 0     # all fingers up -> ready for the next touch
            direction = classify(dx, dy, self.span_x, self.span_y, self.fraction)
            return direction, dx, dy
        if n == 0:
            self.armed = True
        elif n == 3 and self.armed:
            self.start = {k: list(v) for k, v in self.slots.items()}
            self.step_anchor = self._mean_xy()
            self.armed = False      # must fully lift before arming again
        elif n == 3 and self.start is not None:
            self._maybe_step()
        return None


def overview_active():
    """True while KWin's Overview effect is showing.

    KWin exposes no DBus object for this, but its Effects service has an
    `activeEffects` property and Overview lists itself there -- one effect
    per line, e.g. "blur\\noverview".
    """
    try:
        r = subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Effects",
             "org.freedesktop.DBus.Properties.Get",
             "org.kde.kwin.Effects", "activeEffects"],
            capture_output=True, text=True, timeout=3)
        return "overview" in r.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return False


_overview_cache = [0.0, False]


def overview_active_cached(ttl=0.5):
    """overview_active() with a short TTL.

    The mid-drag stepper runs on every touch frame, and spawning a qdbus6 per
    frame would be absurd. A stale False only degrades the first swipe after
    the grid opens to the old one-step behaviour, because fire() re-reads the
    state fresh when the gesture completes.
    """
    now = time.monotonic()
    if now - _overview_cache[0] > ttl:
        _overview_cache[:] = [now, overview_active()]
    return _overview_cache[1]


def overview_key(direction, overview):
    """Arrow-key code to send for a direction while the Overview is open, or
    None to use that direction's normal KWin action."""
    if overview and direction in OVERVIEW_KEYS:
        return OVERVIEW_KEYS[direction]
    return None


def press_key(code):
    """Send one press/release via ydotool.

    The Overview has no KGlobalAccel action for moving its selection, and it
    is the only part of this daemon that cannot be driven by invokeShortcut.
    """
    try:
        r = subprocess.run(["ydotool", "key", f"{code}:1", f"{code}:0"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            print(f"[3finger] ydotool key {code} failed: {r.stderr.strip()}",
                  flush=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[3finger] ydotool unavailable: {e}", flush=True)


class Stepper:
    """Moves the Overview selection as the fingers move, and remembers that it
    did -- so the gesture's completion does not send a duplicate arrow."""

    def __init__(self):
        self.used = False

    def __call__(self, direction):
        if not overview_active_cached():
            return
        self.used = True
        press_key(OVERVIEW_KEYS[direction])

    def reset(self):
        self.used = False


def fire(direction):
    code = overview_key(direction, overview_active())
    if code is not None:
        press_key(code)
        return
    comp, name = ACTIONS[direction]
    # ponytail: blocking, ~10-50ms per call, and this bounds a hang
    try:
        r = subprocess.run(
            ["qdbus6", "org.kde.kglobalaccel", f"/component/{comp}",
             "org.kde.kglobalaccel.Component.invokeShortcut", name],
            capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            print(f"[3finger] {name} failed: {r.stderr.strip()}", flush=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[3finger] {name} error: {e}", flush=True)


def watch(fd, span_x, span_y, debug=False):
    stepper = Stepper()
    detector = SwipeDetector(span_x, span_y, on_step=stepper)
    last_fire = 0.0
    while True:
        chunk = os.read(fd, EVENT.size * 64)
        for off in range(0, len(chunk) - EVENT.size + 1, EVENT.size):
            _s, _u, typ, code, val = EVENT.unpack_from(chunk, off)
            result = detector.feed(typ, code, val)
            if result is None:
                continue
            direction, dx, dy = result
            stepped = stepper.used      # set by the mid-drag stepper
            stepper.reset()
            if direction and time.monotonic() - last_fire > COOLDOWN:
                last_fire = time.monotonic()
                if debug:
                    extra = " (already stepped)" if stepped else ""
                    print(f"[3finger] {direction}  dx={dx:.0f} dy={dy:.0f}{extra}",
                          flush=True)
                if not stepped:         # the steps already moved the selection
                    fire(direction)
            elif debug:
                print(f"[3finger] (no swipe)  dx={dx:.0f} dy={dy:.0f}", flush=True)


def verify_actions():
    """KGlobalAccel silently ignores unknown action names -- invoking a bogus
    one still exits 0 -- so an unmapped gesture would fail invisibly. Check
    every action the daemon can actually fire is genuinely registered.
    """
    wanted = {}
    for comp, name in ACTIONS.values():
        wanted.setdefault(comp, set()).add(name)
    for comp, names in wanted.items():
        try:
            r = subprocess.run(
                ["qdbus6", "org.kde.kglobalaccel", f"/component/{comp}",
                 "org.kde.kglobalaccel.Component.shortcutNames"],
                capture_output=True, text=True, timeout=5)
            # qdbus6 prints one action name per LINE (names contain spaces,
            # so splitting on anything but newlines corrupts them)
            registered = {s.strip() for s in r.stdout.splitlines() if s.strip()}
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[3finger] WARNING: cannot verify {comp}: {e}", flush=True)
            continue
        for name in sorted(names):
            if name not in registered:
                print(f"[3finger] WARNING: {comp}/{name!r} is not registered; "
                      f"that gesture will do nothing", flush=True)


def main():
    debug = "--debug" in sys.argv
    verify_actions()
    # Re-open loop: a touchpad can vanish (suspend/resume, i2c or USB glitch).
    # Without this the daemon dies on the first read error and gestures stop
    # silently -- systemd would restart it, but a long-lived reattach is
    # cheaper than a restart storm.
    while True:
        dev = find_touchpad()
        if not dev:
            print("[3finger] no touchpad found; retrying in 5s", flush=True)
            time.sleep(5)
            continue
        try:
            fd = os.open(dev, os.O_RDONLY)
        except OSError as e:
            print(f"[3finger] cannot open {dev}: {e}; retrying", flush=True)
            time.sleep(5)
            continue
        try:
            span_x, span_y = spans(fd)
            if debug:
                print(f"[3finger] watching {dev}  span={span_x:.0f}x{span_y:.0f}  "
                      f"threshold={SWIPE_FRACTION:.0%}", flush=True)
            watch(fd, span_x, span_y, debug)
        except KeyboardInterrupt:
            return
        except OSError as e:
            print(f"[3finger] {dev} lost: {e}; reattaching", flush=True)
        finally:
            os.close(fd)
        time.sleep(2)


if __name__ == "__main__":
    main()
