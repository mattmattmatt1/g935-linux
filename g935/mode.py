"""Persisted headset mode: "hardware" (stock) or "ghub" (DSP + host mic)."""
from __future__ import annotations

import os

from g935.paths import config_dir, ensure_config_dir

DEFAULT_MODE = "hardware"
VALID_MODES = frozenset({"hardware", "ghub"})


def mode_file() -> str:
    return os.path.join(config_dir(), "mode")


def load_mode() -> str:
    """Return the configured mode. Missing/empty/corrupt → hardware (safe default)."""
    try:
        with open(mode_file()) as f:
            val = f.read().strip()
    except OSError:
        return DEFAULT_MODE
    if val in VALID_MODES:
        return val
    return DEFAULT_MODE


def save_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}")
    ensure_config_dir()
    path = mode_file()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(mode + "\n")
    os.replace(tmp, path)
