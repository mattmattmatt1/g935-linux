"""Controlled handling for the G935 earcup volume wheel.

The receiver exposes the wheel as an evdev keyboard containing only
KEY_VOLUMEUP and KEY_VOLUMEDOWN. Desktop environments commonly apply a 5%
step to every pulse, which makes this free-spinning encoder feel abrupt. The
daemon grabs that one media-key interface and translates clean presses into a
small, configurable PulseAudio/PipeWire step.
"""
from __future__ import annotations

import fcntl
import glob
import json
import logging
import math
import os
import re
import shutil
import statistics
import struct
import subprocess
import time

from g935.paths import config_dir

EV_KEY = 0x01
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
EVIOCGRAB = 0x40044590
INPUT_EVENT = struct.Struct("llHHi")

DEFAULT_ENABLED = True
DEFAULT_STEP = 48
DEFAULT_CEILING = 100
# The receiver encodes wheel distance as key-hold duration: fine movements in
# live captures last roughly 50–200 ms, while fast sweeps last 700–1700 ms.
# Start continuous growth above the fine-motion range and add one percent at a
# time.  Calibration refines both values for the individual wheel.
HOLD_REPEAT_DELAY_S = 0.25
HOLD_REPEAT_INTERVAL_S = 0.02
FINE_INTERVAL_S = 0.083
DEFAULT_FINE_STEP = 1
# The captured encoder occasionally reports the opposite direction for the
# next one or two activations (up, down, down within 0.44 s while only rolling
# upward). Require a short pause before accepting a reversal.
DIRECTION_GUARD_S = 0.52
FAST_ROLL_GAP_S = 0.75
MEDIUM_ROLL_GAP_S = 1.50
# Apply a computed gesture as a short series of real 1% changes.  Jumping
# directly to the final percentage makes an otherwise-correct fast roll feel
# like a binary switch and gives mixers no useful intermediate feedback.
RAMP_INTERVAL_S = 0.010
RETRY_S = 5.0

_PERCENT_RE = re.compile(r"(\d+)%")
_AUDIO_ENV = {**os.environ, "LC_ALL": "C"}


