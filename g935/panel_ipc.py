"""Wake a running control panel (second-instance → show window)."""
from __future__ import annotations

import os
import socket

from g935.paths import runtime_dir

SOCKET_NAME = "g935-panel.sock"
PRESENT = b"present"


def socket_path() -> str:
    return os.path.join(runtime_dir(), SOCKET_NAME)


def notify_running_instance() -> bool:
    """True if a running panel received the present request."""
    path = socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.settimeout(0.3)
    try:
        sock.sendto(PRESENT, path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def bind_listener():
    """Datagram socket the running panel owns. Replaces a stale path."""
    path = socket_path()
    try:
        os.unlink(path)
    except OSError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    sock.setblocking(False)
    return sock
