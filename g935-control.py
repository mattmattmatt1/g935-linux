#!/usr/bin/env python3
"""
g935-control.py - GTK control panel for the Logitech G935 (HID++ over hidraw).

Feature map (from the headset IFeatureSet, 2026-07-20):
  idx 04 = 0x8070 COLOR_LED_EFFECTS   2 zones: 0=logo, 1=front strip
  idx 05 = 0x8010 G-KEYS / "G HUB MODE"  052b 01/00
  idx 06 = 0x8310 EQUALIZER           10 bands 32Hz-16kHz, +/-12 dB
  idx 07 = 0x8300 SIDETONE            0-100
  idx 08 = 0x1f20 ADC/BATTERY         voltage mV + flags

Mode is persisted in ~/.config/g935/mode ("ghub"/"hardware"). While g935-dspd
is running it owns power-on DSP enable and boom-mic handling; the GUI drives
EQ, lighting, sidetone, and the mode toggle. Default mode is hardware (stock)
until the user opts in.

Needs r/w on the hidraw node. Run: python3 g935-control.py
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

# Allow `python3 g935-control.py` from a git checkout without install.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Gio, Pango

from g935.battery import (
    HEALTH_DEFAULTS, HealthTracker, batt_percent, batt_state,
    merge_discharge_sessions, session_full_runtime_h,
)
from g935.charts import (
    ChargeHistoryChart, DrainProfileChart, ExpectActualChart,
    HealthGaugeChart, SessionRuntimeChart,
    build_expected_overlay, build_history_points,
)
from g935.daemon_status import daemon_running
from g935.hidpp import ERROR_CODES, HidWorker, find_headset, open_hidraw
from g935.mic import (
    BOOM_UP, BOOM_DOWN, BUTTON, read_boom, read_host_mic_switch, set_host_mic_switch,
)
from g935.mode import load_mode, save_mode
from g935.paths import config_dir, ensure_config_dir, runtime_dir

# Ubuntu/Debian/Fedora ship the Ayatana fork; some distros still ship the
# original namespace. Either works; without both we run windowed only.
AppIndicator = None
for _ns in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(_ns, "0.1")
        AppIndicator = getattr(__import__("gi.repository", fromlist=[_ns]), _ns)
        break
    except (ValueError, ImportError, AttributeError):
        continue


def sni_watcher_present():
    """True if a StatusNotifier tray host is on the session bus."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 500, None)
        return bool(res.unpack()[0])
    except Exception:
        return False


# On Wayland the taskbar icon comes from matching the window\'s app-id against a
# .desktop file name — so the program name must equal "g935-control" (.desktop).
GLib.set_prgname("g935-control")

# HID++ 2.0 features this app knows how to drive. Indices are discovered live.
FEATURES = [
    ("devinfo",  0x0003),
    ("gkeys",    0x8010),   # + the G935 "G HUB mode" side effect
    ("lighting", 0x8070),
    ("eq",       0x8310),
    ("sidetone", 0x8300),
    ("battery",  0x1f20),
]

UI_FILE = os.path.join(config_dir(), "ui.json")
ALSA_USBID = "046d:0a87"    # reassigned from the device profile in main()
MIC_SWITCH_NAME = "Mic Capture Switch"


def _read_host_mic():
    return read_host_mic_switch(ALSA_USBID, MIC_SWITCH_NAME)


def _set_host_mic(on):
    set_host_mic_switch(on, ALSA_USBID, MIC_SWITCH_NAME)


EQ_BANDS = ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
EQ_PRESETS = {
    "Flat":          [0] * 10,
    "G HUB baseline": [0, 0, 0, -1, 3, 4, 3, 3, 1, 0],
    "Bass boost":    [6, 5, 3, 1, 0, 0, 0, 0, 0, 0],
    "V-shape":       [4, 3, 1, -1, -2, -2, -1, 2, 4, 5],
}

G935_CONNECT_BURST = [
    ("devinfo", 1, ""), ("battery", 0, ""), ("lighting", 0, ""),
    ("lighting", 1, "00"),
    ("lighting", 2, "0000"), ("lighting", 2, "0001"),
    ("lighting", 2, "0002"), ("lighting", 2, "0003"),
    ("lighting", 1, "01"),
    ("lighting", 2, "0100"), ("lighting", 2, "0101"),
    ("lighting", 2, "0102"), ("lighting", 2, "0103"),
    ("gkeys", 2, "01"), ("lighting", 8, "0101"),
    ("sidetone", 0, ""), ("sidetone", 1, "64"),
    ("lighting", 3, "010200b4ff1388000700"), ("lighting", 3, "000200b4ff1388000800"),
    ("lighting", 4, "0001"), ("battery", 1, ""), ("eq", 2, ""),
]

# ---------- device profiles ----------
DEVICE_PROFILES = {
    0x0A87: {
        "name": "G935",
        "zones": ["Logo", "Strip"],
        "zone_brt": {0: 0x08, 1: 0x07},
        "eq_bands": EQ_BANDS,
        "eq_presets": EQ_PRESETS,
        "health_defaults": HEALTH_DEFAULTS,
        "has_boom_mic": True,
        "alsa_usbid": "046d:0a87",
        "mic_switch_name": "Mic Capture Switch",
        "connect_burst": G935_CONNECT_BURST,
    },
}
GENERIC_PROFILE = {
    "name": None,
    "zones": None,
    "zone_brt": {},
    "eq_bands": None,
    "eq_presets": {"Flat": None},
    "health_defaults": HEALTH_DEFAULTS,
    "has_boom_mic": False,
    "alsa_usbid": None,
    "mic_switch_name": None,
    "connect_burst": [],
}


UI_DEFAULTS = {"hidden_sinks": [], "hidden_sources": [], "lighting": {}}

class AppSettings:
    """ui.json: audio-device visibility + saved lighting state. Loaded once,
    saved atomically on change (same pattern as HealthTracker)."""

    def __init__(self):
        try:
            with open(UI_FILE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.data = {**UI_DEFAULTS, **{k: data[k] for k in UI_DEFAULTS if k in data}}

    def save(self):
        ensure_config_dir()
        tmp = UI_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 1, **self.data}, f)
        os.replace(tmp, UI_FILE)


class AudioControl:
    """pactl wrapper + subscribe thread. Never lists easyeffects_* devices
    (g935-ee-unity owns them and would fight any change) and never lists
    .monitor sources. Change notifications land debounced on the main loop."""

    # pactl output is localized - force C so "Name:"/"Description:" and the
    # subscribe event wording are parseable on any system language
    _ENV = {**os.environ, "LC_ALL": "C"}

    def __init__(self, on_change):
        self.on_change = on_change
        self.available = shutil.which("pactl") is not None
        self._pending = None
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
                l = line.lower()
                if ("sink" in l or "source" in l or "server" in l) and \
                        ("new" in l or "remove" in l or "change" in l):
                    GLib.idle_add(self._poke)
            if not self._stop:
                # a subscription that dies instantly means no sound server:
                # back off instead of respawning pactl twice a second forever
                delay = 0.5 if time.time() - started > 5 else min(delay * 2, 30)
                time.sleep(delay)

    def _poke(self):
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(300, self._fire)
        return False

    def _fire(self):
        self._pending = None
        self.on_change()
        return False

    def stop(self):
        self._stop = True
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


