"""
toby_anim.py — the motion vocabulary every Little Toby animation shares.

One small module so that the pill, the chibi, the Dynamic Island and the
lid fold all move the same way. Nothing here imports GTK; it is pure maths
plus the user's animation settings, which keeps it cheap to import from the
separate fold daemon and easy to test.

The curves are chosen to feel physical rather than decorative:

- ``ease_out_expo`` for things arriving: fast start, long soft settle. It is
  the curve iOS uses for most sheet and panel motion.
- ``ease_in_out_cubic`` for things moving between two resting places.
- ``Spring`` for anything that should respond to being touched. A critically
  damped spring never overshoots; a slightly underdamped one gives the tiny
  "give" of a physical object without the cartoon bounce the design brief
  rules out.
"""

import math
import time

# ---------------------------------------------------------------------------
# Easing curves. All take t in 0..1 and return 0..1 (spring-ish ones may
# overshoot very slightly by design). Inputs outside 0..1 are clamped, so a
# timer that runs one frame long never produces a jump.
# ---------------------------------------------------------------------------


def clamp01(t):
    return 0.0 if t <= 0.0 else 1.0 if t >= 1.0 else t


def linear(t):
    return clamp01(t)


def ease_out_cubic(t):
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def ease_in_cubic(t):
    t = clamp01(t)
    return t * t * t


def ease_in_out_cubic(t):
    t = clamp01(t)
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_expo(t):
    t = clamp01(t)
    return 1.0 if t >= 1.0 else 1 - 2 ** (-10 * t)


def ease_in_expo(t):
    t = clamp01(t)
    return 0.0 if t <= 0.0 else 2 ** (10 * t - 10)


def ease_in_out_sine(t):
    t = clamp01(t)
    return -(math.cos(math.pi * t) - 1) / 2


def ease_out_back(t, overshoot=1.2):
    """A gentle overshoot. The default is well under the classic 1.70158,
    deliberately: enough to feel springy, not enough to look like a toy."""
    t = clamp01(t)
    c3 = overshoot + 1
    return 1 + c3 * (t - 1) ** 3 + overshoot * (t - 1) ** 2


def lerp(a, b, t):
    return a + (b - a) * t


# ---------------------------------------------------------------------------
# Springs — for motion that reacts to input and can be retargeted mid-flight
# ---------------------------------------------------------------------------


class Spring:
    """A damped spring toward a target, stepped with real elapsed time.

    Retargeting mid-motion keeps the current velocity, which is what makes a
    spring feel continuous where a restarted tween would visibly jerk.
    ``stiffness`` sets speed; ``damping`` sets how much it settles versus
    rings. The defaults give a quick, slightly soft settle with no visible
    bounce.
    """

    def __init__(self, value=0.0, stiffness=260.0, damping=26.0):
        self.value = float(value)
        self.target = float(value)
        self.velocity = 0.0
        self.stiffness = stiffness
        self.damping = damping

    def set_target(self, target):
        self.target = float(target)

    def kick(self, velocity):
        """Add an instantaneous push — used for tap and click reactions."""
        self.velocity += velocity

    def step(self, dt):
        # Integrate in small fixed substeps: stable even if a frame is late,
        # which on a busy laptop it sometimes will be.
        dt = max(0.0, min(dt, 0.1))
        substeps = max(1, int(dt / 0.004))
        h = dt / substeps
        for _ in range(substeps):
            force = -self.stiffness * (self.value - self.target) - self.damping * self.velocity
            self.velocity += force * h
            self.value += self.velocity * h
        return self.value

    def at_rest(self, epsilon=0.001):
        return abs(self.value - self.target) < epsilon and abs(self.velocity) < epsilon * 10


# ---------------------------------------------------------------------------
# Timed tweens — for motion with a fixed duration (appear, disappear, fold)
# ---------------------------------------------------------------------------


