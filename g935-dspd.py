#!/usr/bin/env python3
"""
g935-dspd.py - Single-owner G935 hardware daemon.

Owns the receiver hidraw node, earcup volume wheel (via native libpulse),
software-mode DSP enable/self-heal, boom-mic state machine, headless G0–G3
bindings, battery health logging + low-battery notifications, and a local
JSON-RPC + event stream socket for g935-control.

Usage: python3 g935-dspd.py   (see g935-dsp.service for systemd --user setup)
"""
from __future__ import annotations

import errno
import logging
import os
import select
import signal
import sys
import time

# Allow `python3 g935-dspd.py` from a git checkout without install.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from g935.battery import (
    HEALTH_DEFAULTS, BatteryAlerts, HealthTracker, batt_state,
)
from g935.bindings import (
    DEFAULT_HOLD_S, HID_KEYS, HoldTracker, fire_headless, key_for_index,
    normalize_bindings,
)
from g935.daemon_ipc import DaemonIpcServer
from g935.daemon_status import acquire_daemon_lock
from g935.features import parse_gkey_mask
from g935.hidpp import (
    G935_PID, PollPresence, drain_idle, find_device_by_pid, open_hidraw,
    transact,
)
from g935.mic import (
    MicHandler, read_boom, read_host_mic_switch, set_host_mic_switch,
)
from g935.mode import load_mode, save_mode
from g935.stereo_route import is_g935_sink, notify_user_with_id
from g935.volume_wheel import VOLUME_BOOST_MAX, VolumeWheel, _load_ui_data

log = logging.getLogger("g935.dspd")

POLL_S = 5.0
MIC_POLL_S = 0.25
DSP_ENABLE_DELAY_S = 2.0
DSP_ENABLE_ATTEMPTS = 5
DSP_HEAL_S = 30.0
MISSED_POLLS_OFFLINE = 6
POST_ENABLE_GRACE_S = 45.0
ENABLE = "11ff052b01"
DISABLE = "11ff052b00"
PRESENCE_GET = "11ff050b"
BATTERY_GET = "11ff080b"
GKEYS_FEAT_IDX = 0x05
ALSA_USBID = "046d:0a87"


def _load_bindings():
    data = _load_ui_data()
    try:
        version = int(data.get("bindings_version", 1))
    except (TypeError, ValueError):
        version = 1
    return normalize_bindings(data.get("bindings"), version)


def _g0_tap_binding():
    return _load_bindings()["g0"].tap


def enable_dsp(fd) -> bool:
    for attempt in range(DSP_ENABLE_ATTEMPTS):
        status, detail = transact(fd, ENABLE)
        if status == "ACK":
            log.info("DSP soundstage enabled")
            return True
        extra = f" code {detail:#04x}" if status == "ERR" and detail is not None else ""
        log.warning("enable attempt %d: %s%s", attempt + 1, status, extra)
        time.sleep(DSP_ENABLE_DELAY_S)
    log.error("giving up until next power-on")
    return False


