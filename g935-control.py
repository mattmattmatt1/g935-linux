#!/usr/bin/env python3
"""
g935-control.py - GTK control panel for the Logitech G935 (HID++ over hidraw).

Feature map (from the headset IFeatureSet, 2026-07-20):
  idx 02 = 0x0003 DEVICE_INFORMATION   identity + firmware entity
  idx 03 = 0x0005 DEVICE_NAME/TYPE     "G935 Gaming Headset"
  idx 04 = 0x8070 COLOR_LED_EFFECTS   2 zones: 0=logo, 1=front strip
  idx 05 = 0x8010 G-KEYS / software mode  052b 01/00; G1-G3 events
  idx 06 = 0x8310 EQUALIZER           10 bands 32Hz-16kHz, +/-12 dB
  idx 07 = 0x8300 SIDETONE            0-100
  idx 08 = 0x1f20 ADC/BATTERY         voltage mV + flags

Mode is persisted in ~/.config/g935/mode ("ghub"/"hardware"). g935-dspd keeps
the mode across power-ons and owns boom-mic handling; the GUI also reasserts
the persisted mode before replaying EQ/lighting after a confirmed reconnect.
Default mode is hardware (stock) until the user opts in.

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
    HEALTH_DEFAULTS, BatteryAlerts, HealthTracker, batt_percent, batt_state,
    build_insights, merge_discharge_sessions, session_full_runtime_h,
)
from g935.charts import (
    ChargeHistoryChart, DrainProfileChart, ExpectActualChart,
    HealthGaugeChart, SessionRuntimeChart,
    build_expected_overlay, build_history_points,
)
from g935.daemon_status import daemon_running
from g935.features import (
    DEVICE_TYPES, FEATURE_LABELS, LED_EFFECTS, LIGHT_MODES,
    build_light_params, format_frequency, parse_device_info, parse_eq_info,
    parse_firmware_info, parse_frequency_page, parse_gkey_mask,
    parse_led_effect_info, parse_led_state, parse_led_zone_info,
)
from g935.hidpp import (
    ERROR_CODES, HidWorker, PollPresence, find_headset, open_hidraw,
)
from g935.mic import (
    BOOM_UP, BOOM_DOWN, BUTTON, read_boom, read_host_mic_switch, set_host_mic_switch,
)
from g935.mode import load_mode, save_mode
from g935.paths import config_dir, ensure_config_dir, runtime_dir
from g935.stereo_route import fix_stereo, inspect_stereo, notify_user
from g935.volume_wheel import (
    DEFAULT_FINE_STEP, DEFAULT_STEP, DIRECTION_GUARD_S, EVIOCGRAB,
    FAST_ROLL_GAP_S, FINE_INTERVAL_S,
    HOLD_REPEAT_DELAY_S, HOLD_REPEAT_INTERVAL_S, INPUT_EVENT,
    KEY_VOLUMEDOWN, KEY_VOLUMEUP, MEDIUM_ROLL_GAP_S,
    analyze_calibration, find_wheel_device,
    parse_key_events,
)

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

APP_CSS = """
.g935-window {
    background-color: @theme_bg_color;
}
.g935-status {
    background-color: alpha(@theme_fg_color, 0.035);
    border-bottom: 1px solid alpha(@theme_fg_color, 0.10);
}
.g935-chip {
    background-color: alpha(@theme_fg_color, 0.07);
    border: 1px solid alpha(@theme_fg_color, 0.10);
    border-radius: 999px;
    padding: 5px 10px;
}
.g935-card > border {
    background-color: alpha(@theme_fg_color, 0.025);
    border: 1px solid alpha(@theme_fg_color, 0.13);
    border-radius: 10px;
}
.g935-section-title {
    font-weight: 700;
    font-size: 1.05em;
    padding: 0 7px;
}
.g935-primary-title {
    font-weight: 700;
    font-size: 1.12em;
}
.g935-subtitle {
    color: alpha(@theme_fg_color, 0.68);
}
.g935-key {
    background-color: alpha(@theme_fg_color, 0.07);
    border: 1px solid alpha(@theme_fg_color, 0.12);
    border-radius: 999px;
    padding: 4px 10px;
    font-weight: 700;
}
.g935-zone {
    background-color: alpha(@theme_fg_color, 0.035);
    border: 1px solid alpha(@theme_fg_color, 0.08);
    border-radius: 8px;
    padding: 8px;
}
.g935-kpi > border {
    background-color: alpha(@theme_fg_color, 0.045);
    border: 1px solid alpha(@theme_fg_color, 0.11);
    border-radius: 8px;
}
.g935-wear > border {
    background-color: alpha(@warning_color, 0.075);
    border: 1px solid alpha(@warning_color, 0.30);
    border-radius: 10px;
}
.g935-wear-copy {
    font-size: 1.02em;
}
.g935-console-expander {
    background-color: alpha(@theme_fg_color, 0.025);
    border: 1px solid alpha(@theme_fg_color, 0.13);
    border-radius: 10px;
    padding: 10px;
}
.g935-preset-menu {
    padding: 12px;
}
.g935-preset-category {
    color: alpha(@theme_fg_color, 0.68);
    font-weight: 700;
    padding-top: 4px;
}
button.g935-preset-item {
    padding: 7px 12px;
}
.g935-info-key {
    color: alpha(@theme_fg_color, 0.62);
}
.g935-page {
    background-color: transparent;
}
.g935-card button {
    min-height: 28px;
}
"""


def install_app_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(APP_CSS.encode("utf-8"))
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def style(widget, *classes):
    context = widget.get_style_context()
    for css_class in classes:
        context.add_class(css_class)
    return widget


# HID++ 2.0 features this app knows how to drive. Indices are discovered live.
FEATURES = [
    ("devinfo",  0x0003),
    ("devname",  0x0005),
    ("gkeys",    0x8010),   # + the G935 "G HUB mode" side effect
    ("lighting", 0x8070),
    ("eq",       0x8310),
    ("sidetone", 0x8300),
    ("battery",  0x1f20),
]

UI_FILE = os.path.join(config_dir(), "ui.json")
WHEEL_CAPTURE_FILE = os.path.join(config_dir(), "wheel-capture.json")
ALSA_USBID = "046d:0a87"    # reassigned from the device profile in main()
MIC_SWITCH_NAME = "Mic Capture Switch"


def _read_host_mic():
    return read_host_mic_switch(ALSA_USBID, MIC_SWITCH_NAME)


def _set_host_mic(on):
    set_host_mic_switch(on, ALSA_USBID, MIC_SWITCH_NAME)


EQ_BANDS = ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
EQ_PRESETS = {
    "Flat":           [0] * 10,
    "G HUB baseline": [0, 0, 0, -1, 3, 4, 3, 3, 1, 0],
    "Bass boost":     [6, 5, 3, 1, 0, 0, 0, 0, 0, 0],
    "Deep bass":      [8, 7, 5, 3, 1, 0, -1, -1, 0, 1],
    "Warm":           [3, 3, 2, 2, 1, 0, -1, -1, -1, -2],
    "Vocal clarity":  [-3, -2, -1, 0, 1, 3, 5, 4, 2, 1],
    "Podcast":        [-6, -5, -3, -1, 2, 4, 5, 4, 2, 0],
    "FPS footsteps":  [-5, -4, -2, 0, 2, 4, 6, 5, 3, 1],
    "Cinematic":      [5, 4, 2, 0, -1, 1, 3, 4, 4, 3],
    "Rock":           [4, 3, 1, -1, -2, 1, 3, 4, 3, 2],
    "Electronic":     [5, 4, 1, -1, -2, 0, 2, 4, 5, 4],
    "Treble boost":   [0, 0, 0, 0, 0, 1, 3, 5, 6, 7],
    "V-shape":        [4, 3, 1, -1, -2, -2, -1, 2, 4, 5],
    "Classical":      [-2, -1, 0, 1, 2, 2, 3, 3, 2, 1],
    "Jazz":           [2, 1, 0, 1, 2, 3, 3, 2, 2, 3],
    "Acoustic":       [-2, -1, 1, 2, 3, 3, 2, 2, 1, 0],
    "Late night":     [-4, -3, -2, 0, 2, 3, 2, 0, -2, -3],
    "Immersive":      [5, 3, 1, -1, 0, 2, 4, 3, 2, 2],
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


UI_DEFAULTS = {
    "hidden_sinks": [], "hidden_sources": [], "lighting": {},
    "wheel_enabled": True, "wheel_step": DEFAULT_STEP,
    "wheel_fine_step": DEFAULT_FINE_STEP,
    "wheel_fine_interval_ms": round(FINE_INTERVAL_S * 1000),
    "wheel_calibrated": False,
    "wheel_fast_gap_ms": round(FAST_ROLL_GAP_S * 1000),
    "wheel_medium_gap_ms": round(MEDIUM_ROLL_GAP_S * 1000),
    "wheel_direction_guard_ms": round(DIRECTION_GUARD_S * 1000),
    "wheel_hold_delay_ms": round(HOLD_REPEAT_DELAY_S * 1000),
    "wheel_hold_interval_ms": round(HOLD_REPEAT_INTERVAL_S * 1000),
    "wheel_calibration_version": 4,
}

class AppSettings:
    """ui.json: audio-device visibility + saved lighting state. Loaded once,
    saved atomically on change (same pattern as HealthTracker)."""

    def __init__(self):
        try:
            with open(UI_FILE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.data = {
            **UI_DEFAULTS,
            **{k: data[k] for k in UI_DEFAULTS if k in data},
        }
        # Version 1 treated a long press as an 800 ms coarse repeat. Version 2
        # decodes press duration continuously; version 3 exposes fine feel;
        # version 4 adds USB-mixer flood protection and safe reversal.
        try:
            wheel_profile_version = int(
                data.get("wheel_calibration_version", 1))
        except (TypeError, ValueError):
            wheel_profile_version = 1
        if wheel_profile_version < 2:
            self.data["wheel_hold_delay_ms"] = round(
                HOLD_REPEAT_DELAY_S * 1000)
            self.data["wheel_hold_interval_ms"] = round(
                HOLD_REPEAT_INTERVAL_S * 1000)
            self.data["wheel_calibrated"] = False
        if wheel_profile_version < 3:
            self.data["wheel_fine_step"] = DEFAULT_FINE_STEP
            self.data["wheel_fine_interval_ms"] = round(
                FINE_INTERVAL_S * 1000)
        if wheel_profile_version < 4:
            self.data["wheel_hold_interval_ms"] = round(
                HOLD_REPEAT_INTERVAL_S * 1000)
            self.data["wheel_direction_guard_ms"] = round(
                DIRECTION_GUARD_S * 1000)
            self.data["wheel_calibrated"] = False
        self.data["wheel_calibration_version"] = 4

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
        style(self, "g935-window")
        self.set_default_size(840, 900)
        self.set_icon_name("audio-headset")
        self.connected = None   # None = unknown, True/False after first battery poll
        self._battery_presence = PollPresence(miss_limit=3)
        self.pid = pid

        self.settings = AppSettings()
        self.features = {}        # feature attr -> discovered index
        self.feature_meta = {}    # attr -> (feature id, type flags, version)
        self.discovered = False
        self._discovering = False
        self.section_frames = {}  # feature attr -> widget to show/hide
        self.mixer_win = None
        self.indicator = None
        self.tray_batt_item = None
        self._vol_pending = {}    # (kind, name) -> debounce source id
        self._vol_containers = []
        self._daemon_poll = None
        self._gkey_mask = 0
        self._gkey_count = 0
        self._device_firmware = []
        self._device_name_parts = bytearray()
        self._device_name_count = 0
        self._light_caps = {}

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
            event_cb=self.on_hidpp_event,
        )
        self.worker.start()
        self.audio = AudioControl(self._on_audio_change)

        # ---- header bar: page switcher + live status ----
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        self.set_titlebar(hb)

        brand = Gtk.Box(spacing=7)
        brand_icon = Gtk.Image.new_from_icon_name(
            "audio-headset", Gtk.IconSize.BUTTON)
        brand.pack_start(brand_icon, False, False, 0)
        brand_label = Gtk.Label()
        brand_label.set_markup(
            f"<b>{GLib.markup_escape_text(self.profile['name'])}</b>")
        brand.pack_start(brand_label, False, False, 0)
        hb.pack_start(brand)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        hb.set_custom_title(switcher)

        # ---- status bar, inside the window above the pages (visible on both) ----
        self.conn_label = Gtk.Label()
        self.conn_label.set_markup(
            "<span foreground='gray'>●</span> Connecting")
        self.conn_label.set_tooltip_text("headset connection")
        style(self.conn_label, "g935-chip")
        self.batt_label = Gtk.Label()
        self.batt_label.set_markup("🔋 …")
        self.batt_label.set_tooltip_text("battery")
        style(self.batt_label, "g935-chip")
        self.mic_label = Gtk.Label()
        self.mic_label.set_markup("🎤 …")
        self.mic_label.set_tooltip_text("boom position (polled from the headset)")
        style(self.mic_label, "g935-chip")
        self.stereo_label = Gtk.Label()
        self.stereo_label.set_markup("")
        self.stereo_label.set_no_show_all(True)
        self.stereo_label.hide()
        self.stereo_label.set_tooltip_text("PipeWire stereo route (L/R into headset)")
        style(self.stereo_label, "g935-chip")

        status = Gtk.Box(spacing=8)
        style(status, "g935-status")
        status.set_margin_start(0)
        status.set_margin_end(0)
        status.set_margin_top(0)
        status.set_margin_bottom(0)
        status.set_border_width(10)
        status.pack_start(self.conn_label, False, False, 0)
        status.pack_start(self.batt_label, False, False, 0)
        status.pack_start(self.stereo_label, False, False, 0)
        status.pack_end(self.mic_label, False, False, 0)

        # Banner when right-channel link is missing after sleep/resume
        self.stereo_banner = Gtk.InfoBar()
        self.stereo_banner.set_message_type(Gtk.MessageType.WARNING)
        self.stereo_banner.set_show_close_button(True)
        self.stereo_banner.connect("response", self._on_stereo_banner_response)
        self.stereo_banner.set_no_show_all(True)
        self.stereo_banner.hide()
        banner_lbl = Gtk.Label(xalign=0)
        banner_lbl.set_line_wrap(True)
        banner_lbl.set_markup(
            "<b>Right earcup silent</b> — PipeWire only linked the left channel "
            "(common after sleep). Click <b>Fix stereo</b> to re-link.")
        content = self.stereo_banner.get_content_area()
        content.add(banner_lbl)
        self.stereo_banner.add_button("Fix stereo", Gtk.ResponseType.APPLY)
        content.show_all()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(status, False, False, 0)
        outer.pack_start(self.stereo_banner, False, False, 0)
        outer.pack_start(Gtk.Separator(), False, False, 0)
        outer.pack_start(self.stack, True, True, 0)
        self.add(outer)

        self._stereo_broken = False
        self._stereo_fixing = False
        self._stereo_last_notify = 0.0
        self._stereo_dismissed_until = 0.0  # user closed banner; quiet until change
        self._batt_alerts = BatteryAlerts()

        # ================= Control page (everything except the console) =================
        sound = self._page("control", "Control", scroll=True)

        gk = self._frame(sound, "Software mode")
        row = Gtk.Box(spacing=8)
        lbl = Gtk.Label(xalign=0)
        style(lbl, "g935-primary-title")
        lbl.set_text("Open soundstage + host-managed controls")
        row.pack_start(lbl, True, True, 0)
        self.dsp_sw = Gtk.Switch()
        self.dsp_sw.set_active(load_mode() == "ghub")
        self.dsp_sw.set_tooltip_text(
            "Saved target mode. The G935 exposes no readback for this state, "
            "so software mode is reasserted after every battery reading.")
        self.dsp_sw.connect("notify::active", self.on_dsp_toggle)
        row.pack_end(self.dsp_sw, False, False, 0)
        gk.pack_start(row, False, False, 0)
        self.mode_note = Gtk.Label(xalign=0)
        style(self.mode_note, "g935-subtitle")
        self.mode_note.set_line_wrap(True)
        gk.pack_start(self.mode_note, False, False, 0)
        key_row = Gtk.Box(spacing=8)
        live_label = Gtk.Label(label="Live G keys", xalign=0)
        style(live_label, "g935-subtitle")
        key_row.pack_start(live_label, False, False, 0)
        self.gkey_labels = []
        for i in range(3):
            key = Gtk.Label(label=f"G{i + 1}")
            style(key, "g935-key")
            key.set_sensitive(False)
            key_row.pack_start(key, False, False, 0)
            self.gkey_labels.append(key)
        self.gkey_last = Gtk.Label(label="waiting for an event", xalign=0)
        style(self.gkey_last, "g935-subtitle")
        key_row.pack_start(self.gkey_last, True, True, 8)
        gk.pack_start(key_row, False, False, 0)
        self.section_frames["gkeys"] = gk.get_parent()
        self._update_mode_note()

        st = self._frame(sound, "Sidetone (your mic mixed into your ears)")
        self.section_frames["sidetone"] = st.get_parent()
        self.sidetone = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.sidetone.set_hexpand(True)
        self.sidetone.set_value(100)
        self._guard_slider_scroll(self.sidetone)
        self._sidetone_pending = None
        self.sidetone.connect("value-changed", self.on_sidetone)
        st.pack_start(self.sidetone, False, False, 0)

        if self.profile["has_boom_mic"]:
            mic = self._frame(sound, "Microphone")
            self.mic_big = Gtk.Label(xalign=0)
            style(self.mic_big, "g935-primary-title")
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
            action_row = Gtk.Box()
            action_row.pack_end(b, False, False, 0)
            mic.pack_start(action_row, False, False, 0)
            self.mic_note = Gtk.Label(xalign=0)
            style(self.mic_note, "g935-subtitle")
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

        # ================= Lighting page =================
        lighting_page = self._page("lighting", "Lighting", scroll=True)
        li = self._frame(lighting_page, "On-headset lighting")
        self.section_frames["lighting"] = self.stack.get_child_by_name("lighting")
        self.light_box = li
        self.zone_widgets = {}
        if self.profile["zones"]:
            self._build_lighting_rows(self.profile["zones"])

        # ================= Battery Health page =================
        self.health = HealthTracker()

        hp = self._page("health", "Battery Health", scroll=True)

        # Power belongs with battery behavior and is intentionally first.
        power = self._frame(hp, "Power management")
        self.power_frame = power.get_parent()
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(
            label="Automatically power off after", xalign=0),
            True, True, 0)
        self.power_timeout = Gtk.SpinButton.new_with_range(0, 255, 1)
        self.power_timeout.set_value(15)
        row.pack_start(self.power_timeout, False, False, 0)
        unit = Gtk.Label(label="minutes")
        style(unit, "g935-subtitle")
        row.pack_start(unit, False, False, 0)
        b = Gtk.Button(label="Apply")
        b.get_style_context().add_class("suggested-action")
        b.connect("clicked", self.on_power_timeout_apply)
        row.pack_end(b, False, False, 0)
        power.pack_start(row, False, False, 0)
        note = Gtk.Label(
            label="Set 0 minutes to keep the headset on indefinitely.", xalign=0)
        style(note, "g935-subtitle")
        power.pack_start(note, False, False, 0)

        # Put wear in its own high-contrast card so health and lost runtime are
        # understandable without reading the charts.
        wear = self._frame(hp, "Battery wear")
        style(wear.get_parent(), "g935-wear")
        wear_row = Gtk.Box(spacing=10)
        wear_row.set_homogeneous(True)
        self.kpi_health = self._kpi_card(wear_row, "Estimated health")
        self.kpi_lost = self._kpi_card(
            wear_row, "Runtime lost per full charge")
        wear.pack_start(wear_row, False, False, 2)
        self.health_wear_description = Gtk.Label(xalign=0)
        style(self.health_wear_description, "g935-wear-copy")
        self.health_wear_description.set_line_wrap(True)
        wear.pack_start(self.health_wear_description, False, False, 2)

        # ---- Hero: remaining time + status + insights ----
        hero = self._frame(hp, "At a glance")
        self.health_remain = Gtk.Label(xalign=0)
        self.health_remain.set_markup(
            "<span size='xx-large' weight='bold'>—</span>")
        self.health_remain.set_line_wrap(True)
        hero.pack_start(self.health_remain, False, False, 0)

        self.health_status = Gtk.Label(xalign=0)
        self.health_status.set_markup("<span size='large'>…</span>")
        self.health_status.set_line_wrap(True)
        hero.pack_start(self.health_status, False, False, 0)

        self.health_session = Gtk.Label(xalign=0)
        self.health_session.set_line_wrap(True)
        hero.pack_start(self.health_session, False, False, 0)

        self.health_insights = Gtk.Label(xalign=0)
        self.health_insights.set_line_wrap(True)
        self.health_insights.set_markup("")
        hero.pack_start(self.health_insights, False, False, 4)

        # KPI strip — supporting runtime metrics
        kpi_row = Gtk.Box(spacing=8)
        kpi_row.set_homogeneous(True)
        self.kpi_full = self._kpi_card(kpi_row, "Full runtime")
        self.kpi_drain = self._kpi_card(kpi_row, "Live drain")
        self.kpi_cap = self._kpi_card(kpi_row, "Capacity")
        hero.pack_start(kpi_row, False, False, 6)

        # ---- Charge history (the visual story) ----
        hist_f = self._frame(hp, "Charge history")
        self.chart_history = ChargeHistoryChart()
        hist_f.pack_start(self.chart_history, False, False, 2)
        self.history_caption = Gtk.Label(xalign=0)
        self.history_caption.set_line_wrap(True)
        hist_f.pack_start(self.history_caption, False, False, 0)

        # ---- Health gauge + remaining comparison ----
        compare = self._frame(hp, "Health & time left")
        row = Gtk.Box(spacing=8)
        self.chart_gauge = HealthGaugeChart()
        self.chart_gauge.set_size_request(200, 150)
        self.chart_gauge.set_hexpand(False)
        row.pack_start(self.chart_gauge, False, False, 0)
        self.chart_expect = ExpectActualChart()
        row.pack_start(self.chart_expect, True, True, 0)
        compare.pack_start(row, False, False, 2)
        self.health_detail = Gtk.Label(xalign=0)
        self.health_detail.set_line_wrap(True)
        compare.pack_start(self.health_detail, False, False, 2)

        # ---- Sessions ----
        sess_f = self._frame(hp, "Usage sessions")
        self.chart_sessions = SessionRuntimeChart()
        sess_f.pack_start(self.chart_sessions, False, False, 2)
        self.health_hist = Gtk.Label(xalign=0)
        self.health_hist.set_line_wrap(True)
        self.health_hist.set_markup("<small>no sessions recorded</small>")
        sess_f.pack_start(self.health_hist, False, False, 2)

        # ---- Drain profile ----
        prof_f = self._frame(hp, "Drain profile (learned)")
        self.chart_profile = DrainProfileChart()
        prof_f.pack_start(self.chart_profile, False, False, 2)
        self.health_profile = Gtk.Label(xalign=0)
        self.health_profile.set_line_wrap(True)
        self.health_profile.set_markup("<small>collecting discharge datapoints…</small>")
        prof_f.pack_start(self.health_profile, False, False, 0)

        # ---- Tracking + how to read (collapsed) ----
        about = Gtk.Expander(label="How these numbers work")
        about.set_expanded(False)
        about_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        about_box.set_border_width(8)
        about.add(about_box)
        hp.pack_start(about, False, False, 0)

        track_row = Gtk.Box(spacing=8)
        track_lbl = Gtk.Label(xalign=0)
        track_lbl.set_markup("<b>Track battery health</b>  "
                             "(log charge/discharge while the panel is open)")
        track_row.pack_start(track_lbl, True, True, 0)
        self.track_sw = Gtk.Switch()
        self.track_sw.set_active(self.health.settings["tracking"])
        self.track_sw.connect("notify::active", self.on_track_toggle)
        track_row.pack_end(self.track_sw, False, False, 0)
        about_box.pack_start(track_row, False, False, 0)

        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_markup(
            "<small>The G935 only reports <b>voltage + charging flag</b> — no coulomb "
            "counter. Health is an honest extrapolation: observed drain over solid "
            "sessions → projected full-to-empty runtime, compared to the rated spec.\n\n"
            "<b>While charging</b>, the ADC often reads the <i>charger path</i> (rail "
            "spikes to ~4.3 V+), not resting cell voltage. SoC % freezes at the last "
            "off-charger reading; the raw path mV is shown separately. Full-charge "
            "peaks are taken from rest after unplug, never from rail spikes.\n\n"
            "<b>Blue</b> = measured from your datapoints. <b>Amber</b> = rated/expected. "
            "<b>Green</b> on the history chart = charging (held SoC, not rail %).\n"
            "Short discharges stitch across brief headset-off gaps (≤15 min). "
            "A solid health evidence point needs ~30 min on-time and ≥8% drop.\n"
            "After 3 qualifying sessions, remaining time is anchored to the median "
            "full-runtime history; a faster live drain can shorten it. Before then, "
            "recent drain and the voltage-bin profile provide the early estimate. "
            "A live rate needs ≥10 min and ≥3% movement so ADC wobble cannot create "
            "an implausibly long ETA.</small>")
        about_box.pack_start(note, False, False, 0)

        # ---- Spec (collapsed for aftermarket cells) ----
        spec_exp = Gtk.Expander(
            label="Battery specification (stock G935 — edit for aftermarket cells)")
        spec_exp.set_expanded(False)
        spec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        spec.set_border_width(8)
        spec_exp.add(spec)
        hp.pack_start(spec_exp, False, False, 0)

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

        # Keep aliases so older refresh paths / tests don't break if referenced
        self.health_live = self.health_status
        self.health_est = self.kpi_health["value"]

        self._refresh_health_display(None, None, "…")
        self.section_frames["battery"] = self.stack.get_child_by_name("health")

        # ================= Hardware page =================
        hw = self._page("hardware", "Hardware", scroll=True)

        ident = self._frame(hw, "Headset identity")
        self.hw_values = {}
        for key, title in (
                ("name", "Marketing name"),
                ("type", "Device type"),
                ("usb", "USB product ID"),
                ("unit", "Unit ID"),
                ("model", "Model ID"),
                ("transport", "Reported transports"),
                ("firmware", "Firmware"),
                ("entities", "Firmware entities")):
            self.hw_values[key] = self._info_row(ident, title)
        self.hw_values["usb"].set_text(f"046D:{self.pid:04X}")

        caps = self._frame(hw, "Advertised HID++ features")
        self.hw_features = Gtk.Label(xalign=0)
        self.hw_features.set_selectable(True)
        self.hw_features.set_line_wrap(True)
        self.hw_features.set_text("Discovering…")
        caps.pack_start(self.hw_features, False, False, 0)

        live = self._frame(hw, "Live capability details")
        self.hw_caps = Gtk.Label(xalign=0)
        self.hw_caps.set_selectable(True)
        self.hw_caps.set_line_wrap(True)
        self.hw_caps.set_text("Probing…")
        live.pack_start(self.hw_caps, False, False, 0)
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_markup(
            "<small>Only functions acknowledged by this headset firmware are "
            "shown as controls. Newer HID++ functions that return “invalid "
            "function” are intentionally omitted.</small>")
        live.pack_start(note, False, False, 4)

        # Raw protocol tools live at the bottom of Hardware and stay out of the
        # way until explicitly expanded.
        self.console_expander = Gtk.Expander()
        style(self.console_expander, "g935-console-expander")
        self.console_expander.set_expanded(False)
        console_title = Gtk.Box(spacing=8)
        title = Gtk.Label()
        title.set_markup("<b>Advanced HID++ console</b>")
        console_title.pack_start(title, False, False, 0)
        subtitle = Gtk.Label(label="raw traffic and developer commands")
        style(subtitle, "g935-subtitle")
        console_title.pack_start(subtitle, False, False, 0)
        self.console_expander.set_label_widget(console_title)
        console = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        console.set_border_width(8)
        self.console_expander.add(console)
        hw.pack_start(self.console_expander, False, False, 0)

        note = Gtk.Label(xalign=0)
        style(note, "g935-subtitle")
        note.set_line_wrap(True)
        note.set_text(
            "Developer controls can change on-device state directly. "
            "Use raw commands only when you know the HID++ frame.")
        console.pack_start(note, False, False, 0)

        if self.profile["connect_burst"]:
            row = Gtk.Box(spacing=8)
            b = Gtk.Button(label="Apply G HUB connect defaults (full sequence)")
            b.connect("clicked", self.on_full_sequence)
            row.pack_start(b, True, True, 0)
            console.pack_start(row, False, False, 0)

        row = Gtk.Box(spacing=8)
        self.raw_entry = Gtk.Entry()
        self.raw_entry.set_placeholder_text("raw HID++ hex, e.g. 11ff062b")
        self.raw_entry.connect("activate", self.on_raw_send)
        row.pack_start(self.raw_entry, True, True, 0)
        b = Gtk.Button(label="Send")
        b.connect("clicked", self.on_raw_send)
        row.pack_start(b, False, False, 0)
        console.pack_start(row, False, False, 0)

        self.logbuf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.logbuf, editable=False, monospace=True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(-1, 280)
        scroll.add(view)
        console.pack_start(scroll, False, False, 0)
        self.logview = view

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
        wheel_row = Gtk.Box(spacing=8)
        wheel_copy = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2)
        wheel_title = Gtk.Label(
            label="Headset earcup wheel", xalign=0)
        style(wheel_title, "g935-primary-title")
        wheel_copy.pack_start(wheel_title, False, False, 0)
        wheel_note = Gtk.Label(xalign=0)
        style(wheel_note, "g935-subtitle")
        wheel_note.set_line_wrap(True)
        wheel_note.set_text(
            "Fine movements use short presses; fast sweeps are decoded from "
            "how long the receiver holds the direction. Every percentage is "
            "applied as a smooth 1% step. "
            "Direction chatter and runaway repeats are filtered. "
            "Wheel-up stops at 100%. "
            "Use a slider for deliberate boost.")
        wheel_copy.pack_start(wheel_note, False, False, 0)
        wheel_row.pack_start(wheel_copy, True, True, 0)
        self.wheel_sw = Gtk.Switch()
        self.wheel_sw.set_active(self.settings.data["wheel_enabled"])
        self.wheel_sw.connect(
            "notify::active", self.on_wheel_setting_changed)
        wheel_row.pack_end(self.wheel_sw, False, False, 0)
        volf.pack_start(wheel_row, False, False, 0)

        feel_heading = Gtk.Box(spacing=8)
        feel_title = Gtk.Label(label="Wheel feel", xalign=0)
        style(feel_title, "g935-primary-title")
        feel_heading.pack_start(feel_title, True, True, 0)
        reset_feel = Gtk.Button(label="Reset balanced defaults")
        reset_feel.connect("clicked", self.on_wheel_defaults)
        feel_heading.pack_end(reset_feel, False, False, 0)
        volf.pack_start(feel_heading, False, False, 4)

        feel_grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        feel_grid.set_hexpand(True)
        self._wheel_feel_controls = {}

        def add_feel_control(row, attr, key, title, detail,
                             lower, upper, step, suffix):
            copy = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=1)
            label = Gtk.Label(label=title, xalign=0)
            copy.pack_start(label, False, False, 0)
            sub = Gtk.Label(label=detail, xalign=0)
            style(sub, "g935-subtitle")
            sub.set_line_wrap(True)
            copy.pack_start(sub, False, False, 0)
            feel_grid.attach(copy, 0, row, 1, 1)

            spin_box = Gtk.Box(spacing=5)
            spin = Gtk.SpinButton.new_with_range(lower, upper, step)
            spin.set_value(self.settings.data[key])
            spin.connect("value-changed", self.on_wheel_setting_changed)
            spin_box.pack_start(spin, False, False, 0)
            spin_box.pack_start(
                Gtk.Label(label=suffix), False, False, 0)
            feel_grid.attach(spin_box, 1, row, 1, 1)
            setattr(self, attr, spin)
            self._wheel_feel_controls[key] = spin

        add_feel_control(
            0, "wheel_fine_step", "wheel_fine_step",
            "Fine adjustment", "Immediate change from a tiny movement.",
            1, 5, 1, "%")
        add_feel_control(
            1, "wheel_fine_interval", "wheel_fine_interval_ms",
            "Fine cadence", "Lower feels more responsive during slow motion.",
            40, 150, 1, "ms")
        add_feel_control(
            2, "wheel_hold_delay", "wheel_hold_delay_ms",
            "Acceleration point",
            "How long a movement stays in the gentle fine-control range.",
            120, 600, 5, "ms")
        add_feel_control(
            3, "wheel_hold_interval", "wheel_hold_interval_ms",
            "Fast cadence", "Delay between each 1% change during a fast sweep.",
            25, 60, 1, "ms")
        add_feel_control(
            4, "wheel_step", "wheel_step",
            "Maximum fast-sweep change",
            "Caps the total change produced by one continuous sweep.",
            10, 50, 1, "%")
        add_feel_control(
            5, "wheel_direction_guard", "wheel_direction_guard_ms",
            "Reversal protection",
            "Filters brief opposite reports; set to 0 for immediate reversal.",
            0, 1000, 10, "ms")
        volf.pack_start(feel_grid, False, False, 2)

        diagnostics = Gtk.Expander(label="Diagnostics & auto-fit")
        diagnostics_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        diagnostics_box.set_border_width(8)
        diagnostics_note = Gtk.Label(xalign=0)
        diagnostics_note.set_line_wrap(True)
        diagnostics_note.set_text(
            "Optional. Capture records the receiver's raw press/release "
            "timeline and can fit the controls above. Manual tuning works "
            "without running it.")
        diagnostics_box.pack_start(
            diagnostics_note, False, False, 0)
        calibration_row = Gtk.Box(spacing=8)
        self.wheel_calibration_status = Gtk.Label(xalign=0)
        style(self.wheel_calibration_status, "g935-subtitle")
        self.wheel_calibration_status.set_line_wrap(True)
        calibration_row.pack_start(
            self.wheel_calibration_status, True, True, 0)
        calibrate = Gtk.Button(label="Capture & auto-fit…")
        calibrate.connect("clicked", self.on_wheel_calibrate)
        calibration_row.pack_end(calibrate, False, False, 0)
        diagnostics_box.pack_start(calibration_row, False, False, 0)
        diagnostics.add(diagnostics_box)
        volf.pack_start(diagnostics, False, False, 2)
        self._refresh_wheel_calibration_status()
        volf.pack_start(Gtk.Separator(), False, False, 2)

        self.settings_vol_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        volf.pack_start(self.settings_vol_box, False, False, 0)
        self._vol_containers.append(self.settings_vol_box)

        # feature discovery first; all device state is asserted from its
        # completion callback. The 5s heartbeat retries discovery while the
        # headset is off and polls battery once it's known.
        self._start_discovery()
        GLib.timeout_add_seconds(5, self.heartbeat)
        GLib.timeout_add_seconds(3, self._poll_daemon_status)
        GLib.timeout_add_seconds(8, self._poll_stereo_route)
        self._setup_tray()
        self.connect("delete-event", self.on_delete_event)
        GLib.idle_add(self._on_audio_change)   # first tray/settings population
        GLib.idle_add(self._poll_stereo_route_once)  # first stereo check

    # ---------- helpers ----------
    def _page(self, name, title, scroll=False):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        style(box, "g935-page")
        box.set_border_width(14)
        if scroll:
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sw.set_shadow_type(Gtk.ShadowType.NONE)
            sw.add(box)
            self.stack.add_titled(sw, name, title)
        else:
            self.stack.add_titled(box, name, title)
        return box

    def _frame(self, parent, title):
        f = Gtk.Frame()
        style(f, "g935-card")
        title_widget = Gtk.Label(label=title, xalign=0)
        style(title_widget, "g935-section-title")
        f.set_label_widget(title_widget)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)
        f.add(box)
        parent.pack_start(f, False, False, 0)
        return box

    def _info_row(self, parent, title):
        row = Gtk.Box(spacing=10)
        key = Gtk.Label(label=title, xalign=0)
        style(key, "g935-info-key")
        key.set_size_request(155, -1)
        value = Gtk.Label(label="—", xalign=0)
        value.set_selectable(True)
        value.set_line_wrap(True)
        row.pack_start(key, False, False, 0)
        row.pack_start(value, True, True, 0)
        parent.pack_start(row, False, False, 0)
        return value

    def _kpi_card(self, parent, title):
        """Compact metric card: title + big value + small subtitle."""
        frame = Gtk.Frame()
        style(frame, "g935-kpi")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(8)
        frame.add(box)
        title_lbl = Gtk.Label(xalign=0)
        title_lbl.set_markup(f"<small>{GLib.markup_escape_text(title)}</small>")
        value_lbl = Gtk.Label(xalign=0)
        value_lbl.set_markup("<span size='large' weight='bold'>—</span>")
        sub_lbl = Gtk.Label(xalign=0)
        sub_lbl.set_markup("<small> </small>")
        sub_lbl.set_line_wrap(True)
        box.pack_start(title_lbl, False, False, 0)
        box.pack_start(value_lbl, False, False, 0)
        box.pack_start(sub_lbl, False, False, 0)
        parent.pack_start(frame, True, True, 0)
        return {"frame": frame, "title": title_lbl, "value": value_lbl, "sub": sub_lbl}

    def _set_kpi(self, card, value_markup, sub_text=""):
        card["value"].set_markup(value_markup)
        esc = GLib.markup_escape_text(sub_text) if sub_text else " "
        card["sub"].set_markup(f"<small>{esc}</small>")

    def _guard_slider_scroll(self, scale):
        """Let the page scroll over a slider until that slider is clicked."""
        scale.set_can_focus(True)
        scale.set_tooltip_text(
            "Click once to focus this slider before adjusting it with the wheel.")
        scale.connect("button-press-event", self._focus_slider)
        scale.connect("scroll-event", self._on_guarded_slider_scroll)

    @staticmethod
    def _focus_slider(scale, _event):
        scale.grab_focus()
        return False

    @staticmethod
    def _on_guarded_slider_scroll(scale, event):
        if scale.has_focus():
            return False

        # Consuming the event protects the value; forward the same motion to
        # the containing page so scrolling over a bank of EQ sliders still
        # moves the page naturally.
        parent = scale.get_parent()
        while parent is not None and not isinstance(parent, Gtk.ScrolledWindow):
            parent = parent.get_parent()
        if parent is None:
            return True

        adj = parent.get_vadjustment()
        step = max(float(adj.get_step_increment()), 36.0)
        direction = event.direction
        if direction == Gdk.ScrollDirection.UP:
            delta = -step
        elif direction == Gdk.ScrollDirection.DOWN:
            delta = step
        elif direction == Gdk.ScrollDirection.SMOOTH:
            ok, _dx, dy = event.get_scroll_deltas()
            if not ok:
                return True
            delta = dy * step
        else:
            return True
        upper = max(adj.get_lower(), adj.get_upper() - adj.get_page_size())
        adj.set_value(min(upper, max(adj.get_lower(), adj.get_value() + delta)))
        return True

    def _build_eq_sliders(self, band_names):
        sliders = Gtk.Box(spacing=4, homogeneous=True)
        self.eq_sliders = []
        self.eq_band_labels = []
        for name in band_names:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            s = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -12, 12, 1)
            s.set_inverted(True)          # +12 at top
            s.set_size_request(-1, 150)
            s.set_value(0)
            s.add_mark(0, Gtk.PositionType.LEFT, None)
            self._guard_slider_scroll(s)
            s.connect("value-changed", self._mark_eq_custom)
            col.pack_start(s, True, True, 0)
            band_label = Gtk.Label(label=name)
            col.pack_start(band_label, False, False, 0)
            sliders.pack_start(col, True, True, 0)
            self.eq_sliders.append(s)
            self.eq_band_labels.append(band_label)
        self.eq_box.pack_start(sliders, False, False, 0)

        preset_row = Gtk.Box(spacing=10)
        preset_label = Gtk.Label(xalign=0)
        preset_label.set_markup("<b>Preset</b>")
        preset_row.pack_start(preset_label, False, False, 0)
        preset_note = Gtk.Label(
            label="choose a tuned profile", xalign=0)
        style(preset_note, "g935-subtitle")
        preset_row.pack_start(preset_note, True, True, 0)

        self.eq_preset_button = Gtk.MenuButton()
        self.eq_preset_button.set_label("Presets")
        self.eq_preset_button.set_size_request(185, -1)
        preset_row.pack_end(self.eq_preset_button, False, False, 0)
        self.eq_box.pack_start(preset_row, False, False, 0)

        popover = Gtk.Popover.new(self.eq_preset_button)
        popover.set_position(Gtk.PositionType.BOTTOM)
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        style(menu_box, "g935-preset-menu")
        menu_box.set_border_width(8)
        popover.add(menu_box)
        self.eq_preset_button.set_popover(popover)

        categories = (
            ("Reference", ("Flat", "G HUB baseline")),
            ("Tone", ("Bass boost", "Deep bass", "Warm", "V-shape",
                      "Treble boost", "Late night")),
            ("Voice & games", ("Vocal clarity", "Podcast", "FPS footsteps",
                               "Cinematic", "Immersive")),
            ("Music", ("Rock", "Electronic", "Classical", "Jazz", "Acoustic")),
        )
        available = self.profile["eq_presets"]
        for category, names in categories:
            valid = [
                name for name in names
                if name in available and (
                    available[name] is None
                    or len(available[name]) == len(band_names))
            ]
            if not valid:
                continue
            category_label = Gtk.Label(label=category, xalign=0)
            style(category_label, "g935-preset-category")
            menu_box.pack_start(category_label, False, False, 0)
            grid = Gtk.Grid(column_spacing=6, row_spacing=6)
            for index, name in enumerate(valid):
                button = Gtk.Button(label=name)
                style(button, "g935-preset-item")
                button.set_hexpand(True)
                button.set_halign(Gtk.Align.FILL)
                button.connect(
                    "clicked", self.on_eq_preset_menu, name, popover)
                grid.attach(button, index % 3, index // 3, 1, 1)
            menu_box.pack_start(grid, False, False, 0)
        menu_box.show_all()
        row = Gtk.Box(spacing=6)
        b = Gtk.Button(label="Apply EQ")
        b.get_style_context().add_class("suggested-action")
        b.connect("clicked", self.on_eq_apply)
        row.pack_start(b, True, True, 0)
        b = Gtk.Button(label="Read active")
        b.set_tooltip_text("Read the EQ currently active in headset RAM")
        b.connect("clicked", lambda *_: self.send("eq", 2, "01", cb=self.got_eq))
        row.pack_start(b, True, True, 0)
        b = Gtk.Button(label="Load saved")
        b.set_tooltip_text("Load the custom EQ stored in headset EEPROM")
        b.connect("clicked", lambda *_: self.send("eq", 2, "00", cb=self.got_eq))
        row.pack_start(b, True, True, 0)
        self.eq_box.pack_start(row, False, False, 0)
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_markup(
            "<small>Apply updates the active EQ immediately and saves it in "
            "headset memory.</small>")
        self.eq_box.pack_start(note, False, False, 0)
        self.eq_box.show_all()

    def _build_lighting_rows(self, zone_names):
        saved_all = self.settings.data["lighting"]
        for zone, zname in enumerate(zone_names):
            saved = saved_all.get(str(zone), {})
            zone_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            style(zone_box, "g935-zone")
            zone_box.set_border_width(8)
            row = Gtk.Box(spacing=8)
            name = Gtk.Label(label=zname, xalign=0)
            style(name, "g935-primary-title")
            name.set_size_request(84, -1)
            row.pack_start(name, False, False, 0)
            combo = Gtk.ComboBoxText()
            for e in LIGHT_MODES:
                combo.append_text(e)
            combo.set_active(saved.get("mode", 2))
            row.pack_start(combo, False, False, 0)
            color = Gtk.ColorButton()
            rgba = Gdk.RGBA(); rgba.parse(saved.get("color", "#00B4FF"))
            color.set_rgba(rgba)
            row.pack_start(color, False, False, 0)
            b = Gtk.Button(label="Apply")
            b.get_style_context().add_class("suggested-action")
            b.connect("clicked", self.on_light_apply, zone)
            row.pack_end(b, False, False, 0)
            zone_box.pack_start(row, False, False, 0)

            opts = Gtk.Box(spacing=7)
            period_label = Gtk.Label(label="Period")
            style(period_label, "g935-subtitle")
            opts.pack_start(period_label, False, False, 0)
            period = Gtk.SpinButton.new_with_range(500, 20000, 100)
            period.set_value(saved.get("period", 5000))
            opts.pack_start(period, False, False, 0)
            intensity_label = Gtk.Label(label="Intensity")
            style(intensity_label, "g935-subtitle")
            opts.pack_start(intensity_label, False, False, 0)
            intensity = Gtk.SpinButton.new_with_range(0, 100, 1)
            intensity.set_value(saved.get(
                "intensity", self.profile["zone_brt"].get(zone, 100)))
            opts.pack_start(intensity, False, False, 0)
            waveform = Gtk.ComboBoxText()
            for text in ("Default", "Sine", "Square", "Triangle",
                         "Sawtooth", "Shark fin", "Exponential"):
                waveform.append_text(text)
            waveform.set_active(saved.get("waveform", 0))
            opts.pack_start(waveform, False, False, 0)
            ramp = Gtk.ComboBoxText()
            for text in ("Default ramp", "Ramp", "No ramp"):
                ramp.append_text(text)
            ramp.set_active(saved.get("ramp", 2))
            opts.pack_start(ramp, False, False, 0)
            zone_box.pack_start(opts, False, False, 0)
            self.light_box.pack_start(zone_box, False, False, 0)
            self.zone_widgets[zone] = {
                "name": name, "mode": combo, "color": color,
                "period": period, "intensity": intensity,
                "waveform": waveform, "ramp": ramp,
            }
            combo.connect("changed", self._update_light_fields, zone)
            self._update_light_fields(combo, zone)
        read_row = Gtk.Box(spacing=6)
        self.light_read_active = Gtk.Button(label="Read active")
        self.light_read_active.set_tooltip_text(
            "Load the effects currently playing from headset RAM")
        self.light_read_active.connect(
            "clicked", lambda *_: self._read_lighting(0))
        self.light_read_active.set_sensitive(False)
        read_row.pack_start(self.light_read_active, True, True, 0)
        self.light_read_saved = Gtk.Button(label="Load saved")
        self.light_read_saved.set_tooltip_text(
            "Load the effects stored in headset EEPROM")
        self.light_read_saved.connect(
            "clicked", lambda *_: self._read_lighting(1))
        self.light_read_saved.set_sensitive(False)
        read_row.pack_start(self.light_read_saved, True, True, 0)
        self.light_box.pack_start(read_row, False, False, 0)
        self.light_boot_row = Gtk.Box(spacing=8)
        self.light_boot_row.pack_start(
            Gtk.Label(label="Headset boot-up animation", xalign=0),
            True, True, 0)
        self.light_boot_switch = Gtk.Switch()
        self.light_boot_switch.set_active(True)
        self.light_boot_switch.connect(
            "notify::active", self.on_light_boot_toggle)
        self.light_boot_row.pack_end(
            self.light_boot_switch, False, False, 0)
        self.light_boot_row.set_no_show_all(True)
        self.light_boot_row.hide()
        self.light_box.pack_start(self.light_boot_row, False, False, 0)
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_markup(
            "<small>Apply updates the active effect and saves it in headset "
            "memory. Reconnect reapplication is RAM-only.</small>")
        self.light_box.pack_start(note, False, False, 0)
        self.light_box.show_all()

    def _update_light_fields(self, combo, zone):
        widgets = self.zone_widgets[zone]
        mode = combo.get_active()
        widgets["color"].set_sensitive(mode in (1, 2))
        widgets["period"].set_sensitive(mode in (2, 3))
        widgets["intensity"].set_sensitive(mode in (2, 3))
        widgets["waveform"].set_sensitive(mode == 2)
        widgets["ramp"].set_sensitive(mode == 1)

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
        self._disc_meta = {}
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
            fid = dict(FEATURES)[attr]
            self._disc_meta[attr] = (
                fid,
                reply[5] if len(reply) > 5 else 0,
                reply[6] if len(reply) > 6 else 0,
            )
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
            fid = (reply[4] << 8) | reply[5]
            self._walk_ids[i] = fid
            wanted = {feature_id: attr for attr, feature_id in FEATURES}
            if fid in wanted:
                self._disc_meta[wanted[fid]] = (
                    fid,
                    reply[6] if len(reply) > 6 else 0,
                    reply[7] if len(reply) > 7 else 0,
                )
        self._walk_next()

    def _finish_discovery(self):
        self.features = self._disc_found
        self.feature_meta = self._disc_meta
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
        elif "lighting" in self.features:
            self.send("lighting", 0, cb=self._got_light_info)
        if "eq" in self.features:
            self.send("eq", 0, cb=self._got_eq_info)
        if "gkeys" in self.features:
            self.send("gkeys", 0, cb=self._got_gkey_info)
        if "battery" in self.features:
            self.send("battery", 1, cb=self._got_power_timeout)
        self.power_frame.set_no_show_all("battery" not in self.features)
        self.power_frame.set_visible("battery" in self.features)
        self._update_feature_summary()
        self._probe_device_identity()
        self._assert_device_state(initial=True)

    def _got_light_info(self, status, reply):
        if status != "ACK":
            return
        n = reply[4] if 1 <= reply[4] <= 8 else 1
        self._light_zone_count = n
        nv_capabilities = (
            (reply[5] << 8) | reply[6] if len(reply) > 6 else 0)
        ext_capabilities = (
            (reply[7] << 8) | reply[8] if len(reply) > 8 else 0)
        readable = bool(ext_capabilities & 1)
        if not self.zone_widgets:
            self._build_lighting_rows([f"Zone {z}" for z in range(n)])
            self._apply_saved_lighting()
        if hasattr(self, "light_read_active"):
            self.light_read_active.set_sensitive(readable)
            self.light_read_saved.set_sensitive(readable)
        if hasattr(self, "light_boot_row"):
            supported = bool(nv_capabilities & 1)
            self.light_boot_row.set_no_show_all(not supported)
            self.light_boot_row.set_visible(supported)
            if supported:
                self.send(
                    "lighting", 4, "0001",
                    cb=self._got_light_boot_state)
        self._light_caps = {}
        for zone in range(n):
            self.send(
                "lighting", 1, f"{zone:02x}",
                cb=lambda st, rep, z=zone: self._got_light_zone(z, st, rep))
        self._update_capability_summary()

    def _got_light_zone(self, zone, status, reply):
        if status != "ACK":
            return
        try:
            _echo, location, count = parse_led_zone_info(reply)
        except ValueError:
            return
        self._light_caps[zone] = {"name": location, "effects": {}}
        if zone in self.zone_widgets:
            self.zone_widgets[zone]["name"].set_text(location)
        for effect_index in range(min(count, 16)):
            self.send(
                "lighting", 2, f"{zone:02x}{effect_index:02x}",
                cb=lambda st, rep, z=zone: self._got_light_effect(z, st, rep))
        self._update_capability_summary()

    def _got_light_effect(self, zone, status, reply):
        if status != "ACK":
            return
        try:
            _z, index, effect_id, capabilities, period = \
                parse_led_effect_info(reply)
        except ValueError:
            return
        if zone not in self._light_caps:
            return
        self._light_caps[zone]["effects"][index] = {
            "id": effect_id,
            "name": LED_EFFECTS.get(effect_id, f"Effect 0x{effect_id:04X}"),
            "capabilities": capabilities,
            "period": period,
        }
        self._update_capability_summary()

    def _got_eq_info(self, status, reply):
        if status != "ACK":
            return
        try:
            info = parse_eq_info(reply)
        except ValueError:
            return
        self._eq_info = info
        n = info.bands
        if 1 <= n <= 12:
            if not self.eq_sliders:
                self._build_eq_sliders([str(i + 1) for i in range(n)])
            for slider in self.eq_sliders:
                slider.set_range(info.minimum_db, info.maximum_db)
            self._eq_freqs = []
            self._request_eq_frequency_page(0)
            self.send("eq", 2, "01", cb=self.got_eq)
            self._update_capability_summary()
        else:                              # can't size the EQ - hide it
            w = self.section_frames["eq"]
            w.set_no_show_all(True)
            w.set_visible(False)

    def _request_eq_frequency_page(self, start):
        total = self._eq_info.bands
        if start >= total:
            self._set_eq_frequency_labels()
            return
        count = min(7, total - start)
        self.send(
            "eq", 1, f"{start:02x}",
            cb=lambda st, rep, s=start, n=count:
            self._got_eq_frequency_page(s, n, st, rep))

    def _got_eq_frequency_page(self, start, count, status, reply):
        if status != "ACK":
            return
        try:
            page = parse_frequency_page(reply, count, start)
        except ValueError:
            return
        if len(self._eq_freqs) < start:
            return
        self._eq_freqs[start:start + count] = page
        self._request_eq_frequency_page(start + count)

    def _set_eq_frequency_labels(self):
        for label, frequency in zip(
                getattr(self, "eq_band_labels", []), self._eq_freqs):
            label.set_text(format_frequency(frequency))
        self._update_capability_summary()

    def _got_gkey_info(self, status, reply):
        if status == "ACK":
            self._gkey_count = min(reply[4], 32)
            for i, label in enumerate(self.gkey_labels):
                label.set_visible(i < self._gkey_count)
            self._update_capability_summary()

    def _update_feature_summary(self):
        lines = ["Root / Feature Set  · protocol discovery"]
        for attr, index in sorted(self.features.items(), key=lambda item: item[1]):
            fid, flags, version = self.feature_meta.get(
                attr, (dict(FEATURES).get(attr, 0), 0, 0))
            label = FEATURE_LABELS.get(fid, attr)
            flag_text = f" · flags 0x{flags:02X}" if flags else ""
            lines.append(
                f"Index {index:02X}  · 0x{fid:04X} {label}  · v{version}"
                f"{flag_text}")
        self.hw_features.set_text("\n".join(lines))

    def _update_capability_summary(self):
        lines = []
        if self._gkey_count:
            lines.append(
                f"G keys: G1–G{self._gkey_count}; diverted press/release events")
        info = getattr(self, "_eq_info", None)
        if info:
            freqs = getattr(self, "_eq_freqs", [])
            freq_text = ", ".join(format_frequency(f) for f in freqs)
            lines.append(
                f"Equalizer: {info.bands} bands, {info.minimum_db:+d} to "
                f"{info.maximum_db:+d} dB"
                + (f" ({freq_text} Hz)" if len(freqs) == info.bands else ""))
            lines.append("EQ storage: active RAM + saved headset EEPROM")
        if self._light_caps:
            for zone in sorted(self._light_caps):
                cap = self._light_caps[zone]
                effects = ", ".join(
                    effect["name"] for _idx, effect
                    in sorted(cap["effects"].items()))
                lines.append(
                    f"Lighting {cap['name']}: {effects or 'probing effects…'}")
        if "sidetone" in self.features:
            lines.append("Sidetone: level 0–100")
        if "battery" in self.features:
            timeout = getattr(self, "_power_timeout_minutes", None)
            timeout_text = (
                "never" if timeout == 0 else
                f"{timeout} minutes" if timeout is not None else "probing…")
            lines.append(
                "Power: ADC voltage, charge-state flags, auto-off "
                f"{timeout_text}")
        self.hw_caps.set_text("\n".join(lines) if lines else "Probing…")

    def _probe_device_identity(self):
        if "devinfo" in self.features:
            self.send("devinfo", 0, cb=self._got_device_info)
        if "devname" in self.features:
            self.send("devname", 0, cb=self._got_device_name_count)
            self.send("devname", 2, cb=self._got_device_type)

    def _got_device_info(self, status, reply):
        if status != "ACK":
            return
        try:
            info = parse_device_info(reply)
        except ValueError:
            return
        self.hw_values["unit"].set_text(info.unit_id)
        self.hw_values["model"].set_text(info.model_id)
        devinfo_version = self.feature_meta.get("devinfo", (0, 0, 0))[2]
        if devinfo_version == 0:
            # Transport semantics were standardized after v0. G935 returns
            # 0x03 here despite being a LIGHTSPEED USB-receiver headset.
            transport = f"v0 raw value 0x{info.transport_mask:02X}"
        else:
            transport = (
                ", ".join(info.transports) if info.transports
                else "Not reported")
        self.hw_values["transport"].set_text(transport)
        self.hw_values["entities"].set_text(str(info.entity_count))
        self._device_firmware = []
        for entity in range(min(info.entity_count, 16)):
            self.send(
                "devinfo", 1, f"{entity:02x}",
                cb=lambda st, rep, e=entity:
                self._got_firmware_info(e, st, rep))

    def _got_firmware_info(self, entity, status, reply):
        if status != "ACK":
            return
        try:
            info = parse_firmware_info(reply)
        except ValueError:
            return
        marker = " (active)" if info.active else ""
        text = (
            f"{info.prefix or info.entity_type} {info.version} "
            f"build {info.build:04d}{marker}")
        self._device_firmware.append((entity, text))
        self._device_firmware.sort()
        self.hw_values["firmware"].set_text(
            "\n".join(value for _index, value in self._device_firmware))

    def _got_device_name_count(self, status, reply):
        if status != "ACK":
            return
        self._device_name_count = min(reply[4], 255)
        self._device_name_parts = bytearray()
        self._request_device_name_chunk(0)

    def _request_device_name_chunk(self, start):
        if start >= self._device_name_count:
            name = bytes(self._device_name_parts[:self._device_name_count])
            self.hw_values["name"].set_text(
                name.decode("utf-8", "replace").rstrip("\0"))
            return
        self.send(
            "devname", 1, f"{start:02x}",
            cb=lambda st, rep, s=start:
            self._got_device_name_chunk(s, st, rep))

    def _got_device_name_chunk(self, start, status, reply):
        if status != "ACK":
            return
        remaining = self._device_name_count - start
        chunk = reply[4:4 + min(16, remaining)]
        self._device_name_parts.extend(chunk)
        self._request_device_name_chunk(start + len(chunk))

    def _got_device_type(self, status, reply):
        if status == "ACK":
            self.hw_values["type"].set_text(
                DEVICE_TYPES.get(reply[4], f"Type {reply[4]}"))

    def _assert_device_state(self, initial):
        """The single re-assert path, used after discovery and on power-on.

        The panel and g935-dspd may both assert the same persisted mode. The
        SET is idempotent, and sending it before lighting/EQ prevents a
        confirmed reconnect replay from leaving the switch visually enabled
        while the on-headset soundstage is actually flat.
        """
        self.send("gkeys", 2, f"{1 if self.dsp_sw.get_active() else 0:02x}")
        self._apply_saved_lighting()
        if initial:
            self.send("sidetone", 0, cb=self.got_sidetone)
            self.send("eq", 2, "01", cb=self.got_eq)
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
            self._battery_presence.reset()
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

    # ---------- software DSP / G-key diversion ----------
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
                    "<small>Software mode: open soundstage ON; G1–G3 are sent as "
                    "live host events below. Mic is managed by <b>g935-dspd</b> "
                    "(auto-unmutes after boom-down and handles the button).</small>")
            else:
                self.mode_note.set_markup(
                    "<small><span foreground='#e01b24'><b>Software mode without "
                    "g935-dspd:</b> "
                    "boom mute will stick until the daemon is running.</span> "
                    "Start it with: <tt>systemctl --user enable --now g935-dsp</tt></small>")
        else:
            self.mode_note.set_markup(
                "<small>Hardware mode (default): stock flat sound; mic is fully "
                "self-managed (boom up = mute, boom down = unmute, button toggles). "
                "Flip the switch for the open G HUB soundstage.</small>")

    def on_hidpp_event(self, report):
        if len(report) < 8:
            return
        if report[2] != self.features.get("gkeys") or report[3] != 0:
            return
        try:
            mask = parse_gkey_mask(report)
        except ValueError:
            return
        changed = self._gkey_mask ^ mask
        events = []
        count = self._gkey_count or len(self.gkey_labels)
        for i in range(min(count, len(self.gkey_labels))):
            bit = 1 << i
            down = bool(mask & bit)
            self.gkey_labels[i].set_sensitive(down)
            if changed & bit:
                events.append(f"G{i + 1} {'down' if down else 'up'}")
        self._gkey_mask = mask
        if events:
            text = ", ".join(events)
            self.gkey_last.set_text(text)
            self.log(f"--- G-key event: {text} ---")

    def _update_mic_note(self):
        if not hasattr(self, "mic_note"):
            return
        if not self.dsp_sw.get_active():
            self.mic_note.set_markup(
                "<small>Hardware mode: boom mute is fully on-device. Host switch is a "
                "separate mute layer.</small>")
        elif daemon_running():
            self.mic_note.set_markup(
                "<small>Software mode: g935-dspd clears the boom flag with a slow host-mute "
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

    # ---------- power management ----------
    def _got_power_timeout(self, status, reply):
        if status != "ACK":
            return
        self._power_timeout_minutes = reply[4]
        self.power_timeout.set_value(reply[4])
        self._update_capability_summary()

    def on_power_timeout_apply(self, _btn):
        minutes = int(self.power_timeout.get_value())
        self.send(
            "battery", 2, f"{minutes:02x}",
            cb=self._got_power_timeout_after_write)

    def _got_power_timeout_after_write(self, status, _reply):
        if status == "ACK":
            self._power_timeout_minutes = int(
                self.power_timeout.get_value())
            self._update_capability_summary()

    # ---------- EQ ----------
    def _mark_eq_custom(self, _scale):
        if hasattr(self, "eq_preset_button"):
            self.eq_preset_button.set_label("Presets")

    def on_eq_preset_menu(self, button, name, popover):
        popover.popdown()
        self.on_eq_preset(button, name)

    def on_eq_preset(self, _btn, name):
        gains = self.profile["eq_presets"][name] or [0] * len(self.eq_sliders)
        for s, v in zip(self.eq_sliders, gains):
            s.set_value(v)
        self.eq_preset_button.set_label(name)
        self.on_eq_apply(None)

    def on_eq_apply(self, _btn):
        if not self.eq_sliders:
            return
        gains = "".join(f"{int(s.get_value()) & 0xFF:02x}" for s in self.eq_sliders)
        # Persistence 1 updates active RAM and saved EEPROM together.
        self.send("eq", 3, "01" + gains)

    def got_eq(self, status, reply):
        if status != "ACK":
            return
        for s, b in zip(self.eq_sliders, reply[4:4 + len(self.eq_sliders)]):
            v = b - 256 if b > 127 else b
            s.set_value(v)
        current = [int(s.get_value()) for s in self.eq_sliders]
        matched = next(
            (name for name, gains in self.profile["eq_presets"].items()
             if (gains or [0] * len(self.eq_sliders)) == current),
            "Presets")
        self.eq_preset_button.set_label(matched)

    # ---------- lighting ----------
    def _got_light_boot_state(self, status, reply):
        if status != "ACK" or len(reply) < 9:
            return
        state = reply[6]
        self._light_boot_params = (reply[7], reply[8])
        self.light_boot_switch.handler_block_by_func(
            self.on_light_boot_toggle)
        # State 0 means the factory default (enabled); 1/2 are enabled/disabled.
        self.light_boot_switch.set_active(state != 2)
        self.light_boot_switch.handler_unblock_by_func(
            self.on_light_boot_toggle)

    def on_light_boot_toggle(self, switch, _pspec):
        state = 1 if switch.get_active() else 2
        param1, param2 = getattr(self, "_light_boot_params", (0, 0))
        self.send(
            "lighting", 5,
            f"0001{state:02x}{param1:02x}{param2:02x}")

    def _light_params(self, zone, persistence=0):
        widgets = self.zone_widgets[zone]
        mode = widgets["mode"].get_active()
        rgba = widgets["color"].get_rgba()
        r, g, b = (int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))
        return build_light_params(
            zone, mode, (r, g, b),
            period_ms=int(widgets["period"].get_value()),
            intensity=int(widgets["intensity"].get_value()),
            waveform=widgets["waveform"].get_active(),
            ramp=widgets["ramp"].get_active(),
            persistence=persistence,
        )

    def on_light_apply(self, _btn, zone):
        # Persistence 1 updates the active effect and saved EEPROM together.
        self.send("lighting", 3, self._light_params(zone, persistence=1))
        widgets = self.zone_widgets[zone]
        rgba = widgets["color"].get_rgba()
        self.settings.data["lighting"][str(zone)] = {
            "mode": widgets["mode"].get_active(),
            "color": "#%02X%02X%02X" % (int(rgba.red * 255), int(rgba.green * 255),
                                        int(rgba.blue * 255)),
            "period": int(widgets["period"].get_value()),
            "intensity": int(widgets["intensity"].get_value()),
            "waveform": widgets["waveform"].get_active(),
            "ramp": widgets["ramp"].get_active(),
        }
        self.settings.save()

    def _apply_saved_lighting(self):
        # Zones the user never Applied are left alone. Reconnect uses volatile
        # writes because the deliberate Apply already saved the EEPROM state.
        for zone in self.zone_widgets:
            if str(zone) in self.settings.data["lighting"]:
                self.send(
                    "lighting", 3,
                    self._light_params(zone, persistence=0))

    def _read_lighting(self, source):
        for zone in self.zone_widgets:
            self.send(
                "lighting", 14, f"{zone:02x}{source:02x}",
                cb=lambda st, rep, z=zone:
                self._got_lighting_state(z, st, rep))

    def _got_lighting_state(self, zone, status, reply):
        if status != "ACK":
            return
        try:
            state = parse_led_state(reply)
        except ValueError:
            return
        if state.zone != zone or state.mode not in range(len(LIGHT_MODES)):
            return
        widgets = self.zone_widgets[zone]
        widgets["mode"].set_active(state.mode)
        if state.mode in (1, 2):
            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue, rgba.alpha = (
                state.rgb[0] / 255.0, state.rgb[1] / 255.0,
                state.rgb[2] / 255.0, 1.0)
            widgets["color"].set_rgba(rgba)
        if state.period_ms:
            widgets["period"].set_value(state.period_ms)
        if state.mode in (2, 3):
            widgets["intensity"].set_value(state.intensity)
        if state.mode == 2:
            widgets["waveform"].set_active(min(state.waveform, 6))
        if state.mode == 1:
            widgets["ramp"].set_active(min(state.ramp, 2))

    # ---------- console ----------
    def on_full_sequence(self, _btn):
        self.log("--- G HUB connect sequence ---")
        self.dsp_sw.set_active(True)   # sequence includes gkeys 01 = G HUB mode
        for feat, fn, params in self.profile["connect_burst"]:
            self.send(feat, fn, params)
        self.send("sidetone", 0, cb=self.got_sidetone)
        self.send("eq", 2, "01", cb=self.got_eq)

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
        if getattr(self, "_stereo_broken", False):
            it = Gtk.MenuItem(label="Fix stereo (right earcup)")
            it.connect("activate", self.on_fix_stereo)
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
        dialog = getattr(self, "_wheel_cal_dialog", None)
        if dialog is not None:
            dialog.response(Gtk.ResponseType.CANCEL)
        self.audio.stop()
        try:
            self.worker.stop()
        except Exception:
            pass
        self.health.save()
        self.settings.save()
        Gtk.main_quit()

    # ---------- audio devices: settings toggles, volume rows, mixer ----------
    def on_wheel_setting_changed(self, *_args):
        self.settings.data["wheel_enabled"] = self.wheel_sw.get_active()
        for key, control in self._wheel_feel_controls.items():
            self.settings.data[key] = int(control.get_value())
        source = _args[0] if _args else None
        if source is not self.wheel_sw:
            self.settings.data["wheel_calibrated"] = False
        self.settings.save()
        self._refresh_wheel_calibration_status()

    def _sync_wheel_feel_controls(self):
        for key, control in self._wheel_feel_controls.items():
            control.handler_block_by_func(self.on_wheel_setting_changed)
            control.set_value(self.settings.data[key])
            control.handler_unblock_by_func(self.on_wheel_setting_changed)

    def on_wheel_defaults(self, *_args):
        defaults = {
            "wheel_fine_step": DEFAULT_FINE_STEP,
            "wheel_fine_interval_ms": round(FINE_INTERVAL_S * 1000),
            "wheel_hold_delay_ms": round(HOLD_REPEAT_DELAY_S * 1000),
            "wheel_hold_interval_ms": round(
                HOLD_REPEAT_INTERVAL_S * 1000),
            "wheel_step": DEFAULT_STEP,
            "wheel_direction_guard_ms": round(
                DIRECTION_GUARD_S * 1000),
        }
        self.settings.data.update(defaults)
        self.settings.data["wheel_calibrated"] = False
        self._sync_wheel_feel_controls()
        self.settings.save()
        self._refresh_wheel_calibration_status()

    def _refresh_wheel_calibration_status(self):
        if self.settings.data.get("wheel_calibrated"):
            self.wheel_calibration_status.set_text(
                "Last capture was applied · "
                f"fast rate after "
                f"{self.settings.data['wheel_hold_delay_ms']} ms · "
                f"1% every "
                f"{self.settings.data['wheel_hold_interval_ms']} ms · "
                f"up to {self.settings.data['wheel_step']}% per sweep")
        else:
            self.wheel_calibration_status.set_text(
                "Manual comfort settings active. Capture is optional.")

    def on_wheel_calibrate(self, *_args):
        if getattr(self, "_wheel_cal_dialog", None) is not None:
            self._wheel_cal_dialog.present()
            return

        original_enabled = self.settings.data["wheel_enabled"]
        self.settings.data["wheel_enabled"] = False
        self.settings.save()
        self.wheel_sw.handler_block_by_func(self.on_wheel_setting_changed)
        self.wheel_sw.set_active(False)
        self.wheel_sw.handler_unblock_by_func(self.on_wheel_setting_changed)

        dialog = Gtk.Dialog(
            title="Calibrate G935 volume wheel",
            transient_for=self, modal=True)
        dialog.set_default_size(620, 460)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        next_button = dialog.add_button("Next", Gtk.ResponseType.APPLY)
        next_button.set_sensitive(False)
        self._wheel_cal_dialog = dialog
        self._wheel_cal_fd = None
        self._wheel_cal_watch = None
        self._wheel_cal_stage = None
        self._wheel_cal_samples = {
            "slow_up": [], "slow_down": [],
            "fast_up": [], "fast_down": [],
        }
        self._wheel_cal_pending = {}
        self._wheel_cal_raw = []
        self._wheel_cal_capture_saved = False

        box = dialog.get_content_area()
        box.set_spacing(12)
        box.set_border_width(16)

        heading = Gtk.Label(xalign=0)
        style(heading, "g935-primary-title")
        heading.set_line_wrap(True)
        box.pack_start(heading, False, False, 0)

        instruction = Gtk.Label(xalign=0)
        instruction.set_line_wrap(True)
        box.pack_start(instruction, False, False, 0)

        progress = Gtk.ProgressBar()
        progress.set_show_text(True)
        box.pack_start(progress, False, False, 0)

        live = Gtk.Label(xalign=0)
        live.set_selectable(True)
        live.set_line_wrap(True)
        live.set_size_request(-1, 125)
        style(live, "g935-zone")
        box.pack_start(live, False, False, 0)

        target_row = Gtk.Box(spacing=8)
        target_row.pack_start(Gtk.Label(
            label="Target fast rolls from 0–100%", xalign=0),
            True, True, 0)
        target_rolls = Gtk.SpinButton.new_with_range(3, 5, 1)
        target_rolls.set_value(3)
        target_row.pack_end(target_rolls, False, False, 0)
        box.pack_start(target_row, False, False, 0)

        foot = Gtk.Label(xalign=0)
        style(foot, "g935-subtitle")
        foot.set_line_wrap(True)
        foot.set_text(
            "The daemon releases the media-key interface only while this "
            "wizard is open. Volume will not change during capture. Every "
            "raw press, repeat, release, direction, and timestamp is recorded "
            "locally for debugging.")
        box.pack_start(foot, False, False, 0)

        self._wheel_cal_widgets = {
            "heading": heading, "instruction": instruction,
            "progress": progress, "live": live,
            "next": next_button,
        }
        stages = (
            ("slow_up", "Slow upward calibration",
             "Make exactly 5 small, deliberate upward rolls—the finest "
             "movements you would use near your target volume. Then click Next.",
             5, KEY_VOLUMEUP),
            ("slow_down", "Slow downward calibration",
             "Make exactly 5 small, deliberate downward rolls. Pause between "
             "them as you would for fine adjustment, then click Next.",
             5, KEY_VOLUMEDOWN),
            ("fast_up", "Fast upward calibration",
             "Make 3 separate fast upward sweeps toward 100%. The receiver "
             "reports each sweep as one held UP press, so let it release "
             "fully between samples. Then click Next.",
             3, KEY_VOLUMEUP),
            ("fast_down", "Fast downward calibration",
             "Make 3 separate fast downward sweeps toward 0%. Let the "
             "receiver release fully between samples, then click Next.",
             3, KEY_VOLUMEDOWN),
        )

        GLib.timeout_add(150, self._wheel_cal_try_open)
        result = None
        applied = False
        try:
            for key, title, copy, target, expected_code in stages:
                self._wheel_cal_stage = key
                self._wheel_cal_target = target
                self._wheel_cal_expected = expected_code
                heading.set_text(title)
                instruction.set_text(copy)
                next_button.set_label("Next")
                self._wheel_cal_update_live()
                dialog.show_all()
                response = dialog.run()
                if response != Gtk.ResponseType.APPLY:
                    break
            else:
                self._wheel_cal_stage = None
                result = analyze_calibration(
                    self._wheel_cal_samples,
                    int(target_rolls.get_value()))
                capture_path = self._wheel_cal_save_capture(result)
                counts = result["counts"]
                fast_ms = [round(value * 1000)
                           for value in result["fast_gaps"]]
                slow_ms = [round(value * 1000)
                           for value in result["slow_gaps"]]
                max_step = result["wheel_step"]
                slow_holds_ms = [
                    round(value * 1000)
                    for value in result["slow_holds"]
                ]
                fast_holds_ms = [
                    round(value * 1000)
                    for value in result["fast_holds"]
                ]
                fine_step = result["wheel_fine_step"]
                heading.set_text("Calibration result")
                instruction.set_text(
                    "Review the measured data below. Apply saves this profile "
                    "and returns the wheel to the background service.")
                live.set_text(
                    "Headset-reported rolls\n"
                    f"  Slow up {counts['slow_up']}/5 · "
                    f"Slow down {counts['slow_down']}/5\n"
                    f"  Fast up {counts['fast_up']}/3 · "
                    f"Fast down {counts['fast_down']}/3\n"
                    f"  Opposite-direction reports: "
                    f"{result['wrong_directions']}\n\n"
                    f"Raw input events captured: "
                    f"{len(self._wheel_cal_raw)}\n"
                    f"Neutral gaps · fast {fast_ms or ['none']} ms · "
                    f"slow {slow_ms or ['none']} ms\n"
                    f"Fine holds: {slow_holds_ms or ['none']} ms\n"
                    f"Fast holds: {fast_holds_ms or ['none']} ms\n"
                    f"Duration decoder · first {fine_step}% immediately · "
                    f"gentle steps every "
                    f"{result['wheel_fine_interval_ms']} ms before "
                    f"{result['wheel_hold_delay_ms']} ms · "
                    f"then fast rate at 1% every "
                    f"{result['wheel_hold_interval_ms']} ms\n"
                    f"Maximum per sweep: {max_step}%\n\n"
                    f"Full event trace saved to {capture_path}")
                progress.set_fraction(1)
                progress.set_text("Ready to apply")
                next_button.set_label("Apply calibration")
                next_button.set_sensitive(True)
                dialog.show_all()
                response = dialog.run()
                if response == Gtk.ResponseType.APPLY:
                    for key, value in result.items():
                        if key.startswith("wheel_"):
                            self.settings.data[key] = value
                    applied = True
        finally:
            if (getattr(self, "_wheel_cal_raw", None)
                    and not self._wheel_cal_capture_saved):
                self._wheel_cal_save_capture(result)
            self._wheel_cal_cleanup()
            dialog.destroy()
            self._wheel_cal_dialog = None
            self.settings.data["wheel_enabled"] = original_enabled
            self.settings.save()
            self.wheel_sw.handler_block_by_func(
                self.on_wheel_setting_changed)
            self.wheel_sw.set_active(original_enabled)
            self.wheel_sw.handler_unblock_by_func(
                self.on_wheel_setting_changed)
            if applied and result is not None:
                self._sync_wheel_feel_controls()
            self._refresh_wheel_calibration_status()

    def _wheel_cal_try_open(self):
        if getattr(self, "_wheel_cal_dialog", None) is None:
            return False
        if self._wheel_cal_fd is not None:
            return False
        path = find_wheel_device()
        if path is None:
            self._wheel_cal_widgets["live"].set_text(
                "Waiting for the G935 input device…")
            return True
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            self._wheel_cal_widgets["live"].set_text(
                f"Waiting for the daemon to release {path}…\n{exc}")
            return True
        self._wheel_cal_fd = fd
        self._wheel_cal_watch = GLib.io_add_watch(
            fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
            self._wheel_cal_on_io)
        self._wheel_cal_widgets["next"].set_sensitive(True)
        self._wheel_cal_update_live()
        return False

    def _wheel_cal_on_io(self, fd, condition):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            self._wheel_cal_widgets["live"].set_text(
                "Wheel device disconnected. Reconnect it and restart calibration.")
            return False
        try:
            data = os.read(fd, INPUT_EVENT.size * 64)
        except (BlockingIOError, OSError):
            return True
        stage = self._wheel_cal_stage
        if stage is None:
            return True
        events = self._wheel_cal_samples[stage]
        for timestamp, code, value in parse_key_events(data):
            self._wheel_cal_raw.append({
                "stage": stage,
                "timestamp": timestamp,
                "code": code,
                "value": value,
            })
            if value == 1:
                event = {
                    "press": timestamp, "release": None, "code": code,
                }
                events.append(event)
                self._wheel_cal_pending[code] = event
            elif value == 0:
                event = self._wheel_cal_pending.pop(code, None)
                if event is not None:
                    event["release"] = timestamp
        self._wheel_cal_update_live()
        return True

    def _wheel_cal_update_live(self):
        widgets = getattr(self, "_wheel_cal_widgets", None)
        stage = getattr(self, "_wheel_cal_stage", None)
        if not widgets or stage is None:
            return
        events = self._wheel_cal_samples[stage]
        expected = self._wheel_cal_expected
        correct = sum(event["code"] == expected for event in events)
        opposite = len(events) - correct
        holds = [
            (event["release"] - event["press"]) * 1000
            for event in events if event["release"] is not None
        ]
        direction = "UP" if expected == KEY_VOLUMEUP else "DOWN"
        last = events[-1] if events else None
        last_text = (
            ("UP" if last["code"] == KEY_VOLUMEUP else "DOWN")
            if last else "waiting")
        stage_raw = [
            event for event in self._wheel_cal_raw
            if event["stage"] == stage
        ]
        origin = stage_raw[0]["timestamp"] if stage_raw else 0
        state_names = {0: "release", 1: "press", 2: "repeat"}
        trace = "\n".join(
            f"+{(event['timestamp'] - origin) * 1000:6.0f} ms  "
            f"{'UP  ' if event['code'] == KEY_VOLUMEUP else 'DOWN'} "
            f"{state_names.get(event['value'], str(event['value']))}"
            for event in stage_raw[-8:]
        )
        widgets["progress"].set_fraction(
            min(1, correct / max(1, self._wheel_cal_target)))
        widgets["progress"].set_text(
            f"{correct} of {self._wheel_cal_target} "
            f"{'sweeps' if stage.startswith('fast_') else 'movements'} "
            f"reported {direction}")
        widgets["live"].set_text(
            f"Expected direction: {direction}\n"
            f"Reported: {correct} expected · {opposite} opposite · "
            f"{len(events)} total\n"
            f"Last report: {last_text}\n"
            f"Completed hold durations: "
            f"{', '.join(f'{value:.0f} ms' for value in holds[-6:]) or '—'}\n"
            f"Raw timeline ({len(stage_raw)} events):\n{trace or 'waiting'}")

    def _wheel_cal_save_capture(self, result=None):
        """Persist a complete, human-readable physical-wheel event trace."""
        raw = getattr(self, "_wheel_cal_raw", [])
        origin = raw[0]["timestamp"] if raw else 0
        state_names = {0: "release", 1: "press", 2: "repeat"}
        payload = {
            "version": 1,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "device": find_wheel_device(),
            "events": [
                {
                    "t_ms": round((event["timestamp"] - origin) * 1000, 3),
                    "stage": event["stage"],
                    "direction": (
                        "up" if event["code"] == KEY_VOLUMEUP else "down"),
                    "state": state_names.get(
                        event["value"], str(event["value"])),
                    "code": event["code"],
                    "value": event["value"],
                }
                for event in raw
            ],
            "gestures": self._wheel_cal_samples,
            "analysis": result,
        }
        ensure_config_dir()
        tmp = WHEEL_CAPTURE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, WHEEL_CAPTURE_FILE)
            self._wheel_cal_capture_saved = True
            return WHEEL_CAPTURE_FILE
        except OSError as exc:
            log.warning("could not save wheel capture: %s", exc)
            return f"capture could not be saved ({exc})"

    def _wheel_cal_cleanup(self):
        watch = getattr(self, "_wheel_cal_watch", None)
        if watch is not None:
            try:
                GLib.source_remove(watch)
            except Exception:
                pass
        self._wheel_cal_watch = None
        fd = getattr(self, "_wheel_cal_fd", None)
        if fd is not None:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self._wheel_cal_fd = None

    def _on_audio_change(self):
        self._rebuild_tray_menu()
        self._refresh_device_toggles()
        self._refresh_volume_rows()
        # Device re-enumerate after resume often breaks FR link — check soon
        GLib.timeout_add(1500, self._poll_stereo_route_once)

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
                sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 150, 1)
                sc.set_size_request(220, -1)
                sc.set_value(self.audio.get_volume(kind, name) or 0)
                sc.set_value_pos(Gtk.PositionType.RIGHT)
                sc.add_mark(100, Gtk.PositionType.TOP, "100% safe stop")
                self._guard_slider_scroll(sc)
                sc.set_tooltip_text(
                    "100% is a soft safety stop. Release at 100%, then drag "
                    "again to unlock boost up to 150%.")
                sc.connect("button-press-event", self._arm_volume_safety_stop)
                sc.connect_after(
                    "button-release-event", self._release_volume_safety_stop)
                sc.connect("value-changed", self._on_vol_slider, kind, name)
                row.pack_start(sc, True, True, 0)
                container.pack_start(row, False, False, 0)
        container.show_all()

    @staticmethod
    def _arm_volume_safety_stop(scale, _event):
        # A gesture that begins below 100% may reach 100% but cannot cross it.
        # Starting a fresh gesture at 100% (or above) deliberately unlocks boost.
        scale._g935_stop_at_100 = scale.get_value() < 100
        return False

    @staticmethod
    def _release_volume_safety_stop(scale, _event):
        scale._g935_stop_at_100 = False
        return False

    def _on_vol_slider(self, scale, kind, name):
        if (getattr(scale, "_g935_stop_at_100", False)
                and scale.get_value() > 100):
            scale.set_value(100)
            return
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

    def _insight_color(self, tone):
        return {
            "good": "#2ec27e",
            "warn": "#e5a50a",
            "bad": "#e01b24",
            "info": "#62a0ea",
            "muted": "#9a9a9a",
        }.get(tone, "#9a9a9a")

    def _remain_color(self, hours, charging=False):
        if charging:
            return "#2ec27e"
        if hours is None:
            return None
        if hours < 0.75:
            return "#e01b24"
        if hours < 2.0:
            return "#e5a50a"
        return "#2ec27e"

    def _refresh_health_display(self, mv=None, pct=None, state="",
                                raw_mv=None, path_is_rail=False):
        h = self.health
        rated = self._rated_runtime()
        charging = (state == "charging") if state else None
        if state in ("discharging",):
            charging = False
        # analysis() converts path mV → cell estimate when charging
        a = h.analysis(mv=mv if mv is not None else raw_mv,
                       rated_runtime_h=rated, charging=charging)
        cell = a.get("cell_mv")
        raw = raw_mv if raw_mv is not None else a.get("raw_mv")
        if pct is None and cell is not None:
            pct = batt_percent(cell)

        # --- Hero remaining time ---
        remain = a.get("remain_best_h")
        if charging:
            hero = ("<span size='xx-large' weight='bold' foreground='#2ec27e'>"
                    "Charging</span>")
            if pct is not None:
                hero += (f"\n<span size='large'>{pct}% at last rest"
                         f" · cell estimate</span>")
            hero += ("\n<small>SoC held while on charger — path voltage is not "
                     "cell OCV</small>")
        elif remain is not None:
            color = self._remain_color(remain) or "#ffffff"
            hero = (f"<span size='xx-large' weight='bold' foreground='{color}'>"
                    f"{self._dur_h(remain)} left</span>")
            src = a.get("remain_source") or "estimate"
            bits = [f"via {src}"]
            if a.get("remain_profile_h") is not None and src != "profile":
                bits.append(f"profile says {self._dur_h(a['remain_profile_h'])}")
            if a.get("remain_vs_rated_pct") is not None:
                rel = a["remain_vs_rated_pct"]
                sign = "+" if rel >= 0 else "−"
                bits.append(f"{sign}{abs(rel):.0f}% vs rated at this level")
            hero += "\n<small>" + " · ".join(bits) + "</small>"
        elif state == "headset off":
            hero = ("<span size='xx-large' weight='bold' foreground='#9a9a9a'>"
                    "Headset off</span>")
        elif mv is not None and state == "discharging":
            hero = ("<span size='xx-large' weight='bold'>—</span>\n"
                    "<small>collecting live drain… need ≥10 min and ≥3% drop</small>")
        else:
            hero = ("<span size='xx-large' weight='bold'>—</span>\n"
                    "<small>waiting for battery data</small>")
        self.health_remain.set_markup(hero)

        # --- Status line: % · cell mV · state (+ path mV when charging) ---
        if cell is None and raw is None:
            self.health_status.set_markup(
                f"<span size='large'>{GLib.markup_escape_text(state or '…')}</span>")
        elif charging:
            arrow = "↑"
            cell_s = f"{cell} mV cell" if cell is not None else "cell ?"
            path_s = f"{raw} mV path" if raw is not None else ""
            rail = " · charge rail" if path_is_rail else ""
            self.health_status.set_markup(
                f"<span size='large'><b>{pct if pct is not None else '—'}%</b>  "
                f"{cell_s}  {arrow} "
                f"{GLib.markup_escape_text(state)}"
                f"</span>\n<small>{GLib.markup_escape_text(path_s)}{rail} "
                f"(not used for %)</small>")
        else:
            arrow = "↓" if state == "discharging" else "·"
            show_mv = cell if cell is not None else raw
            self.health_status.set_markup(
                f"<span size='large'><b>{pct}%</b>  {show_mv} mV  {arrow} "
                f"{GLib.markup_escape_text(state)}</span>")

        # --- Open session strip ---
        if h.cur:
            seg = h.cur
            dt = seg["t_end"] - seg["t_start"]
            line = (f"This session: <b>{seg['type']}</b> {self._dur(dt)} · "
                    f"{seg['pct_start']}→{seg['pct_end']}% · "
                    f"{seg['mv_start']}→{seg['mv_end']} mV")
            if seg["type"] == "discharge" and dt >= 600:
                rate = (seg["pct_start"] - seg["pct_end"]) / (dt / 3600.0)
                if rate > 0:
                    line += f" · −{rate:.1f}%/h"
            self.health_session.set_markup(f"<small>{line}</small>")
        else:
            self.health_session.set_markup(
                "<small>No open session — samples appear once the headset is on "
                "and tracking is enabled.</small>")

        # --- Insights ---
        insights = build_insights(a, state=state, charging=charging)
        if insights:
            parts = []
            for ins in insights:
                col = self._insight_color(ins["tone"])
                text = GLib.markup_escape_text(ins["text"])
                parts.append(
                    f"<span foreground='{col}'>●</span> {text}")
            self.health_insights.set_markup(
                "<small>" + "\n".join(parts) + "</small>")
        else:
            self.health_insights.set_markup("")

        # --- Wear summary: health + concrete time cost ---
        if a.get("health_pct") is not None:
            health_pct = a["health_pct"]
            conf = ("early" if a["n_sessions"] < 3
                    else f"{a['n_sessions']} sessions")
            health_color = (
                "#2ec27e" if health_pct >= 75 else
                "#e5a50a" if health_pct >= 55 else
                "#e01b24")
            self._set_kpi(
                self.kpi_health,
                f"<span size='x-large' weight='bold' "
                f"foreground='{health_color}'>≈{health_pct:.0f}%</span>",
                conf)

            learned = a.get("learned_full_runtime_h")
            lost_h = max(0.0, rated - learned) if learned is not None else None
            if lost_h is not None:
                lost_color = (
                    "#2ec27e" if lost_h < 0.25 else
                    "#e5a50a" if lost_h < rated * 0.30 else
                    "#e01b24")
                lost_sub = (
                    "meeting or exceeding rated runtime"
                    if lost_h < (1.0 / 120.0)
                    else f"against the selected {rated:g}h rating")
                self._set_kpi(
                    self.kpi_lost,
                    f"<span size='x-large' weight='bold' "
                    f"foreground='{lost_color}'>{self._dur_h(lost_h)}</span>",
                    lost_sub)
                if lost_h < (1.0 / 120.0):
                    wear_lead = (
                        "No measurable runtime has been lost against the selected "
                        "rating.")
                else:
                    wear_lead = (
                        f"A full charge is currently costing about "
                        f"{self._dur_h(lost_h)} of listening time.")
                evidence = (
                    "This is still an early estimate from one or two qualifying "
                    "discharges."
                    if a["n_sessions"] < 3 else
                    f"It is based on the median of {a['n_sessions']} qualifying "
                    "discharge sessions, which reduces one-off load swings.")
                self.health_wear_description.set_markup(
                    f"<b>{wear_lead}</b> The headset projects to "
                    f"{self._dur_h(learned)} from full versus {rated:g}h rated. "
                    f"{evidence} Health measures usable runtime—not just voltage—"
                    "so lighting, volume, and radio conditions can move the estimate.")
            else:
                self._set_kpi(
                    self.kpi_lost,
                    "<span size='x-large' weight='bold'>—</span>",
                    "waiting for a full-runtime estimate")
                self.health_wear_description.set_text(
                    "A health estimate exists, but more continuous runtime data is "
                    "needed before the lost time per full charge is dependable.")
        else:
            self._set_kpi(self.kpi_health,
                          "<span size='x-large' weight='bold'>—</span>",
                          "need solid sessions")
            self._set_kpi(
                self.kpi_lost,
                "<span size='x-large' weight='bold'>—</span>",
                "not enough evidence yet")
            self.health_wear_description.set_markup(
                "<b>Wear is still being measured.</b> Keep the panel open during "
                "normal off-charger use. A qualifying evidence point needs about "
                "30 minutes of use and at least an 8% drop; short sessions can join "
                "across brief headset-off gaps. Once available, this card will show "
                "both health percentage and the actual time lost from every full charge.")

        # --- Supporting KPI cards ---
        if a.get("learned_full_runtime_h") is not None:
            self._set_kpi(
                self.kpi_full,
                f"<span size='large' weight='bold'>"
                f"{self._dur_h(a['learned_full_runtime_h'])}</span>",
                f"of {rated:g}h rated")
        else:
            self._set_kpi(self.kpi_full,
                          "<span size='large' weight='bold'>—</span>",
                          f"rated {rated:g}h")

        if a.get("live_rate_pct_per_h") is not None:
            sub = f"over {self._dur(a['live_rate_span_s'])}"
            if a.get("rated_rate_pct_per_h"):
                sub += f" · rated −{a['rated_rate_pct_per_h']:.1f}%/h"
            self._set_kpi(
                self.kpi_drain,
                f"<span size='large' weight='bold'>"
                f"−{a['live_rate_pct_per_h']:.1f}%/h</span>",
                sub)
        elif charging:
            self._set_kpi(self.kpi_drain,
                          "<span size='large' weight='bold'>—</span>",
                          "on charger")
        else:
            self._set_kpi(self.kpi_drain,
                          "<span size='large' weight='bold'>—</span>",
                          "need ≥10 min / 3% drop")

        if a.get("effective_mah") is not None:
            self._set_kpi(
                self.kpi_cap,
                f"<span size='large' weight='bold'>~{a['effective_mah']}</span>",
                f"of {a['design_mah']} mAh design")
        else:
            self._set_kpi(
                self.kpi_cap,
                f"<span size='large' weight='bold'>{a.get('design_mah', '—')}</span>",
                "mAh design (stock)")

        # --- Charts (use cell mV so charge-rail doesn't fake 100%) ---
        self._update_health_charts(a, rated, mv=cell)

        # --- History caption ---
        n_recent = len(h.recent)
        if n_recent >= 2:
            span_h = (h.recent[-1][0] - h.recent[0][0]) / 3600.0
            chg_n = sum(1 for r in h.recent if r[2])
            self.history_caption.set_markup(
                f"<small>{n_recent} samples · {span_h:.1f} h window · "
                f"{chg_n} charging / {n_recent - chg_n} discharge · "
                f"amber dashed = rated drain from first discharge</small>")
        else:
            self.history_caption.set_markup(
                "<small>Samples land about once per minute while the panel is open "
                "and tracking is on.</small>")

        # --- Detail under health/compare ---
        details = []
        if a.get("remain_expected_adj_h") is not None and a.get("health_pct") is not None:
            details.append(
                f"If health holds: ~{self._dur_h(a['remain_expected_adj_h'])} left "
                f"(rated remaining × {a['health_pct']:.0f}%).")
        ps_peak = a.get("peak_summary")
        if ps_peak and ps_peak.get("latest_mv"):
            details.append(
                f"Recent full-charge peak: {ps_peak['latest_mv']} mV "
                f"(median {ps_peak['median_mv']:.0f} mV over {ps_peak['n']} charges"
                f"{', ' + ps_peak['direction'] if ps_peak.get('direction') else ''}).")
        last_chg = next((s for s in reversed(h.segments) if s["type"] == "charge"), None)
        if last_chg:
            details.append(
                f"Last charge session: {self._dur(last_chg['t_end'] - last_chg['t_start'])}, "
                f"{last_chg['mv_start']}→{last_chg['mv_end']} mV.")
        self.health_detail.set_markup(
            "<small>" + GLib.markup_escape_text(" ".join(details) or " ") + "</small>")

        # --- Profile narrative ---
        ps = a["profile_summary"]
        if not ps or ps.get("hours_logged", 0) <= 0:
            self.health_profile.set_markup(
                "<small>No discharge datapoints yet. Wear the headset off-charger; "
                "samples land about once per minute and teach the profile which "
                "voltage ranges drain faster.</small>")
        else:
            span = ps.get("span_mv")
            span_s = f"{span[0]}–{span[1]} mV" if span else "—"
            ready = ("ready — used as a voltage-shaped cross-check"
                     if ps.get("ready") else "still building")
            bins = a["profile"].get("bins") or {}
            # Top / bottom rate for storytelling
            story = ""
            if bins:
                lo_b = min(bins, key=bins.get)
                hi_b = max(bins, key=bins.get)
                story = (f" Slowest near {lo_b} mV ({bins[lo_b]:.0f} mV/h); "
                         f"fastest near {hi_b} mV ({bins[hi_b]:.0f} mV/h).")
            weak = a["profile"].get("weak_bins") or {}
            extra = f" · +{len(weak)} sparse bins" if weak else ""
            self.health_profile.set_markup(
                f"<small><b>{ps['hours_logged']:.1f} h</b> discharge logged · "
                f"<b>{ps.get('bins_filled', 0)}</b> solid bins · span {span_s} · "
                f"{ps.get('n_pairs', 0)} slope windows · {ready}{extra}."
                f"{GLib.markup_escape_text(story)}</small>")

        # --- Session list (readable, not monospace dump) ---
        rows = a.get("session_rows") or []
        if not rows:
            self.health_hist.set_markup(
                "<small>No usage sessions yet. Wear the headset off-charger with "
                "tracking on — short hops merge across ≤15 min gaps.</small>")
        else:
            lines = []
            for r in rows[:8]:
                when = time.strftime("%a %m/%d %H:%M", time.localtime(r["t_start"]))
                dur = self._dur(r["on_time_s"])
                drain = f"{r['pct_start']}→{r['pct_end']}%"
                parts = f" · {r['parts']} parts" if r["parts"] > 1 else ""
                if r["quality"] == "solid":
                    badge_col = (self._insight_color("good")
                                 if (r.get("pct_of_rated") or 0) >= 85
                                 else self._insight_color("warn")
                                 if (r.get("pct_of_rated") or 0) >= 70
                                 else self._insight_color("bad"))
                    tail = (f"<span foreground='{badge_col}'>"
                            f"→ {self._dur_h(r['full_h'])} full "
                            f"({r['pct_of_rated']:.0f}% rated)</span>")
                else:
                    tail = "<span foreground='#9a9a9a'>short — not in health median</span>"
                lines.append(
                    f"<b>{GLib.markup_escape_text(when)}</b>  {dur}{parts}  "
                    f"{drain}  {tail}")
            solid_n = sum(1 for r in rows if r["quality"] == "solid")
            foot = (f"\n<span foreground='#9a9a9a'>{solid_n} solid / {len(rows)} shown · "
                    f"solid sessions feed the health median</span>")
            self.health_hist.set_markup(
                "<small>" + "\n".join(lines) + foot + "</small>")

    def _update_health_charts(self, analysis, rated, mv=None):
        """Push latest analysis into the Cairo chart widgets."""
        h = self.health
        full = int(h.settings.get("full_mv", HEALTH_DEFAULTS["full_mv"]))
        pts = build_history_points(h.recent, batt_percent, full_mv=full)
        expected_line = build_expected_overlay(pts, rated)
        self.chart_history.set_data({
            "points": pts,
            "expected_points": expected_line,
        })
        self.chart_gauge.set_data({
            "health_pct": analysis.get("health_pct"),
            "learned_h": analysis.get("learned_full_runtime_h"),
            "rated_h": rated,
            "n_sessions": analysis.get("n_sessions"),
            "effective_mah": analysis.get("effective_mah"),
            "design_mah": analysis.get("design_mah"),
        })
        self.chart_expect.set_data({
            "expected_h": analysis.get("remain_expected_h"),
            "actual_h": analysis.get("remain_best_h"),
            "delta_pct": analysis.get("remain_vs_rated_pct"),
        })
        # Session bars: last up to 12 merged sessions (chart filters short ones)
        sess_rows = []
        for sess in merge_discharge_sessions(h.segments)[-12:]:
            full = session_full_runtime_h(sess)
            label = time.strftime("%m/%d", time.localtime(sess["t_start"]))
            # Prefer time-of-day when many sessions share a day
            if full is not None:
                label = time.strftime("%m/%d\n%H:%M", time.localtime(sess["t_start"]))
            sess_rows.append({"label": label.replace("\n", " "), "hours": full})
        self.chart_sessions.set_data({
            "sessions": sess_rows,
            "rated_h": rated,
        })
        profile = analysis.get("profile") or {}
        self.chart_profile.set_data({
            "bins": profile.get("bins") or {},
            "weak_bins": profile.get("weak_bins") or {},
            "current_mv": mv,
            "bin_mv": profile.get("bin_mv"),
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

    # ---------- stereo route (post-resume left-only fix) ----------
    def _poll_stereo_route(self):
        """Periodic check: EasyEffects/DTS FL linked, FR dropped after sleep."""
        self._poll_stereo_route_once()
        return True  # keep the 8s timer

    def _poll_stereo_route_once(self):
        """One-shot check (idle / post-audio-change). Returns False for GLib."""
        if self._stereo_fixing:
            return False
        try:
            st = inspect_stereo()
        except Exception as e:
            self.log(f"stereo check error: {e}")
            return False
        self._apply_stereo_status(st, auto_fix=True)
        return False

    def _apply_stereo_status(self, st, auto_fix=False):
        was_broken = self._stereo_broken
        broken = bool(st.broken)

        if not st.pw_link_available:
            self.stereo_label.set_markup("")
            self.stereo_label.hide()
            self.stereo_label.set_tooltip_text(st.detail)
            self._stereo_broken = False
            self.stereo_banner.hide()
            return

        if not st.g935_present:
            self.stereo_label.show()
            self.stereo_label.set_markup(
                "<span foreground='gray'>🎧 —</span>")
            self.stereo_label.set_tooltip_text(st.detail)
            self._stereo_broken = False
            self.stereo_banner.hide()
            return

        if broken:
            self.stereo_label.show()
            self.stereo_label.set_markup(
                "<span foreground='#e01b24'><b>🎧 L-only</b></span>")
            self.stereo_label.set_tooltip_text(st.detail)
            quiet = time.time() < self._stereo_dismissed_until
            if not quiet:
                self.stereo_banner.show()
            # Notify once per incident (not every poll)
            if not was_broken and not quiet:
                notify_user(
                    "G935: right earcup silent",
                    "PipeWire only linked the left channel (often after sleep). "
                    "Opening Fix…",
                    urgency="critical",
                )
                self.log(f"STEREO BROKEN: {st.detail}")
            self._stereo_broken = True
            if auto_fix and not quiet:
                # Auto-repair immediately — same as the manual pw-link fix
                GLib.idle_add(self._do_stereo_fix, st)
        else:
            self.stereo_label.show()
            self.stereo_label.set_markup(
                "<span foreground='#2ec27e'>🎧 stereo</span>")
            tip = st.detail
            if st.fr_port and st.fr_port.endswith(":playback_2"):
                tip += " (right port named playback_2 — normal after some resumes)"
            self.stereo_label.set_tooltip_text(tip)
            if was_broken:
                self.log(f"STEREO OK: {st.detail}")
                notify_user("G935: stereo restored", st.detail)
            self._stereo_broken = False
            self.stereo_banner.hide()
            self._stereo_dismissed_until = 0.0

        # Refresh tray so "Fix stereo" appears when needed
        if was_broken != self._stereo_broken:
            self._rebuild_tray_menu()

    def _do_stereo_fix(self, st=None):
        if self._stereo_fixing:
            return False
        self._stereo_fixing = True
        try:
            ok, msg = fix_stereo(st)
            self.log(f"stereo fix: {msg}")

            def _recheck():
                try:
                    after = inspect_stereo()
                    # Update UI without a second "restored" notify from was_broken
                    healed = after.ok and self._stereo_broken
                    if after.ok:
                        self._stereo_broken = False
                        self.stereo_label.set_markup(
                            "<span foreground='#2ec27e'>🎧 stereo</span>")
                        self.stereo_label.set_tooltip_text(after.detail or msg)
                        self.stereo_banner.hide()
                        self._stereo_dismissed_until = 0.0
                        self._rebuild_tray_menu()
                        if healed or ok:
                            notify_user("G935: stereo fixed", msg)
                    else:
                        self._apply_stereo_status(after, auto_fix=False)
                        if not ok:
                            notify_user(
                                "G935: stereo fix failed",
                                msg + " — try restarting EasyEffects.",
                                urgency="critical",
                            )
                finally:
                    self._stereo_fixing = False
                return False

            GLib.timeout_add(400, _recheck)
        except Exception as e:
            self._stereo_fixing = False
            self.log(f"stereo fix error: {e}")
            notify_user("G935: stereo fix error", str(e), urgency="critical")
        return False

    def _on_stereo_banner_response(self, bar, response_id):
        if response_id == Gtk.ResponseType.APPLY:
            self._do_stereo_fix()
        elif response_id == Gtk.ResponseType.CLOSE:
            # Quiet for 10 minutes unless it heals and breaks again
            self._stereo_dismissed_until = time.time() + 600
            bar.hide()

    def on_fix_stereo(self, *_):
        self._stereo_dismissed_until = 0.0
        self._do_stereo_fix()

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
            self._battery_presence.observe(True)
            # 0x8010 has no readable state on this headset. Keep each required
            # ADC/battery read self-healing by reasserting the saved target
            # immediately afterward; this avoids a visually-on/actually-flat
            # soundstage without introducing an audible off/on pulse.
            if self.dsp_sw.get_active():
                self.send("gkeys", 2, "01")
            mv = (reply[4] << 8) | reply[5]
            flags = reply[6]
            state, charging = batt_state(flags)
            # Feed tracker first so last_rest_mv is current, then interpret
            if charging is not None:
                self.health.add_sample(time.time(), mv, charging)
            if charging is None:
                reading = {
                    "raw_mv": mv, "cell_mv": mv, "pct": batt_percent(mv),
                    "charging": None, "path_is_rail": False,
                }
            else:
                reading = self.health.reading(mv, charging)
            pct = reading["pct"]
            cell = reading["cell_mv"]
            raw = reading["raw_mv"]
            rail = reading.get("path_is_rail", False)
            icon = "🔌" if charging else ("❓" if charging is None else "🔋")
            self._refresh_health_display(
                cell, pct, state, raw_mv=raw, path_is_rail=rail)
            pct_s = f"{pct}%" if pct is not None else "—%"
            if charging:
                path_note = f"path {raw} mV" + (" rail" if rail else "")
                cell_s = f"~{cell} mV cell" if cell is not None else "cell ?"
                self.batt_label.set_markup(
                    f"{icon} <b>{pct_s}</b>  "
                    f"<span size='small'>Charging</span>")
                self.batt_label.set_tooltip_text(
                    f"SoC from last off-charger rest"
                    f"{f' ({cell} mV, {pct}%)' if cell is not None else ' (unknown)'}."
                    f" ADC path {raw} mV while charging is not open-circuit "
                    f"cell voltage (flags {flags:#04x}).")
            else:
                self.batt_label.set_markup(
                    f"{icon} <b>{pct_s}</b>")
                self.batt_label.set_tooltip_text(
                    f"{cell} mV, {state} (flags {flags:#04x})")
            self._update_tray_battery(f"{icon} {pct_s} · {state}")
            self._check_battery_alerts(pct, charging)
            if self.connected is not True:
                was_off = self.connected is False
                self.connected = True
                self.conn_label.set_markup(
                    "<span foreground='#2ec27e'>●</span> <b>Connected</b>")
                self.conn_label.set_tooltip_text("headset on")
                if was_off:
                    # power-on wipes on-device state - re-assert panel settings
                    self.log("--- headset powered on: re-applying panel state ---")
                    self._assert_device_state(initial=False)
        elif self._battery_presence.observe(False) is False:
            self._mark_disconnected()
        else:
            # A single missed reply is normally receiver contention/noise, not
            # a power cycle. Keep the current state and avoid replaying
            # lighting/EQ on the next successful battery poll.
            self.log(
                f"--- battery poll missed "
                f"({self._battery_presence.misses}/"
                f"{self._battery_presence.miss_limit}); keeping state ---")

    def _check_battery_alerts(self, pct, charging):
        """Desktop notify once per low/critical threshold while discharging."""
        for alert in self._batt_alerts.update(pct, charging):
            # Stable replace ids so repeated cycles update the same toast slot
            rid = 93510 if alert.level == "low" else 93505
            ok = notify_user(
                alert.summary, alert.body, urgency=alert.urgency,
                replace_id=rid)
            self.log(
                f"BATT ALERT {alert.level}: {alert.body}"
                + ("" if ok else " (notify-send failed — check libnotify / DBus)"))

    def _mark_disconnected(self):
        self._battery_presence.reset()
        self.health.mark_offline()
        self._refresh_health_display(None, None, "headset off")
        self._update_tray_battery("headset off")
        if self.connected is not False:
            self.connected = False
            self.conn_label.set_markup(
                "<span foreground='gray'>●</span> Offline")
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
    install_app_css()
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
    win.present()
    Gtk.main()


if __name__ == "__main__":
    main()