class App(Gtk.Window):
    def __init__(self, dev_path, pid, hid_name):
        self.profile = {**GENERIC_PROFILE,
                        **DEVICE_PROFILES.get(pid, {"name": hid_name})}
        super().__init__(title=f"{self.profile['name']} Control")
        self.set_default_size(720, 860)
        self.set_icon_name("audio-headset")
        self.connected = None   # None = unknown, True/False after first battery poll
        self.pid = pid

        self.settings = AppSettings()
        self.features = {}        # feature attr -> discovered index
        self.discovered = False
        self._discovering = False
        self.section_frames = {}  # feature attr -> widget to show/hide
        self.mixer_win = None
        self.indicator = None
        self.tray_batt_item = None
        self._vol_pending = {}    # (kind, name) -> debounce source id
        self._vol_containers = []
        self._daemon_poll = None

        boom = self.profile["has_boom_mic"]
        self.worker = HidWorker(
            dev_path, self.log_traffic,
            mic_cb=self.on_mic_event if boom else None,
            boom_cb=self.on_boom_change if boom else None,
            poll_boom=boom,
            on_link=self.on_link_change,
            prefer_pid=pid,
            known_pids=set(DEVICE_PROFILES),
            boom_reader=read_boom if boom else None,
            idle_add=GLib.idle_add,
        )
        self.worker.start()
        self.audio = AudioControl(self._on_audio_change)

        # ---- header bar: page switcher + live status ----
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        self.set_titlebar(hb)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        hb.set_custom_title(switcher)

        # ---- status bar, inside the window above the pages (visible on both) ----
        self.conn_label = Gtk.Label()
        self.conn_label.set_markup("<span foreground='gray'>●</span>")
        self.conn_label.set_tooltip_text("headset connection")
        self.batt_label = Gtk.Label()
        self.batt_label.set_markup("🔋 …")
        self.batt_label.set_tooltip_text("battery")
        self.mic_label = Gtk.Label()
        self.mic_label.set_markup("🎤 …")
        self.mic_label.set_tooltip_text("boom position (polled from the headset)")

        status = Gtk.Box(spacing=12)
        status.set_border_width(10)
        status.pack_start(self.conn_label, False, False, 0)
        status.pack_start(self.batt_label, False, False, 0)
        status.pack_end(self.mic_label, False, False, 0)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(status, False, False, 0)
        outer.pack_start(Gtk.Separator(), False, False, 0)
        outer.pack_start(self.stack, True, True, 0)
        self.add(outer)

        # ================= Control page (everything except the console) =================
        sound = self._page("control", "Control", scroll=True)

        gk = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        row = Gtk.Box(spacing=8)
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup("<b>G HUB mode</b>  (DSP soundstage + software mic handling)")
        row.pack_start(lbl, True, True, 0)
        self.dsp_sw = Gtk.Switch()
        self.dsp_sw.set_active(load_mode() == "ghub")
        self.dsp_sw.connect("notify::active", self.on_dsp_toggle)
        row.pack_end(self.dsp_sw, False, False, 0)
        gk.pack_start(row, False, False, 0)
        self.mode_note = Gtk.Label(xalign=0)
        self.mode_note.set_line_wrap(True)
        gk.pack_start(self.mode_note, False, False, 0)
        sound.pack_start(gk, False, False, 0)
        self.section_frames["gkeys"] = gk
        self._update_mode_note()

        st = self._frame(sound, "Sidetone (your mic mixed into your ears)")
        self.section_frames["sidetone"] = st.get_parent()
        self.sidetone = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.sidetone.set_hexpand(True)
        self.sidetone.set_value(100)
        self._sidetone_pending = None
        self.sidetone.connect("value-changed", self.on_sidetone)
        st.pack_start(self.sidetone, False, False, 0)

        if self.profile["has_boom_mic"]:
            mic = self._frame(sound, "Microphone")
            self.mic_big = Gtk.Label()
            self.mic_big.set_markup("<span size='large'>🎤 reading boom position…</span>")
            mic.pack_start(self.mic_big, False, False, 2)
            row = Gtk.Box(spacing=8)
            row.pack_start(Gtk.Label(label="Host capture switch (ALSA / USB-audio mute layer)",
                                     xalign=0), True, True, 0)
            self.hostmic_sw = Gtk.Switch()
            self.hostmic_sw.set_active(_read_host_mic() is not False)
            self.hostmic_sw.connect("notify::active", self.on_hostmic_toggle)
            row.pack_end(self.hostmic_sw, False, False, 0)
            mic.pack_start(row, False, False, 0)
            b = Gtk.Button(label="Unstick mic (force host unmute)")
            b.connect("clicked", lambda *_: (_set_host_mic(True),
                                             self._refresh_hostmic()))
            mic.pack_start(b, False, False, 0)
            self.mic_note = Gtk.Label(xalign=0)
            self.mic_note.set_line_wrap(True)
            mic.pack_start(self.mic_note, False, False, 0)
            self._update_mic_note()
        else:
            self.mic_label.set_no_show_all(True)
            self.mic_label.set_visible(False)

        eq = self._frame(sound, "Hardware EQ (on-device, ±12 dB)")
        self.section_frames["eq"] = eq.get_parent()
        self.eq_box = eq
        self.eq_sliders = []
        if self.profile["eq_bands"]:
            self._build_eq_sliders(self.profile["eq_bands"])

        li = self._frame(sound, "Lighting")
        self.section_frames["lighting"] = li.get_parent()
        self.light_box = li
        self.zone_widgets = {}
        if self.profile["zones"]:
            self._build_lighting_rows(self.profile["zones"])

        # ================= Battery Health page =================
        self.health = HealthTracker()

        hp = self._page("health", "Battery Health", scroll=True)

        row = Gtk.Box(spacing=8)
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup("<b>Track battery health</b>  (log charge/discharge sessions)")
        row.pack_start(lbl, True, True, 0)
        self.track_sw = Gtk.Switch()
        self.track_sw.set_active(self.health.settings["tracking"])
        self.track_sw.connect("notify::active", self.on_track_toggle)
        row.pack_end(self.track_sw, False, False, 0)
        hp.pack_start(row, False, False, 0)

        live = self._frame(hp, "Live · remaining runtime")
        self.health_live = Gtk.Label(xalign=0)
        self.health_live.set_markup("<span size='large'>…</span>")
        self.health_live.set_line_wrap(True)
        live.pack_start(self.health_live, False, False, 2)

        # ---- Graphs (battery-manager style) ----
        graphs = self._frame(hp, "Graphs")
        # top row: history (wide) 
        self.chart_history = ChargeHistoryChart()
        graphs.pack_start(self.chart_history, False, False, 4)
        # second row: gauge + expect/actual side by side
        row = Gtk.Box(spacing=8)
        self.chart_gauge = HealthGaugeChart()
        self.chart_gauge.set_size_request(180, 140)
        self.chart_gauge.set_hexpand(False)
        row.pack_start(self.chart_gauge, False, False, 0)
        self.chart_expect = ExpectActualChart()
        row.pack_start(self.chart_expect, True, True, 0)
        graphs.pack_start(row, False, False, 4)
        # sessions + drain profile
        self.chart_sessions = SessionRuntimeChart()
        graphs.pack_start(self.chart_sessions, False, False, 4)
        self.chart_profile = DrainProfileChart()
        graphs.pack_start(self.chart_profile, False, False, 4)

        est = self._frame(hp, "Expect vs real · degradation")
        self.health_est = Gtk.Label(xalign=0)
        self.health_est.set_markup("<span size='x-large'>—</span>  <small>no data yet</small>")
        self.health_est.set_line_wrap(True)
        est.pack_start(self.health_est, False, False, 2)
        self.health_detail = Gtk.Label(xalign=0)
        self.health_detail.set_line_wrap(True)
        est.pack_start(self.health_detail, False, False, 0)
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_markup(
            "<small>Blue = measured from your datapoints. Amber = rated/expected "
            "spec. Green segments on the history chart are charging. Health % = "
            "learned full runtime ÷ rated. Short discharge sessions stitch across "
            "brief headset-off gaps.</small>")
        est.pack_start(note, False, False, 0)

        prof = self._frame(hp, "Learned drain profile")
        self.health_profile = Gtk.Label(xalign=0)
        self.health_profile.set_line_wrap(True)
        self.health_profile.set_markup("<small>collecting discharge datapoints…</small>")
        prof.pack_start(self.health_profile, False, False, 0)

        hist = self._frame(hp, "Recent usage sessions")
        self.health_hist = Gtk.Label(xalign=0)
        self.health_hist.set_markup("<tt>no sessions recorded</tt>")
        hist.pack_start(self.health_hist, False, False, 0)

        spec = self._frame(hp, "Battery specification (stock G935 defaults — edit for "
                               "aftermarket cells)")
        self.spec_spins = {}
        for key, label, lo, hi, step in (
                ("design_capacity_mah", "Design capacity (mAh)", 300, 5000, 50),
                ("rated_runtime_h_rgb_on", "Rated runtime, RGB on (h)", 1, 40, 0.5),
                ("rated_runtime_h_rgb_off", "Rated runtime, RGB off (h)", 1, 40, 0.5),
                ("full_mv", "Full-charge voltage (mV)", 3900, 4400, 10),
                ("empty_mv", "Empty voltage (mV)", 3000, 3700, 10)):
            row = Gtk.Box(spacing=8)
            row.pack_start(Gtk.Label(label=label, xalign=0), True, True, 0)
            sp = Gtk.SpinButton.new_with_range(lo, hi, step)
            if step < 1:
                sp.set_digits(1)
            sp.set_value(self.health.settings[key])
            sp.connect("value-changed", self.on_spec_changed, key)
            row.pack_end(sp, False, False, 0)
            spec.pack_start(row, False, False, 0)
            self.spec_spins[key] = sp
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Compare against", xalign=0), True, True, 0)
        self.profile_combo = Gtk.ComboBoxText()
        for t in ("RGB lighting on spec", "RGB lighting off spec"):
            self.profile_combo.append_text(t)
        self.profile_combo.set_active(
            0 if self.health.settings["runtime_profile"] == "rgb_on" else 1)
        self.profile_combo.connect("changed", self.on_profile_changed)
        row.pack_end(self.profile_combo, False, False, 0)
        spec.pack_start(row, False, False, 0)
        b = Gtk.Button(label="Reset to stock G935 values")
        b.connect("clicked", self.on_spec_reset)
        spec.pack_start(b, False, False, 0)
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_markup(
            "<small>Stock cell: 1100 mAh 3.7 V Li-Po (part 533-000132), "
            "4200 mV full / 3500 mV empty, rated 8 h with default RGB or "
            "12 h lights-off at 50% volume. Aftermarket cells claim up to "
            "2500 mAh. Spec fields set the <i>expected</i> side of the "
            "comparison; measured drain builds the <i>real</i> side.</small>")
        spec.pack_start(note, False, False, 0)

        self._refresh_health_display(None, None, "…")
        self.section_frames["battery"] = self.stack.get_child_by_name("health")

        # ================= Settings page =================
        sp = self._page("settings", "Settings", scroll=True)

        devf = self._frame(sp, "Tray & audio devices (unchecked = hidden from "
                               "tray menu and mixer)")
        self.dev_toggle_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        devf.pack_start(self.dev_toggle_box, False, False, 0)
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_markup("<small>easyeffects devices and monitor sources are managed "
                        "automatically and never listed.</small>")
        devf.pack_start(note, False, False, 0)

        volf = self._frame(sp, "Volume")
        self.settings_vol_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        volf.pack_start(self.settings_vol_box, False, False, 0)
        self._vol_containers.append(self.settings_vol_box)

        # ================= Console page =================
        con = self._page("console", "Console")

        if self.profile["connect_burst"]:
            row = Gtk.Box(spacing=8)
            b = Gtk.Button(label="Apply G HUB connect defaults (full sequence)")
            b.connect("clicked", self.on_full_sequence)
            row.pack_start(b, True, True, 0)
            con.pack_start(row, False, False, 0)

        row = Gtk.Box(spacing=8)
        self.raw_entry = Gtk.Entry()
        self.raw_entry.set_placeholder_text("raw HID++ hex, e.g. 11ff062b")
        self.raw_entry.connect("activate", self.on_raw_send)
        row.pack_start(self.raw_entry, True, True, 0)
        b = Gtk.Button(label="Send")
        b.connect("clicked", self.on_raw_send)
        row.pack_start(b, False, False, 0)
        con.pack_start(row, False, False, 0)

        self.logbuf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.logbuf, editable=False, monospace=True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(view)
        con.pack_start(scroll, True, True, 0)
        self.logview = view

        # feature discovery first; all device state is asserted from its
        # completion callback. The 5s heartbeat retries discovery while the
        # headset is off and polls battery once it's known.
        self._start_discovery()
        GLib.timeout_add_seconds(5, self.heartbeat)
        GLib.timeout_add_seconds(3, self._poll_daemon_status)
        self._setup_tray()
        self.connect("delete-event", self.on_delete_event)
        GLib.idle_add(self._on_audio_change)   # first tray/settings population

    # ---------- helpers ----------
    def _page(self, name, title, scroll=False):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)
        if scroll:
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sw.add(box)
            self.stack.add_titled(sw, name, title)
        else:
            self.stack.add_titled(box, name, title)
        return box

    def _frame(self, parent, title):
        f = Gtk.Frame(label=title)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        f.add(box)
        parent.pack_start(f, False, False, 0)
        return box

    def _build_eq_sliders(self, band_names):
        sliders = Gtk.Box(spacing=4, homogeneous=True)
        self.eq_sliders = []
        for name in band_names:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            s = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -12, 12, 1)
            s.set_inverted(True)          # +12 at top
            s.set_size_request(-1, 150)
            s.set_value(0)
            s.add_mark(0, Gtk.PositionType.LEFT, None)
            col.pack_start(s, True, True, 0)
            col.pack_start(Gtk.Label(label=name), False, False, 0)
            sliders.pack_start(col, True, True, 0)
            self.eq_sliders.append(s)
        self.eq_box.pack_start(sliders, False, False, 0)

        row = Gtk.Box(spacing=6)
        for name, gains in self.profile["eq_presets"].items():
            if gains is not None and len(gains) != len(band_names):
                continue
            b = Gtk.Button(label=name)
            b.connect("clicked", self.on_eq_preset, name)
            row.pack_start(b, True, True, 0)
        self.eq_box.pack_start(row, False, False, 0)
        row = Gtk.Box(spacing=6)
        b = Gtk.Button(label="Apply EQ")
        b.get_style_context().add_class("suggested-action")
        b.connect("clicked", self.on_eq_apply)
        row.pack_start(b, True, True, 0)
        b = Gtk.Button(label="Read from headset")
        b.connect("clicked", lambda *_: self.send("eq", 2, cb=self.got_eq))
        row.pack_start(b, True, True, 0)
        self.eq_box.pack_start(row, False, False, 0)
        self.eq_box.show_all()

    def _build_lighting_rows(self, zone_names):
        saved_all = self.settings.data["lighting"]
        for zone, zname in enumerate(zone_names):
            saved = saved_all.get(str(zone), {})
            row = Gtk.Box(spacing=8)
            row.pack_start(Gtk.Label(label=zname, xalign=0), False, False, 0)
            combo = Gtk.ComboBoxText()
            for e in ("Off", "Fixed", "Breathing"):
                combo.append_text(e)
            combo.set_active(saved.get("mode", 2))
            row.pack_start(combo, False, False, 0)
            color = Gtk.ColorButton()
            rgba = Gdk.RGBA(); rgba.parse(saved.get("color", "#00B4FF"))
            color.set_rgba(rgba)
            row.pack_start(color, False, False, 0)
            row.pack_start(Gtk.Label(label="period ms"), False, False, 0)
            period = Gtk.SpinButton.new_with_range(500, 20000, 100)
            period.set_value(saved.get("period", 5000))
            row.pack_start(period, False, False, 0)
            b = Gtk.Button(label="Apply")
            b.connect("clicked", self.on_light_apply, zone)
            row.pack_end(b, False, False, 0)
            self.light_box.pack_start(row, False, False, 0)
            self.zone_widgets[zone] = (combo, color, period)
        self.light_box.show_all()

    # ---------- HID++ feature discovery / command building ----------
    def cmd(self, feat, fn, params=""):
        """Frame for a discovered feature: 11 ff <idx> <fn<<4|swid> <params>.
        None if the device doesn't have the feature."""
        idx = self.features.get(feat)
        if idx is None:
            return None
        return f"11ff{idx:02x}{(fn << 4) | 0x0b:02x}{params}"

    def send(self, feat, fn, params="", cb=None):
        hx = self.cmd(feat, fn, params)
        if hx:
            self.worker.submit(hx, cb)

    def _start_discovery(self):
        if self.discovered or self._discovering:
            return
        self._discovering = True
        self._disc_found = {}
        self._disc_err = False
        self._disc_queue = list(FEATURES)
        self._disc_next()

    def _disc_next(self):
        if not self._disc_queue:
            if self._disc_err and not self._disc_found:
                return self._walk_featureset()
            return self._finish_discovery()
        self._disc_cur = self._disc_queue.pop(0)
        self._disc_retried = False
        self._disc_probe()

    def _disc_probe(self):
        attr, fid = self._disc_cur
        # IRoot getFeature(featureID): reply[4] = index, 0 = not present
        self.worker.submit(f"11ff000b{fid:04x}",
                           lambda st, rep, a=attr: self._got_feature(a, st, rep))

    def _got_feature(self, attr, status, reply):
        if status == "TIMEOUT":
            # one retry: g935-dspd shares the hidraw and a collided reply
            # looks identical to a powered-off headset
            if not self._disc_retried:
                self._disc_retried = True
                self._disc_probe()
                return
            self._discovering = False      # headset off - heartbeat retries
            self._mark_disconnected()
            return
        if status == "ACK" and reply[4]:
            self._disc_found[attr] = reply[4]
        elif status == "ERR":
            self._disc_err = True
        self._disc_next()

    def _walk_featureset(self):
        """Fallback when IRoot getFeature errors: walk IFeatureSet instead."""
        self.worker.submit("11ff010b", self._got_feat_count)

    def _got_feat_count(self, status, reply):
        if status != "ACK":
            self._discovering = False
            if status == "TIMEOUT":
                self._mark_disconnected()
            return
        self._walk_ids = {}
        self._walk_left = list(range(1, min(reply[4], 32) + 1))
        self._walk_next()

    def _walk_next(self):
        if not self._walk_left:
            wanted = {fid: attr for attr, fid in FEATURES}
            for idx, fid in self._walk_ids.items():
                if fid in wanted:
                    self._disc_found[wanted[fid]] = idx
            return self._finish_discovery()
        i = self._walk_left.pop(0)
        self.worker.submit(f"11ff011b{i:02x}",
                           lambda st, rep, i=i: self._got_feat_id(i, st, rep))

    def _got_feat_id(self, i, status, reply):
        if status == "TIMEOUT":
            self._discovering = False
            self._mark_disconnected()
            return
        if status == "ACK":
            self._walk_ids[i] = (reply[4] << 8) | reply[5]
        self._walk_next()

    def _finish_discovery(self):
        self.features = self._disc_found
        self.discovered = True
        self._discovering = False
        self.log("--- discovered features: "
                 + (", ".join(f"{a}={i:02x}" for a, i in self.features.items())
                    or "none") + " ---")
        for attr, widget in self.section_frames.items():
            present = attr in self.features
            widget.set_no_show_all(not present)
            widget.set_visible(present)
        if "lighting" in self.features and not self.zone_widgets:
            self.send("lighting", 0, cb=self._got_light_info)
        if "eq" in self.features and not self.eq_sliders:
            self.send("eq", 0, cb=self._got_eq_info)
        self._assert_device_state(initial=True)

    def _got_light_info(self, status, reply):
        # 0x8070 getInfo: reply[4] = zone count on the G935 (unverified on
        # other models - clamp hard, fall back to a single zone)
        n = reply[4] if status == "ACK" and 1 <= reply[4] <= 8 else 1
        self._build_lighting_rows([f"Zone {z}" for z in range(n)])
        self._apply_saved_lighting()

    def _got_eq_info(self, status, reply):
        n = reply[4] if status == "ACK" else 0
        if 1 <= n <= 12:
            self._build_eq_sliders([str(i + 1) for i in range(n)])
            self.send("eq", 2, cb=self.got_eq)
        else:                              # can't size the EQ - hide it
            w = self.section_frames["eq"]
            w.set_no_show_all(True)
            w.set_visible(False)

    def _assert_device_state(self, initial):
        """The single re-assert path, used after discovery and on power-on.

        When g935-dspd is running it owns power-on DSP enable; the GUI only
        re-applies panel-owned state (lighting / sidetone / EQ). Mode toggle
        by the user still sends gkeys immediately via on_dsp_toggle.
        """
        if not daemon_running():
            self.send("gkeys", 2, f"{1 if self.dsp_sw.get_active() else 0:02x}")
        else:
            self.log("--- dspd running: leaving power-on mode enable to the daemon ---")
        self._apply_saved_lighting()
        if initial:
            self.send("sidetone", 0, cb=self.got_sidetone)
            self.send("eq", 2, cb=self.got_eq)
        else:
            self._apply_sidetone()
            self.on_eq_apply(None)

    def on_link_change(self, up):
        """HidWorker reopened (or lost) the hidraw node after unplug/replug."""
        if up:
            self.log("--- receiver link up: rediscovering ---")
            self.discovered = False
            self._discovering = False
            self.features = {}
            self._start_discovery()
        else:
            self.log("--- receiver link down ---")
            self.discovered = False
            self._discovering = False
            self.features = {}
            self._mark_disconnected()

    def log(self, text):
        self.logbuf.insert(self.logbuf.get_end_iter(), text + "\n")
        mark = self.logbuf.create_mark(None, self.logbuf.get_end_iter(), False)
        self.logview.scroll_mark_onscreen(mark)

    def log_traffic(self, hx, status, reply):
        if status == "ACK":
            h = reply.hex()
            while h.endswith("00"):    # trim padding bytes, not meaningful nibbles
                h = h[:-2]
            self.log(f"→ {hx:<30} ✓ {h}")
        elif status == "ERR":
            self.log(f"→ {hx:<30} ✗ ERR {reply:#04x} ({ERROR_CODES.get(reply, '?')})")
        elif status == "BADHEX":
            self.log(f"→ {hx:<30} ✗ invalid hex (4–20 bytes)")
        elif status == "GONE":
            self.log(f"→ {hx:<30} ✗ device unreachable")
        else:
            self.log(f"→ {hx:<30} … no reply")

    def _set_mic_status(self, markup_small, markup_big):
        self.mic_label.set_markup(markup_small)
        self.mic_big.set_markup(f"<span size='large'>{markup_big}</span>")

    # ---------- G HUB mode switch ----------
    def on_dsp_toggle(self, sw, _pspec):
        # The toggle is write-only on the device; the switch position is the state.
        on = sw.get_active()
        self.send("gkeys", 2, f"{1 if on else 0:02x}")
        save_mode("ghub" if on else "hardware")
        self._update_mode_note()
        self._update_mic_note()

    def _update_mode_note(self):
        if self.dsp_sw.get_active():
            if daemon_running():
                self.mode_note.set_markup(
                    "<small>G HUB mode: open soundstage ON. Mic is host-managed by "
                    "<b>g935-dspd</b> (auto-unmutes ~2s after boom-down; handles the "
                    "button).</small>")
            else:
                self.mode_note.set_markup(
                    "<small><span foreground='#e01b24'><b>G HUB mode without g935-dspd:</b> "
                    "boom mute will stick until the daemon is running.</span> "
                    "Start it with: <tt>systemctl --user enable --now g935-dsp</tt></small>")
        else:
            self.mode_note.set_markup(
                "<small>Hardware mode (default): stock flat sound; mic is fully "
                "self-managed (boom up = mute, boom down = unmute, button toggles). "
                "Flip the switch for the open G HUB soundstage.</small>")

    def _update_mic_note(self):
        if not hasattr(self, "mic_note"):
            return
        if not self.dsp_sw.get_active():
            self.mic_note.set_markup(
                "<small>Hardware mode: boom mute is fully on-device. Host switch is a "
                "separate mute layer.</small>")
        elif daemon_running():
            self.mic_note.set_markup(
                "<small>G HUB mode: g935-dspd clears the boom flag with a slow host-mute "
                "pulse ~2s after boom-down, and implements the button toggle.</small>")
        else:
            self.mic_note.set_markup(
                "<small><span foreground='#e01b24'>Daemon not running — boom mute will "
                "stick after raising the mic. Start g935-dsp, or use Unstick / hardware "
                "mode.</span></small>")

    # ---------- host mic switch ----------
    def on_hostmic_toggle(self, sw, _pspec):
        _set_host_mic(sw.get_active())

    def _refresh_hostmic(self):
        state = _read_host_mic()
        if state is None:
            return
        self.hostmic_sw.handler_block_by_func(self.on_hostmic_toggle)
        self.hostmic_sw.set_active(state)
        self.hostmic_sw.handler_unblock_by_func(self.on_hostmic_toggle)

    # ---------- mic state ----------
    def on_boom_change(self, down):
        """Boom position, polled from the headset itself (see read_boom). The
        0x08 event stream is only a notification channel - our own host-mute
        writes echo back through it looking exactly like real boom moves."""
        if not down:
            self._set_mic_status("🎤 <b>muted</b>", "🎤 muted (boom up)")
            self.log("--- boom raised: headset mute flag SET ---")
        elif self.dsp_sw.get_active():
            self._set_mic_status("🎤 <b>live</b>", "🎤 boom down — daemon unmutes (~2s)")
            self.log("--- boom lowered: daemon runs the unmute pulse ---")
        else:
            self._set_mic_status("🎤 <b>live</b>", "🎤 live (boom down)")
            self.log("--- boom lowered: unmuted (hardware mode) ---")

    def on_mic_event(self, bits):
        if bits & BUTTON and not bits & (BOOM_UP | BOOM_DOWN):
            self.log("--- mic button pressed ---")
            GLib.timeout_add(300, lambda: (self._refresh_hostmic(), False)[1])

    # ---------- sidetone ----------
    def on_sidetone(self, _scale):
        if self._sidetone_pending:
            GLib.source_remove(self._sidetone_pending)
        self._sidetone_pending = GLib.timeout_add(200, self._apply_sidetone)

    def _apply_sidetone(self):
        self._sidetone_pending = None
        self.send("sidetone", 1, f"{int(self.sidetone.get_value()):02x}")
        return False

    def got_sidetone(self, status, reply):
        if status == "ACK":
            self.sidetone.handler_block_by_func(self.on_sidetone)
            self.sidetone.set_value(reply[4])
            self.sidetone.handler_unblock_by_func(self.on_sidetone)

    # ---------- EQ ----------
    def on_eq_preset(self, _btn, name):
        gains = self.profile["eq_presets"][name] or [0] * len(self.eq_sliders)
        for s, v in zip(self.eq_sliders, gains):
            s.set_value(v)
        self.on_eq_apply(None)

    def on_eq_apply(self, _btn):
        if not self.eq_sliders:
            return
        gains = "".join(f"{int(s.get_value()) & 0xFF:02x}" for s in self.eq_sliders)
        self.send("eq", 3, "02" + gains)

    def got_eq(self, status, reply):
        if status != "ACK":
            return
        for s, b in zip(self.eq_sliders, reply[4:4 + len(self.eq_sliders)]):
            v = b - 256 if b > 127 else b
            s.set_value(v)

    # ---------- lighting ----------
    def _light_params(self, zone):
        combo, color, period = self.zone_widgets[zone]
        mode = combo.get_active()          # 0 off, 1 fixed, 2 breathing
        rgba = color.get_rgba()
        r, g, b = (int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))
        if mode == 0:
            return f"{zone:02x}00"
        if mode == 1:
            return f"{zone:02x}01{r:02x}{g:02x}{b:02x}02"
        p = int(period.get_value())
        brt = self.profile["zone_brt"].get(zone, 0x00)
        return f"{zone:02x}02{r:02x}{g:02x}{b:02x}{p:04x}00{brt:02x}"

    def on_light_apply(self, _btn, zone):
        self.send("lighting", 3, self._light_params(zone))
        combo, color, period = self.zone_widgets[zone]
        rgba = color.get_rgba()
        self.settings.data["lighting"][str(zone)] = {
            "mode": combo.get_active(),
            "color": "#%02X%02X%02X" % (int(rgba.red * 255), int(rgba.green * 255),
                                        int(rgba.blue * 255)),
            "period": int(period.get_value()),
        }
        self.settings.save()

    def _apply_saved_lighting(self):
        # zones the user never Applied have no entry and are left alone
        for zone in self.zone_widgets:
            if str(zone) in self.settings.data["lighting"]:
                self.send("lighting", 3, self._light_params(zone))

    # ---------- console ----------
    def on_full_sequence(self, _btn):
        self.log("--- G HUB connect sequence ---")
        self.dsp_sw.set_active(True)   # sequence includes gkeys 01 = G HUB mode
        for feat, fn, params in self.profile["connect_burst"]:
            self.send(feat, fn, params)
        self.send("sidetone", 0, cb=self.got_sidetone)
        self.send("eq", 2, cb=self.got_eq)

    def on_raw_send(self, _w):
        hx = self.raw_entry.get_text().strip().replace(" ", "")
        if hx:
            self.worker.submit(hx)

    # ---------- tray ----------
    def _setup_tray(self):
        if AppIndicator is None:
            return
        if not sni_watcher_present():
            # no tray host (stock GNOME without the AppIndicator extension):
            # stay windowed and let the close button quit normally
            return
        self.indicator = AppIndicator.Indicator.new(
            "g935-control", "audio-headset",
            AppIndicator.IndicatorCategory.HARDWARE)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title(f"{self.profile['name']} Control")
        try:
            self.indicator.connect("scroll-event", self.on_tray_scroll)
        except TypeError:
            pass
        self._rebuild_tray_menu()

    def on_tray_scroll(self, _ind, _delta, direction):
        try:
            up = direction == Gdk.ScrollDirection.UP
        except Exception:
            up = True
        self.audio.step_default_volume(5 if up else -5)

    def _update_tray_battery(self, text):
        if self.indicator:
            self.indicator.set_title(f"{self.profile['name']} · {text}")
        if self.tray_batt_item:
            self.tray_batt_item.set_label(f"{self.profile['name']} — {text}")

    def _rebuild_tray_menu(self):
        if not self.indicator:
            return
        menu = Gtk.Menu()

        self.tray_batt_item = Gtk.MenuItem(label=f"{self.profile['name']}")
        self.tray_batt_item.set_sensitive(False)
        menu.append(self.tray_batt_item)
        menu.append(Gtk.SeparatorMenuItem())

        for kind, devs, hidden, default, setter in (
                ("Output", self.audio.list_sinks(),
                 set(self.settings.data["hidden_sinks"]),
                 self.audio.default_sink(), self.audio.set_default_sink),
                ("Input", self.audio.list_sources(),
                 set(self.settings.data["hidden_sources"]),
                 self.audio.default_source(), self.audio.set_default_source)):
            hdr = Gtk.MenuItem(label=kind)
            hdr.set_sensitive(False)
            menu.append(hdr)
            group = None
            for name, desc in devs:
                if name in hidden:
                    continue
                item = Gtk.RadioMenuItem.new_with_label_from_widget(group, desc)
                group = group or item
                item.set_active(name == default)   # before connect: no echo
                item.connect("activate",
                             lambda it, n=name, f=setter:
                             it.get_active() and f(n))
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())

        it = Gtk.MenuItem(label="Mixer…")
        it.connect("activate", lambda *_: self._show_mixer())
        menu.append(it)
        it = Gtk.MenuItem(label="Show/Hide Control Panel")
        it.connect("activate", lambda *_: (self.hide() if self.get_visible()
                                           else self.present()))
        menu.append(it)
        menu.append(Gtk.SeparatorMenuItem())
        it = Gtk.MenuItem(label="Quit")
        it.connect("activate", self.on_quit)
        menu.append(it)

        menu.show_all()
        self.indicator.set_menu(menu)

    def on_delete_event(self, *_):
        if self.indicator:          # hide to tray, keep running
            self.hide()
            return True
        return False                # no tray: normal close -> destroy -> quit

    def on_quit(self, *_):
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        self.audio.stop()
        try:
            self.worker.stop()
        except Exception:
            pass
        self.health.save()
        self.settings.save()
        Gtk.main_quit()

    # ---------- audio devices: settings toggles, volume rows, mixer ----------
    def _on_audio_change(self):
        self._rebuild_tray_menu()
        self._refresh_device_toggles()
        self._refresh_volume_rows()

    def _refresh_device_toggles(self):
        box = self.dev_toggle_box
        for c in box.get_children():
            c.destroy()
        for kind_lbl, devs, key in (
                ("Outputs", self.audio.list_sinks(), "hidden_sinks"),
                ("Inputs", self.audio.list_sources(), "hidden_sources")):
            hdr = Gtk.Label(xalign=0)
            hdr.set_markup(f"<b>{kind_lbl}</b>")
            box.pack_start(hdr, False, False, 2)
            hidden = set(self.settings.data[key])
            for name, desc in devs:
                cb = Gtk.CheckButton(label=desc)
                cb.set_active(name not in hidden)
                cb.set_tooltip_text(name)
                cb.connect("toggled", self._on_dev_toggle, key, name)
                box.pack_start(cb, False, False, 0)
        box.show_all()

    def _on_dev_toggle(self, cb, key, name):
        hidden = set(self.settings.data[key])
        if cb.get_active():
            hidden.discard(name)
        else:
            hidden.add(name)
        self.settings.data[key] = sorted(hidden)
        self.settings.save()
        self._rebuild_tray_menu()
        self._refresh_volume_rows()

    def _refresh_volume_rows(self):
        if self._vol_pending:       # user mid-drag: don't fight them
            return
        for box in self._vol_containers:
            self._build_volume_rows(box)

    def _build_volume_rows(self, container):
        for c in container.get_children():
            c.destroy()
        for kind, devs, hidden, default, setter in (
                ("sink", self.audio.list_sinks(),
                 set(self.settings.data["hidden_sinks"]),
                 self.audio.default_sink(), self.audio.set_default_sink),
                ("source", self.audio.list_sources(),
                 set(self.settings.data["hidden_sources"]),
                 self.audio.default_source(), self.audio.set_default_source)):
            for name, desc in devs:
                if name in hidden:
                    continue
                row = Gtk.Box(spacing=8)
                rb = Gtk.CheckButton()
                rb.set_active(name == default)
                rb.set_tooltip_text("make default")
                rb.connect("toggled",
                           lambda b, n=name, f=setter: b.get_active() and f(n))
                row.pack_start(rb, False, False, 0)
                lbl = Gtk.Label(label=desc, xalign=0)
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                lbl.set_size_request(180, -1)
                row.pack_start(lbl, True, True, 0)
                sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
                sc.set_size_request(160, -1)
                sc.set_value(self.audio.get_volume(kind, name) or 0)
                sc.set_value_pos(Gtk.PositionType.RIGHT)
                sc.connect("value-changed", self._on_vol_slider, kind, name)
                row.pack_start(sc, True, True, 0)
                container.pack_start(row, False, False, 0)
        container.show_all()

    def _on_vol_slider(self, scale, kind, name):
        key = (kind, name)
        if key in self._vol_pending:
            GLib.source_remove(self._vol_pending[key])
        self._vol_pending[key] = GLib.timeout_add(
            150, self._flush_vol, scale, kind, name)

    def _flush_vol(self, scale, kind, name):
        self._vol_pending.pop((kind, name), None)
        self.audio.set_volume(kind, name, scale.get_value())
        return False

    def _show_mixer(self):
        if self.mixer_win is None:
            self.mixer_win = Gtk.Window(title="Mixer")
            self.mixer_win.set_default_size(460, -1)
            self.mixer_win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.mixer_win.set_position(Gtk.WindowPosition.CENTER)
            self.mixer_win.set_icon_name("audio-headset")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_border_width(10)
            self.mixer_win.add(box)
            self.mixer_box = box
            self._vol_containers.append(box)
            self.mixer_win.connect("delete-event",
                                   lambda w, e: (w.hide(), True)[1])
        self._build_volume_rows(self.mixer_box)
        self.mixer_win.show_all()
        self.mixer_win.present()

    # ---------- battery health ----------
    def on_track_toggle(self, sw, _pspec):
        self.health.settings["tracking"] = sw.get_active()
        if not sw.get_active():
            self.health.close_segment("tracking disabled")
        self.health.save()

    def on_spec_changed(self, spin, key):
        v = spin.get_value()
        self.health.settings[key] = v if spin.get_digits() else int(v)
        self.health.save()
        self._refresh_health_display()

    def on_profile_changed(self, _combo):
        self.health.settings["runtime_profile"] = (
            "rgb_on" if self.profile_combo.get_active() == 0 else "rgb_off")
        self.health.save()
        self._refresh_health_display()

    def on_spec_reset(self, _btn):
        for key, sp in self.spec_spins.items():
            sp.handler_block_by_func(self.on_spec_changed)
            sp.set_value(HEALTH_DEFAULTS[key])
            sp.handler_unblock_by_func(self.on_spec_changed)
            self.health.settings[key] = HEALTH_DEFAULTS[key]
        self.health.settings["runtime_profile"] = HEALTH_DEFAULTS["runtime_profile"]
        self.profile_combo.handler_block_by_func(self.on_profile_changed)
        self.profile_combo.set_active(0)
        self.profile_combo.handler_unblock_by_func(self.on_profile_changed)
        self.health.save()
        self._refresh_health_display()

    @staticmethod
    def _dur(seconds):
        if seconds is None:
            return "—"
        seconds = int(seconds)
        if seconds < 0:
            seconds = 0
        m = seconds // 60
        if m >= 60:
            return f"{m // 60}h{m % 60:02d}m"
        if m > 0:
            return f"{m}m"
        return f"{seconds}s"

    @staticmethod
    def _dur_h(hours):
        if hours is None:
            return "—"
        if hours <= 0:
            return "0m"
        total_m = int(round(hours * 60))
        if total_m >= 60:
            return f"{total_m // 60}h{total_m % 60:02d}m"
        return f"{total_m}m"

    def _rated_runtime(self):
        key = ("rated_runtime_h_rgb_on"
               if self.health.settings["runtime_profile"] == "rgb_on"
               else "rated_runtime_h_rgb_off")
        return float(self.health.settings[key])

    def _refresh_health_display(self, mv=None, pct=None, state=""):
        h = self.health
        rated = self._rated_runtime()
        a = h.analysis(mv=mv, rated_runtime_h=rated)

        # --- Live: reading + session + remaining ---
        if mv is None:
            live = f"<span size='large'>{GLib.markup_escape_text(state or '…')}</span>"
        else:
            live = (f"<span size='large'>{mv} mV · {pct}% · "
                    f"{GLib.markup_escape_text(state)}</span>")

        lines = []
        if h.cur:
            seg = h.cur
            dt = seg["t_end"] - seg["t_start"]
            line = (f"session: {seg['type']} {self._dur(dt)}, "
                    f"{seg['mv_start']}→{seg['mv_end']} mV "
                    f"({seg['pct_start']}→{seg['pct_end']}%)")
            if seg["type"] == "discharge" and dt >= 600:
                rate = (seg["pct_start"] - seg["pct_end"]) / (dt / 3600.0)
                if rate > 0:
                    line += f", −{rate:.1f}%/h"
            lines.append(line)

        if a["live_rate_pct_per_h"] is not None:
            lines.append(
                f"live drain: −{a['live_rate_pct_per_h']:.1f}%/h "
                f"over {self._dur(a['live_rate_span_s'])} "
                f"({a['live_rate_points']} pts)")
        elif mv is not None and state == "discharging":
            lines.append("live drain: collecting… (need ~10 min discharge)")

        if a["remain_best_h"] is not None:
            src = a["remain_source"] or "estimate"
            lines.append(
                f"<b>remaining (from data): {self._dur_h(a['remain_best_h'])}</b> "
                f"via {src}")
            if (a["remain_profile_h"] is not None
                    and a["remain_source"] != "profile"
                    and a["profile_summary"].get("ready")):
                lines.append(
                    f"profile-shaped remaining: {self._dur_h(a['remain_profile_h'])}")
        if a["remain_expected_h"] is not None and mv is not None:
            delta = ""
            if a["remain_best_h"] is not None and a["remain_expected_h"] > 0:
                rel = 100.0 * (a["remain_best_h"] / a["remain_expected_h"] - 1.0)
                sign = "+" if rel >= 0 else "−"
                delta = f"  ({sign}{abs(rel):.0f}% vs rated for this level)"
            lines.append(
                f"expected at rated health: {self._dur_h(a['remain_expected_h'])}"
                f"{delta}")

        if lines:
            live += "\n<small>" + "\n".join(lines) + "</small>"
        self.health_live.set_markup(live)

        # --- Graphs ---
        self._update_health_charts(a, rated)

        # --- Expect vs real / degradation ---
        if a["health_pct"] is None:
            self.health_est.set_markup(
                "<span size='x-large'>—</span>  <small>collecting data — need "
                "merged discharge ≥30 min spanning ≥8% (short sessions stitch "
                "across brief offs)</small>")
        else:
            conf = ("low confidence" if a["n_sessions"] < 3
                    else f"median of {a['n_sessions']} sessions")
            learned = a["learned_full_runtime_h"]
            eff = a["effective_mah"]
            self.health_est.set_markup(
                f"<span size='x-large'>≈ {a['health_pct']:.0f}%</span>  "
                f"<small>health · {conf}</small>\n"
                f"<small>real full runtime: <b>{self._dur_h(learned)}</b>  ·  "
                f"expected (rated): <b>{rated:g} h</b>\n"
                f"effective capacity ≈ {eff} mAh of {a['design_mah']} mAh design"
                f"</small>")

        details = []
        if a.get("peak_recent_mv"):
            peak = a["peak_recent_mv"]
            full = a["full_mv"]
            details.append(
                f"Full-charge peak (recent): {peak} mV "
                f"({100.0 * peak / full:.1f}% of {full} mV spec)")
        last_chg = next((s for s in reversed(h.segments) if s["type"] == "charge"), None)
        if last_chg:
            details.append(
                f"Last charge: {self._dur(last_chg['t_end'] - last_chg['t_start'])}, "
                f"{last_chg['mv_start']}→{last_chg['mv_end']} mV")
        if a["remain_expected_adj_h"] is not None and a["health_pct"] is not None:
            details.append(
                f"Remaining if health holds: {self._dur_h(a['remain_expected_adj_h'])} "
                f"(rated scaled by {a['health_pct']:.0f}%)")
        self.health_detail.set_markup(
            "<small>" + GLib.markup_escape_text("\n".join(details) or " ") + "</small>")

        # --- Learned profile ---
        ps = a["profile_summary"]
        if not ps or ps.get("hours_logged", 0) <= 0:
            self.health_profile.set_markup(
                "<small>No discharge datapoints yet. Wear the headset off-charger; "
                "samples land about once per minute.</small>")
        else:
            span = ps.get("span_mv")
            span_s = f"{span[0]}–{span[1]} mV" if span else "—"
            ready = "ready for profile ETA" if ps.get("ready") else "building…"
            bins = a["profile"].get("bins") or {}
            # Show top bins by voltage descending as a compact rate table
            bin_bits = []
            for b in sorted(bins.keys(), reverse=True)[:8]:
                bin_bits.append(f"{b}mV: {bins[b]:.0f} mV/h")
            weak = a["profile"].get("weak_bins") or {}
            extra = f" · +{len(weak)} sparse bins" if weak else ""
            table = ("\n" + "   ".join(bin_bits)) if bin_bits else ""
            self.health_profile.set_markup(
                f"<small>{ps['hours_logged']:.1f} h discharge logged · "
                f"{ps.get('bins_filled', 0)} solid bins · span {span_s} · "
                f"{ps.get('n_pairs', 0)} pairs · <b>{ready}</b>{extra}"
                f"{GLib.markup_escape_text(table)}</small>")

        # --- History: merged usage sessions (what health uses) ---
        rows = []
        sessions = merge_discharge_sessions(h.segments)
        for sess in reversed(sessions[-8:]):
            when = time.strftime("%m-%d %H:%M", time.localtime(sess["t_start"]))
            dur = self._dur(sess["on_time_s"])
            span = (f"{sess['mv_start']}→{sess['mv_end']}mV "
                    f"{sess['pct_start']}→{sess['pct_end']}%")
            full = session_full_runtime_h(sess)
            if full is not None:
                pct_of_rated = min(120.0, 100.0 * full / rated) if rated else 0
                tail = f"→{full:.1f}h full ({pct_of_rated:.0f}% rated)"
            else:
                tail = "(too short)"
            parts = f" ×{sess['parts']}" if sess["parts"] > 1 else ""
            rows.append(f"{when}  {dur:>6}{parts}  {span}  {tail}")
        # Also show last few charge segments briefly
        for seg in reversed([s for s in h.segments if s["type"] == "charge"][-3:]):
            when = time.strftime("%m-%d %H:%M", time.localtime(seg["t_start"]))
            dur = self._dur(seg["t_end"] - seg["t_start"])
            rows.append(
                f"{when}  chg {dur:>5}  {seg['mv_start']}→{seg['mv_end']}mV")
        self.health_hist.set_markup(
            "<tt>" + GLib.markup_escape_text("\n".join(rows) or "no sessions recorded")
            + "</tt>")

    def _update_health_charts(self, analysis, rated):
        """Push latest analysis into the Cairo chart widgets."""
        h = self.health
        pts = build_history_points(h.recent, batt_percent)
        expected_line = build_expected_overlay(pts, rated)
        self.chart_history.set_data({
            "points": pts,
            "expected_points": expected_line,
        })
        self.chart_gauge.set_data({
            "health_pct": analysis.get("health_pct"),
            "learned_h": analysis.get("learned_full_runtime_h"),
            "rated_h": rated,
        })
        self.chart_expect.set_data({
            "expected_h": analysis.get("remain_expected_h"),
            "actual_h": analysis.get("remain_best_h"),
            "health_pct": analysis.get("health_pct"),
        })
        # Session bars: last up to 12 merged sessions with a full-runtime estimate
        sess_rows = []
        for sess in merge_discharge_sessions(h.segments)[-12:]:
            full = session_full_runtime_h(sess)
            label = time.strftime("%m/%d", time.localtime(sess["t_start"]))
            sess_rows.append({"label": label, "hours": full})
        self.chart_sessions.set_data({
            "sessions": sess_rows,
            "rated_h": rated,
        })
        profile = analysis.get("profile") or {}
        self.chart_profile.set_data({
            "bins": profile.get("bins") or {},
            "weak_bins": profile.get("weak_bins") or {},
        })

    # ---------- heartbeat / battery / connection ----------
    def _poll_daemon_status(self):
        """Refresh mode/mic notes if dspd appears or disappears."""
        running = daemon_running()
        if running != getattr(self, "_daemon_was_running", None):
            self._daemon_was_running = running
            self._update_mode_note()
            self._update_mic_note()
        return True

    def heartbeat(self):
        if not self.discovered:
            self._start_discovery()
        else:
            self.send("battery", 0, cb=self.got_battery)
        if self.profile["has_boom_mic"]:
            self._refresh_hostmic()
        return True

    def got_battery(self, status, reply):
        if status == "ACK":
            mv = (reply[4] << 8) | reply[5]
            flags = reply[6]
            state, charging = batt_state(flags)
            pct = batt_percent(mv)
            icon = "🔌" if charging else ("❓" if charging is None else "🔋")
            if charging is not None:
                self.health.add_sample(time.time(), mv, charging)
            self._refresh_health_display(mv, pct, state)
            self.batt_label.set_markup(
                f"{icon} <b>{pct}%</b>  <span size='small' foreground='gray'>"
                f"{mv} mV · {state}</span>")
            self.batt_label.set_tooltip_text(f"{mv} mV, {state} (flags {flags:#04x})")
            self._update_tray_battery(f"{icon} {pct}% · {state}")
            if self.connected is not True:
                was_off = self.connected is False
                self.connected = True
                self.conn_label.set_markup("<span foreground='#2ec27e'>●</span>")
                self.conn_label.set_tooltip_text("headset on")
                if was_off:
                    # power-on wipes on-device state - re-assert panel settings
                    self.log("--- headset powered on: re-applying panel state ---")
                    self._assert_device_state(initial=False)
        else:
            self._mark_disconnected()

    def _mark_disconnected(self):
        self.health.mark_offline()
        self._refresh_health_display(None, None, "headset off")
        self._update_tray_battery("headset off")
        if self.connected is not False:
            self.connected = False
            self.conn_label.set_markup("<span foreground='gray'>●</span>")
            self.conn_label.set_tooltip_text("headset off / unreachable")
            self.batt_label.set_markup("🔋 —")
            self.log("--- headset unreachable (powered off?) ---")