def _load_ui_data():
    try:
        with open(os.path.join(config_dir(), "ui.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_wheel_settings():
    """Read the small shared subset of ui.json used by the daemon."""
    data = _load_ui_data()
    enabled = data.get("wheel_enabled", DEFAULT_ENABLED)
    try:
        step = int(data.get("wheel_step", DEFAULT_STEP))
    except (TypeError, ValueError):
        step = DEFAULT_STEP
    return bool(enabled), max(1, min(50, step))


def load_wheel_calibration():
    """Return calibrated timing thresholds in seconds."""
    data = _load_ui_data()
    try:
        profile_version = int(data.get("wheel_calibration_version", 1))
    except (TypeError, ValueError):
        profile_version = 1

    def seconds(key, default, lo, hi, migrate=False):
        try:
            stored = (
                round(default * 1000)
                if migrate and profile_version < 2
                else data.get(key, round(default * 1000))
            )
            value = float(stored) / 1000
        except (TypeError, ValueError):
            value = default
        return max(lo, min(hi, value))

    try:
        fine_step = int(data.get("wheel_fine_step", DEFAULT_FINE_STEP))
    except (TypeError, ValueError):
        fine_step = DEFAULT_FINE_STEP

    return {
        "fine_step": max(1, min(5, fine_step)),
        "fine_interval": seconds(
            "wheel_fine_interval_ms", FINE_INTERVAL_S, 0.04, 0.15),
        "fast_gap": seconds(
            "wheel_fast_gap_ms", FAST_ROLL_GAP_S, 0.15, 1.5),
        "medium_gap": seconds(
            "wheel_medium_gap_ms", MEDIUM_ROLL_GAP_S, 0.3, 3.0),
        "direction_guard": seconds(
            "wheel_direction_guard_ms", DIRECTION_GUARD_S, 0.0, 2.0),
        "hold_delay": seconds(
            "wheel_hold_delay_ms", HOLD_REPEAT_DELAY_S, 0.10, 0.60,
            migrate=True),
        "hold_interval": seconds(
            "wheel_hold_interval_ms", HOLD_REPEAT_INTERVAL_S, 0.01, 0.10,
            migrate=True),
    }


def find_wheel_device(vid=0x046D, pid=0x0A87):
    """Return the matching evdev node, or None when the receiver is absent."""
    for base in glob.glob("/sys/class/input/event*"):
        try:
            with open(os.path.join(base, "device", "id", "vendor")) as f:
                found_vid = int(f.read().strip(), 16)
            with open(os.path.join(base, "device", "id", "product")) as f:
                found_pid = int(f.read().strip(), 16)
        except (OSError, ValueError):
            continue
        if found_vid == vid and found_pid == pid:
            return os.path.join("/dev/input", os.path.basename(base))
    return None


def parse_key_events(data):
    """Yield (timestamp, key code, value) from a Linux input-event buffer."""
    usable = len(data) - (len(data) % INPUT_EVENT.size)
    for offset in range(0, usable, INPUT_EVENT.size):
        sec, usec, event_type, code, value = INPUT_EVENT.unpack_from(data, offset)
        if event_type == EV_KEY and code in (KEY_VOLUMEDOWN, KEY_VOLUMEUP):
            yield sec + usec / 1_000_000, code, value


def accelerated_step(max_step, gap_s, fast_gap=FAST_ROLL_GAP_S,
                     medium_gap=MEDIUM_ROLL_GAP_S):
    """Continuously map time between activations to a volume step.

    The smoothstep curve has zero slope at both calibrated endpoints, avoiding
    an audible cliff when a roll lands just above or below a threshold.
    """
    max_step = max(1, min(50, int(max_step)))
    fine = 1
    if gap_s is None or gap_s > medium_gap:
        return fine
    if gap_s <= fast_gap:
        return max_step
    span = max(0.001, medium_gap - fast_gap)
    speed = max(0.0, min(1.0, (medium_gap - gap_s) / span))
    smooth = speed * speed * (3 - 2 * speed)
    return round(fine + (max_step - fine) * smooth)


def analyze_calibration(stages, target_fast_rolls=3):
    """Build an adaptive profile from wizard samples.

    ``stages`` maps slow_up/slow_down/fast_up/fast_down to event dictionaries
    containing ``press``, ``release`` and ``code``.
    """
    expected = {
        "slow_up": KEY_VOLUMEUP, "fast_up": KEY_VOLUMEUP,
        "slow_down": KEY_VOLUMEDOWN, "fast_down": KEY_VOLUMEDOWN,
    }

    def matching(name):
        return [
            event for event in stages.get(name, [])
            if event.get("code") == expected[name]
        ]

    def neutral_gaps(names):
        gaps = []
        for name in names:
            events = matching(name)
            for previous, current in zip(events, events[1:]):
                release = previous.get("release")
                if release is not None:
                    gap = current["press"] - release
                    if 0 <= gap <= 5:
                        gaps.append(gap)
        return gaps

    fast_gaps = neutral_gaps(("fast_up", "fast_down"))
    slow_gaps = neutral_gaps(("slow_up", "slow_down"))
    holds = [
        event["release"] - event["press"]
        for name in ("slow_up", "slow_down")
        for event in matching(name)
        if event.get("release") is not None
        and 0 < event["release"] - event["press"] < 4
    ]
    fast_holds = [
        event["release"] - event["press"]
        for name in ("fast_up", "fast_down")
        for event in matching(name)
        if event.get("release") is not None
        and 0 < event["release"] - event["press"] < 5
    ]

    fast_observed = max(fast_gaps) if fast_gaps else FAST_ROLL_GAP_S
    slow_typical = (
        statistics.median(slow_gaps) if slow_gaps else MEDIUM_ROLL_GAP_S
    )
    slow_hold_typical = (
        statistics.median(holds) if holds else 0.65
    )

    fast_gap = max(0.25, min(1.5, fast_observed + 0.12))
    medium_gap = max(
        fast_gap + 0.25,
        min(3.0, max(fast_gap + 0.4, slow_typical * 0.80)),
    )
    direction_guard = max(0.4, min(1.5, fast_gap))
    target_fast_rolls = max(3, min(5, int(target_fast_rolls)))
    max_step = max(
        10, min(50, math.ceil(100 / (target_fast_rolls - 0.9))))
    fine_step = 1
    # Use the longest normal fine movement plus a small noise margin as the
    # point where a press becomes a continuous sweep.
    slow_ceiling = max(holds) if holds else slow_hold_typical
    hold_delay = max(0.12, min(0.60, slow_ceiling + 0.05))
    fine_interval = max(0.05, min(0.10, hold_delay / 3))
    amount_before_fast = 1 + math.floor(hold_delay / fine_interval)
    fast_hold_typical = (
        statistics.median(fast_holds)
        if fast_holds else max(0.9, hold_delay + 0.65)
    )
    usable_fast_hold = max(0.12, fast_hold_typical - hold_delay)
    hold_interval = max(
        0.012,
        min(
            0.060,
            usable_fast_hold / max(1, max_step - amount_before_fast),
        ),
    )

    wrong = sum(
        event.get("code") != expected[name]
        for name in expected
        for event in stages.get(name, [])
    )
    counts = {
        name: len(matching(name))
        for name in expected
    }
    return {
        "wheel_step": max_step,
        "wheel_fast_gap_ms": round(fast_gap * 1000),
        "wheel_medium_gap_ms": round(medium_gap * 1000),
        "wheel_direction_guard_ms": round(direction_guard * 1000),
        "wheel_hold_delay_ms": round(hold_delay * 1000),
        "wheel_hold_interval_ms": round(hold_interval * 1000),
        "wheel_fine_step": DEFAULT_FINE_STEP,
        "wheel_fine_interval_ms": round(fine_interval * 1000),
        "wheel_calibration_version": 3,
        "wheel_calibrated": True,
        "counts": counts,
        "wrong_directions": wrong,
        "fast_gaps": fast_gaps,
        "slow_gaps": slow_gaps,
        "hold_typical": slow_hold_typical,
        "slow_holds": holds,
        "fast_holds": fast_holds,
        "fast_hold_typical": fast_hold_typical,
    }


class VolumeWheel:
    """Own the G935 media-key interface and apply predictable volume steps."""

    def __init__(self, log=None, settings_loader=load_wheel_settings,
                 calibration_loader=load_wheel_calibration,
                 command_runner=None):
        self.log = log or logging.getLogger("g935.volume_wheel")
        self.settings_loader = settings_loader
        self.calibration_loader = calibration_loader
        self.command_runner = command_runner or self._run
        self.fd = None
        self.path = None
        self.next_retry = 0.0
        self.held_code = None
        self.held_effective_code = None
        self.next_hold_step = 0.0
        self.activation_amount = 0
        self.activation_limit = 0
        self.activation_started = 0.0
        self.last_effective_code = None
        self.last_activation = 0.0
        self.last_release = 0.0
        self.ramp_current = None
        self.ramp_target = None
        self.next_ramp_step = 0.0
        self.failures = 0

    @staticmethod
    def _run(*args):
        return subprocess.run(
            ["pactl", *args], capture_output=True, text=True,
            env=_AUDIO_ENV, timeout=2,
        )

    def maintain(self, now=None):
        """Open/close the wheel as settings, permissions, and devices change."""
        now = time.monotonic() if now is None else now
        enabled, _step = self.settings_loader()
        if not enabled:
            self.close("disabled in Settings")
            return
        if self.fd is not None or now < self.next_retry:
            return
        self.next_retry = now + RETRY_S
        if shutil.which("pactl") is None:
            return
        path = find_wheel_device()
        if path is None:
            return
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            self.log.debug("cannot claim %s: %s", path, exc)
            return
        self.fd = fd
        self.path = path
        self.failures = 0
        self.log.info("controlling earcup wheel on %s", path)

    def close(self, reason=None):
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        if reason:
            self.log.info("released earcup wheel: %s", reason)
        self.fd = None
        self.path = None
        self.held_code = None
        self.held_effective_code = None
        self.next_hold_step = 0.0
        self.activation_amount = 0
        self.activation_limit = 0
        self.activation_started = 0.0
        self.last_effective_code = None
        self.last_activation = 0.0
        self.last_release = 0.0
        self.ramp_current = None
        self.ramp_target = None
        self.next_ramp_step = 0.0
        self.next_retry = time.monotonic() + RETRY_S

    def fileno(self):
        return self.fd

    def handle_ready(self):
        """Consume one available evdev burst and apply its debounced net step."""
        if self.fd is None:
            return
        try:
            data = os.read(self.fd, INPUT_EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError:
            self.close("device unavailable")
            return
        if not data:
            self.close("device disconnected")
            return

        _enabled, max_step = self.settings_loader()
        calibration = self.calibration_loader()
        delta = 0
        for event_time, code, value in parse_key_events(data):
            # The receiver emits one press/release pair. Short holds are fine
            # movement; longer holds encode a faster, farther wheel sweep.
            if value == 1:
                now = time.monotonic()
                direction_gap = (
                    None if not self.last_activation
                    else event_time - self.last_activation
                )
                effective_code = code
                if (self.last_effective_code is not None
                        and code != self.last_effective_code
                        and direction_gap is not None
                        and direction_gap < calibration["direction_guard"]):
                    effective_code = self.last_effective_code
                else:
                    self.last_effective_code = code
                self.last_activation = event_time
                self.held_code = code
                self.held_effective_code = effective_code
                self.activation_started = now
                self.next_hold_step = now + min(
                    calibration["fine_interval"],
                    calibration["hold_delay"])
                direction = 1 if effective_code == KEY_VOLUMEUP else -1
                # A new edge is a fine movement. Additional distance from a
                # fast sweep is encoded by how long this key remains held.
                fine_step = calibration["fine_step"]
                self.activation_amount = fine_step
                self.activation_limit = max_step
                delta += direction * fine_step
            elif value == 0 and code == self.held_code:
                self.last_release = event_time
                self.held_code = None
                self.held_effective_code = None
                self.next_hold_step = 0.0
                self.activation_amount = 0
                self.activation_limit = 0
                self.activation_started = 0.0
        if delta:
            self._queue_delta(delta)

    def tick(self, now=None):
        """Advance the 1% ramp and recover merged physical activations.

        This evdev interface does not advertise EV_REP. The receiver encodes
        distance in the press duration, so the daemon supplies a progressive
        cadence independently of synthetic desktop key repeat.
        """
        if self.fd is None:
            return
        now = time.monotonic() if now is None else now
        if (self.held_effective_code is not None
                and now >= self.next_hold_step
                and self.activation_amount < self.activation_limit):
            calibration = self.calibration_loader()
            direction = (
                1 if self.held_effective_code == KEY_VOLUMEUP else -1)
            self._queue_delta(direction, now)
            self.activation_amount += 1
            elapsed = now - self.activation_started
            until_fast = calibration["hold_delay"] - elapsed
            if until_fast > 0:
                # Keep short presses granular, but move during that window
                # instead of pausing until the fast-sweep threshold.
                interval = min(calibration["fine_interval"], until_fast)
            else:
                interval = calibration["hold_interval"]
            self.next_hold_step = now + interval
        self._advance_ramp(now)

    def seconds_until_tick(self, now=None):
        """Return the daemon wait needed for the next ramp/hold action."""
        if self.fd is None:
            return None
        now = time.monotonic() if now is None else now
        deadlines = []
        if self.ramp_current is not None and self.ramp_target is not None:
            deadlines.append(self.next_ramp_step)
        if (self.held_effective_code is not None
                and self.activation_amount < self.activation_limit):
            deadlines.append(self.next_hold_step)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def _read_volume(self):
        current_reply = self.command_runner(
            "get-sink-volume", "@DEFAULT_SINK@")
        values = [
            int(value)
            for value in _PERCENT_RE.findall(current_reply.stdout or "")
        ]
        if current_reply.returncode or not values:
            raise RuntimeError(current_reply.stderr.strip() or "no volume")
        return round(sum(values) / len(values))

    def _set_volume(self, target):
        set_reply = self.command_runner(
            "set-sink-volume", "@DEFAULT_SINK@", f"{target}%")
        if set_reply.returncode:
            raise RuntimeError(set_reply.stderr.strip() or "set failed")

    def _queue_delta(self, delta, now=None):
        """Queue a gesture; each percent is emitted later by ``tick``."""
        now = time.monotonic() if now is None else now
        try:
            if self.ramp_current is None or self.ramp_target is None:
                self.ramp_current = self._read_volume()
                self.ramp_target = self.ramp_current
            current = self.ramp_current
            queued_direction = self.ramp_target - current
            # Reversing the wheel must reverse immediately rather than first
            # draining a long queue from the previous direction.
            if queued_direction and queued_direction * delta < 0:
                base = current
            else:
                base = self.ramp_target
            target = max(0, base + delta)
            # A deliberate slider boost can remain above 100 and be stepped
            # down, but wheel-up never crosses the hearing-safety ceiling.
            if delta > 0:
                target = min(DEFAULT_CEILING, target)
            if target == current:
                self.ramp_current = None
                self.ramp_target = None
                self.next_ramp_step = 0.0
                return
            self.ramp_target = target
            self.next_ramp_step = min(
                self.next_ramp_step or now, now)
            self.failures = 0
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            self._volume_failure(exc)

    def _advance_ramp(self, now):
        if self.ramp_current is None or self.ramp_target is None:
            return
        if now < self.next_ramp_step:
            return
        old = self.ramp_current
        target = self.ramp_target
        value = old + (1 if target > old else -1)
        try:
            self._set_volume(value)
            self.ramp_current = value
            self.next_ramp_step = now + RAMP_INTERVAL_S
            self.failures = 0
            self.log.debug(
                "earcup wheel ramp: %d%% -> %d%% (target %d%%)",
                old, value, target)
            if value == target:
                self.ramp_current = None
                self.ramp_target = None
                self.next_ramp_step = 0.0
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            self._volume_failure(exc)

    def _volume_failure(self, exc):
        self.failures += 1
        self.log.warning("earcup wheel volume change failed: %s", exc)
        if self.failures >= 3:
            # Never leave a grabbed media key doing nothing.
            self.close("audio control unavailable")
