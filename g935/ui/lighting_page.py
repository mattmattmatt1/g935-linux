"""Lighting page: live animated G935 RGB earcup preview + 2-column zone controls."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk

from g935.features import LIGHT_MODES
from g935.ui.theme import make_card, style
from g935.ui.visuals import HeadsetLightingPreview


def build_lighting_page(app, container):
    hdr_actions = Gtk.Box(spacing=6)
    app.light_read_active = Gtk.Button(label="Read active")
    app.light_read_active.set_tooltip_text("Read playing effects from RAM")
    app.light_read_active.connect("clicked", lambda *_: app._read_lighting(0))
    app.light_read_active.set_sensitive(False)
    hdr_actions.pack_start(app.light_read_active, False, False, 0)

    app.light_read_saved = Gtk.Button(label="Load saved")
    app.light_read_saved.set_tooltip_text("Load stored effects from EEPROM")
    app.light_read_saved.connect("clicked", lambda *_: app._read_lighting(1))
    app.light_read_saved.set_sensitive(False)
    hdr_actions.pack_start(app.light_read_saved, False, False, 0)

    apply_all = Gtk.Button(label="Apply all")
    apply_all.get_style_context().add_class("suggested-action")
    apply_all.connect("clicked", app.on_light_apply_all)
    hdr_actions.pack_start(apply_all, False, False, 0)

    _card, body = make_card(
        container,
        "On-headset lighting",
        subtitle="Live animated earcup preview — Apply saves to headset EEPROM",
        header_widget=hdr_actions,
    )
    app.light_preview = HeadsetLightingPreview()
    body.pack_start(app.light_preview, False, False, 2)

    app.light_box = body
    app.zone_widgets = {}
    if app.profile["zones"]:
        build_lighting_rows(app, app.profile["zones"])


def sync_lighting_preview(app, zone: int | None = None) -> None:
    preview = getattr(app, "light_preview", None)
    if preview is None:
        return
    zones = [zone] if zone is not None else list(app.zone_widgets)
    for z in zones:
        widgets = app.zone_widgets.get(z)
        if not widgets:
            continue
        rgba = widgets["color"].get_rgba()
        rgb = (
            round(rgba.red * 255),
            round(rgba.green * 255),
            round(rgba.blue * 255),
        )
        preview.set_zone_state(
            z,
            widgets["mode"].get_active(),
            rgb,
            int(widgets["period"].get_value()),
            int(widgets["intensity"].get_value()),
            widgets["waveform"].get_active(),
        )


def build_lighting_rows(app, zone_names) -> None:
    saved_all = app.settings.data["lighting"]
    zones_row = Gtk.Box(spacing=10)
    zones_row.set_homogeneous(True)

    for zone, zname in enumerate(zone_names):
        saved = saved_all.get(str(zone), {})
        zone_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        style(zone_box, "g935-zone")

        top = Gtk.Box(spacing=8)
        name = Gtk.Label(label=zname, xalign=0)
        style(name, "g935-primary-title")
        top.pack_start(name, True, True, 0)

        color = Gtk.ColorButton()
        color.set_title(f"{zname} color")
        rgba = Gdk.RGBA()
        rgba.parse(saved.get("color", "#00B4FF"))
        color.set_rgba(rgba)
        top.pack_start(color, False, False, 0)

        b = Gtk.Button(label="Apply")
        b.connect("clicked", app.on_light_apply, zone)
        top.pack_end(b, False, False, 0)
        zone_box.pack_start(top, False, False, 0)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.set_hexpand(True)

        m_lbl = Gtk.Label(label="Effect", xalign=0)
        style(m_lbl, "g935-caption")
        grid.attach(m_lbl, 0, 0, 1, 1)
        combo = Gtk.ComboBoxText()
        combo.set_hexpand(True)
        for e in LIGHT_MODES:
            combo.append_text(e)
        combo.set_active(saved.get("mode", 2))
        grid.attach(combo, 1, 0, 1, 1)

        p_lbl = Gtk.Label(label="Period (ms)", xalign=0)
        style(p_lbl, "g935-caption")
        grid.attach(p_lbl, 0, 1, 1, 1)
        period = Gtk.SpinButton.new_with_range(500, 20000, 100)
        period.set_value(saved.get("period", 5000))
        grid.attach(period, 1, 1, 1, 1)

        i_lbl = Gtk.Label(label="Intensity (%)", xalign=0)
        style(i_lbl, "g935-caption")
        grid.attach(i_lbl, 0, 2, 1, 1)
        intensity = Gtk.SpinButton.new_with_range(0, 100, 1)
        intensity.set_value(saved.get(
            "intensity", app.profile["zone_brt"].get(zone, 100)))
        grid.attach(intensity, 1, 2, 1, 1)

        w_lbl = Gtk.Label(label="Waveform", xalign=0)
        style(w_lbl, "g935-caption")
        grid.attach(w_lbl, 0, 3, 1, 1)
        waveform = Gtk.ComboBoxText()
        waveform.set_hexpand(True)
        for text in ("Default", "Sine", "Square", "Triangle",
                     "Sawtooth", "Shark fin", "Exponential"):
            waveform.append_text(text)
        waveform.set_active(saved.get("waveform", 0))
        grid.attach(waveform, 1, 3, 1, 1)

        r_lbl = Gtk.Label(label="Fixed ramp", xalign=0)
        style(r_lbl, "g935-caption")
        grid.attach(r_lbl, 0, 4, 1, 1)
        ramp = Gtk.ComboBoxText()
        ramp.set_hexpand(True)
        for text in ("Default ramp", "Ramp", "No ramp"):
            ramp.append_text(text)
        ramp.set_active(saved.get("ramp", 2))
        grid.attach(ramp, 1, 4, 1, 1)

        zone_box.pack_start(grid, False, False, 0)
        zones_row.pack_start(zone_box, True, True, 0)

        app.zone_widgets[zone] = {
            "name": name, "mode": combo, "color": color,
            "period": period, "intensity": intensity,
            "waveform": waveform, "ramp": ramp,
        }
        combo.connect("changed", app._update_light_fields, zone)
        color.connect("color-set", lambda _w, z=zone: sync_lighting_preview(app, z))
        period.connect("value-changed", lambda _w, z=zone: sync_lighting_preview(app, z))
        intensity.connect("value-changed", lambda _w, z=zone: sync_lighting_preview(app, z))
        waveform.connect("changed", lambda _w, z=zone: sync_lighting_preview(app, z))
        app._update_light_fields(combo, zone)

    app.light_box.pack_start(zones_row, False, False, 0)

    app.light_boot_row = Gtk.Box(spacing=8)
    app.light_boot_row.pack_start(
        Gtk.Label(label="Headset boot-up animation", xalign=0),
        True, True, 0)
    app.light_boot_switch = Gtk.Switch()
    app.light_boot_switch.set_active(True)
    app.light_boot_switch.connect("notify::active", app.on_light_boot_toggle)
    app.light_boot_row.pack_end(app.light_boot_switch, False, False, 0)
    app.light_boot_row.set_no_show_all(True)
    app.light_boot_row.hide()
    app.light_box.pack_start(app.light_boot_row, False, False, 2)
    app.light_box.show_all()
    sync_lighting_preview(app)