class Daemon:
    def __init__(self) -> None:
        self.stop_requested = False
        self.fd: int | None = None
        self.dev_path: str | None = None
        self.pid = G935_PID
        self.wheel_pause_clients: set[int] = set()
        self.ipc = DaemonIpcServer(on_disconnect=self._on_client_disconnect)
        self.wheel = VolumeWheel(log.getChild("wheel"))
        self.mic = MicHandler(
            usbid=ALSA_USBID,
            mode_loader=load_mode,
            button_gate=self._ui_owns_bindings,
            button_binding=_g0_tap_binding,
        )
        self.health = HealthTracker(auto_save_scrub=True)
        self.alerts = BatteryAlerts()
        self.notify_ids: dict[str, int] = {}
        self.presence = PollPresence(
            miss_limit=MISSED_POLLS_OFFLINE, grace_s=POST_ENABLE_GRACE_S)
        self._holds = HoldTracker(DEFAULT_HOLD_S)
        self._gkey_mask = 0
        self._last_boom: bool | None = None
        self._last_battery: dict | None = None
        self._dsp_attempts_left = 0
        self._next_dsp_try = 0.0
        self._last_dsp_heal = 0.0
        self._next_poll = 0.0
        self._next_rescan = 0.0
        self._denied_logged = False

    def _ui_owns_bindings(self) -> bool:
        return self.ipc.client_count > 0

    def _on_client_disconnect(self, client_fd: int) -> None:
        if client_fd in self.wheel_pause_clients:
            self.wheel_pause_clients.discard(client_fd)
            if not self.wheel_pause_clients:
                self.wheel.set_paused(False)

    def _run_headless_binding(self, key: str, layer: str) -> None:
        kb = _load_bindings().get(key)
        if kb is None:
            return
        binding = kb.layer(layer)
        action = binding.action
        if action == "none":
            return
        if action in ("vol_up", "vol_down"):
            delta = 5 if action == "vol_up" else -5
            self._nudge_headset_volume(delta)
            log.info("headless %s %s: volume %+d%%", key, layer, delta)
            return
        if action == "mute_output":
            self._toggle_headset_mute()
            log.info("headless %s %s: toggled headset mute", key, layer)
            return
        if action == "mic_mute":
            cur = read_host_mic_switch(ALSA_USBID)
            if cur is None:
                return
            if not cur and self._last_boom is False:
                log.info("headless %s %s: ignored mic unmute (boom up)", key, layer)
                return
            set_host_mic_switch(not cur, ALSA_USBID)
            log.info("headless %s %s: mic %s", key, layer, "muted" if cur else "unmuted")
            return
        ok, detail = fire_headless(binding)
        log.info("headless %s %s (%s): %s (%s)", key, layer, action, "ok" if ok else "err", detail)

    def _nudge_headset_volume(self, delta: int) -> None:
        pulse = self.wheel._pulse
        target = self.wheel._volume_target()
        if pulse is not None:
            dev = pulse.get_device("sink", target)
            if dev is not None:
                new = max(0, min(VOLUME_BOOST_MAX, dev.volume_pct + delta))
                if new != dev.volume_pct:
                    pulse.set_volume("sink", dev.name, new)
                return
        try:
            cur = self.wheel._read_volume()
            new = max(0, min(VOLUME_BOOST_MAX, cur + delta))
            if new != cur:
                self.wheel._set_volume(new)
        except Exception as exc:
            log.warning("headless volume nudge failed: %s", exc)

    def _toggle_headset_mute(self) -> None:
        pulse = self.wheel._pulse
        target = self.wheel._volume_target()
        if pulse is not None:
            if pulse.toggle_mute("sink", target) is not None:
                return
        try:
            self.wheel.command_runner("set-sink-mute", target, "toggle")
        except Exception as exc:
            log.warning("headless mute toggle failed: %s", exc)

    def _on_non_hidpp(self, buf: bytes) -> None:
        if len(buf) >= 2 and buf[0] == 0x08:
            self.ipc.broadcast("mic_event", {"bits": int(buf[1])})
        self.mic.handle_report(buf, self.fd)

    def _on_hidpp_notification(self, buf: bytes) -> None:
        self.ipc.broadcast("hidpp_event", {"report_hex": buf.hex()})
        if self._ui_owns_bindings():
            return
        if len(buf) < 8 or buf[2] != GKEYS_FEAT_IDX or buf[3] != 0:
            return
        if load_mode() != "ghub":
            return
        try:
            mask = parse_gkey_mask(buf)
        except ValueError:
            return
        changed = self._gkey_mask ^ mask
        now = time.monotonic()
        for i in range(len(HID_KEYS)):
            bit = 1 << i
            down = bool(mask & bit)
            key = key_for_index(i)
            if not key:
                continue
            if changed & bit:
                if down:
                    self._holds.press(key, now)
                elif self._holds.release(key) == "tap":
                    self._run_headless_binding(key, "tap")
            elif changed == 0 and down:
                if self._holds.release(key) == "tap":
                    self._run_headless_binding(key, "tap")
                self._holds.press(key, now)
        self._gkey_mask = mask

    def _tick_holds(self) -> None:
        if self._ui_owns_bindings():
            return
        for key in self._holds.due(time.monotonic()):
            self._run_headless_binding(key, "hold")

    def _transact(self, hx: str, timeout: float = 1.0,
                  broadcast_traffic: bool = False):
        if self.fd is None:
            return "GONE", None
        status, detail = transact(
            self.fd, hx, timeout=timeout,
            on_non_hidpp=self._on_non_hidpp,
            on_hidpp_notification=self._on_hidpp_notification,
        )
        if broadcast_traffic:
            self.ipc.broadcast("traffic", {
                "hx": hx,
                "status": status,
                "reply_hex": detail.hex() if isinstance(detail, (bytes, bytearray)) else None,
                "err_code": int(detail) if status == "ERR" and detail is not None else None,
            })
        return status, detail

    def _handle_rpc(self, client_fd: int, method: str, params: dict):
        if method == "get_state":
            return {
                "link_up": self.fd is not None,
                "connected": self.presence.connected,
                "dev_path": self.dev_path,
                "pid": self.pid,
                "mode": load_mode(),
                "boom": self._last_boom,
                "battery": self._last_battery,
                "wheel_paused": self.wheel.paused,
                "health_settings": dict(self.health.settings),
            }
        if method == "hidpp":
            hx = str(params.get("hx", "")).replace(" ", "").lower()
            timeout = float(params.get("timeout", 1.0))
            status, detail = self._transact(hx, timeout=timeout, broadcast_traffic=True)
            if status == "GONE":
                self._handle_receiver_gone()
            return {
                "status": status,
                "reply_hex": detail.hex() if isinstance(detail, (bytes, bytearray)) else None,
                "err_code": int(detail) if status == "ERR" and detail is not None else None,
            }
        if method == "set_mode":
            mode = str(params.get("mode", "hardware"))
            save_mode(mode)
            hx = ENABLE if mode == "ghub" else DISABLE
            status, _detail = self._transact(hx, broadcast_traffic=True)
            if status == "GONE":
                self._handle_receiver_gone()
            self.ipc.broadcast("mode", {"mode": mode})
            return {"mode": mode, "status": status}
        if method == "pause_wheel":
            paused = bool(params.get("paused", False))
            if paused:
                self.wheel_pause_clients.add(client_fd)
            else:
                self.wheel_pause_clients.discard(client_fd)
            self.wheel.set_paused(bool(self.wheel_pause_clients))
            return {"paused": self.wheel.paused}
        if method == "update_health_settings":
            updates = params.get("settings")
            if isinstance(updates, dict):
                for key, val in updates.items():
                    if key in HEALTH_DEFAULTS:
                        self.health.settings[key] = val
                if not self.health.settings.get("tracking", True):
                    self.health.close_segment("tracking disabled")
                self.health.save()
                self.ipc.broadcast("health_updated", {
                    "settings": dict(self.health.settings),
                })
            return {"settings": dict(self.health.settings)}
        if method == "set_host_mic":
            on = bool(params.get("on", True))
            set_host_mic_switch(on, ALSA_USBID)
            return {"on": read_host_mic_switch(ALSA_USBID)}
        raise ValueError(f"unknown method {method!r}")

    def _check_battery_alerts(self, pct, charging) -> None:
        for alert in self.alerts.update(pct, charging):
            prev_id = self.notify_ids.get(alert.level)
            ok, new_id = notify_user_with_id(
                alert.summary, alert.body,
                urgency=alert.urgency, replace_id=prev_id,
            )
            if ok and new_id:
                self.notify_ids[alert.level] = new_id
            log.info("battery alert %s: %s", alert.level, alert.body)

    def _poll_battery_and_presence(self, mono: float) -> None:
        status, reply = self._transact(BATTERY_GET, timeout=1.5)
        self._next_poll = mono + POLL_S
        if status == "GONE":
            raise OSError("device gone during battery poll")
        reachable = status == "ACK" and reply is not None and len(reply) > 6
        if not reachable:
            # Fall back to G-key capability read in case battery isn't ready.
            p_st, _ = self._transact(PRESENCE_GET, timeout=1.0)
            if p_st == "GONE":
                raise OSError("device gone during presence poll")
            reachable = p_st == "ACK"

        transition = self.presence.observe(reachable)
        if transition is True:
            mode = load_mode()
            log.info("headset detected (power-on), mode=%s", mode)
            if mode == "ghub":
                self._dsp_attempts_left = DSP_ENABLE_ATTEMPTS
                self._next_dsp_try = mono + DSP_ENABLE_DELAY_S
                self.presence.note_activity(POST_ENABLE_GRACE_S)
            else:
                log.info("hardware mode — leaving headset stock")
                self._dsp_attempts_left = 0
                self.presence.note_activity(POST_ENABLE_GRACE_S)
            self.ipc.broadcast("presence", {"connected": True})
        elif transition is False:
            log.info(
                "headset unavailable after %d missed polls",
                MISSED_POLLS_OFFLINE,
            )
            self._dsp_attempts_left = 0
            self.mic.reset()
            self.health.mark_offline()
            self._last_battery = None
            self.ipc.broadcast("presence", {"connected": False})

        if reachable and status == "ACK" and reply is not None and len(reply) > 6:
            now_wall = time.time()
            mv = (reply[4] << 8) | reply[5]
            flags = reply[6]
            state, charging = batt_state(flags)
            if charging is not None:
                self.health.add_sample(now_wall, mv, charging)
                reading = self.health.reading(mv, charging)
            else:
                from g935.battery import batt_percent
                reading = {
                    "raw_mv": mv, "cell_mv": mv, "pct": batt_percent(mv),
                    "charging": None, "path_is_rail": False,
                }
            self._last_battery = {
                "t": now_wall,
                "mv": mv,
                "flags": flags,
                "state": state,
                "charging": charging,
                "cell_mv": reading["cell_mv"],
                "pct": reading["pct"],
                "path_is_rail": reading.get("path_is_rail", False),
            }
            self.ipc.broadcast("battery", self._last_battery)
            self._check_battery_alerts(reading["pct"], charging)

            # Periodic software-mode self-heal (same 30s cadence as before).
            if load_mode() == "ghub" and self._dsp_attempts_left == 0:
                if mono - self._last_dsp_heal >= DSP_HEAL_S:
                    self._last_dsp_heal = mono
                    self._transact(ENABLE, timeout=1.0)

    def _step_dsp_enable(self, mono: float) -> None:
        if self._dsp_attempts_left <= 0 or mono < self._next_dsp_try:
            return
        status, detail = self._transact(ENABLE, broadcast_traffic=True)
        if status == "GONE":
            raise OSError("device gone during DSP enable")
        if status == "ACK":
            log.info("DSP soundstage enabled")
            self._dsp_attempts_left = 0
            self._last_dsp_heal = mono
            self.presence.note_activity(POST_ENABLE_GRACE_S)
            self.mic.mark_needs_unmute()
        else:
            attempt_no = DSP_ENABLE_ATTEMPTS - self._dsp_attempts_left + 1
            extra = (
                f" code {detail:#04x}"
                if status == "ERR" and detail is not None else ""
            )
            log.warning("enable attempt %d: %s%s", attempt_no, status, extra)
            self._dsp_attempts_left -= 1
            if self._dsp_attempts_left > 0:
                self._next_dsp_try = mono + DSP_ENABLE_DELAY_S
            else:
                log.error("giving up until next power-on")
                self.presence.note_activity(POST_ENABLE_GRACE_S)
                self.mic.mark_needs_unmute()

    def _poll_boom_and_mic(self) -> None:
        if self.fd is None:
            return
        down = read_boom(self.fd)
        if down is not None and down != self._last_boom:
            self._last_boom = down
            self.ipc.broadcast("boom", {"down": down})
        if self.presence.connected:
            self.mic.poll(self.fd)
            self.mic.tick(self.fd)

    def _try_open_hidraw(self, mono: float) -> None:
        if self.fd is not None or mono < self._next_rescan:
            return
        self._next_rescan = mono + POLL_S
        dev = find_device_by_pid(G935_PID)
        if dev is None:
            self.presence.reset()
            self._dsp_attempts_left = 0
            return
        try:
            self.fd = open_hidraw(dev)
            self.dev_path = dev
            if self._denied_logged:
                log.info("opened %s (ACL ready)", dev)
                self._denied_logged = False
            else:
                log.info("opened %s", dev)
            self._next_poll = 0.0
            self.ipc.broadcast("link", {"up": True, "dev_path": dev, "pid": self.pid})
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EPERM):
                if not self._denied_logged:
                    log.warning(
                        "open %s: permission denied (waiting for udev/seat ACL)", dev)
                    self._denied_logged = True
                else:
                    log.debug("open %s: permission denied", dev)
            else:
                log.warning("open %s: %s", dev, e)

    def _handle_receiver_gone(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self.dev_path = None
        self._dsp_attempts_left = 0
        self.presence.reset()
        self.mic.reset()
        self._last_boom = None
        self._last_battery = None
        self.health.mark_offline()
        self._next_rescan = time.monotonic() + 1.0
        self.ipc.broadcast("link", {"up": False})
        log.info("receiver gone, rescanning")

    def run(self) -> None:
        try:
            while not self.stop_requested:
                mono = time.monotonic()
                self._try_open_hidraw(mono)
                self.wheel.maintain(mono)

                try:
                    if self.fd is not None:
                        self._step_dsp_enable(mono)
                        if mono >= self._next_poll:
                            self._poll_battery_and_presence(mono)
                        self._tick_holds()

                    now_m = time.monotonic()
                    wait = MIC_POLL_S
                    if self.fd is not None:
                        wait = min(wait, max(0.0, self._next_poll - now_m))
                        if self._dsp_attempts_left > 0:
                            wait = min(wait, max(0.0, self._next_dsp_try - now_m))
                    else:
                        wait = min(wait, max(0.05, self._next_rescan - now_m))
                    wheel_wait = self.wheel.seconds_until_tick(now_m)
                    if wheel_wait is not None:
                        wait = min(wait, wheel_wait)
                    mic_wait = self.mic.seconds_until_tick(now_m)
                    if mic_wait is not None:
                        wait = min(wait, mic_wait)
                    if not self._ui_owns_bindings() and self._holds.pending():
                        wait = min(wait, 0.05)

                    readers = list(self.ipc.filenos())
                    if self.fd is not None:
                        readers.append(self.fd)
                    if self.wheel.fileno() is not None:
                        readers.append(self.wheel.fileno())
                    try:
                        r, _, _ = select.select(readers, [], [], max(0.0, wait))
                    except InterruptedError:
                        continue
                    except (OSError, ValueError):
                        if self.fd is not None:
                            self._handle_receiver_gone()
                        continue

                    if self.wheel.fileno() is not None and self.wheel.fileno() in r:
                        self.wheel.handle_ready()
                    self.wheel.tick()

                    if self.fd is not None and self.fd in r:
                        if not drain_idle(
                                self.fd,
                                on_non_hidpp=self._on_non_hidpp,
                                on_hidpp_notification=self._on_hidpp_notification):
                            raise OSError("device gone during idle drain")

                    ipc_ready = [fd for fd in r if fd != self.fd and fd != self.wheel.fileno()]
                    if ipc_ready:
                        self.ipc.service(ipc_ready, self._handle_rpc)

                    if self.fd is not None:
                        self._poll_boom_and_mic()
                except OSError:
                    self._handle_receiver_gone()
        finally:
            self.wheel.shutdown()
            self.ipc.close()
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
            if self.health.cur is not None:
                self.health.close_segment("daemon stop")
            elif self.health.dirty:
                self.health.save()


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

    daemon = Daemon()

    def _on_signal(_signum, _frame):
        daemon.stop_requested = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    daemon.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
