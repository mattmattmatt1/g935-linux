"""HID++ over hidraw: device discovery, report framing, transactions, worker thread."""
from __future__ import annotations

import glob
import os
import queue
import re
import select
import threading
import time

LOGITECH_VID = 0x046D
G935_PID = 0x0A87
# HID_ID=0003:0000046D:00000A87 form in uevent
HID_ID_RE = re.compile(r"HID_ID=0003:0000046D:0000([0-9A-Fa-f]{4})")
# usage page 0xFF43 (Logitech vendor / HID++) in the report descriptor
HIDPP_PAGE = b"\x06\x43\xff"
REPORT_SIZE = 20

ERROR_CODES = {
    0x01: "unknown", 0x02: "invalid argument", 0x03: "out of range",
    0x04: "hardware error", 0x05: "not allowed", 0x06: "invalid feature index",
    0x07: "invalid function", 0x08: "busy", 0x09: "unsupported",
}


def to_report(hx: str, size: int = REPORT_SIZE) -> bytes:
    """Pad a hex string (with or without spaces) to a fixed-size HID report."""
    b = bytes.fromhex(hx.replace(" ", ""))
    if len(b) > size:
        raise ValueError(f"report longer than {size} bytes: {hx!r}")
    return b + b"\x00" * (size - len(b))


def parse_reply(buf: bytes, feat: int, fnsw: int):
    """Match a HID++ long report to a pending command.

    Returns ("ACK", buf), ("ERR", err_code), or None if unrelated/incomplete.
    """
    if len(buf) < 5 or buf[0] != 0x11:
        return None
    if buf[2] == 0xFF and buf[3] == feat and buf[4] == fnsw:
        return "ERR", buf[5] if len(buf) > 5 else 0
    if buf[2] == feat and buf[3] == fnsw:
        return "ACK", buf
    return None


def find_hidpp_devices():
    """All Logitech hidraw nodes whose report descriptor carries HID++.

    Returns list of (path, pid, hid_name).
    """
    cands = []
    for d in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            with open(os.path.join(d, "device", "uevent")) as f:
                uevent = f.read()
            with open(os.path.join(d, "device", "report_descriptor"), "rb") as f:
                rdesc = f.read()
        except OSError:
            continue
        m = HID_ID_RE.search(uevent)
        if not m or HIDPP_PAGE not in rdesc:
            continue
        nm = re.search(r"HID_NAME=(.*)", uevent)
        name = nm.group(1).strip() if nm else "Logitech headset"
        cands.append(("/dev/" + os.path.basename(d), int(m.group(1), 16), name))
    return cands


def find_headset(known_pids=None, prefer_pid=None):
    """First HID++ Logitech hidraw, preferring known / last-used PIDs.

    Returns (path, pid, name) or None.
    """
    cands = find_hidpp_devices()
    if not cands:
        return None
    known = set(known_pids or ())

    def sort_key(c):
        path, pid, name = c
        # lower is better
        prefer = 0 if (prefer_pid is not None and pid == prefer_pid) else 1
        known_rank = 0 if pid in known else 1
        return (prefer, known_rank, pid)

    cands.sort(key=sort_key)
    return cands[0]


def find_device_by_pid(pid: int = G935_PID):
    """Legacy helper: path of the HID++ interface for a specific USB PID, or None."""
    for path, p, _name in find_hidpp_devices():
        if p == pid:
            return path
    return None


def open_hidraw(path: str) -> int:
    return os.open(path, os.O_RDWR | os.O_NONBLOCK)


def transact(fd, hx, timeout=1.0, on_non_hidpp=None):
    """Send one HID++ report and wait for its ACK/ERR.

    on_non_hidpp(buf) is called for non-0x11 input reports (e.g. mic events).
    Returns (status, detail) where status is ACK|ERR|TIMEOUT|GONE|BADHEX.
    """
    try:
        clean = hx.replace(" ", "").lower()
        rpt = to_report(clean)
    except ValueError:
        return "BADHEX", None
    if not 4 <= len(bytes.fromhex(clean)) <= REPORT_SIZE:
        return "BADHEX", None
    feat, fnsw = rpt[2], rpt[3]
    try:
        os.write(fd, rpt)
        deadline = time.time() + timeout
        while time.time() < deadline:
            wait = deadline - time.time()
            if wait <= 0:
                break
            r, _, _ = select.select([fd], [], [], wait)
            if not r:
                break
            buf = os.read(fd, 64)
            if len(buf) < 2:
                continue
            if buf[0] != 0x11:
                if on_non_hidpp is not None:
                    on_non_hidpp(buf)
                continue
            matched = parse_reply(buf, feat, fnsw)
            if matched is None:
                continue
            status, detail = matched
            return status, detail
    except OSError:
        return "GONE", None
    return "TIMEOUT", None


