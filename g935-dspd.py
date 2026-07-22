#!/usr/bin/env python3
"""
g935-dspd.py - G935 housekeeping daemon: keep the chosen mode across power-ons.

When mode is "ghub" (see ~/.config/g935/mode, written by g935-control):
  - re-enable the on-device DSP soundstage on every headset power-on
  - host-manage boom mute / unmute / mic button (firmware hands that off)

When mode is "hardware" (the safe default): leave the headset stock.

Owns mic + power-on DSP while running (GUI skips those duties when this lock
is held). Runs fine alongside g935-control.py — each hidraw fd gets its own
copy of input reports.

Usage: python3 g935-dspd.py   (see g935-dsp.service for systemd --user setup)
"""
from __future__ import annotations

import logging
import os
import select
import sys
import time

# Allow `python3 g935-dspd.py` from a git checkout without install.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from g935.daemon_status import acquire_daemon_lock
from g935.hidpp import G935_PID, find_device_by_pid, open_hidraw, transact
from g935.mic import MicHandler
from g935.mode import load_mode

log = logging.getLogger("g935.dspd")

POLL_S = 5
MIC_POLL_S = 0.25
ENABLE = "11ff052b01"
BATTERY_GET = "11ff080b"
ALSA_USBID = "046d:0a87"


def enable_dsp(fd) -> bool:
    for attempt in range(5):
        status, detail = transact(fd, ENABLE)
        if status == "ACK":
            log.info("DSP soundstage enabled")
            return True
        extra = f" code {detail:#04x}" if status == "ERR" and detail is not None else ""
        log.warning("enable attempt %d: %s%s", attempt + 1, status, extra)
        time.sleep(2)
    log.error("giving up until next power-on")
    return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    lock_fd = acquire_daemon_lock()
    if lock_fd is None:
        log.error("another g935-dspd is already running")
        sys.exit(1)

    mic = MicHandler(usbid=ALSA_USBID, mode_loader=load_mode)
    connected = False
    fd = None
    next_poll = 0.0

    while True:
        if fd is None:
            dev = find_device_by_pid(G935_PID)
            if dev is None:
                connected = False
                time.sleep(POLL_S)
                continue
            try:
                fd = open_hidraw(dev)
                log.info("opened %s", dev)
            except OSError as e:
                log.warning("open %s: %s", dev, e)
                time.sleep(POLL_S)
                continue
        try:
            now = time.time()
            if now >= next_poll:
                status, _ = transact(
                    fd, BATTERY_GET,
                    on_non_hidpp=lambda buf: mic.handle_report(buf, fd),
                )
                next_poll = now + POLL_S
                if status == "ACK":
                    if not connected:
                        mode = load_mode()
                        log.info("headset detected (power-on), mode=%s", mode)
                        if mode == "ghub":
                            time.sleep(2)
                            enable_dsp(fd)
                            # Power-on with boom up may carry the device flag.
                            mic.mark_needs_unmute()
                        else:
                            log.info("hardware mode — leaving headset stock")
                        connected = True
                elif status == "GONE":
                    raise OSError("device gone during battery poll")
                else:
                    connected = False
                    mic.reset()

            wait = max(0.0, min(next_poll, time.time() + MIC_POLL_S) - time.time())
            r, _, _ = select.select([fd], [], [], wait)
            if r:
                buf = os.read(fd, 64)
                if len(buf) >= 2 and buf[0] != 0x11:
                    mic.handle_report(buf, fd)
            if connected:
                mic.poll(fd)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
            connected = False
            mic.reset()
            log.info("receiver gone, rescanning")
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
