"""XDG config / runtime paths for g935-linux."""
from __future__ import annotations

import os


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "g935")


def runtime_dir() -> str:
    """Prefer XDG_RUNTIME_DIR; fall back to the config dir."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return base
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    return d


def ensure_config_dir() -> str:
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    return d
