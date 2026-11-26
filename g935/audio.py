"""pactl wrapper + subscribe thread for the G935 control panel."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib
except ImportError:  # headless / pure-stdlib test environments
    GLib = None

from g935.pulse import PulseClient, libpulse_available


def classify_pactl_event(line: str) -> str | None:
    """Classify one ``pactl subscribe`` line.

    ``devices`` means a sink, source, card, or the default device appeared,
    disappeared, or changed identity. ``levels`` means only a volume or mute
    on an existing sink/source. Stream events (``sink-input``,
    ``source-output``) are ignored: they fire for the whole session and are
    not the headset control.
    """
    match = re.match(
        r"event '(\w+)' on ([a-z0-9-]+)", line.strip().lower())
    if not match:
        return None
    action, obj = match.group(1), match.group(2)
    if obj == "server":
        return "devices" if action == "change" else None
    if obj == "card" and action in ("new", "remove", "change"):
        return "devices"
    if obj in ("sink", "source"):
        if action in ("new", "remove"):
            return "devices"
        if action == "change":
            return "levels"
    return None


class AudioControl:
    """pactl wrapper + subscribe thread. Never lists easyeffects_* devices
    (g935-ee-unity owns them and would fight any change) and never lists
    .monitor sources. Change notifications land debounced on the main loop.

    The callback receives "devices" or "levels". A device event inside the
    debounce window wins over a volume event.
    """

    # pactl output is localized - force C so "Name:"/"Description:" and the
    # subscribe event wording are parseable on any system language
    _ENV = {**os.environ, "LC_ALL": "C"}

    def __init__(self, on_change):
        self.on_change = on_change
        self._pending = None
        self._pending_kind = None
        self._stop = False
        self._proc = None
        self._pulse = None
        if libpulse_available():
            client = PulseClient(
                app_name="g935-control",
                on_event=self._on_pulse_event,
            )
            if client.connected:
                self._pulse = client
            else:
                client.close()
        self.available = (
            self._pulse is not None or shutil.which("pactl") is not None
        )
        if self._pulse is None and self.available:
            threading.Thread(target=self._subscribe_loop, daemon=True).start()

    def _on_pulse_event(self, kind: str):
        if not self._stop:
            GLib.idle_add(self._poke, kind)

    @classmethod
    def _run_checked(cls, *args) -> tuple[bool, str]:
        try:
            res = subprocess.run(
                ["pactl", *args], capture_output=True, text=True,
                timeout=2, env=cls._ENV)
            return res.returncode == 0, res.stdout or ""
        except (FileNotFoundError, subprocess.SubprocessError):
            return False, ""

    @classmethod
    def _run(cls, *args):
        _ok, out = cls._run_checked(*args)
        return out

    @staticmethod
    def _filter_visible(name: str) -> bool:
        return "easyeffects" not in name and not name.endswith(".monitor")

    def _list_checked(self, kind) -> tuple[bool, list[tuple[str, str]]]:
        if self._pulse is not None:
            ok, devs = (
                self._pulse.list_sinks()
                if kind == "sinks" else self._pulse.list_sources()
            )
            if ok:
                return True, [
                    (d.name, d.description) for d in devs
                    if self._filter_visible(d.name)
                ]
        ok, out = self._run_checked("list", kind)
        if not ok:
            return False, []
        devs, name = [], None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Description:") and name:
                if self._filter_visible(name):
                    devs.append((name, line.split(":", 1)[1].strip()))
                name = None
        return True, devs

    def _list(self, kind):
        _ok, devs = self._list_checked(kind)
        return devs

    def list_sinks(self):           return self._list("sinks")
    def list_sinks_checked(self):   return self._list_checked("sinks")
    def list_sources(self):         return self._list("sources")

    def default_sink(self):
        if self._pulse is not None:
            dsink, _dsrc = self._pulse.default_devices()
            if dsink:
                return dsink
        return self._run("get-default-sink").strip()

    def default_source(self):
        if self._pulse is not None:
            _dsink, dsrc = self._pulse.default_devices()
            if dsrc:
                return dsrc
        return self._run("get-default-source").strip()

    def set_default_sink(self, name):
        if self._pulse is not None and self._pulse.set_default("sink", name):
            return
        self._run("set-default-sink", name)

    def set_default_source(self, name):
        if self._pulse is not None and self._pulse.set_default("source", name):
            return
        self._run("set-default-source", name)

    def get_volume(self, kind, name):
        if self._pulse is not None:
            dev = self._pulse.get_device(kind, name)
            if dev is not None:
                return dev.volume_pct
        vals = [int(v) for v in re.findall(
            r"(\d+)%", self._run(f"get-{kind}-volume", name))]
        return round(sum(vals) / len(vals)) if vals else None

    def get_mute(self, kind, name) -> bool | None:
        if self._pulse is not None:
            dev = self._pulse.get_device(kind, name)
            if dev is not None:
                return dev.mute
        out = self._run(f"get-{kind}-mute", name).lower()
        if "yes" in out:
            return True
        if "no" in out:
            return False
        return None

    def set_volume(self, kind, name, pct):
        if self._pulse is not None and self._pulse.set_volume(kind, name, pct):
            return
        # %-form applies to all channels (some sinks are 8ch)
        self._run(f"set-{kind}-volume", name, f"{int(pct)}%")

    def step_default_volume(self, delta_pct):
        sign = "+" if delta_pct >= 0 else "-"
        self._run("set-sink-volume", "@DEFAULT_SINK@", f"{sign}{abs(delta_pct)}%")

    def toggle_default_mute(self):
        if self._pulse is not None and self._pulse.toggle_mute("sink", "@DEFAULT_SINK@") is not None:
            return
        self._run("set-sink-mute", "@DEFAULT_SINK@", "toggle")

    def toggle_mute(self, kind, name):
        if self._pulse is not None and self._pulse.toggle_mute(kind, name) is not None:
            return
        self._run(f"set-{kind}-mute", name, "toggle")

    def _subscribe_loop(self):
        delay = 0.5
        while not self._stop:
            try:
                self._proc = subprocess.Popen(["pactl", "subscribe"],
                                              stdout=subprocess.PIPE, text=True,
                                              bufsize=1, env=self._ENV)
            except FileNotFoundError:
                self.available = False
                return
            started = time.time()
            for line in self._proc.stdout:
                if self._stop:
                    return
                kind = classify_pactl_event(line)
                if kind:
                    GLib.idle_add(self._poke, kind)
            if not self._stop:
                # a subscription that dies instantly means no sound server:
                # back off instead of respawning pactl twice a second forever
                delay = 0.5 if time.time() - started > 5 else min(delay * 2, 30)
                time.sleep(delay)

    def _poke(self, kind="devices"):
        # Device add/remove must not be lost behind a volume-only refresh.
        if self._pending_kind != "devices":
            self._pending_kind = kind
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(300, self._fire)
        return False

    def _fire(self):
        kind = self._pending_kind or "devices"
        self._pending = None
        self._pending_kind = None
        self.on_change(kind)
        return False

    def stop(self):
        self._stop = True
        if self._pulse is not None:
            try:
                self._pulse.close()
            except Exception:
                pass
            self._pulse = None
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
