"""Persistent native libpulse client over ctypes (zero external dependencies).

Talks directly to PipeWire's PulseAudio socket (or PulseAudio) over a single
long-lived ``pa_context`` + ``pa_threaded_mainloop`` instead of forking
``pactl`` subprocesses on every volume step or mixer refresh.

Falls back gracefully (``available == False``) when ``libpulse.so.0`` is
unavailable so callers can fall back to ``pactl``.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("g935.pulse")

PA_VOLUME_NORM = 0x10000
PA_VOLUME_MAX = 0x7FFFFFFF  # uint32 max / 2 (PulseAudio's PA_VOLUME_MAX)

# pa_context_state_t
PA_CONTEXT_UNCONNECTED = 0
PA_CONTEXT_CONNECTING = 1
PA_CONTEXT_AUTHORIZING = 2
PA_CONTEXT_SETTING_NAME = 3
PA_CONTEXT_READY = 4
PA_CONTEXT_FAILED = 5
PA_CONTEXT_TERMINATED = 6

# pa_subscription_mask_t
PA_SUBSCRIPTION_MASK_SINK = 0x0001
PA_SUBSCRIPTION_MASK_SOURCE = 0x0002
PA_SUBSCRIPTION_MASK_SERVER = 0x0080
PA_SUBSCRIPTION_MASK_CARD = 0x0200
PA_SUBSCRIPTION_MASK_WATCH = (
    PA_SUBSCRIPTION_MASK_SINK
    | PA_SUBSCRIPTION_MASK_SOURCE
    | PA_SUBSCRIPTION_MASK_SERVER
    | PA_SUBSCRIPTION_MASK_CARD
)

# pa_subscription_event_type_t
PA_SUBSCRIPTION_EVENT_FACILITY_MASK = 0x000F
PA_SUBSCRIPTION_EVENT_TYPE_MASK = 0x0030
PA_SUBSCRIPTION_EVENT_SINK = 0x0000
PA_SUBSCRIPTION_EVENT_SOURCE = 0x0001
PA_SUBSCRIPTION_EVENT_SERVER = 0x0007
PA_SUBSCRIPTION_EVENT_CARD = 0x0009
PA_SUBSCRIPTION_EVENT_NEW = 0x0000
PA_SUBSCRIPTION_EVENT_CHANGE = 0x0010
PA_SUBSCRIPTION_EVENT_REMOVE = 0x0020


class _pa_sample_spec(ctypes.Structure):
    _fields_ = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


class _pa_channel_map(ctypes.Structure):
    _fields_ = [
        ("channels", ctypes.c_uint8),
        ("map", ctypes.c_int * 32),
    ]


class _pa_cvolume(ctypes.Structure):
    _fields_ = [
        ("channels", ctypes.c_uint8),
        ("values", ctypes.c_uint32 * 32),
    ]


class _pa_device_info(ctypes.Structure):
    """Prefix shared by both pa_sink_info and pa_source_info (ABI-stable)."""

    _fields_ = [
        ("name", ctypes.c_char_p),
        ("index", ctypes.c_uint32),
        ("description", ctypes.c_char_p),
        ("sample_spec", _pa_sample_spec),
        ("channel_map", _pa_channel_map),
        ("owner_module", ctypes.c_uint32),
        ("volume", _pa_cvolume),
        ("mute", ctypes.c_int),
    ]


class _pa_server_info(ctypes.Structure):
    """Prefix of pa_server_info (ABI-stable)."""

    _fields_ = [
        ("user_name", ctypes.c_char_p),
        ("host_name", ctypes.c_char_p),
        ("server_version", ctypes.c_char_p),
        ("server_name", ctypes.c_char_p),
        ("sample_spec", _pa_sample_spec),
        ("default_sink_name", ctypes.c_char_p),
        ("default_source_name", ctypes.c_char_p),
    ]


_STATE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_SUCCESS_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
_DEV_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.POINTER(_pa_device_info), ctypes.c_int, ctypes.c_void_p
)
_SERVER_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.POINTER(_pa_server_info), ctypes.c_void_p
)
_SUB_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p
)


@dataclass(frozen=True)
class PulseDevice:
    name: str
    description: str
    channels: int
    volume_pct: int
    mute: bool


def pct_to_pa_volume(pct: int | float) -> int:
    val = int(round(max(0.0, float(pct)) * PA_VOLUME_NORM / 100.0))
    return max(0, min(PA_VOLUME_MAX, val))


def pa_cvolume_to_pct(cvol: _pa_cvolume) -> int:
    n = min(int(cvol.channels), 32)
    if n <= 0:
        return 0
    avg = sum(int(cvol.values[i]) for i in range(n)) / n
    return int(round(avg * 100.0 / PA_VOLUME_NORM))


def classify_pa_event(event_type: int) -> str | None:
    """Map a libpulse subscription event bitmask to 'devices' | 'levels' | None."""
    facility = event_type & PA_SUBSCRIPTION_EVENT_FACILITY_MASK
    action = event_type & PA_SUBSCRIPTION_EVENT_TYPE_MASK
    if facility == PA_SUBSCRIPTION_EVENT_SERVER:
        return "devices" if action == PA_SUBSCRIPTION_EVENT_CHANGE else None
    if facility == PA_SUBSCRIPTION_EVENT_CARD:
        if action in (
            PA_SUBSCRIPTION_EVENT_NEW,
            PA_SUBSCRIPTION_EVENT_REMOVE,
            PA_SUBSCRIPTION_EVENT_CHANGE,
        ):
            return "devices"
        return None
    if facility in (PA_SUBSCRIPTION_EVENT_SINK, PA_SUBSCRIPTION_EVENT_SOURCE):
        if action in (PA_SUBSCRIPTION_EVENT_NEW, PA_SUBSCRIPTION_EVENT_REMOVE):
            return "devices"
        if action == PA_SUBSCRIPTION_EVENT_CHANGE:
            return "levels"
    return None


def _load_libpulse():
    name = ctypes.util.find_library("pulse") or "libpulse.so.0"
    try:
        lib = ctypes.CDLL(name)
    except OSError:
        return None

    lib.pa_threaded_mainloop_new.restype = ctypes.c_void_p
    lib.pa_threaded_mainloop_get_api.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_get_api.restype = ctypes.c_void_p
    lib.pa_threaded_mainloop_start.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_start.restype = ctypes.c_int
    lib.pa_threaded_mainloop_stop.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_free.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_lock.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_unlock.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_signal.argtypes = [ctypes.c_void_p, ctypes.c_int]

    lib.pa_context_new.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.pa_context_new.restype = ctypes.c_void_p
    lib.pa_context_connect.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p
    ]
    lib.pa_context_connect.restype = ctypes.c_int
    lib.pa_context_disconnect.argtypes = [ctypes.c_void_p]
    lib.pa_context_unref.argtypes = [ctypes.c_void_p]
    lib.pa_context_get_state.argtypes = [ctypes.c_void_p]
    lib.pa_context_get_state.restype = ctypes.c_int
    lib.pa_context_set_state_callback.argtypes = [
        ctypes.c_void_p, _STATE_CB, ctypes.c_void_p
    ]
    lib.pa_context_set_subscribe_callback.argtypes = [
        ctypes.c_void_p, _SUB_CB, ctypes.c_void_p
    ]
    lib.pa_context_subscribe.argtypes = [
        ctypes.c_void_p, ctypes.c_int, _SUCCESS_CB, ctypes.c_void_p
    ]
    lib.pa_context_subscribe.restype = ctypes.c_void_p

    lib.pa_context_get_sink_info_list.argtypes = [
        ctypes.c_void_p, _DEV_CB, ctypes.c_void_p
    ]
    lib.pa_context_get_sink_info_list.restype = ctypes.c_void_p
    lib.pa_context_get_sink_info_by_name.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, _DEV_CB, ctypes.c_void_p
    ]
    lib.pa_context_get_sink_info_by_name.restype = ctypes.c_void_p
    lib.pa_context_get_source_info_list.argtypes = [
        ctypes.c_void_p, _DEV_CB, ctypes.c_void_p
    ]
    lib.pa_context_get_source_info_list.restype = ctypes.c_void_p
    lib.pa_context_get_source_info_by_name.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, _DEV_CB, ctypes.c_void_p
    ]
    lib.pa_context_get_source_info_by_name.restype = ctypes.c_void_p
    lib.pa_context_get_server_info.argtypes = [
        ctypes.c_void_p, _SERVER_CB, ctypes.c_void_p
    ]
    lib.pa_context_get_server_info.restype = ctypes.c_void_p

    lib.pa_context_set_sink_volume_by_name.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(_pa_cvolume),
        _SUCCESS_CB, ctypes.c_void_p,
    ]
    lib.pa_context_set_sink_volume_by_name.restype = ctypes.c_void_p
    lib.pa_context_set_source_volume_by_name.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(_pa_cvolume),
        _SUCCESS_CB, ctypes.c_void_p,
    ]
    lib.pa_context_set_source_volume_by_name.restype = ctypes.c_void_p

    lib.pa_context_set_sink_mute_by_name.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
        _SUCCESS_CB, ctypes.c_void_p,
    ]
    lib.pa_context_set_sink_mute_by_name.restype = ctypes.c_void_p
    lib.pa_context_set_source_mute_by_name.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
        _SUCCESS_CB, ctypes.c_void_p,
    ]
    lib.pa_context_set_source_mute_by_name.restype = ctypes.c_void_p

    lib.pa_context_set_default_sink.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, _SUCCESS_CB, ctypes.c_void_p
    ]
    lib.pa_context_set_default_sink.restype = ctypes.c_void_p
    lib.pa_context_set_default_source.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, _SUCCESS_CB, ctypes.c_void_p
    ]
    lib.pa_context_set_default_source.restype = ctypes.c_void_p

    lib.pa_operation_unref.argtypes = [ctypes.c_void_p]
    lib.pa_operation_cancel.argtypes = [ctypes.c_void_p]
    return lib


_LIBPULSE = _load_libpulse()


def libpulse_available() -> bool:
    return _LIBPULSE is not None


class PulseClient:
    """Thread-safe persistent libpulse connection with automatic reconnect."""

    def __init__(self, app_name: str = "g935-linux", on_event=None,
                 timeout: float = 1.5):
        self._lib = _LIBPULSE
        self._app_name = app_name.encode("utf-8")
        self._on_event = on_event
        self._timeout = float(timeout)
        self._op_lock = threading.RLock()
        self._ml = None
        self._ctx = None
        self._state = PA_CONTEXT_UNCONNECTED
        self._ready_ev = threading.Event()
        self._closed = False
        self._channels: dict[tuple[str, str], int] = {}
        self._last_reconnect = 0.0
        # Keep C callback references alive for the context lifetime.
        self._state_cb = _STATE_CB(self._handle_state)
        self._sub_cb = _SUB_CB(self._handle_subscribe)
        if self._lib is not None:
            self._connect()

    @property
    def available(self) -> bool:
        return self._lib is not None and not self._closed

    @property
    def connected(self) -> bool:
        return self._ctx is not None and self._state == PA_CONTEXT_READY

    def _handle_state(self, ctx, _userdata):
        state = int(self._lib.pa_context_get_state(ctx))
        self._state = state
        if state in (PA_CONTEXT_READY, PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED):
            self._ready_ev.set()

    def _handle_subscribe(self, _ctx, event_type, _idx, _userdata):
        cb = self._on_event
        if cb is None:
            return
        kind = classify_pa_event(int(event_type))
        if kind:
            try:
                cb(kind)
            except Exception:
                pass

    def _teardown_unlocked(self):
        lib = self._lib
        if lib is None:
            return
        ml, ctx = self._ml, self._ctx
        self._ml = None
        self._ctx = None
        self._state = PA_CONTEXT_UNCONNECTED
        if ml is not None:
            if ctx is not None:
                lib.pa_threaded_mainloop_lock(ml)
                try:
                    lib.pa_context_set_state_callback(ctx, _STATE_CB(0), None)
                    lib.pa_context_set_subscribe_callback(ctx, _SUB_CB(0), None)
                    lib.pa_context_disconnect(ctx)
                    lib.pa_context_unref(ctx)
                finally:
                    lib.pa_threaded_mainloop_unlock(ml)
            lib.pa_threaded_mainloop_stop(ml)
            lib.pa_threaded_mainloop_free(ml)

    def _connect(self) -> bool:
        lib = self._lib
        if lib is None or self._closed:
            return False
        with self._op_lock:
            self._teardown_unlocked()
            self._ready_ev.clear()
            ml = lib.pa_threaded_mainloop_new()
            if not ml:
                return False
            api = lib.pa_threaded_mainloop_get_api(ml)
            ctx = lib.pa_context_new(api, self._app_name)
            if not ctx:
                lib.pa_threaded_mainloop_free(ml)
                return False
            self._ml = ml
            self._ctx = ctx
            lib.pa_context_set_state_callback(ctx, self._state_cb, None)
            if self._on_event is not None:
                lib.pa_context_set_subscribe_callback(ctx, self._sub_cb, None)
            lib.pa_threaded_mainloop_lock(ml)
            started = lib.pa_threaded_mainloop_start(ml) == 0
            if not started:
                lib.pa_threaded_mainloop_unlock(ml)
                self._teardown_unlocked()
                return False
            rc = lib.pa_context_connect(ctx, None, 0, None)
            lib.pa_threaded_mainloop_unlock(ml)
            if rc < 0:
                self._teardown_unlocked()
                return False
            self._ready_ev.wait(self._timeout)
            if self._state != PA_CONTEXT_READY:
                self._teardown_unlocked()
                return False
            if self._on_event is not None:
                self._subscribe_unlocked()
            return True

    def _subscribe_unlocked(self):
        done = threading.Event()

        @_SUCCESS_CB
        def cb(_ctx, _ok, _ud):
            done.set()

        lib = self._lib
        lib.pa_threaded_mainloop_lock(self._ml)
        op = lib.pa_context_subscribe(
            self._ctx, PA_SUBSCRIPTION_MASK_WATCH, cb, None)
        lib.pa_threaded_mainloop_unlock(self._ml)
        if op:
            done.wait(self._timeout)
            lib.pa_threaded_mainloop_lock(self._ml)
            lib.pa_operation_unref(op)
            lib.pa_threaded_mainloop_unlock(self._ml)

    def _ensure_connected(self) -> bool:
        if self._closed or self._lib is None:
            return False
        if self.connected:
            return True
        now = time.monotonic()
        if now - self._last_reconnect < 1.0:
            return False
        self._last_reconnect = now
        return self._connect()

    def _run_op(self, start_fn, done_ev: threading.Event) -> bool:
        """Run a libpulse async op and wait up to self._timeout."""
        lib = self._lib
        ml = self._ml
        if lib is None or ml is None or self._ctx is None:
            return False
        lib.pa_threaded_mainloop_lock(ml)
        try:
            if self._state != PA_CONTEXT_READY:
                return False
            op = start_fn(self._ctx)
        finally:
            lib.pa_threaded_mainloop_unlock(ml)
        if not op:
            return False
        finished = done_ev.wait(self._timeout)
        lib.pa_threaded_mainloop_lock(ml)
        try:
            if not finished:
                lib.pa_operation_cancel(op)
            lib.pa_operation_unref(op)
        finally:
            lib.pa_threaded_mainloop_unlock(ml)
        return finished

    def _list_devices(self, kind: str) -> tuple[bool, list[PulseDevice]]:
        with self._op_lock:
            if not self._ensure_connected():
                return False, []
            done = threading.Event()
            items: list[PulseDevice] = []

            @_DEV_CB
            def cb(_ctx, info_ptr, eol, _ud):
                if eol:
                    done.set()
                    return
                if not info_ptr:
                    return
                info = info_ptr.contents
                name = (info.name or b"").decode("utf-8", "replace")
                desc = (info.description or b"").decode("utf-8", "replace")
                ch = max(1, min(int(info.volume.channels), 32))
                pct = pa_cvolume_to_pct(info.volume)
                self._channels[(kind, name)] = ch
                items.append(PulseDevice(
                    name=name,
                    description=desc,
                    channels=ch,
                    volume_pct=pct,
                    mute=bool(info.mute),
                ))

            fn = (
                self._lib.pa_context_get_sink_info_list
                if kind == "sink"
                else self._lib.pa_context_get_source_info_list
            )
            ok = self._run_op(lambda ctx: fn(ctx, cb, None), done)
            return ok, items

    def list_sinks(self) -> tuple[bool, list[PulseDevice]]:
        return self._list_devices("sink")

    def list_sources(self) -> tuple[bool, list[PulseDevice]]:
        return self._list_devices("source")

    def get_device(self, kind: str, name: str) -> PulseDevice | None:
        with self._op_lock:
            if not self._ensure_connected():
                return None
            if name in ("@DEFAULT_SINK@", "@DEFAULT_SOURCE@"):
                dsink, dsrc = self.default_devices()
                name = dsink if name == "@DEFAULT_SINK@" else dsrc
                if not name:
                    return None
            done = threading.Event()
            result: list[PulseDevice] = []

            @_DEV_CB
            def cb(_ctx, info_ptr, eol, _ud):
                if eol:
                    done.set()
                    return
                if not info_ptr:
                    return
                info = info_ptr.contents
                dname = (info.name or b"").decode("utf-8", "replace")
                desc = (info.description or b"").decode("utf-8", "replace")
                ch = max(1, min(int(info.volume.channels), 32))
                pct = pa_cvolume_to_pct(info.volume)
                self._channels[(kind, dname)] = ch
                result.append(PulseDevice(
                    name=dname, description=desc,
                    channels=ch, volume_pct=pct, mute=bool(info.mute),
                ))

            fn = (
                self._lib.pa_context_get_sink_info_by_name
                if kind == "sink"
                else self._lib.pa_context_get_source_info_by_name
            )
            encoded = name.encode("utf-8")
            ok = self._run_op(lambda ctx: fn(ctx, encoded, cb, None), done)
            return result[0] if (ok and result) else None

    def default_devices(self) -> tuple[str, str]:
        with self._op_lock:
            if not self._ensure_connected():
                return "", ""
            done = threading.Event()
            out = ["", ""]

            @_SERVER_CB
            def cb(_ctx, info_ptr, _ud):
                if info_ptr:
                    info = info_ptr.contents
                    out[0] = (info.default_sink_name or b"").decode("utf-8", "replace")
                    out[1] = (info.default_source_name or b"").decode("utf-8", "replace")
                done.set()

            self._run_op(
                lambda ctx: self._lib.pa_context_get_server_info(ctx, cb, None),
                done,
            )
            return out[0], out[1]

    def set_volume(self, kind: str, name: str, pct: int | float) -> bool:
        with self._op_lock:
            if not self._ensure_connected():
                return False
            if name in ("@DEFAULT_SINK@", "@DEFAULT_SOURCE@"):
                dsink, dsrc = self.default_devices()
                name = dsink if name == "@DEFAULT_SINK@" else dsrc
                if not name:
                    return False
            ch = self._channels.get((kind, name))
            if ch is None:
                dev = self.get_device(kind, name)
                ch = dev.channels if dev is not None else 2
            cvol = _pa_cvolume()
            cvol.channels = ch
            raw = pct_to_pa_volume(pct)
            for i in range(ch):
                cvol.values[i] = raw
            done = threading.Event()
            success = [False]

            @_SUCCESS_CB
            def cb(_ctx, ok_int, _ud):
                success[0] = bool(ok_int)
                done.set()

            fn = (
                self._lib.pa_context_set_sink_volume_by_name
                if kind == "sink"
                else self._lib.pa_context_set_source_volume_by_name
            )
            encoded = name.encode("utf-8")
            ran = self._run_op(
                lambda ctx: fn(ctx, encoded, ctypes.byref(cvol), cb, None),
                done,
            )
            return ran and success[0]

    def set_mute(self, kind: str, name: str, mute: bool) -> bool:
        with self._op_lock:
            if not self._ensure_connected():
                return False
            if name in ("@DEFAULT_SINK@", "@DEFAULT_SOURCE@"):
                dsink, dsrc = self.default_devices()
                name = dsink if name == "@DEFAULT_SINK@" else dsrc
                if not name:
                    return False
            done = threading.Event()
            success = [False]

            @_SUCCESS_CB
            def cb(_ctx, ok_int, _ud):
                success[0] = bool(ok_int)
                done.set()

            fn = (
                self._lib.pa_context_set_sink_mute_by_name
                if kind == "sink"
                else self._lib.pa_context_set_source_mute_by_name
            )
            encoded = name.encode("utf-8")
            ran = self._run_op(
                lambda ctx: fn(ctx, encoded, 1 if mute else 0, cb, None),
                done,
            )
            return ran and success[0]

    def toggle_mute(self, kind: str, name: str) -> bool | None:
        """Toggle mute and return the new mute state, or None on error."""
        dev = self.get_device(kind, name)
        if dev is None:
            return None
        new_mute = not dev.mute
        if self.set_mute(kind, dev.name, new_mute):
            return new_mute
        return None

    def set_default(self, kind: str, name: str) -> bool:
        with self._op_lock:
            if not self._ensure_connected():
                return False
            done = threading.Event()
            success = [False]

            @_SUCCESS_CB
            def cb(_ctx, ok_int, _ud):
                success[0] = bool(ok_int)
                done.set()

            fn = (
                self._lib.pa_context_set_default_sink
                if kind == "sink"
                else self._lib.pa_context_set_default_source
            )
            encoded = name.encode("utf-8")
            ran = self._run_op(lambda ctx: fn(ctx, encoded, cb, None), done)
            return ran and success[0]

    def close(self) -> None:
        self._closed = True
        with self._op_lock:
            self._teardown_unlocked()
