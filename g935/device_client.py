"""Unified hardware client for g935-control.

Connects to ``g935-dspd`` over its UNIX JSON-RPC socket when the daemon is
running (single hardware owner — zero hidraw contention, zero health.json
write races). Transparently falls back to a direct ``HidWorker`` when the
daemon is not running so the panel still works standalone in hardware mode.
"""
from __future__ import annotations

import logging

from g935.daemon_ipc import DaemonIpcClient, daemon_ipc_available
from g935.hidpp import HidWorker

log = logging.getLogger("g935.device_client")


class DeviceClient:
    """Facade exposing HidWorker-compatible submit() plus daemon IPC helpers."""

    def __init__(
        self,
        dev_path,
        log_cb,
        mic_cb=None,
        boom_cb=None,
        poll_boom=True,
        on_link=None,
        prefer_pid=None,
        known_pids=None,
        boom_reader=None,
        idle_add=None,
        event_cb=None,
        battery_cb=None,
        presence_cb=None,
        mode_cb=None,
        transport_cb=None,
    ):
        self._dev_path = dev_path
        self._log_cb = log_cb
        self._mic_cb = mic_cb
        self._boom_cb = boom_cb
        self._poll_boom = poll_boom
        self._on_link = on_link
        self._prefer_pid = prefer_pid
        self._known_pids = known_pids
        self._boom_reader = boom_reader
        self._idle_add = idle_add or (lambda fn, *a: fn(*a) or False)
        self._event_cb = event_cb
        self._battery_cb = battery_cb
        self._presence_cb = presence_cb
        self._mode_cb = mode_cb
        self._transport_cb = transport_cb

        self._ipc: DaemonIpcClient | None = None
        self._worker: HidWorker | None = None
        self._boom: bool | None = None
        self._stopped = False

    @property
    def via_daemon(self) -> bool:
        return self._ipc is not None and self._ipc.connected

    @property
    def transport_label(self) -> str:
        return "via g935-dspd (single owner)" if self.via_daemon else "direct hidraw"

    def start(self) -> None:
        if self._try_connect_daemon():
            return
        self._start_direct_worker()

    def _try_connect_daemon(self) -> bool:
        if self._stopped or not daemon_ipc_available():
            return False
        client = DaemonIpcClient(
            on_event=self._on_daemon_event,
            on_disconnect=self._on_daemon_disconnect,
        )
        if not client.connect(timeout=0.5):
            client.close()
            return False
        self._ipc = client
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        if self._transport_cb:
            self._idle_add(self._transport_cb, True)
        # Seed initial boom/battery state immediately from daemon.
        client.call_async("get_state", {}, cb=self._on_initial_state)
        return True

    def try_upgrade_to_daemon(self) -> bool:
        """If currently in direct mode and the daemon came up, switch to it."""
        if self.via_daemon or self._stopped:
            return False
        return self._try_connect_daemon()

    def _start_direct_worker(self) -> None:
        if self._stopped or self._worker is not None:
            return

        def wrapped_boom(down):
            self._boom = down
            if self._boom_cb:
                self._boom_cb(down)

        self._worker = HidWorker(
            self._dev_path,
            self._log_cb,
            mic_cb=self._mic_cb,
            boom_cb=wrapped_boom if self._poll_boom else None,
            poll_boom=self._poll_boom,
            on_link=self._on_link,
            prefer_pid=self._prefer_pid,
            known_pids=self._known_pids,
            boom_reader=self._boom_reader,
            idle_add=self._idle_add,
            event_cb=self._event_cb,
        )
        self._worker.start()
        if self._transport_cb:
            self._idle_add(self._transport_cb, False)

    def _on_initial_state(self, ok: bool, result) -> None:
        if not ok or not isinstance(result, dict):
            return
        boom = result.get("boom")
        if boom is not None:
            self._boom = bool(boom)
            if self._boom_cb:
                self._idle_add(self._boom_cb, self._boom)
        batt = result.get("battery")
        if isinstance(batt, dict) and self._battery_cb:
            self._idle_add(self._battery_cb, batt)

    def _on_daemon_disconnect(self) -> None:
        if self._stopped:
            return
        self._ipc = None
        self._start_direct_worker()
        if self._on_link:
            self._idle_add(self._on_link, True)

    def _on_daemon_event(self, event: str, data: dict) -> None:
        if event == "boom":
            down = data.get("down")
            if down is not None:
                self._boom = bool(down)
                if self._boom_cb:
                    self._idle_add(self._boom_cb, self._boom)
        elif event == "mic_event":
            bits = data.get("bits")
            if bits is not None and self._mic_cb:
                self._idle_add(self._mic_cb, int(bits))
        elif event == "hidpp_event":
            hx = data.get("report_hex")
            if hx and self._event_cb:
                try:
                    self._idle_add(self._event_cb, bytes.fromhex(hx))
                except ValueError:
                    pass
        elif event == "battery":
            if self._battery_cb:
                self._idle_add(self._battery_cb, data)
        elif event == "presence":
            if self._presence_cb:
                self._idle_add(self._presence_cb, bool(data.get("connected")))
        elif event == "link":
            if self._on_link:
                self._idle_add(self._on_link, bool(data.get("up")))
        elif event == "mode":
            if self._mode_cb and data.get("mode"):
                self._idle_add(self._mode_cb, str(data["mode"]))

    def submit(self, hx: str, done_cb=None) -> None:
        clean = hx.replace(" ", "").lower()
        if self.via_daemon:
            def _on_reply(ok: bool, result):
                if not ok or not isinstance(result, dict):
                    status, detail = "GONE", None
                else:
                    status = str(result.get("status", "TIMEOUT"))
                    if status == "ACK" and result.get("reply_hex"):
                        detail = bytes.fromhex(result["reply_hex"])
                    elif status == "ERR":
                        detail = result.get("err_code")
                    else:
                        detail = None
                self._idle_add(self._log_cb, clean, status, detail)
                if done_cb:
                    self._idle_add(done_cb, status, detail)

            self._ipc.call_async("hidpp", {"hx": clean}, cb=_on_reply)
            return
        if self._worker is not None:
            self._worker.submit(clean, done_cb)
        elif done_cb:
            self._idle_add(done_cb, "GONE", None)

    def pause_wheel(self, paused: bool) -> bool:
        if self.via_daemon:
            ok, _res = self._ipc.request("pause_wheel", {"paused": bool(paused)}, timeout=1.0)
            return ok
        return False

    def update_health_settings(self, settings: dict) -> bool:
        if self.via_daemon:
            ok, _res = self._ipc.request(
                "update_health_settings", {"settings": dict(settings)}, timeout=1.5)
            return ok
        return False

    def stop(self) -> None:
        self._stopped = True
        if self._ipc is not None:
            self._ipc.close()
            self._ipc = None
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
