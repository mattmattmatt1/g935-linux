"""System tray (Ayatana AppIndicator) and pop-out Mixer window."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from g935.app_icon import APP_ID, icon_theme_dir
from g935.tray_scroll import tray_scroll_notches
from g935.ui.settings_page import build_volume_rows
from g935.ui.theme import apply_window_icon, sni_icon_name
from g935.volume_wheel import VOLUME_BOOST_MAX

AppIndicator = None
for _ns in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(_ns, "0.1")
        AppIndicator = getattr(__import__("gi.repository", fromlist=[_ns]), _ns)
        break
    except (ValueError, ImportError, AttributeError):
        continue

TRAY_SNI_RETRY_S = 1
TRAY_SNI_RETRY_MAX = 20


def sni_watcher_present() -> bool:
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


class TrayController:
    """Manages the Ayatana StatusNotifierItem, tray menu, scroll, and Mixer."""

    def __init__(self, app):
        self.app = app
        self.indicator = None
        self.tray_batt_item = None
        self.mixer_win = None
        self.mixer_box = None
        self.waiting = False
        self._sni_tries = 0
        self._status = None
        self._scroll_acc = 0
        self._menu_sig = None

    def setup(self) -> None:
        if self.indicator:
            return
        if AppIndicator is None:
            self.waiting = False
            self.app.log("--- tray skipped: Ayatana AppIndicator not installed ---")
            return
        if not sni_watcher_present():
            self._sni_tries += 1
            if self._sni_tries <= TRAY_SNI_RETRY_MAX:
                self.waiting = True
                if self._sni_tries == 1:
                    self.app.log("--- tray host not ready, waiting ---")
                GLib.timeout_add_seconds(TRAY_SNI_RETRY_S, self._retry)
                return
            self.waiting = False
            self.app.log("--- tray host still missing; registering anyway ---")
        self.waiting = False
        icon = sni_icon_name()
        category = AppIndicator.IndicatorCategory.APPLICATION_STATUS
        self.indicator = AppIndicator.Indicator.new(APP_ID, icon, category)
        theme_dir = icon_theme_dir()
        if theme_dir:
            self.indicator.set_icon_theme_path(theme_dir)
        try:
            self.indicator.set_icon_full(icon, "G935 Control")
        except Exception:
            self.indicator.set_icon(icon)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title(f"{self.app.profile['name']} Control")
        try:
            self.indicator.connect("scroll-event", self.on_scroll)
        except TypeError:
            pass
        self.rebuild_menu()
        self.app.log(f"--- tray active ({icon}) ---")

    def _retry(self) -> bool:
        self.setup()
        return False

    def on_scroll(self, _ind, delta, direction) -> None:
        if direction == Gdk.ScrollDirection.DOWN:
            way = "down"
        elif direction == Gdk.ScrollDirection.UP:
            way = "up"
        else:
            way = "other"
        notches, self._scroll_acc = tray_scroll_notches(
            way, delta, self._scroll_acc)
        if notches == 0:
            return
        name = self.app._headset_sink()
        if not name:
            return
        cur = self.app.audio.get_volume("sink", name)
        if cur is None:
            return
        new = max(0, min(VOLUME_BOOST_MAX, cur + notches * 5))
        if new != cur:
            self.app.audio.set_volume("sink", name, new)

    def _header_label(self) -> str:
        name = self.app.profile["name"]
        return f"{name} — {self._status}" if self._status else name

    def update_battery(self, text: str) -> None:
        self._status = text
        if self.indicator:
            self.indicator.set_title(f"{self.app.profile['name']} · {text}")
        if self.tray_batt_item:
            self.tray_batt_item.set_label(self._header_label())

    def _menu_signature(self, snapshot):
        all_sinks, def_sink, all_sources, def_source = snapshot
        hidden_sinks = set(self.app.settings.data["hidden_sinks"])
        hidden_sources = set(self.app.settings.data["hidden_sources"])
        sinks = tuple(
            (name, desc) for name, desc in all_sinks
            if name not in hidden_sinks)
        sources = tuple(
            (name, desc) for name, desc in all_sources
            if name not in hidden_sources)
        return (
            sinks, def_sink,
            sources, def_source,
            bool(getattr(self.app, "_stereo_broken", False)),
        )

    def rebuild_menu(self, snapshot=None) -> None:
        if not self.indicator:
            return
        if snapshot is None:
            snapshot = self.app._audio_snapshot()
        signature = self._menu_signature(snapshot)
        if signature == self._menu_sig and self.tray_batt_item:
            self.tray_batt_item.set_label(self._header_label())
            return
        self._menu_sig = signature
        menu = Gtk.Menu()

        self.tray_batt_item = Gtk.MenuItem(label=self._header_label())
        self.tray_batt_item.set_sensitive(False)
        menu.append(self.tray_batt_item)
        menu.append(Gtk.SeparatorMenuItem())

        all_sinks, def_sink, all_sources, def_source = snapshot
        hidden_sinks = set(self.app.settings.data["hidden_sinks"])
        hidden_sources = set(self.app.settings.data["hidden_sources"])
        for kind, devs, hidden, default, setter in (
                ("Output", all_sinks, hidden_sinks,
                 def_sink, self.app.audio.set_default_sink),
                ("Input", all_sources, hidden_sources,
                 def_source, self.app.audio.set_default_source)):
            hdr = Gtk.MenuItem(label=kind)
            hdr.set_sensitive(False)
            menu.append(hdr)
            visible = [(n, d) for n, d in devs if n not in hidden]
            group = None
            if not any(n == default for n, _d in visible):
                group = Gtk.RadioMenuItem.new_with_label_from_widget(None, "")
                group.set_active(True)
            for name, desc in visible:
                item = Gtk.RadioMenuItem.new_with_label_from_widget(group, desc)
                group = group or item
                item.set_active(name == default)
                item.connect(
                    "activate",
                    lambda it, n=name, f=setter: it.get_active() and f(n))
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())

        it = Gtk.MenuItem(label="Mixer…")
        it.connect("activate", lambda *_: self.show_mixer())
        menu.append(it)
        if getattr(self.app, "_stereo_broken", False):
            it = Gtk.MenuItem(label="Fix stereo (right earcup)")
            it.connect("activate", self.app.on_fix_stereo)
            menu.append(it)
        it = Gtk.MenuItem(label="Show/Hide Control Panel")
        it.connect(
            "activate",
            lambda *_: (
                self.app.hide() if self.app.get_visible() else self.app.present()
            ))
        menu.append(it)
        menu.append(Gtk.SeparatorMenuItem())
        it = Gtk.MenuItem(label="Quit")
        it.connect("activate", self.app.on_quit)
        menu.append(it)

        menu.show_all()
        self.indicator.set_menu(menu)

    def show_mixer(self) -> None:
        if self.mixer_win is None:
            self.mixer_win = Gtk.Window(title="Mixer")
            self.mixer_win.set_default_size(500, -1)
            self.mixer_win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
            self.mixer_win.set_position(Gtk.WindowPosition.CENTER)
            apply_window_icon(self.mixer_win)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_border_width(14)
            self.mixer_win.add(box)
            self.mixer_box = box
            self.app._vol_containers.append(box)
            self.mixer_win.connect(
                "delete-event", lambda w, _e: (w.hide(), True)[1])
        build_volume_rows(self.app, self.mixer_box)
        self.mixer_win.show_all()
        self.mixer_win.present()
