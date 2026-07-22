"""Boom-mic position and host ALSA capture-switch helpers."""
from __future__ import annotations

import fcntl
import glob
import logging
import os
import subprocess
import time

log = logging.getLogger("g935.mic")

MIC_REPORT = 0x08
# Measured 2026-07-21: boom up → 0x10 (state 0x11), boom down → 0x20 (state 0x21)
BOOM_UP, BOOM_DOWN, BUTTON = 0x10, 0x20, 0x01
DEFAULT_USBID = "046d:0a87"
DEFAULT_SWITCH = "Mic Capture Switch"
UNMUTE_HOLD_S = 1.5


def hidiocginput(length: int) -> int:
    """_IOC(_IOC_READ|_IOC_WRITE, 'H', 0x0A, length)"""
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x0A


def find_alsa_card(usbid: str = DEFAULT_USBID):
    """ALSA card index string whose USB id matches, or None."""
    if not usbid:
        return None
    want = usbid.lower()
    for p in glob.glob("/proc/asound/card*/usbid"):
        try:
            with open(p) as f:
                if f.read().strip().lower() == want:
                    return os.path.basename(os.path.dirname(p))[4:]
        except OSError:
            continue
    return None


def read_host_mic_switch(usbid: str = DEFAULT_USBID,
                         switch_name: str = DEFAULT_SWITCH):
    """True = capture on, False = host-muted, None = unknown."""
    card = find_alsa_card(usbid)
    if card is None:
        return None
    try:
        out = subprocess.run(
            ["amixer", "-c", card, "cget", "name=" + switch_name],
            capture_output=True, text=True,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except FileNotFoundError:
        return None
    for line in out.splitlines():
        if ": values=" in line:
            return line.strip().endswith("=on")
    return None


def set_host_mic_switch(on: bool, usbid: str = DEFAULT_USBID,
                        switch_name: str = DEFAULT_SWITCH) -> None:
    card = find_alsa_card(usbid)
    if card is None:
        return
    try:
        subprocess.run(
            ["amixer", "-q", "-c", card, "cset",
             "name=" + switch_name, "on" if on else "off"],
            check=False, env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError:
        log.warning("amixer not found — install alsa-utils for mic handling")


def read_boom(fd) -> bool | None:
    """Where the boom is right now: True=down, False=up, None=unknown.

    Level truth via HIDIOCGINPUT — immune to ALSA-write echo events.
    """
    buf = bytearray(2)
    buf[0] = MIC_REPORT
    try:
        fcntl.ioctl(fd, hidiocginput(len(buf)), buf, True)
    except OSError:
        return None
    if buf[1] & BOOM_DOWN:
        return True
    if buf[1] & BOOM_UP:
        return False
    return None


def unmute_pulse(fd, usbid: str = DEFAULT_USBID,
                 switch_name: str = DEFAULT_SWITCH,
                 hold_s: float = UNMUTE_HOLD_S) -> None:
    """Host-side UAC mute pulse that merges the device boom flag away.

    Off → hold ≥1.5s → on. Aborts the final "on" if boom goes up mid-hold.
    """
    log.info("unmute pulse: switch off, hold %.1fs, on", hold_s)
    set_host_mic_switch(False, usbid=usbid, switch_name=switch_name)
    time.sleep(hold_s)
    if read_boom(fd) is False:
        log.info("boom went up mid-pulse: staying muted")
        return
    set_host_mic_switch(True, usbid=usbid, switch_name=switch_name)


class MicHandler:
    """G HUB-mode boom/button mic state machine (the host side of G HUB's job).

    Only active when mode_loader() returns "ghub". Hardware mode: log events only.
    """

    def __init__(self, usbid: str = DEFAULT_USBID,
                 switch_name: str = DEFAULT_SWITCH,
                 mode_loader=None):
        self.usbid = usbid
        self.switch_name = switch_name
        self.mode_loader = mode_loader or (lambda: "ghub")
        self._boom_down = None       # last observed position
        self._needs_unmute = False   # boom-up happened; next boom-down owes a pulse

    def reset(self):
        self._boom_down = None
        self._needs_unmute = False

    def mark_needs_unmute(self):
        """Headset powered on may already carry a raised-boom flag."""
        self._needs_unmute = True

    def _mode_is_ghub(self) -> bool:
        return self.mode_loader() == "ghub"

    def handle_report(self, buf, fd) -> None:
        if len(buf) < 2 or buf[0] != MIC_REPORT:
            return
        bits = buf[1]
        log.info("mic event: %s", buf[:2].hex())
        if not self._mode_is_ghub():
            return
        if bits & BOOM_UP and read_boom(fd) is False:
            self._needs_unmute = True
        elif bits & BUTTON and not bits & (BOOM_UP | BOOM_DOWN):
            cur = read_host_mic_switch(self.usbid, self.switch_name)
            if cur is None:
                return
            if cur:
                log.info("button: muting (host switch off)")
                set_host_mic_switch(False, self.usbid, self.switch_name)
            elif not self._boom_down:
                log.info("button: ignored, boom is up")
            elif self._needs_unmute:
                log.info("button: unmuting (stale boom flag, full pulse)")
                self._needs_unmute = False
                unmute_pulse(fd, self.usbid, self.switch_name)
            else:
                log.info("button: unmuting (host switch on)")
                set_host_mic_switch(True, self.usbid, self.switch_name)

    def poll(self, fd) -> None:
        """Track boom position by level and settle the mic to match it."""
        down = read_boom(fd)
        if down is None:
            return
        changed = down != self._boom_down
        if changed and self._boom_down is not None:
            log.info("boom %s", "down" if down else "up")
        self._boom_down = down
        if not self._mode_is_ghub():
            return
        if not down:
            if changed:
                self._needs_unmute = True
                if read_host_mic_switch(self.usbid, self.switch_name):
                    log.info("boom up: host switch off")
                    set_host_mic_switch(False, self.usbid, self.switch_name)
            return
        if not self._needs_unmute:
            return
        self._needs_unmute = False
        unmute_pulse(fd, self.usbid, self.switch_name)
        self.poll(fd)  # resync after the blind hold window
