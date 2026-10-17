"""pactl wrapper + subscribe thread for the G935 control panel."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib


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
        self.available = shutil.which("pactl") is not None
        self._pending = None
        self._pending_kind = None
        self._stop = False
        self._proc = None
        if self.available:
            threading.Thread(target=self._subscribe_loop, daemon=True).start()

    @classmethod
    def _run(cls, *args):
        try:
            return subprocess.run(["pactl", *args], capture_output=True,
                                  text=True, env=cls._ENV).stdout
        except FileNotFoundError:
            return ""

    def _list(self, kind):
        devs, name = [], None
        for line in self._run("list", kind).splitlines():
            line = line.strip()
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Description:") and name:
                if "easyeffects" not in name and not name.endswith(".monitor"):
                    devs.append((name, line.split(":", 1)[1].strip()))
                name = None
        return devs

    def list_sinks(self):   return self._list("sinks")
    def list_sources(self): return self._list("sources")
    def default_sink(self):   return self._run("get-default-sink").strip()
    def default_source(self): return self._run("get-default-source").strip()
    def set_default_sink(self, name):   self._run("set-default-sink", name)
    def set_default_source(self, name): self._run("set-default-source", name)

    def get_volume(self, kind, name):
        m = re.search(r"(\d+)%", self._run(f"get-{kind}-volume", name))
        return int(m.group(1)) if m else None

    def set_volume(self, kind, name, pct):
        # %-form applies to all channels (some sinks are 8ch)
        self._run(f"set-{kind}-volume", name, f"{int(pct)}%")

    def step_default_volume(self, delta_pct):
        sign = "+" if delta_pct >= 0 else "-"
        self._run("set-sink-volume", "@DEFAULT_SINK@", f"{sign}{abs(delta_pct)}%")

    def toggle_default_mute(self):
        self._run("set-sink-mute", "@DEFAULT_SINK@", "toggle")

    def toggle_mute(self, kind, name):
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
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
