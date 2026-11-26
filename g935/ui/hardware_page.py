"""Hardware page: 2-column live summary at top, features & console below."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from g935.ui.theme import make_card, make_info_row, style


def build_hardware_page(app, container):
    """Build a dense Hardware page with Identity + Stereo/Caps side-by-side."""
    top_pair = Gtk.Box(spacing=10)
    top_pair.set_homogeneous(True)

    # ---- Left: Headset identity ----
    _ident_card, ident = make_card(top_pair, "Headset identity")
    app.hw_values = {}
    for key, title in (
            ("transport_link", "Control link"),
            ("name", "Name"),
            ("firmware", "Firmware"),
            ("usb", "USB ID"),
            ("unit", "Unit ID"),
            ("model", "Model ID"),
            ("type", "Type"),
            ("transport", "Transports"),
            ("entities", "Entities")):
        app.hw_values[key] = make_info_row(ident, title)
    app.hw_values["usb"].set_text(f"046D:{app.pid:04X}")
    app.hw_values["transport_link"].set_text(app.worker.transport_label)

    # ---- Right: Stereo route + Live capabilities ----
    right_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

    stereo_actions = Gtk.Box(spacing=6)
    recheck_btn = Gtk.Button(label="Recheck")
    recheck_btn.connect("clicked", lambda *_: app._poll_stereo_route_once())
    stereo_actions.pack_start(recheck_btn, False, False, 0)
    fix_btn = Gtk.Button(label="Fix")
    fix_btn.connect("clicked", app.on_fix_stereo)
    stereo_actions.pack_start(fix_btn, False, False, 0)

    _stereo_card, stereo_body = make_card(
        right_col, "PipeWire stereo", header_widget=stereo_actions)
    app.hw_stereo = Gtk.Label(label="Checking…", xalign=0)
    app.hw_stereo.set_selectable(True)
    app.hw_stereo.set_line_wrap(True)
    stereo_body.pack_start(app.hw_stereo, False, False, 0)

    _live_card, live = make_card(right_col, "Live capabilities")
    app.hw_caps = Gtk.Label(xalign=0)
    app.hw_caps.set_selectable(True)
    app.hw_caps.set_line_wrap(True)
    app.hw_caps.set_text("Probing…")
    live.pack_start(app.hw_caps, False, False, 0)

    top_pair.pack_start(right_col, True, True, 0)
    container.pack_start(top_pair, False, False, 0)

    # ---- Advertised HID++ features (compact collapsed or clean card) ----
    _caps_card, caps = make_card(container, "Advertised HID++ features")
    app.hw_features = Gtk.Label(xalign=0)
    app.hw_features.set_selectable(True)
    app.hw_features.set_line_wrap(True)
    app.hw_features.set_text("Discovering…")
    caps.pack_start(app.hw_features, False, False, 0)

    # ---- Collapsed Advanced HID++ console ----
    app.console_expander = Gtk.Expander()
    style(app.console_expander, "g935-expander")
    app.console_expander.set_expanded(False)
    console_title = Gtk.Box(spacing=8)
    title = Gtk.Label()
    title.set_markup("<b>Advanced HID++ console</b>")
    console_title.pack_start(title, False, False, 0)
    subtitle = Gtk.Label(label="raw traffic & developer commands")
    style(subtitle, "g935-subtitle")
    console_title.pack_start(subtitle, False, False, 0)
    app.console_expander.set_label_widget(console_title)

    console = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    console.set_margin_top(8)
    app.console_expander.add(console)
    container.pack_start(app.console_expander, False, False, 0)

    if app.profile["connect_burst"]:
        row = Gtk.Box(spacing=8)
        b = Gtk.Button(label="Apply G HUB connect defaults (full sequence)")
        b.connect("clicked", app.on_full_sequence)
        row.pack_start(b, True, True, 0)
        console.pack_start(row, False, False, 0)

    row = Gtk.Box(spacing=8)
    app.raw_entry = Gtk.Entry()
    app.raw_entry.set_placeholder_text("raw HID++ hex, e.g. 11ff062b")
    app.raw_entry.connect("activate", app.on_raw_send)
    row.pack_start(app.raw_entry, True, True, 0)
    send_btn = Gtk.Button(label="Send")
    send_btn.connect("clicked", app.on_raw_send)
    row.pack_start(send_btn, False, False, 0)
    console.pack_start(row, False, False, 0)

    app.logbuf = Gtk.TextBuffer()
    view = Gtk.TextView(buffer=app.logbuf, editable=False, monospace=True)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_size_request(-1, 260)
    scroll.add(view)
    console.pack_start(scroll, False, False, 0)
    app.logview = view
