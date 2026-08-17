"""Detect / claim the g935-dspd singleton via flock."""
from __future__ import annotations

import fcntl
import os

from g935.paths import runtime_dir


def lock_path() -> str:
    return os.path.join(runtime_dir(), "g935-dspd.lock")


def panel_lock_path() -> str:
    return os.path.join(runtime_dir(), "g935-control.lock")


def _lock_held(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)
    return False


def acquire_daemon_lock():
    """Hold an exclusive flock for the lifetime of the daemon process.

    Returns the open lock fd on success, or None if another daemon already holds it.
    The caller must keep the fd open (and not unlock) until exit.
    """
    path = lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    # leave a pid for humans reading the file
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def daemon_running() -> bool:
    """True if g935-dspd currently holds the daemon lock."""
    return _lock_held(lock_path())


def panel_running() -> bool:
    """True if g935-control currently holds the panel lock."""
    return _lock_held(panel_lock_path())