def error_dialog(text):
    dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                            buttons=Gtk.ButtonsType.CLOSE, text=text)
    dlg.run()
    dlg.destroy()


def acquire_single_instance():
    """flock guard: two instances would fight over the hidraw node (and show
    two tray icons). Returns the held lock fd, or None if already running."""
    rundir = runtime_dir()
    os.makedirs(rundir, exist_ok=True)
    fd = os.open(os.path.join(rundir, "g935-control.lock"),
                 os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main():
    global ALSA_USBID, MIC_SWITCH_NAME
    lock = acquire_single_instance()
    if lock is None:
        error_dialog("Another instance is already running (check the tray).")
        return
    found = find_headset(known_pids=set(DEVICE_PROFILES))
    if not found:
        error_dialog("No Logitech HID++ headset found (no 046D hidraw node with "
                     "a HID++ report descriptor).\nIs the receiver plugged in?")
        return
    path, pid, name = found
    if pid not in DEVICE_PROFILES:
        # vendor page 0xFF43 also matches Logitech mice/keyboards/receivers;
        # don't silently start probing whatever was found
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO,
                                text=f'Found "{name}" (PID {pid:04x}) — not a known '
                                     "headset.\nThis tool is only tested on the "
                                     "Logitech G935. Probe it anyway?")
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.YES:
            return
    prof = {**GENERIC_PROFILE, **DEVICE_PROFILES.get(pid, {"name": name})}
    if prof["alsa_usbid"]:
        ALSA_USBID, MIC_SWITCH_NAME = prof["alsa_usbid"], prof["mic_switch_name"]
    # Probe permissions before building the full UI (worker opens later).
    try:
        probe = open_hidraw(path)
        os.close(probe)
    except PermissionError:
        error_dialog(f"No permission to open {path}.\n\n"
                     "Install the udev rule from this repo:\n"
                     "  sudo cp 99-g935.rules /etc/udev/rules.d/\n"
                     "  sudo udevadm control --reload && sudo udevadm trigger\n"
                     "then unplug/replug the receiver.\n\n"
                     f"(Looking for hidraw access on {path}, PID {pid:04x}.)")
        return
    except OSError as e:
        error_dialog(f"Could not open {path}:\n{e}\n\n"
                     "Was the receiver unplugged?")
        return
    win = App(path, pid, name)
    win.connect("destroy", win.on_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
