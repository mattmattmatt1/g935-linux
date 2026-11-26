"""Persisted panel settings (ui.json): device visibility and wheel feel."""
from __future__ import annotations

import json
import os

from g935.bindings import (
    BINDINGS_VERSION, DEFAULT_BINDINGS, bindings_to_data, normalize_bindings,
)
from g935.paths import config_dir, ensure_config_dir
from g935.volume_wheel import (
    DEFAULT_FINE_STEP, DEFAULT_STEP, DIRECTION_GUARD_S, FAST_ROLL_GAP_S,
    FINE_INTERVAL_S, HOLD_REPEAT_DELAY_S, HOLD_REPEAT_INTERVAL_S,
    MEDIUM_ROLL_GAP_S, VOLUME_BOOST_DEFAULT,
)

UI_FILE = os.path.join(config_dir(), "ui.json")


def ui_file() -> str:
    return os.path.join(config_dir(), "ui.json")

UI_DEFAULTS = {
    "hidden_sinks": [], "hidden_sources": [], "lighting": {},
    "wheel_enabled": True, "wheel_step": DEFAULT_STEP,
    "wheel_fine_step": DEFAULT_FINE_STEP,
    "wheel_fine_interval_ms": round(FINE_INTERVAL_S * 1000),
    "wheel_calibrated": False,
    "wheel_fast_gap_ms": round(FAST_ROLL_GAP_S * 1000),
    "wheel_medium_gap_ms": round(MEDIUM_ROLL_GAP_S * 1000),
    "wheel_direction_guard_ms": round(DIRECTION_GUARD_S * 1000),
    "wheel_hold_delay_ms": round(HOLD_REPEAT_DELAY_S * 1000),
    "wheel_hold_interval_ms": round(HOLD_REPEAT_INTERVAL_S * 1000),
    "wheel_calibration_version": 4,
    "volume_boost_pct": VOLUME_BOOST_DEFAULT,
    "bindings": {key: dict(value) for key, value in DEFAULT_BINDINGS.items()},
    "bindings_version": BINDINGS_VERSION,
}


class AppSettings:
    """ui.json: audio-device visibility + saved lighting state. Loaded once,
    saved atomically on change (same pattern as HealthTracker)."""

    def __init__(self, path=None):
        self.path = path or ui_file()
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.data = {
            **UI_DEFAULTS,
            **{k: data[k] for k in UI_DEFAULTS if k in data},
        }
        # Version 1 treated a long press as an 800 ms coarse repeat. Version 2
        # decodes press duration continuously; version 3 exposes fine feel;
        # version 4 adds USB-mixer flood protection and safe reversal.
        try:
            wheel_profile_version = int(
                data.get("wheel_calibration_version", 1))
        except (TypeError, ValueError):
            wheel_profile_version = 1
        if wheel_profile_version < 2:
            self.data["wheel_hold_delay_ms"] = round(
                HOLD_REPEAT_DELAY_S * 1000)
            self.data["wheel_hold_interval_ms"] = round(
                HOLD_REPEAT_INTERVAL_S * 1000)
            self.data["wheel_calibrated"] = False
        if wheel_profile_version < 3:
            self.data["wheel_fine_step"] = DEFAULT_FINE_STEP
            self.data["wheel_fine_interval_ms"] = round(
                FINE_INTERVAL_S * 1000)
        if wheel_profile_version < 4:
            self.data["wheel_hold_interval_ms"] = round(
                HOLD_REPEAT_INTERVAL_S * 1000)
            self.data["wheel_direction_guard_ms"] = round(
                DIRECTION_GUARD_S * 1000)
            self.data["wheel_calibrated"] = False
        self.data["wheel_calibration_version"] = 4
        try:
            bind_ver = int(data.get("bindings_version", 1))
        except (TypeError, ValueError):
            bind_ver = 1
        self.data["bindings"] = bindings_to_data(
            normalize_bindings(self.data.get("bindings"), bind_ver))
        self.data["bindings_version"] = BINDINGS_VERSION

    def save(self):
        ensure_config_dir()
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 1, **self.data}, f)
        os.replace(tmp, self.path)
