#!/usr/bin/env python3
"""
g935-dspd.py - G935 housekeeping daemon: keep the chosen mode across power-ons.

When mode is "ghub" (see ~/.config/g935/mode, written by g935-control):
  - re-enable the on-device DSP soundstage on every headset power-on
  - host-manage boom mute / unmute / mic button (firmware hands that off)

When mode is "hardware" (the safe default): leave the headset stock.

Owns mic handling and watches for power-on while running. The GUI can safely
reassert the same persisted DSP mode after a confirmed reconnect; shared
HID++ transactions are serialized across processes.

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
from g935.hidpp import (
    G935_PID, PollPresence, find_device_by_pid, open_hidraw, transact,
)
from g935.mic import MicHandler
from g935.mode import load_mode
from g935.volume_wheel import VolumeWheel

log = logging.getLogger("g935.dspd")

POLL_S = 5
MIC_POLL_S = 0.25
# 3 misses (~15s) was too tight: a reconnect assert storm from the panel makes
# consecutive presence TIMEOUTs look like power-off, which re-triggers enable
# and loops. 6 misses ≈ 30s of sustained silence before declaring offline.
MISSED_POLLS_OFFLINE = 6
# After power-on enable, ignore presence misses while the panel reasserts.
POST_ENABLE_GRACE_S = 45.0
ENABLE = "11ff052b01"
# A root ping is answered by the USB receiver even while the wireless headset
# is off.  G-key capability is a harmless, device-level read: it ACKs only when
# the headset itself is reachable and avoids touching ADC/battery state.
PRESENCE_GET = "11ff050b"
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
    wheel = VolumeWheel(log.getChild("wheel"))
    presence = PollPresence(
        miss_limit=MISSED_POLLS_OFFLINE, grace_s=POST_ENABLE_GRACE_S)
    fd = None
    next_poll = 0.0

    while True:
        if fd is None:
            dev = find_device_by_pid(G935_PID)
            if dev is None:
                presence.reset()
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
            wheel.maintain()
            now = time.time()
            if now >= next_poll:
                status, _ = transact(
                    fd, PRESENCE_GET,
                    on_non_hidpp=lambda buf: mic.handle_report(buf, fd),
                    timeout=1.5,
                )
                next_poll = now + POLL_S
                if status == "GONE":
                    raise OSError("device gone during presence poll")
                transition = presence.observe(status == "ACK")
                if transition is True:
                    mode = load_mode()
                    log.info("headset detected (power-on), mode=%s", mode)
                    if mode == "ghub":
                        time.sleep(2)
                        enable_dsp(fd)
                        # Panel will reassert lighting/EQ on the same bus;
                        # hold grace so TIMEOUT streaks do not re-enter power-on.
                        presence.note_activity(POST_ENABLE_GRACE_S)
                        # Power-on with boom up may carry the device flag.
                        mic.mark_needs_unmute()
                    else:
                        log.info("hardware mode — leaving headset stock")
                        presence.note_activity(POST_ENABLE_GRACE_S)
                elif transition is False:
                    log.info(
                        "headset unavailable after %d missed polls",
                        MISSED_POLLS_OFFLINE,
                    )
                    mic.reset()

            wait = max(0.0, min(next_poll, time.time() + MIC_POLL_S) - time.time())
            wheel_wait = wheel.seconds_until_tick()
            if wheel_wait is not None:
                wait = min(wait, wheel_wait)
            readers = [fd]
            if wheel.fileno() is not None:
                readers.append(wheel.fileno())
            r, _, _ = select.select(readers, [], [], wait)
            if wheel.fileno() is not None and wheel.fileno() in r:
                wheel.handle_ready()
            wheel.tick()
            if fd in r:
                buf = os.read(fd, 64)
                if len(buf) >= 2 and buf[0] != 0x11:
                    mic.handle_report(buf, fd)
            if presence.connected:
                mic.poll(fd)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
            presence.reset()
            mic.reset()
            log.info("receiver gone, rescanning")
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
