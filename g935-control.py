#!/usr/bin/env python3
"""
g935-control.py - GTK3 control panel for the Logitech G935.

Connects to g935-dspd over its local UNIX JSON-RPC socket when the daemon is
running (single hardware owner — zero hidraw contention, zero health.json
write races), and falls back transparently to direct hidraw when standalone.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys
import threading
import time

# Allow `python3 g935-control.py` from a git checkout without install.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib

from g935.app_icon import APP_ID

# Must run before Gtk is imported so Plasma 6 Wayland matches <id>.desktop.
GLib.set_prgname(APP_ID)
GLib.set_application_name("G935 Control")

from gi.repository import Gdk, Gtk

Gdk.set_program_class(APP_ID)

from g935.app_settings import AppSettings
from g935.audio import AudioControl
from g935.battery import (
    HEALTH_DEFAULTS, BatteryAlerts, HealthTracker, batt_percent, batt_state,
)
from g935.bindings import (
    DEFAULT_HOLD_S, HID_KEYS, KEY_LABELS, Binding, HoldTracker, KeyBinding,
    bindings_to_data, cycle_name, describe, key_for_index, media_call,
    normalize_bindings, run_command,
)
from g935.daemon_status import daemon_running, panel_lock_path, panel_running
from g935.device_client import DeviceClient
from g935.features import (
    DEVICE_TYPES, FEATURE_LABELS, LED_EFFECTS, LIGHT_MODES,
    build_light_params, format_frequency, parse_device_info, parse_eq_info,
    parse_firmware_info, parse_frequency_page, parse_gkey_control,
    parse_gkey_mask, parse_led_effect_info, parse_led_state,
    parse_led_zone_info,
)
from g935.hidpp import ERROR_CODES, PollPresence, find_headset, open_hidraw
from g935.mic import (
    BOOM_DOWN, BOOM_UP, BUTTON, read_boom, read_host_mic_switch,
    set_host_mic_switch,
)
from g935.mode import load_mode, save_mode
from g935.panel_ipc import bind_listener, notify_running_instance
from g935.paths import runtime_dir
from g935.stereo_route import (
    fix_stereo, inspect_stereo, is_g935_sink, notify_user, notify_user_with_id,
)
from g935.ui.battery_page import build_battery_page, render_health_display
from g935.ui.control_page import (
    build_control_page, build_eq_sliders, set_gkey_pill_active,
    sync_eq_curve, update_mic_badge,
)
from g935.ui.hardware_page import build_hardware_page
from g935.ui.lighting_page import (
    build_lighting_page, build_lighting_rows, sync_lighting_preview,
)
from g935.ui.settings_page import (
    build_settings_page, build_volume_rows, update_headset_vol_badge,
)
from g935.ui.theme import (
    apply_window_icon, brand_image, install_app_css, set_chip_tone, style,
)
from g935.ui.tray import TrayController
from g935.ui.wheel_calibration import WheelCalibrationWizard
from g935.volume_wheel import (
    DEFAULT_FINE_STEP, DEFAULT_STEP, DIRECTION_GUARD_S, FINE_INTERVAL_S,
    HOLD_REPEAT_DELAY_S, HOLD_REPEAT_INTERVAL_S, VOLUME_BOOST_MAX,
)

# HID++ 2.0 features this app knows how to drive. Indices are discovered live.
FEATURES = [
    ("devinfo",  0x0003),
    ("devname",  0x0005),
    ("gkeys",    0x8010),
    ("lighting", 0x8070),
    ("eq",       0x8310),
    ("sidetone", 0x8300),
    ("battery",  0x1f20),
]

ALSA_USBID = "046d:0a87"
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
    ("lighting", 3, "010200b4ff1388000700"),
    ("lighting", 3, "000200b4ff1388000800"),
    ("lighting", 4, "0001"), ("battery", 1, ""), ("eq", 2, ""),
]

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


class App(Gtk.Window):
    def __init__(self, dev_path, pid, hid_name, start_in_tray=False):
        self.profile = {
            **GENERIC_PROFILE,
            **DEVICE_PROFILES.get(pid, {"name": hid_name}),
        }
        self._start_in_tray = start_in_tray
        super().__init__(title=f"{self.profile['name']} Control")
        style(self, "g935-window")
        self.set_default_size(860, 900)
        apply_window_icon(self)

        self.pid = pid
        self.connected = None
        self._battery_presence = PollPresence(miss_limit=6, grace_s=45.0)
        self.settings = AppSettings()
        self.health = HealthTracker(auto_save_scrub=False)
        self._bindings = normalize_bindings(self.settings.data.get("bindings"))
        self._holds = HoldTracker(DEFAULT_HOLD_S)
        self._hold_src = None

        self.features = {}
        self.feature_meta = {}
        self.discovered = False
        self._discovering = False
        self.section_frames = {}
        self._headset_sink_cache = None
        self._last_known_g935_sink = None
        self._vol_pending = {}
        self._vol_containers = []
        self._vol_interacting = False
        self._headset_vol_pending = None
        self._gkey_mask = 0
        self._gkey_count = 0
        self._gkey_control = None
        self._g0_down = False
        self._g0_flash_src = None
        self._just_discovered = False
        self._last_batt_display = None
        self._health_ui_dirty = False
        self._batt_alerts = BatteryAlerts()
        self._batt_notify_ids = {}
        self._stereo_broken = False
        self._stereo_fixing = False
        self._stereo_checking = False
        self._stereo_dismissed_until = 0.0
        self._wheel_wizard: WheelCalibrationWizard | None = None
        self._device_firmware = []
        self._device_name_parts = bytearray()
        self._device_name_count = 0
        self._light_caps = {}

        boom = self.profile["has_boom_mic"]
        self.worker = DeviceClient(
            dev_path,
            self.log_traffic,
            mic_cb=self.on_mic_event if boom else None,
            boom_cb=self.on_boom_change if boom else None,
            poll_boom=boom,
            on_link=self.on_link_change,
            prefer_pid=pid,
            known_pids=set(DEVICE_PROFILES),
            boom_reader=read_boom if boom else None,
            idle_add=GLib.idle_add,
            event_cb=self.on_hidpp_event,
            battery_cb=self._on_daemon_battery,
            presence_cb=self._on_daemon_presence,
            mode_cb=self._on_daemon_mode,
            transport_cb=self._on_transport_change,
        )
        self.worker.start()
        self.audio = AudioControl(self._on_audio_change)
        self.tray = TrayController(self)

        # ---- HeaderBar ----
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        self.set_titlebar(hb)

        brand = Gtk.Box(spacing=7)
        brand.pack_start(brand_image(), False, False, 0)
        brand_label = Gtk.Label()
        brand_label.set_markup(
            f"<b>{GLib.markup_escape_text(self.profile['name'])}</b>")
        brand.pack_start(brand_label, False, False, 0)
        hb.pack_start(brand)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(140)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        hb.set_custom_title(switcher)

        # ---- Top status bar ----
        self.conn_label = Gtk.Label()
        self.conn_label.set_markup("<span foreground='gray'>●</span> Connecting")
        self.conn_label.set_tooltip_text("Headset wireless connection")
        style(self.conn_label, "g935-chip")

        self.mode_chip = Gtk.Label()
        style(self.mode_chip, "g935-chip")
        self.mode_chip.set_tooltip_text("Active headset mode and daemon status")

        self.batt_label = Gtk.Label()
        self.batt_label.set_markup("🔋 …")
        self.batt_label.set_tooltip_text("Battery level")
        style(self.batt_label, "g935-chip")

        self.stereo_label = Gtk.Label()
        self.stereo_label.set_no_show_all(True)
        self.stereo_label.hide()
        self.stereo_label.set_tooltip_text("PipeWire stereo route (L/R into headset)")
        style(self.stereo_label, "g935-chip")

        self.mic_label = Gtk.Label()
        self.mic_label.set_markup("🎤 …")
        self.mic_label.set_tooltip_text("Boom-mic position")
        style(self.mic_label, "g935-chip")

        status = Gtk.Box(spacing=8)
        style(status, "g935-status")
        status.set_border_width(10)
        status.pack_start(self.conn_label, False, False, 0)
        status.pack_start(self.mode_chip, False, False, 0)
        status.pack_start(self.batt_label, False, False, 0)
        status.pack_start(self.stereo_label, False, False, 0)
        status.pack_end(self.mic_label, False, False, 0)

        # ---- Stereo warning banner ----
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
        outer.pack_start(self.stack, True, True, 0)
        self.add(outer)

        # ---- Pages (modular builders in g935/ui/) ----
        control_box = self._page("control", "Control")
        build_control_page(self, control_box, _read_host_mic, _set_host_mic)

        lighting_box = self._page("lighting", "Lighting")
        build_lighting_page(self, lighting_box)
        self.section_frames["lighting"] = self.stack.get_child_by_name("lighting")

        health_box = self._page("health", "Battery Health")
        build_battery_page(self, health_box)
        self.section_frames["battery"] = self.stack.get_child_by_name("health")

        hw_box = self._page("hardware", "Hardware")
        build_hardware_page(self, hw_box)

        settings_box = self._page("settings", "Settings")
        build_settings_page(self, settings_box)

        self._update_mode_chip()
        self._refresh_health_display(None, None, "…")

        # ---- Background timers & tray ----
        self._start_discovery()
        GLib.timeout_add_seconds(5, self.heartbeat)
        GLib.timeout_add_seconds(3, self._poll_daemon_status)
        GLib.timeout_add_seconds(8, self._poll_stereo_route)
        self.tray.setup()
        self._ipc_sock = None
        self.connect("delete-event", self.on_delete_event)
        self.connect("show", self._on_window_show)
        self.connect("hide", self._on_view_changed)
        self.stack.connect("notify::visible-child-name", self._on_view_changed)
        GLib.idle_add(self._on_audio_change)
        GLib.idle_add(self._poll_stereo_route_once)

    def _page(self, name: str, title: str):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        style(box, "g935-page")
        box.set_border_width(16)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_shadow_type(Gtk.ShadowType.NONE)
        sw.add(box)
        self.stack.add_titled(sw, name, title)
        return box

    # ---------- HID++ feature discovery ----------
    def cmd(self, feat: str, fn: int, params: str = ""):
        idx = self.features.get(feat)
        if idx is None:
            return None
        return f"11ff{idx:02x}{(fn << 4) | 0x0b:02x}{params}"

    def send(self, feat: str, fn: int, params: str = "", cb=None) -> None:
        hx = self.cmd(feat, fn, params)
        if hx:
            self.worker.submit(hx, cb)

    def _start_discovery(self) -> None:
        if self.discovered or self._discovering:
            return
        self._discovering = True
        self._disc_found = {}
        self._disc_meta = {}
        self._disc_err = False
        self._disc_queue = list(FEATURES)
        self._disc_next()

    def _disc_next(self) -> None:
        if not self._disc_queue:
            if self._disc_err and not self._disc_found:
                return self._walk_featureset()
            return self._finish_discovery()
        self._disc_cur = self._disc_queue.pop(0)
        self._disc_retried = False
        self._disc_probe()

    def _disc_probe(self) -> None:
        attr, fid = self._disc_cur
        self.worker.submit(
            f"11ff000b{fid:04x}",
            lambda st, rep, a=attr: self._got_feature(a, st, rep))

    def _got_feature(self, attr, status, reply) -> None:
        if status == "GONE" or not self._discovering:
            self._discovering = False
            return
        if status == "TIMEOUT":
            if not self._disc_retried:
                self._disc_retried = True
                self._disc_probe()
                return
            self._discovering = False
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

    def _walk_featureset(self) -> None:
        self.worker.submit("11ff010b", self._got_feat_count)

    def _got_feat_count(self, status, reply) -> None:
        if status == "GONE" or not self._discovering:
            self._discovering = False
            return
        if status != "ACK":
            self._discovering = False
            if status == "TIMEOUT":
                self._mark_disconnected()
            return
        self._walk_ids = {}
        self._walk_left = list(range(1, min(reply[4], 32) + 1))
        self._walk_next()

    def _walk_next(self) -> None:
        if not self._walk_left:
            wanted = {fid: attr for attr, fid in FEATURES}
            for idx, fid in self._walk_ids.items():
                if fid in wanted:
                    self._disc_found[wanted[fid]] = idx
            return self._finish_discovery()
        i = self._walk_left.pop(0)
        self.worker.submit(
            f"11ff011b{i:02x}",
            lambda st, rep, idx=i: self._got_feat_id(idx, st, rep))

    def _got_feat_id(self, i, status, reply) -> None:
        if status == "GONE" or not self._discovering:
            self._discovering = False
            return
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

    def _finish_discovery(self) -> None:
        self.features = self._disc_found
        self.feature_meta = self._disc_meta
        self.discovered = True
        self._discovering = False
        self.log(
            "--- discovered features: "
            + (", ".join(f"{a}={i:02x}" for a, i in self.features.items()) or "none")
            + " ---")
        for attr, widget in self.section_frames.items():
            present = attr in self.features
            widget.set_no_show_all(not present)
            widget.set_visible(present)
        if "lighting" in self.features:
            self.send("lighting", 0, cb=self._got_light_info)
        if "eq" in self.features:
            self.send("eq", 0, cb=self._got_eq_info)
        if "gkeys" in self.features:
            self.send("gkeys", 0, cb=self._got_gkey_info)
            self._request_gkey_control()
        if "battery" in self.features:
            self.send("battery", 1, cb=self._got_power_timeout)
        self.power_frame.set_no_show_all("battery" not in self.features)
        self.power_frame.set_visible("battery" in self.features)
        self._update_feature_summary()
        self._probe_device_identity()
        self._just_discovered = True
        self._assert_device_state(initial=True)

    # ---------- Capability & identity probes ----------
    def _got_light_info(self, status, reply) -> None:
        if status != "ACK":
            return
        n = reply[4] if 1 <= reply[4] <= 8 else 1
        nv_caps = (reply[5] << 8) | reply[6] if len(reply) > 6 else 0
        ext_caps = (reply[7] << 8) | reply[8] if len(reply) > 8 else 0
        readable = bool(ext_caps & 1)
        if not self.zone_widgets:
            build_lighting_rows(self, [f"Zone {z}" for z in range(n)])
            self._apply_saved_lighting()
        if hasattr(self, "light_read_active"):
            self.light_read_active.set_sensitive(readable)
            self.light_read_saved.set_sensitive(readable)
        if hasattr(self, "light_boot_row"):
            supported = bool(nv_caps & 1)
            self.light_boot_row.set_no_show_all(not supported)
            self.light_boot_row.set_visible(supported)
            if supported:
                self.send("lighting", 4, "0001", cb=self._got_light_boot_state)
        self._light_caps = {}
        for zone in range(n):
            self.send(
                "lighting", 1, f"{zone:02x}",
                cb=lambda st, rep, z=zone: self._got_light_zone(z, st, rep))
        self._update_capability_summary()

    def _got_light_zone(self, zone, status, reply) -> None:
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

    def _got_light_effect(self, zone, status, reply) -> None:
        if status != "ACK":
            return
        try:
            _z, index, effect_id, capabilities, period = parse_led_effect_info(reply)
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

    def _got_eq_info(self, status, reply) -> None:
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
                build_eq_sliders(self, [str(i + 1) for i in range(n)])
            for slider in self.eq_sliders:
                slider.set_range(info.minimum_db, info.maximum_db)
            sync_eq_curve(self)
            self._eq_freqs = []
            self._request_eq_frequency_page(0)
            self.send("eq", 2, "01", cb=self.got_eq)
            self._update_capability_summary()
        else:
            w = self.section_frames["eq"]
            w.set_no_show_all(True)
            w.set_visible(False)

    def _request_eq_frequency_page(self, start: int) -> None:
        total = self._eq_info.bands
        if start >= total:
            self._set_eq_frequency_labels()
            return
        count = min(7, total - start)
        self.send(
            "eq", 1, f"{start:02x}",
            cb=lambda st, rep, s=start, n=count:
            self._got_eq_frequency_page(s, n, st, rep))

    def _got_eq_frequency_page(self, start, count, status, reply) -> None:
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

    def _set_eq_frequency_labels(self) -> None:
        for label, frequency in zip(
                getattr(self, "eq_band_labels", []), self._eq_freqs):
            label.set_text(format_frequency(frequency))
        sync_eq_curve(self)
        self._update_capability_summary()

    def _got_gkey_info(self, status, reply) -> None:
        if status == "ACK":
            self._gkey_count = min(reply[4], 32)
            self.gkey_labels["g0"].set_visible(True)
            for i, name in enumerate(HID_KEYS):
                self.gkey_labels[name].set_visible(i < self._gkey_count)
            self._update_capability_summary()

    def _request_gkey_control(self) -> None:
        self.send("gkeys", 1, cb=self._got_gkey_control)

    def _got_gkey_control(self, status, reply) -> None:
        if status != "ACK":
            return
        try:
            self._gkey_control = parse_gkey_control(reply)
        except ValueError:
            return
        self._update_capability_summary()

    def _update_feature_summary(self) -> None:
        lines = ["Root / Feature Set  · protocol discovery"]
        for attr, index in sorted(self.features.items(), key=lambda x: x[1]):
            fid, flags, version = self.feature_meta.get(
                attr, (dict(FEATURES).get(attr, 0), 0, 0))
            label = FEATURE_LABELS.get(fid, attr)
            flag_text = f" · flags 0x{flags:02X}" if flags else ""
            lines.append(
                f"Index {index:02X}  · 0x{fid:04X} {label}  · v{version}{flag_text}")
        self.hw_features.set_text("\n".join(lines))

    def _update_capability_summary(self) -> None:
        lines = []
        if self._gkey_count:
            lines.append(
                f"G keys: G0 (earcup mute) + G1–G{self._gkey_count} (HID++); "
                "tap/hold bindings in software mode")
        if self._gkey_control is not None:
            lines.append(
                f"G-key control GET: 0x{self._gkey_control:02X} "
                "(constant on this firmware; not mode readback)")
        info = getattr(self, "_eq_info", None)
        if info:
            freqs = getattr(self, "_eq_freqs", [])
            freq_text = ", ".join(format_frequency(f) for f in freqs)
            lines.append(
                f"Equalizer: {info.bands} bands, {info.minimum_db:+d} to "
                f"{info.maximum_db:+d} dB"
                + (f" ({freq_text} Hz)" if len(freqs) == info.bands else ""))
        if self._light_caps:
            for zone in sorted(self._light_caps):
                cap = self._light_caps[zone]
                effects = ", ".join(
                    e["name"] for _i, e in sorted(cap["effects"].items()))
                lines.append(f"Lighting {cap['name']}: {effects or 'probing…'}")
        if "sidetone" in self.features:
            lines.append("Sidetone: level 0–100")
        if "battery" in self.features:
            timeout = getattr(self, "_power_timeout_minutes", None)
            t_str = (
                "never" if timeout == 0 else
                f"{timeout} minutes" if timeout is not None else "probing…")
            lines.append(f"Power: ADC voltage, charge-state flags, auto-off {t_str}")
        self.hw_caps.set_text("\n".join(lines) if lines else "Probing…")

    def _probe_device_identity(self) -> None:
        if "devinfo" in self.features:
            self.send("devinfo", 0, cb=self._got_device_info)
        if "devname" in self.features:
            self.send("devname", 0, cb=self._got_device_name_count)
            self.send("devname", 2, cb=self._got_device_type)

    def _got_device_info(self, status, reply) -> None:
        if status != "ACK":
            return
        try:
            info = parse_device_info(reply)
        except ValueError:
            return
        self.hw_values["unit"].set_text(info.unit_id)
        self.hw_values["model"].set_text(info.model_id)
        devinfo_ver = self.feature_meta.get("devinfo", (0, 0, 0))[2]
        transport = (
            f"v0 raw value 0x{info.transport_mask:02X}" if devinfo_ver == 0
            else (", ".join(info.transports) or "Not reported")
        )
        self.hw_values["transport"].set_text(transport)
        self.hw_values["entities"].set_text(str(info.entity_count))
        self._device_firmware = []
        for entity in range(min(info.entity_count, 16)):
            self.send(
                "devinfo", 1, f"{entity:02x}",
                cb=lambda st, rep, e=entity: self._got_firmware_info(e, st, rep))

    def _got_firmware_info(self, entity, status, reply) -> None:
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
            "\n".join(v for _i, v in self._device_firmware))

    def _got_device_name_count(self, status, reply) -> None:
        if status != "ACK":
            return
        self._device_name_count = min(reply[4], 255)
        self._device_name_parts = bytearray()
        self._request_device_name_chunk(0)

    def _request_device_name_chunk(self, start: int) -> None:
        if start >= self._device_name_count:
            name = bytes(self._device_name_parts[:self._device_name_count])
            self.hw_values["name"].set_text(
                name.decode("utf-8", "replace").rstrip("\0"))
            return
        self.send(
            "devname", 1, f"{start:02x}",
            cb=lambda st, rep, s=start: self._got_device_name_chunk(s, st, rep))

    def _got_device_name_chunk(self, start, status, reply) -> None:
        if status != "ACK":
            return
        remaining = self._device_name_count - start
        chunk = reply[4:4 + min(16, remaining)]
        if not chunk:
            return
        self._device_name_parts.extend(chunk)
        self._request_device_name_chunk(start + len(chunk))

    def _got_device_type(self, status, reply) -> None:
        if status == "ACK":
            self.hw_values["type"].set_text(
                DEVICE_TYPES.get(reply[4], f"Type {reply[4]}"))

    def _assert_device_state(self, initial: bool) -> None:
        self.send("gkeys", 2, f"{1 if self.dsp_sw.get_active() else 0:02x}")
        self._apply_saved_lighting()
        if initial:
            self.send("sidetone", 0, cb=self.got_sidetone)
        else:
            self._apply_sidetone()
            self._apply_eq(persistence=0)
        self._battery_presence.note_activity(25.0)

    # ---------- Link, transport & logging ----------
    def _on_transport_change(self, via_daemon: bool) -> None:
        if hasattr(self, "hw_values") and "transport_link" in self.hw_values:
            self.hw_values["transport_link"].set_text(self.worker.transport_label)
        self._update_mode_chip()
        self.log(f"--- control link: {self.worker.transport_label} ---")

    def on_link_change(self, up: bool) -> None:
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

    def log(self, text: str) -> None:
        if not hasattr(self, "logbuf"):
            return
        self.logbuf.insert(self.logbuf.get_end_iter(), text + "\n")
        count = self.logbuf.get_line_count()
        if count > 2000:
            start = self.logbuf.get_start_iter()
            end = self.logbuf.get_iter_at_line(count - 1500)
            self.logbuf.delete(start, end)
        mark = self.logbuf.create_mark(None, self.logbuf.get_end_iter(), False)
        self.logview.scroll_mark_onscreen(mark)

    def log_traffic(self, hx, status, reply) -> None:
        if status == "ACK":
            h = reply.hex()
            while h.endswith("00"):
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

    # ---------- Software mode & G-key bindings ----------
    def _update_mode_chip(self) -> None:
        if not hasattr(self, "mode_chip"):
            return
        sw_on = self.dsp_sw.get_active()
        d_up = self.worker.via_daemon or daemon_running()
        if sw_on and d_up:
            self.mode_chip.set_markup("<b>DSP</b> · daemon")
            set_chip_tone(self.mode_chip, "ok")
        elif sw_on:
            self.mode_chip.set_markup("<b>DSP</b> · no daemon")
            set_chip_tone(self.mode_chip, "warn")
        else:
            self.mode_chip.set_markup("HW mode")
            set_chip_tone(self.mode_chip, None)

    def on_dsp_toggle(self, sw, _pspec) -> None:
        on = sw.get_active()
        mode = "ghub" if on else "hardware"
        save_mode(mode)
        self.send("gkeys", 2, f"{1 if on else 0:02x}")
        self._request_gkey_control()
        self._update_mode_note()
        self._update_mic_note()
        self._update_mode_chip()

    def _on_daemon_mode(self, mode: str) -> None:
        want = mode == "ghub"
        if self.dsp_sw.get_active() != want:
            self.dsp_sw.handler_block_by_func(self.on_dsp_toggle)
            self.dsp_sw.set_active(want)
            self.dsp_sw.handler_unblock_by_func(self.on_dsp_toggle)
            self._update_mode_note()
            self._update_mic_note()
            self._update_mode_chip()

    def _update_mode_note(self) -> None:
        if self.dsp_sw.get_active():
            if self.worker.via_daemon or daemon_running():
                self.mode_note.set_markup(
                    "<small>Software mode active: open DSP soundstage ON, "
                    "G0–G3 fire your bindings, and <b>g935-dspd</b> manages "
                    "boom mute and the earcup wheel.</small>")
            else:
                self.mode_note.set_markup(
                    "<small><span foreground='#e01b24'><b>Software mode without "
                    "g935-dspd:</b> boom mute will stick until the daemon runs.</span> "
                    "Start with: <tt>systemctl --user enable --now g935-dsp</tt></small>")
        else:
            self.mode_note.set_markup(
                "<small>Hardware mode: stock flat sound; boom mute and earcup "
                "buttons are handled directly in headset firmware.</small>")

    def on_binding_changed(self, combo, key, layer) -> None:
        action = combo.get_active_id() or "none"
        prev = self._bindings[key].layer(layer)
        self._set_binding_layer(key, layer, Binding(action, prev.command))
        self._bind_cmds[key][layer].set_visible(action == "command")
        self._save_bindings()

    def on_binding_command(self, entry, key, layer) -> None:
        prev = self._bindings[key].layer(layer)
        self._set_binding_layer(key, layer, Binding(prev.action, entry.get_text()))
        self._save_bindings()

    def _set_binding_layer(self, key, layer, slot: Binding) -> None:
        cur = self._bindings[key]
        self._bindings[key] = (
            KeyBinding(cur.tap, slot) if layer == "hold"
            else KeyBinding(slot, cur.hold)
        )

    def _save_bindings(self) -> None:
        self.settings.data["bindings"] = bindings_to_data(self._bindings)
        self.settings.save()

    def _fire_binding(self, key: str, layer: str) -> None:
        kb = self._bindings.get(key)
        if kb is None:
            return
        binding = kb.layer(layer)
        if binding.action == "none":
            return
        ok, detail = self._run_binding(binding)
        label = describe(binding)
        self.log(
            f"--- {KEY_LABELS[key]} {layer} {label}: "
            f"{'ok' if ok else 'failed'} {detail} ---")
        self.gkey_last.set_text(f"{KEY_LABELS[key]} {layer}: {label}")

    def _arm_hold_check(self) -> None:
        if self._hold_src is not None:
            return
        self._hold_src = GLib.timeout_add(50, self._hold_tick)

    def _hold_tick(self) -> bool:
        for key in self._holds.due(time.monotonic()):
            self._fire_binding(key, "hold")
        if self._holds.pending():
            return True
        self._hold_src = None
        return False

    def _run_binding(self, binding: Binding):
        action = binding.action
        if action == "play_pause":
            return media_call("PlayPause")
        if action == "next_track":
            return media_call("Next")
        if action == "prev_track":
            return media_call("Previous")
        if action == "vol_up":
            return self._nudge_headset_volume(5)
        if action == "vol_down":
            return self._nudge_headset_volume(-5)
        if action == "mute_output":
            name = self._headset_sink()
            if not name:
                return False, "no sink"
            self.audio.toggle_mute("sink", name)
            return True, "toggled headset mute"
        if action == "mic_mute":
            cur = _read_host_mic()
            if cur is None:
                return False, "host mic switch unknown"
            if not cur and self.profile["has_boom_mic"] and self.worker._boom is False:
                return False, "ignored — boom is up"
            _set_host_mic(not cur)
            self._refresh_hostmic()
            return True, "muted" if cur else "unmuted"
        if action == "show_panel":
            self.present()
            return True, "presented"
        if action in ("next_eq", "prev_eq"):
            names = list(self.profile.get("eq_presets") or ())
            current = (
                self.eq_preset_button.get_label()
                if hasattr(self, "eq_preset_button") else "")
            nxt = cycle_name(names, current, 1 if action == "next_eq" else -1)
            if not nxt:
                return False, "no presets"
            self.on_eq_preset(None, nxt)
            return True, nxt
        if action == "toggle_mode":
            self.dsp_sw.set_active(not self.dsp_sw.get_active())
            return True, "software" if self.dsp_sw.get_active() else "hardware"
        if action == "command":
            return run_command(binding.command)
        return False, f"unknown {action}"

    def on_hidpp_event(self, report: bytes) -> None:
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
        count = self._gkey_count or len(HID_KEYS)
        now = time.monotonic()
        for i in range(min(count, len(HID_KEYS))):
            bit = 1 << i
            down = bool(mask & bit)
            set_gkey_pill_active(self.gkey_labels[HID_KEYS[i]], down)
            key = key_for_index(i)
            if changed & bit:
                events.append(f"G{i + 1} {'down' if down else 'up'}")
                if key:
                    if down:
                        self._holds.press(key, now)
                        self._arm_hold_check()
                    elif self._holds.release(key) == "tap":
                        self._fire_binding(key, "tap")
            elif changed == 0 and down and key:
                events.append(f"G{i + 1} down")
                if self._holds.release(key) == "tap":
                    self._fire_binding(key, "tap")
                self._holds.press(key, now)
                self._arm_hold_check()
        self._gkey_mask = mask
        if events:
            text = ", ".join(events)
            self.gkey_last.set_text(text)
            self.log(f"--- G-key event: {text} ---")

    # ---------- Mic ----------
    def _set_mic_status(self, markup_small: str, markup_big: str) -> None:
        self.mic_label.set_markup(markup_small)
        if hasattr(self, "mic_big"):
            self.mic_big.set_markup(markup_big)

    def _update_mic_note(self) -> None:
        if not hasattr(self, "mic_note"):
            return
        if not self.dsp_sw.get_active():
            self.mic_note.set_markup(
                "<small>Hardware mode: boom mute is handled on-device. "
                "Host switch is an independent capture layer.</small>")
        elif self.worker.via_daemon or daemon_running():
            self.mic_note.set_markup(
                "<small>Software mode: g935-dspd clears the boom flag with a "
                "1.5s host-mute pulse after boom-down.</small>")
        else:
            self.mic_note.set_markup(
                "<small><span foreground='#e01b24'>Daemon not running — boom "
                "mute will stick after raising the mic.</span></small>")

    def on_hostmic_toggle(self, sw, _pspec) -> None:
        _set_host_mic(sw.get_active())

    def _refresh_hostmic(self) -> None:
        if not hasattr(self, "hostmic_sw"):
            return
        state = _read_host_mic()
        if state is None:
            return
        self.hostmic_sw.handler_block_by_func(self.on_hostmic_toggle)
        self.hostmic_sw.set_active(state)
        self.hostmic_sw.handler_unblock_by_func(self.on_hostmic_toggle)

    def on_boom_change(self, down: bool) -> None:
        update_mic_badge(self, down)
        if not down:
            self._set_mic_status("🎤 <b>muted</b>", "Boom raised — headset mute flag set")
            set_chip_tone(self.mic_label, "bad")
            self.log("--- boom raised: headset mute flag SET ---")
        elif self.dsp_sw.get_active():
            self._set_mic_status("🎤 <b>live</b>", "Boom lowered — daemon clears mute (~2s)")
            set_chip_tone(self.mic_label, "ok")
            self.log("--- boom lowered: daemon runs the unmute pulse ---")
        else:
            self._set_mic_status("🎤 <b>live</b>", "Boom lowered — microphone live")
            set_chip_tone(self.mic_label, "ok")
            self.log("--- boom lowered: unmuted (hardware mode) ---")

    def _clear_g0_flash(self) -> bool:
        self._g0_flash_src = None
        if not self._g0_down and "g0" in self.gkey_labels:
            set_gkey_pill_active(self.gkey_labels["g0"], False)
        return False

    def on_mic_event(self, bits: int) -> None:
        button = bool(bits & BUTTON) and not bits & (BOOM_UP | BOOM_DOWN)
        hold_set = self._bindings["g0"].hold.action != "none"
        if button:
            if not hold_set:
                self._g0_down = False
                if "g0" in self.gkey_labels:
                    set_gkey_pill_active(self.gkey_labels["g0"], True)
                    if self._g0_flash_src is not None:
                        GLib.source_remove(self._g0_flash_src)
                    self._g0_flash_src = GLib.timeout_add(
                        200, self._clear_g0_flash)
                if self.dsp_sw.get_active():
                    self._fire_binding("g0", "tap")
            else:
                if self._g0_down and self._holds.release("g0") == "tap":
                    self._fire_binding("g0", "tap")
                self._g0_down = True
                self._on_g0_edge(True)
            self.log("--- G0 (earcup key) ---")
            GLib.timeout_add(300, lambda: (self._refresh_hostmic(), False)[1])
        elif self._g0_down:
            self._g0_down = False
            self._on_g0_edge(False)

    def _on_g0_edge(self, down: bool) -> None:
        if "g0" in self.gkey_labels:
            set_gkey_pill_active(self.gkey_labels["g0"], down)
        if not self.dsp_sw.get_active():
            return
        hold_set = self._bindings["g0"].hold.action != "none"
        if down:
            self._holds.press("g0", time.monotonic())
            if hold_set:
                self._arm_hold_check()
            else:
                self._fire_binding("g0", "tap")
        elif hold_set and self._holds.release("g0") == "tap":
            self._fire_binding("g0", "tap")
        else:
            self._holds.release("g0")

    # ---------- Sidetone ----------
    def on_sidetone(self, scale) -> None:
        val = int(round(scale.get_value()))
        if hasattr(self, "sidetone_badge"):
            self.sidetone_badge.set_text(f"{val}%")
        if self._sidetone_pending:
            GLib.source_remove(self._sidetone_pending)
        self._sidetone_pending = GLib.timeout_add(160, self._apply_sidetone)

    def _apply_sidetone(self) -> bool:
        self._sidetone_pending = None
        self.send("sidetone", 1, f"{int(self.sidetone.get_value()):02x}")
        return False

    def got_sidetone(self, status, reply) -> None:
        if status == "ACK":
            self.sidetone.handler_block_by_func(self.on_sidetone)
            self.sidetone.set_value(reply[4])
            self.sidetone.handler_unblock_by_func(self.on_sidetone)
            if hasattr(self, "sidetone_badge"):
                self.sidetone_badge.set_text(f"{int(reply[4])}%")

    # ---------- Power management ----------
    def _got_power_timeout(self, status, reply) -> None:
        if status != "ACK":
            return
        self._power_timeout_minutes = reply[4]
        self.power_timeout.set_value(reply[4])
        self._update_capability_summary()

    def on_power_timeout_apply(self, _btn) -> None:
        minutes = int(self.power_timeout.get_value())
        self.send(
            "battery", 2, f"{minutes:02x}",
            cb=self._got_power_timeout_after_write)

    def _got_power_timeout_after_write(self, status, _reply) -> None:
        if status == "ACK":
            self._power_timeout_minutes = int(self.power_timeout.get_value())
            self._update_capability_summary()

    # ---------- EQ ----------
    def _mark_eq_custom(self, _scale) -> None:
        if hasattr(self, "eq_preset_button"):
            self.eq_preset_button.set_label("Presets")
        sync_eq_curve(self)

    def on_eq_preset_menu(self, button, name, popover) -> None:
        popover.popdown()
        self.on_eq_preset(button, name)

    def on_eq_preset(self, _btn, name: str) -> None:
        gains = self.profile["eq_presets"][name] or [0] * len(self.eq_sliders)
        for s, v in zip(self.eq_sliders, gains):
            s.set_value(v)
        self.eq_preset_button.set_label(name)
        sync_eq_curve(self)
        self.on_eq_apply(None)

    def _apply_eq(self, persistence: int = 1) -> None:
        if not self.eq_sliders:
            return
        gains = "".join(
            f"{int(s.get_value()) & 0xFF:02x}" for s in self.eq_sliders)
        self.send("eq", 3, f"{persistence:02x}" + gains)

    def on_eq_apply(self, _btn) -> None:
        self._apply_eq(persistence=1)

    def got_eq(self, status, reply) -> None:
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
        sync_eq_curve(self)

    # ---------- Lighting ----------
    def _update_light_fields(self, combo, zone) -> None:
        widgets = self.zone_widgets[zone]
        mode = combo.get_active()
        widgets["color"].set_sensitive(mode in (1, 2))
        widgets["period"].set_sensitive(mode in (2, 3))
        widgets["intensity"].set_sensitive(mode in (2, 3))
        widgets["waveform"].set_sensitive(mode == 2)
        widgets["ramp"].set_sensitive(mode == 1)
        sync_lighting_preview(self, zone)

    def _got_light_boot_state(self, status, reply) -> None:
        if status != "ACK" or len(reply) < 9:
            return
        state = reply[6]
        self._light_boot_params = (reply[7], reply[8])
        self.light_boot_switch.handler_block_by_func(self.on_light_boot_toggle)
        self.light_boot_switch.set_active(state != 2)
        self.light_boot_switch.handler_unblock_by_func(self.on_light_boot_toggle)

    def on_light_boot_toggle(self, switch, _pspec) -> None:
        state = 1 if switch.get_active() else 2
        param1, param2 = getattr(self, "_light_boot_params", (0, 0))
        self.send("lighting", 5, f"0001{state:02x}{param1:02x}{param2:02x}")

    def _light_params(self, zone, persistence: int = 0) -> str:
        widgets = self.zone_widgets[zone]
        mode = widgets["mode"].get_active()
        rgba = widgets["color"].get_rgba()
        rgb = (round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255))
        return build_light_params(
            zone, mode, rgb,
            period_ms=int(widgets["period"].get_value()),
            intensity=int(widgets["intensity"].get_value()),
            waveform=widgets["waveform"].get_active(),
            ramp=widgets["ramp"].get_active(),
            persistence=persistence,
        )

    def on_light_apply(self, _btn, zone) -> None:
        self.send("lighting", 3, self._light_params(zone, persistence=1))
        widgets = self.zone_widgets[zone]
        rgba = widgets["color"].get_rgba()
        self.settings.data["lighting"][str(zone)] = {
            "mode": widgets["mode"].get_active(),
            "color": "#%02X%02X%02X" % (
                round(rgba.red * 255),
                round(rgba.green * 255),
                round(rgba.blue * 255),
            ),
            "period": int(widgets["period"].get_value()),
            "intensity": int(widgets["intensity"].get_value()),
            "waveform": widgets["waveform"].get_active(),
            "ramp": widgets["ramp"].get_active(),
        }
        self.settings.save()

    def on_light_apply_all(self, _btn) -> None:
        for zone in sorted(self.zone_widgets):
            self.on_light_apply(None, zone)

    def _apply_saved_lighting(self) -> None:
        for zone in self.zone_widgets:
            if str(zone) in self.settings.data["lighting"]:
                self.send("lighting", 3, self._light_params(zone, persistence=0))

    def _read_lighting(self, source: int) -> None:
        for zone in self.zone_widgets:
            self.send(
                "lighting", 14, f"{zone:02x}{source:02x}",
                cb=lambda st, rep, z=zone: self._got_lighting_state(z, st, rep))

    def _got_lighting_state(self, zone, status, reply) -> None:
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
        sync_lighting_preview(self, zone)

    # ---------- Developer console ----------
    def on_full_sequence(self, _btn) -> None:
        self.log("--- G HUB connect sequence ---")
        self.dsp_sw.set_active(True)
        for feat, fn, params in self.profile["connect_burst"]:
            self.send(feat, fn, params)
        self.send("sidetone", 0, cb=self.got_sidetone)
        self.send("eq", 2, "01", cb=self.got_eq)

    def on_raw_send(self, _w) -> None:
        hx = self.raw_entry.get_text().strip().replace(" ", "")
        if hx:
            self.worker.submit(hx)

    # ---------- Window lifecycle & single-instance IPC ----------
    def on_delete_event(self, *_) -> bool:
        if self.tray.indicator or self.tray.waiting:
            self.hide()
            return True
        return False

    def _on_window_show(self, *_) -> None:
        self._on_view_changed()

    def _on_view_changed(self, *_) -> None:
        visible = bool(self.get_visible())
        page = self.stack.get_visible_child_name()
        if hasattr(self, "light_preview"):
            if visible and page == "lighting":
                sync_lighting_preview(self)
            self.light_preview.set_animating(visible and page == "lighting")
        if hasattr(self, "batt_graphic"):
            self.batt_graphic.set_animating(visible and page == "health")
        if visible and page == "health":
            if self.worker.via_daemon:
                if self.health.reload_if_changed():
                    self._health_ui_dirty = True
            if self._health_ui_dirty:
                self._health_ui_dirty = False
                if self._last_batt_display is not None:
                    self._refresh_health_display(
                        *self._last_batt_display, force_ui=True)
                else:
                    self._refresh_health_display(None, None, "…", force_ui=True)

    def listen_for_activation(self, sock=None) -> None:
        if sock is not None:
            self._ipc_sock = sock
        if getattr(self, "_ipc_sock", None) is None:
            try:
                self._ipc_sock = bind_listener()
            except OSError as exc:
                self.log(f"--- panel activation socket failed: {exc} ---")
                return
        GLib.io_add_watch(
            self._ipc_sock.fileno(), GLib.IO_IN, self._on_activation)

    def _on_activation(self, *_) -> bool:
        try:
            self._ipc_sock.recv(64)
        except OSError:
            return True
        self.present()
        return True

    def on_quit(self, *_) -> None:
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        if hasattr(self, "light_preview"):
            self.light_preview.set_animating(False)
        if hasattr(self, "batt_graphic"):
            self.batt_graphic.set_animating(False)
        if self._wheel_wizard is not None:
            self._wheel_wizard.cancel()
        self.audio.stop()
        was_daemon = self.worker.via_daemon
        try:
            self.worker.stop()
        except Exception:
            pass
        if not was_daemon:
            self.health.save()
        self.settings.save()
        Gtk.main_quit()

    # ---------- Audio devices, volume & wheel settings ----------
    def _audio_snapshot(self):
        return (
            self.audio.list_sinks(),
            self.audio.default_sink(),
            self.audio.list_sources(),
            self.audio.default_source(),
        )

    def _nudge_headset_volume(self, delta: int):
        name = self._headset_sink()
        if not name:
            return False, "no sink"
        cur = self.audio.get_volume("sink", name)
        if cur is None:
            return False, "volume unreadable"
        new = max(0, min(VOLUME_BOOST_MAX, cur + delta))
        if new != cur:
            self.audio.set_volume("sink", name, new)
        return True, f"{new}%"

    def _headset_sink(self):
        now = time.monotonic()
        cached = self._headset_sink_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        listed, sinks = self.audio.list_sinks_checked()
        name = None
        if listed:
            for sink_name, desc in sinks:
                if is_g935_sink(sink_name, desc):
                    name = sink_name
                    self._last_known_g935_sink = sink_name
                    break
        elif self._last_known_g935_sink:
            name = self._last_known_g935_sink
        if name is None:
            name = self.audio.default_sink()
        self._headset_sink_cache = (now + 2.0, name)
        return name

    def _sync_headset_vol_slider(self) -> None:
        if getattr(self, "headset_vol", None) is None:
            return
        if self._vol_interacting or self._headset_vol_pending:
            return
        name = self._headset_sink()
        raw = self.audio.get_volume("sink", name) if name else None
        muted = self.audio.get_mute("sink", name) if name else None
        if raw is None:
            update_headset_vol_badge(self, None, None)
            return
        self.headset_vol.handler_block_by_func(self.on_headset_vol_changed)
        self.headset_vol.set_value(raw)
        self.headset_vol.handler_unblock_by_func(self.on_headset_vol_changed)
        update_headset_vol_badge(self, raw, muted)

    def on_headset_vol_changed(self, scale) -> None:
        pct = int(round(scale.get_value()))
        update_headset_vol_badge(self, pct, False)
        if self._headset_vol_pending:
            GLib.source_remove(self._headset_vol_pending)
        self._headset_vol_pending = GLib.timeout_add(
            60, self._flush_headset_vol, pct)

    def _flush_headset_vol(self, pct: int) -> bool:
        self._headset_vol_pending = None
        name = self._headset_sink()
        if name:
            self.audio.set_volume("sink", name, pct)
        return False

    def on_headset_mute_toggle(self, _btn) -> None:
        name = self._headset_sink()
        if name:
            self.audio.toggle_mute("sink", name)
            GLib.timeout_add(60, lambda: (self._sync_headset_vol_slider(), False)[1])

    def _on_vol_press(self, _scale, _event) -> bool:
        self._vol_interacting = True
        return False

    def _on_vol_release(self, _scale, _event) -> bool:
        self._vol_interacting = False
        return False

    def on_wheel_setting_changed(self, *args) -> None:
        self.settings.data["wheel_enabled"] = self.wheel_sw.get_active()
        for key, control in self._wheel_feel_controls.items():
            self.settings.data[key] = int(control.get_value())
        source = args[0] if args else None
        if source is not self.wheel_sw:
            self.settings.data["wheel_calibrated"] = False
        self.settings.save()
        self._refresh_wheel_calibration_status()

    def _sync_wheel_feel_controls(self) -> None:
        for key, control in self._wheel_feel_controls.items():
            control.handler_block_by_func(self.on_wheel_setting_changed)
            control.set_value(self.settings.data[key])
            control.handler_unblock_by_func(self.on_wheel_setting_changed)

    def on_wheel_defaults(self, *_args) -> None:
        self.settings.data.update({
            "wheel_fine_step": DEFAULT_FINE_STEP,
            "wheel_fine_interval_ms": round(FINE_INTERVAL_S * 1000),
            "wheel_hold_delay_ms": round(HOLD_REPEAT_DELAY_S * 1000),
            "wheel_hold_interval_ms": round(HOLD_REPEAT_INTERVAL_S * 1000),
            "wheel_step": DEFAULT_STEP,
            "wheel_direction_guard_ms": round(DIRECTION_GUARD_S * 1000),
            "wheel_calibrated": False,
        })
        self._sync_wheel_feel_controls()
        self.settings.save()
        self._refresh_wheel_calibration_status()

    def _refresh_wheel_calibration_status(self) -> None:
        if self.settings.data.get("wheel_calibrated"):
            self.wheel_calibration_status.set_text(
                f"Calibrated · fast rate after {self.settings.data['wheel_hold_delay_ms']} ms · "
                f"every {self.settings.data['wheel_hold_interval_ms']} ms · "
                f"up to {self.settings.data['wheel_step']}% per sweep")
        else:
            self.wheel_calibration_status.set_text(
                "Manual comfort settings active. Capture is optional.")

    def on_wheel_calibrate(self, *_args) -> None:
        if self._wheel_wizard is not None and self._wheel_wizard.dialog is not None:
            self._wheel_wizard.dialog.present()
            return
        self._wheel_wizard = WheelCalibrationWizard(self)
        try:
            self._wheel_wizard.run()
        finally:
            self._wheel_wizard = None

    def _on_audio_change(self, kind: str = "devices") -> bool:
        if kind == "levels":
            self._sync_headset_vol_slider()
            self._sync_volume_row_levels()
            return False
        self._headset_sink_cache = None
        snapshot = self._audio_snapshot()
        self.tray.rebuild_menu(snapshot=snapshot)
        self._refresh_device_toggles(snapshot=snapshot)
        self._sync_headset_vol_slider()
        self._refresh_volume_rows(snapshot=snapshot)
        self._schedule_stereo_check()
        return False

    def _schedule_stereo_check(self) -> None:
        if getattr(self, "_stereo_check_id", None):
            return
        self._stereo_check_id = GLib.timeout_add(1500, self._stereo_check_done)

    def _stereo_check_done(self) -> bool:
        self._stereo_check_id = None
        self._poll_stereo_route_once()
        return False

    def _refresh_device_toggles(self, snapshot=None) -> None:
        all_sinks, _ds, all_sources, _dsrc = (
            snapshot if snapshot is not None else self._audio_snapshot()
        )
        box = self.dev_toggle_box
        for c in box.get_children():
            c.destroy()
        for kind_lbl, devs, key in (
                ("Outputs", all_sinks, "hidden_sinks"),
                ("Inputs", all_sources, "hidden_sources")):
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

    def _on_dev_toggle(self, cb, key: str, name: str) -> None:
        hidden = set(self.settings.data[key])
        if cb.get_active():
            hidden.discard(name)
        else:
            hidden.add(name)
        self.settings.data[key] = sorted(hidden)
        self.settings.save()
        snapshot = self._audio_snapshot()
        self.tray.rebuild_menu(snapshot=snapshot)
        self._refresh_volume_rows(snapshot=snapshot)

    def _refresh_volume_rows(self, snapshot=None) -> None:
        if self._vol_pending or self._vol_interacting:
            return
        if snapshot is None:
            snapshot = self._audio_snapshot()
        for box in self._vol_containers:
            build_volume_rows(self, box, snapshot=snapshot)

    def _sync_volume_row_levels(self) -> None:
        if self._vol_interacting or self._vol_pending:
            return
        for box in self._vol_containers:
            for row in box.get_children():
                scale = getattr(row, "_g935_scale", None)
                if scale is None:
                    continue
                raw = self.audio.get_volume(scale._g935_kind, scale._g935_name)
                if raw is not None and int(scale.get_value()) != int(raw):
                    scale.handler_block_by_func(self._on_vol_slider)
                    scale.set_value(raw)
                    scale.handler_unblock_by_func(self._on_vol_slider)
                mbtn = getattr(row, "_g935_mute_btn", None)
                if mbtn is not None:
                    muted = self.audio.get_mute(scale._g935_kind, scale._g935_name)
                    if muted is not None:
                        mbtn.set_label("🔇" if muted else "🔊")

    def _on_vol_slider(self, scale, kind: str, name: str) -> None:
        key = (kind, name)
        if key in self._vol_pending:
            GLib.source_remove(self._vol_pending[key])
        self._vol_pending[key] = GLib.timeout_add(
            120, self._flush_vol, scale, kind, name)

    def _flush_vol(self, scale, kind: str, name: str) -> bool:
        self._vol_pending.pop((kind, name), None)
        self.audio.set_volume(kind, name, scale.get_value())
        return False

    # ---------- Battery health ----------
    def _persist_health_settings(self) -> None:
        if self.worker.via_daemon:
            self.worker.update_health_settings(self.health.settings)
        else:
            self.health.save()

    def _refresh_health_display_cached(self) -> None:
        if self._last_batt_display is not None:
            self._refresh_health_display(*self._last_batt_display, force_ui=True)
        else:
            self._refresh_health_display(force_ui=True)

    def on_track_toggle(self, sw, _pspec) -> None:
        self.health.settings["tracking"] = sw.get_active()
        if not sw.get_active() and not self.worker.via_daemon:
            self.health.close_segment("tracking disabled")
        self._persist_health_settings()

    def on_spec_changed(self, spin, key: str) -> None:
        v = spin.get_value()
        self.health.settings[key] = v if spin.get_digits() else int(v)
        self._persist_health_settings()
        self._refresh_health_display_cached()

    def on_profile_changed(self, _combo) -> None:
        self.health.settings["runtime_profile"] = (
            "rgb_on" if self.profile_combo.get_active() == 0 else "rgb_off")
        self._persist_health_settings()
        self._refresh_health_display_cached()

    def on_spec_reset(self, _btn) -> None:
        for key, sp in self.spec_spins.items():
            sp.handler_block_by_func(self.on_spec_changed)
            sp.set_value(HEALTH_DEFAULTS[key])
            sp.handler_unblock_by_func(self.on_spec_changed)
            self.health.settings[key] = HEALTH_DEFAULTS[key]
        self.health.settings["runtime_profile"] = HEALTH_DEFAULTS["runtime_profile"]
        self.profile_combo.handler_block_by_func(self.on_profile_changed)
        self.profile_combo.set_active(0)
        self.profile_combo.handler_unblock_by_func(self.on_profile_changed)
        self._persist_health_settings()
        self._refresh_health_display_cached()

    def _rated_runtime(self) -> float:
        key = (
            "rated_runtime_h_rgb_on"
            if self.health.settings["runtime_profile"] == "rgb_on"
            else "rated_runtime_h_rgb_off"
        )
        return float(self.health.settings[key])

    def _refresh_health_display(self, mv=None, pct=None, state="",
                                raw_mv=None, path_is_rail=False,
                                force_ui: bool = False) -> None:
        self._last_batt_display = (mv, pct, state, raw_mv, path_is_rail)
        on_health_tab = (
            self.get_visible()
            and self.stack.get_visible_child_name() == "health"
        )
        if not force_ui and not on_health_tab:
            self._health_ui_dirty = True
            return
        self._health_ui_dirty = False
        if self.worker.via_daemon:
            self.health.reload_if_changed()
        render_health_display(
            self, mv=mv, pct=pct, state=state,
            raw_mv=raw_mv, path_is_rail=path_is_rail)

    # ---------- Stereo route ----------
    def _poll_stereo_route(self) -> bool:
        self._poll_stereo_route_once()
        return True

    def _poll_stereo_route_once(self) -> bool:
        if self._stereo_fixing or self._stereo_checking:
            return False
        self._stereo_checking = True

        def _worker():
            try:
                st = inspect_stereo()
            except Exception as e:
                GLib.idle_add(self._on_stereo_check_error, str(e))
                return
            GLib.idle_add(self._on_stereo_check_done, st)

        threading.Thread(target=_worker, daemon=True).start()
        return False

    def _on_stereo_check_error(self, msg: str) -> bool:
        self._stereo_checking = False
        self.log(f"stereo check error: {msg}")
        return False

    def _on_stereo_check_done(self, st) -> bool:
        self._stereo_checking = False
        if not self._stereo_fixing:
            self._apply_stereo_status(st, auto_fix=True)
        return False

    def _apply_stereo_status(self, st, auto_fix: bool = False) -> None:
        was_broken = self._stereo_broken
        broken = bool(st.broken)
        if hasattr(self, "hw_stereo"):
            self.hw_stereo.set_text(st.detail or ("Stereo OK" if st.ok else "Broken"))

        if not st.pw_link_available:
            self.stereo_label.set_markup("")
            self.stereo_label.hide()
            self._stereo_broken = False
            self.stereo_banner.hide()
            return
        if not st.g935_present:
            self.stereo_label.show()
            self.stereo_label.set_markup("<span foreground='gray'>🎧 —</span>")
            set_chip_tone(self.stereo_label, None)
            self.stereo_label.set_tooltip_text(st.detail)
            self._stereo_broken = False
            self.stereo_banner.hide()
            return

        if broken:
            self.stereo_label.show()
            self.stereo_label.set_markup(
                "<span foreground='#f66151'><b>🎧 L-only</b></span>")
            set_chip_tone(self.stereo_label, "bad")
            self.stereo_label.set_tooltip_text(st.detail)
            quiet = time.monotonic() < self._stereo_dismissed_until
            if not quiet:
                self.stereo_banner.show()
            if not was_broken and not quiet:
                notify_user(
                    "G935: right earcup silent",
                    "PipeWire only linked the left channel (often after sleep). Repairing…",
                    urgency="critical",
                )
                self.log(f"STEREO BROKEN: {st.detail}")
            self._stereo_broken = True
            if auto_fix and not quiet:
                GLib.idle_add(self._do_stereo_fix, st)
        else:
            self.stereo_label.show()
            self.stereo_label.set_markup(
                "<span foreground='#2ec27e'>🎧 stereo</span>")
            set_chip_tone(self.stereo_label, "ok")
            tip = st.detail
            if st.fr_port and st.fr_port.endswith(":playback_2"):
                tip += " (right port named playback_2)"
            self.stereo_label.set_tooltip_text(tip)
            if was_broken:
                self.log(f"STEREO OK: {st.detail}")
                notify_user("G935: stereo restored", st.detail)
            self._stereo_broken = False
            self.stereo_banner.hide()
            self._stereo_dismissed_until = 0.0

        if was_broken != self._stereo_broken:
            self.tray.rebuild_menu()

    def _do_stereo_fix(self, st=None) -> bool:
        if self._stereo_fixing:
            return False
        self._stereo_fixing = True

        def _worker():
            try:
                ok, msg = fix_stereo(st)
            except Exception as exc:
                GLib.idle_add(self._on_stereo_fix_error, str(exc))
                return
            GLib.idle_add(self._on_stereo_fix_done, ok, msg)

        threading.Thread(target=_worker, daemon=True).start()
        return False

    def _on_stereo_fix_error(self, err: str) -> bool:
        self._stereo_fixing = False
        self.log(f"stereo fix error: {err}")
        notify_user("G935: stereo fix error", err, urgency="critical")
        return False

    def _on_stereo_fix_done(self, ok: bool, msg: str) -> bool:
        self.log(f"stereo fix: {msg}")

        def _recheck():
            def _bg():
                after = inspect_stereo()
                GLib.idle_add(self._finish_stereo_recheck, ok, msg, after)
            threading.Thread(target=_bg, daemon=True).start()
            return False

        GLib.timeout_add(400, _recheck)
        return False

    def _finish_stereo_recheck(self, ok: bool, msg: str, after) -> bool:
        try:
            healed = after.ok and self._stereo_broken
            if after.ok:
                self._stereo_broken = False
                self.stereo_label.set_markup(
                    "<span foreground='#2ec27e'>🎧 stereo</span>")
                set_chip_tone(self.stereo_label, "ok")
                self.stereo_label.set_tooltip_text(after.detail or msg)
                if hasattr(self, "hw_stereo"):
                    self.hw_stereo.set_text(after.detail or msg)
                self.stereo_banner.hide()
                self._stereo_dismissed_until = 0.0
                self.tray.rebuild_menu()
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

    def _on_stereo_banner_response(self, bar, response_id) -> None:
        if response_id == Gtk.ResponseType.APPLY:
            self._do_stereo_fix()
        elif response_id == Gtk.ResponseType.CLOSE:
            self._stereo_dismissed_until = time.monotonic() + 600
            bar.hide()

    def on_fix_stereo(self, *_) -> None:
        self._stereo_dismissed_until = 0.0
        self._do_stereo_fix()

    # ---------- Heartbeat, daemon events & battery ----------
    def _poll_daemon_status(self) -> bool:
        if not self.worker.via_daemon:
            self.worker.try_upgrade_to_daemon()
        running = self.worker.via_daemon or daemon_running()
        if running != getattr(self, "_daemon_was_running", None):
            self._daemon_was_running = running
            self._update_mode_note()
            self._update_mic_note()
            self._update_mode_chip()
            if hasattr(self, "hw_values") and "transport_link" in self.hw_values:
                self.hw_values["transport_link"].set_text(self.worker.transport_label)
        return True

    def heartbeat(self) -> bool:
        if not self.discovered:
            self._start_discovery()
        elif not self.worker.via_daemon:
            # When connected to g935-dspd, the daemon pushes live battery events.
            self.send("battery", 0, cb=self.got_battery)
        if self.profile["has_boom_mic"]:
            self._refresh_hostmic()
        return True

    def _on_daemon_presence(self, connected: bool) -> None:
        if connected:
            self._mark_connected(reassert_if_was_off=True)
        else:
            self._mark_disconnected()

    def _on_daemon_battery(self, payload: dict) -> None:
        mv = payload.get("mv")
        flags = payload.get("flags", 0)
        state = payload.get("state", "unknown")
        charging = payload.get("charging")
        cell = payload.get("cell_mv")
        pct = payload.get("pct")
        rail = bool(payload.get("path_is_rail", False))
        self._apply_battery_ui(mv, flags, state, charging, cell, pct, rail)
        self._mark_connected(reassert_if_was_off=True)

    def _apply_battery_ui(self, raw, flags, state, charging, cell, pct, rail) -> None:
        icon = "🔌" if charging else ("❓" if charging is None else "🔋")
        self._refresh_health_display(
            cell, pct, state, raw_mv=raw, path_is_rail=rail)
        pct_s = f"{pct}%" if pct is not None else "—%"
        if charging:
            self.batt_label.set_markup(
                f"{icon} <b>{pct_s}</b> <span size='small'>Charging</span>")
            set_chip_tone(self.batt_label, "ok")
            self.batt_label.set_tooltip_text(
                f"SoC from last off-charger rest"
                f"{f' ({cell} mV, {pct}%)' if cell is not None else ' (unknown)'}."
                f" ADC path {raw} mV while charging (flags {flags:#04x}).")
        else:
            self.batt_label.set_markup(f"{icon} <b>{pct_s}</b>")
            tone = (
                "bad" if pct is not None and pct <= 10 else
                "warn" if pct is not None and pct <= 25 else None
            )
            set_chip_tone(self.batt_label, tone)
            self.batt_label.set_tooltip_text(
                f"{cell} mV, {state} (flags {flags:#04x})")
        self.tray.update_battery(f"{icon} {pct_s} · {state}")

    def _mark_connected(self, reassert_if_was_off: bool = True) -> None:
        if self.connected is not True:
            was_off = self.connected is False
            just_discovered = self._just_discovered
            self._just_discovered = False
            self.connected = True
            self.conn_label.set_markup(
                "<span foreground='#2ec27e'>●</span> <b>Connected</b>")
            set_chip_tone(self.conn_label, "ok")
            self.conn_label.set_tooltip_text("Headset online")
            if reassert_if_was_off and was_off and not just_discovered:
                self.log("--- headset powered on: re-applying panel state ---")
                self._assert_device_state(initial=False)

    def got_battery(self, status, reply) -> None:
        if status == "ACK":
            self._battery_presence.observe(True)
            if self.dsp_sw.get_active():
                now = time.monotonic()
                if now - getattr(self, "_last_dsp_heal", 0.0) >= 30.0:
                    self._last_dsp_heal = now
                    self.send("gkeys", 2, "01")
            mv = (reply[4] << 8) | reply[5]
            flags = reply[6]
            state, charging = batt_state(flags)
            if charging is not None:
                self.health.add_sample(time.time(), mv, charging)
                reading = self.health.reading(mv, charging)
            else:
                reading = {
                    "raw_mv": mv, "cell_mv": mv, "pct": batt_percent(mv),
                    "charging": None, "path_is_rail": False,
                }
            self._apply_battery_ui(
                reading["raw_mv"], flags, state, charging,
                reading["cell_mv"], reading["pct"],
                reading.get("path_is_rail", False))
            self._check_battery_alerts(reading["pct"], charging)
            self._mark_connected(reassert_if_was_off=True)
        elif status == "GONE":
            return
        elif self._battery_presence.observe(False) is False:
            self._mark_disconnected()
        else:
            self.log(
                f"--- battery poll missed ({self._battery_presence.misses}/"
                f"{self._battery_presence.miss_limit}); keeping state ---")

    def _check_battery_alerts(self, pct, charging) -> None:
        for alert in self._batt_alerts.update(pct, charging):
            prev_id = self._batt_notify_ids.get(alert.level)
            ok, new_id = notify_user_with_id(
                alert.summary, alert.body, urgency=alert.urgency,
                replace_id=prev_id)
            if ok and new_id:
                self._batt_notify_ids[alert.level] = new_id
            self.log(
                f"BATT ALERT {alert.level}: {alert.body}"
                + ("" if ok else " (notify-send failed)"))

    def _mark_disconnected(self) -> None:
        self._battery_presence.reset()
        if not self.worker.via_daemon:
            self.health.mark_offline()
        self._refresh_health_display(None, None, "headset off")
        self.tray.update_battery("headset off")
        if self.connected is not False:
            self.connected = False
            self.conn_label.set_markup(
                "<span foreground='gray'>●</span> Offline")
            set_chip_tone(self.conn_label, None)
            self.conn_label.set_tooltip_text("Headset off / unreachable")
            self.batt_label.set_markup("🔋 —")
            set_chip_tone(self.batt_label, None)
            self.log("--- headset unreachable (powered off?) ---")


def error_dialog(text: str) -> None:
    dlg = Gtk.MessageDialog(
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.CLOSE,
        text=text,
    )
    dlg.run()
    dlg.destroy()


def acquire_single_instance():
    rundir = runtime_dir()
    os.makedirs(rundir, exist_ok=True)
    fd = os.open(panel_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main() -> None:
    global ALSA_USBID, MIC_SWITCH_NAME
    args = argparse.ArgumentParser(description="G935 Control")
    args.add_argument(
        "--tray", action="store_true",
        help="start hidden in the system tray")
    opts = args.parse_args()
    install_app_css()
    if opts.tray and panel_running():
        return
    if not opts.tray and notify_running_instance():
        return
    lock = acquire_single_instance()
    if lock is None:
        if opts.tray or notify_running_instance():
            return
        error_dialog("Another instance is already running (check the tray).")
        return
    try:
        ipc_sock = bind_listener()
    except OSError:
        ipc_sock = None

    found = find_headset(known_pids=set(DEVICE_PROFILES))
    if not found:
        path, pid, name = None, 0x0A87, "G935"
    else:
        path, pid, name = found

    if found and pid not in DEVICE_PROFILES:
        dlg = Gtk.MessageDialog(
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=(
                f'Found "{name}" (PID {pid:04x}) — not a known headset.\n'
                "This tool is only tested on the Logitech G935. Probe it anyway?"
            ),
        )
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.YES:
            return

    prof = {**GENERIC_PROFILE, **DEVICE_PROFILES.get(pid, {"name": name})}
    if prof["alsa_usbid"]:
        ALSA_USBID, MIC_SWITCH_NAME = prof["alsa_usbid"], prof["mic_switch_name"]

    # Skip direct hidraw permission check when daemon owns hardware or --tray.
    if path is not None and not daemon_running():
        try:
            probe = open_hidraw(path)
            os.close(probe)
        except PermissionError:
            if not opts.tray:
                error_dialog(
                    f"No permission to open {path}.\n\n"
                    "Install the udev rule from this repo:\n"
                    "  ./install.sh --udev\n"
                    "then unplug/replug the receiver.")
                return
        except OSError as e:
            if not opts.tray:
                error_dialog(f"Could not open {path}:\n{e}")
                return

    win = App(path, pid, name, start_in_tray=opts.tray)
    win.connect("destroy", win.on_quit)
    win.listen_for_activation(ipc_sock)
    win.show_all()
    if opts.tray and (win.tray.indicator or win.tray.waiting):
        win.hide()
    else:
        win.present()
    Gtk.main()


if __name__ == "__main__":
    main()
