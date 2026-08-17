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

import errno
import logging
import os
import select
import sys
import time

# Allow `python3 g935-dspd.py` from a git checkout without install.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from g935.battery import RECENT_DECIM_S, HealthTracker, batt_state
from g935.daemon_status import acquire_daemon_lock, panel_running
from g935.hidpp import (
    G935_PID, PollPresence, drain_idle, find_device_by_pid, open_hidraw,
    transact,
)
from g935.bindings import BINDINGS_VERSION, normalize_bindings
from g935.mic import MicHandler
from g935.mode import load_mode
from g935.paths import config_dir
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
# Feature index 08 = 0x1f20 ADC/battery on the G935 (same hardcode style as ENABLE).
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


def _g0_tap_binding():
    """G0 tap from ui.json so the mute key follows the panel when it is closed."""
    import json
    path = os.path.join(config_dir(), "ui.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    try:
        version = int(data.get("bindings_version", 1))
    except (TypeError, ValueError):
        version = 1
    return normalize_bindings(data.get("bindings"), version)["g0"].tap


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

    mic = MicHandler(
        usbid=ALSA_USBID, mode_loader=load_mode,
        button_gate=panel_running, button_binding=_g0_tap_binding)
    wheel = VolumeWheel(log.getChild("wheel"))
    health = HealthTracker()
    presence = PollPresence(
        miss_limit=MISSED_POLLS_OFFLINE, grace_s=POST_ENABLE_GRACE_S)
    fd = None
    next_poll = 0.0
    next_health = 0.0
    denied_logged = False

    while True:
        if fd is None:
            dev = find_device_by_pid(G935_PID)
            if dev is None:
                presence.reset()
                time.sleep(POLL_S)
                continue
            try:
                fd = open_hidraw(dev)
                if denied_logged:
                    log.info("opened %s (ACL ready)", dev)
                    denied_logged = False
                else:
                    log.info("opened %s", dev)
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EPERM):
                    if not denied_logged:
                        log.warning(
                            "open %s: permission denied "
                            "(waiting for udev/seat ACL)", dev)
                        denied_logged = True
                    else:
                        log.debug("open %s: permission denied", dev)
                else:
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
                    health.mark_offline()
                elif (presence.connected
                        and not panel_running()
                        and now >= next_health):
                    next_health = now + RECENT_DECIM_S
                    batt_st, reply = transact(
                        fd, BATTERY_GET,
                        on_non_hidpp=lambda buf: mic.handle_report(buf, fd),
                        timeout=1.5,
                    )
                    if batt_st == "ACK" and reply is not None and len(reply) > 6:
                        mv = (reply[4] << 8) | reply[5]
                        _state, charging = batt_state(reply[6])
                        if charging is not None:
                            health.add_sample(now, mv, charging)
                    elif batt_st == "GONE":
                        raise OSError("device gone during battery sample")

            wait = max(0.0, min(next_poll, time.time() + MIC_POLL_S) - time.time())
            wheel_wait = wheel.seconds_until_tick()
            if wheel_wait is not None:
                wait = min(wait, wheel_wait)
            mic_wait = mic.seconds_until_tick()
            if mic_wait is not None:
                wait = min(wait, mic_wait)
            readers = [fd]
            if wheel.fileno() is not None:
                readers.append(wheel.fileno())
            r, _, _ = select.select(readers, [], [], wait)
            if wheel.fileno() is not None and wheel.fileno() in r:
                wheel.handle_ready()
            wheel.tick()
            if fd in r:
                if not drain_idle(
                        fd,
                        on_non_hidpp=lambda buf: mic.handle_report(buf, fd)):
                    raise OSError("device gone during idle drain")
            if presence.connected:
                mic.poll(fd)
                mic.tick(fd)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
            presence.reset()
            mic.reset()
            health.mark_offline()
            log.info("receiver gone, rescanning")
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