class Tween:
    """Progress from 0 to 1 over a duration, with an easing curve.

    Can be reversed mid-flight: reversing keeps the *eased* position where it
    is and runs back from there, so interrupting an animation never snaps.
    """

    def __init__(self, duration, curve=ease_out_cubic, clock=time.monotonic):
        self.duration = max(0.001, float(duration))
        self.curve = curve
        self._clock = clock
        self._start = None
        self._from = 0.0
        self._to = 1.0

    def start(self, from_value=0.0, to_value=1.0):
        self._from = from_value
        self._to = to_value
        self._start = self._clock()
        return self

    def raw(self):
        if self._start is None:
            return 1.0
        return clamp01((self._clock() - self._start) / self.duration)

    def value(self):
        return lerp(self._from, self._to, self.curve(self.raw()))

    def done(self):
        return self._start is not None and self.raw() >= 1.0

    def retarget(self, to_value):
        """Head somewhere new from wherever we are right now."""
        here = self.value()
        self.start(here, to_value)


# ---------------------------------------------------------------------------
# Animation settings — one place for every knob the user can turn
# ---------------------------------------------------------------------------

# Every value here is a multiplier or a duration in seconds, so "1.0" always
# means "as designed". Intensity scales how far things move; 0 turns that
# part of the motion off without disabling the feature.
ANIMATION_DEFAULTS = {
    "enabled": True,                 # master switch for everything below

    # Toby appearing, disappearing, being touched
    "appear_enabled": True,
    "appear_duration": 0.34,
    "disappear_duration": 0.24,
    "appear_scale_from": 0.86,       # how small Toby starts before growing in
    "interaction_enabled": True,
    "interaction_intensity": 1.0,    # tap squash, finish-task hop

    # Idle life: breathing, blinking, glancing
    "idle_enabled": True,
    "idle_intensity": 1.0,
    "desktop_reactions": True,       # glance when windows/workspaces change

    # The chibi that walks out and does tasks
    "chibi_enabled": True,
    "chibi_walk_speed": 900.0,       # pixels per second across the screen
    "chibi_transform_duration": 0.55,

    # The laptop lid fold
    "fold_enabled": True,
    "fold_close_duration": 0.62,
    "fold_open_duration": 0.72,
    "fold_strength": 1.0,            # how far the display bends toward the hinge
    "fold_perspective": 1.0,         # how strongly the top leans toward you
    "fold_zoom": 0.07,               # how much the desktop shrinks toward the centre
    "fold_blur": 1.0,                # motion blur amount (0 = off)
    "fold_lighting": 1.0,            # hinge shadow and glare
    "shutdown_fade": True,

    # Hyprland's own window/workspace animations (applied at runtime only)
    "hypr_animations_enabled": False,
    "hypr_animation_speed": 1.0,     # lower is faster
}


def animation_settings(settings=None):
    """The animation settings with defaults filled in and values sanity-
    clamped, so a hand-edited settings.json can't produce a negative
    duration or a fold that turns the screen inside out."""
    merged = dict(ANIMATION_DEFAULTS)
    if settings:
        user = settings.get("animations", settings)
        if isinstance(user, dict):
            for key, value in user.items():
                if key in ANIMATION_DEFAULTS and type(value) is type(ANIMATION_DEFAULTS[key]):
                    merged[key] = value
                elif key in ANIMATION_DEFAULTS and isinstance(ANIMATION_DEFAULTS[key], float) \
                        and isinstance(value, int) and not isinstance(value, bool):
                    merged[key] = float(value)

    limits = {
        "appear_duration": (0.05, 2.0), "disappear_duration": (0.05, 2.0),
        "appear_scale_from": (0.3, 1.0),
        "interaction_intensity": (0.0, 3.0), "idle_intensity": (0.0, 3.0),
        "chibi_walk_speed": (100.0, 5000.0), "chibi_transform_duration": (0.1, 3.0),
        "fold_close_duration": (0.15, 1.6), "fold_open_duration": (0.15, 2.0),
        "fold_strength": (0.0, 1.5), "fold_perspective": (0.0, 2.0),
        "fold_zoom": (0.0, 0.3), "fold_blur": (0.0, 3.0), "fold_lighting": (0.0, 2.0),
        "hypr_animation_speed": (0.2, 4.0),
    }
    for key, (low, high) in limits.items():
        merged[key] = max(low, min(high, merged[key]))
    if not merged["enabled"]:
        for key in merged:
            if key.endswith("_enabled") or key in ("desktop_reactions", "shutdown_fade"):
                merged[key] = False
    return merged