class HidWorker(threading.Thread):
    """Serializes hidraw I/O on one thread; results come back via callbacks.

    Survives receiver unplug/replug: on GONE, closes the fd, rescans, reopens,
    and fires on_link(False/True) so the GUI can rediscover features.
    """

    daemon = True
    MIC_POLL_S = 0.25
    RESCAN_S = 1.0

    def __init__(self, path, log_cb, mic_cb=None, boom_cb=None, poll_boom=True,
                 on_link=None, prefer_pid=None, known_pids=None,
                 boom_reader=None, idle_add=None):
        super().__init__()
        self.path = path
        self.prefer_pid = prefer_pid
        self.known_pids = known_pids
        self.log_cb = log_cb
        self.mic_cb = mic_cb
        self.boom_cb = boom_cb
        self.poll_boom = poll_boom
        self.boom_reader = boom_reader  # callable(fd) -> bool|None
        self.on_link = on_link
        # GLib.idle_add when provided; else call directly (tests / non-GTK)
        self.idle_add = idle_add or (lambda fn, *a: fn(*a) or False)
        self.q = queue.Queue()
        self.fd = None
        self._boom = None
        self._stop = False
        self._linked = False
        self._ever_linked = False  # only fire on_link(True) after a prior loss

    def submit(self, hx, done_cb=None):
        self.q.put((hx.replace(" ", "").lower(), done_cb))

    def stop(self):
        self._stop = True
        self.q.put(None)  # wake the queue

    def run(self):
        while not self._stop:
            if self.fd is None:
                if not self._ensure_open():
                    time.sleep(self.RESCAN_S)
                    continue
            try:
                item = self.q.get(timeout=self.MIC_POLL_S)
            except queue.Empty:
                if not self._drain():
                    continue
            else:
                if item is None:
                    if self._stop:
                        break
                    continue
                hx, done_cb = item
                status, reply = self._transact(hx)
                if status == "GONE":
                    self._handle_gone()
                self.idle_add(self.log_cb, hx, status, reply)
                if done_cb:
                    self.idle_add(done_cb, status, reply)
                if status == "GONE":
                    continue
            self._poll_boom()
        self._close_fd()

    def _ensure_open(self) -> bool:
        path = self.path
        if path is None or not os.path.exists(path):
            found = find_headset(known_pids=self.known_pids,
                                 prefer_pid=self.prefer_pid)
            if not found:
                return False
            path, pid, _name = found
            self.path = path
            if self.prefer_pid is None:
                self.prefer_pid = pid
        try:
            self.fd = open_hidraw(self.path)
        except OSError:
            self.path = None
            return False
        self._boom = None
        if not self._linked:
            # Notify GUI only on *re*connect so first open doesn't race discovery.
            if self._ever_linked and self.on_link:
                self.idle_add(self.on_link, True)
            self._linked = True
            self._ever_linked = True
        return True

    def _close_fd(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _handle_gone(self):
        self._close_fd()
        self.path = None
        if self._linked:
            self._linked = False
            if self.on_link:
                self.idle_add(self.on_link, False)

    def _drain(self) -> bool:
        """Read pending input reports. Returns False if the device is gone."""
        if self.fd is None:
            return False
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 0)
            except (OSError, ValueError):
                self._handle_gone()
                return False
            if not r:
                return True
            try:
                buf = os.read(self.fd, 64)
            except OSError:
                self._handle_gone()
                return False
            if len(buf) >= 2 and buf[0] == 0x08 and self.mic_cb:
                self.idle_add(self.mic_cb, buf[1])
        return True

    def _poll_boom(self):
        if not self.poll_boom or self.fd is None or self.boom_reader is None:
            return
        try:
            down = self.boom_reader(self.fd)
        except OSError:
            self._handle_gone()
            return
        if down is not None and down != self._boom:
            self._boom = down
            if self.boom_cb:
                self.idle_add(self.boom_cb, down)

    def _transact(self, hx, timeout=1.0):
        if self.fd is None:
            return "GONE", None

        def on_non(buf):
            if buf[0] == 0x08 and self.mic_cb and len(buf) >= 2:
                self.idle_add(self.mic_cb, buf[1])

        return transact(self.fd, hx, timeout=timeout, on_non_hidpp=on_non)
