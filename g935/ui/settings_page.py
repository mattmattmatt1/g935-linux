"""Settings page: Headset volume + Mixer at top, compact 2-col Wheel feel below."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

from g935.ui.theme import (
    guard_slider_scroll, make_card, set_badge_tone, style,
)
from g935.volume_wheel import VOLUME_BOOST_MAX


def build_settings_page(app, container):
    """Build a condensed Settings page: Volume → Mixer → Compact Wheel → Visibility."""
    # ---- 1. Headset volume (0–200% hero card) ----
    vol_header = Gtk.Box(spacing=8)
    app.headset_vol_value = Gtk.Label(label="—")
    style(app.headset_vol_value, "g935-badge", "g935-badge-accent")
    vol_header.pack_start(app.headset_vol_value, False, False, 0)

    app.headset_mute_btn = Gtk.Button(label="Mute")
    app.headset_mute_btn.set_tooltip_text("Toggle mute on the G935 output sink")
    app.headset_mute_btn.connect("clicked", app.on_headset_mute_toggle)
    vol_header.pack_start(app.headset_mute_btn, False, False, 0)

    _vol_card, vol_body = make_card(
        container,
        "Headset volume",
        subtitle="0–100% USB hardware mixer · 101–200% PipeWire software gain",
        header_widget=vol_header,
    )
    app.headset_vol = Gtk.Scale.new_with_range(
        Gtk.Orientation.HORIZONTAL, 0, VOLUME_BOOST_MAX, 1)
    app.headset_vol.set_digits(0)
    app.headset_vol.set_draw_value(False)
    app.headset_vol.set_hexpand(True)
    app.headset_vol.add_mark(100, Gtk.PositionType.BOTTOM, "100% HW")
    app.headset_vol.add_mark(150, Gtk.PositionType.BOTTOM, "150%")
    app.headset_vol.add_mark(200, Gtk.PositionType.BOTTOM, "200%")
    guard_slider_scroll(app.headset_vol)
    app.headset_vol.connect("button-press-event", app._on_vol_press)
    app.headset_vol.connect_after("button-release-event", app._on_vol_release)
    app.headset_vol.connect("value-changed", app.on_headset_vol_changed)
    vol_body.pack_start(app.headset_vol, False, False, 0)
    app._sync_headset_vol_slider()

    # ---- 2. Desktop audio mixer (primary daily controls right below headset vol) ----
    _mixer_card, mixer_body = make_card(
        container,
        "Desktop audio mixer",
        subtitle="Default output/input selection and per-device levels",
    )
    app.settings_vol_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    mixer_body.pack_start(app.settings_vol_box, False, False, 0)
    app._vol_containers.append(app.settings_vol_box)

    # ---- 3. Earcup volume wheel (compact 2-column 3x2 grid) ----
    wheel_hdr = Gtk.Box(spacing=8)
    reset_feel = Gtk.Button(label="Defaults")
    reset_feel.set_tooltip_text("Reset balanced wheel feel defaults")
    reset_feel.connect("clicked", app.on_wheel_defaults)
    wheel_hdr.pack_start(reset_feel, False, False, 0)
    app.wheel_sw = Gtk.Switch()
    app.wheel_sw.set_valign(Gtk.Align.CENTER)
    app.wheel_sw.set_active(app.settings.data["wheel_enabled"])
    app.wheel_sw.connect("notify::active", app.on_wheel_setting_changed)
    wheel_hdr.pack_start(app.wheel_sw, False, False, 0)

    _wheel_card, wheel_body = make_card(
        container,
        "Earcup volume wheel",
        subtitle="Duration-decoded smooth ramps with USB mixer pacing & reversal guard",
        header_widget=wheel_hdr,
    )

    feel_grid = Gtk.Grid(column_spacing=18, row_spacing=8)
    feel_grid.set_hexpand(True)
    app._wheel_feel_controls = {}

    feel_specs = (
        ("wheel_fine_step", "wheel_fine_step", "Fine step", "Immediate change per tiny roll", 1, 5, 1, "%"),
        ("wheel_fine_interval", "wheel_fine_interval_ms", "Fine cadence", "Slow-motion step interval", 40, 150, 1, "ms"),
        ("wheel_hold_delay", "wheel_hold_delay_ms", "Accel point", "Fine-to-fast hold threshold", 120, 600, 5, "ms"),
        ("wheel_hold_interval", "wheel_hold_interval_ms", "Fast cadence", "Delay between fast-sweep steps", 25, 60, 1, "ms"),
        ("wheel_step", "wheel_step", "Sweep limit", "Max change per continuous sweep", 10, 50, 1, "%"),
        ("wheel_direction_guard", "wheel_direction_guard_ms", "Reversal guard", "0 ms = immediate reversal", 0, 1000, 10, "ms"),
    )
    for idx, (attr, key, title, tip, lo, hi, step, suffix) in enumerate(feel_specs):
        r, c = idx // 2, (idx % 2) * 2
        lbl = Gtk.Label(label=title, xalign=0)
        lbl.set_tooltip_text(tip)
        lbl.set_hexpand(True)
        feel_grid.attach(lbl, c, r, 1, 1)

        sbox = Gtk.Box(spacing=4)
        spin = Gtk.SpinButton.new_with_range(lo, hi, step)
        spin.set_value(app.settings.data[key])
        spin.set_tooltip_text(tip)
        spin.connect("value-changed", app.on_wheel_setting_changed)
        sbox.pack_start(spin, False, False, 0)
        suf = Gtk.Label(label=suffix, xalign=0)
        suf.set_size_request(22, -1)
        style(suf, "g935-caption")
        sbox.pack_start(suf, False, False, 0)
        feel_grid.attach(sbox, c + 1, r, 1, 1)
        setattr(app, attr, spin)
        app._wheel_feel_controls[key] = spin

    wheel_body.pack_start(feel_grid, False, False, 0)

    diagnostics = Gtk.Expander(label="Diagnostics & auto-fit")
    diagnostics_box = Gtk.Box(spacing=8)
    diagnostics_box.set_margin_top(6)
    app.wheel_calibration_status = Gtk.Label(xalign=0)
    style(app.wheel_calibration_status, "g935-caption")
    app.wheel_calibration_status.set_line_wrap(True)
    diagnostics_box.pack_start(app.wheel_calibration_status, True, True, 0)
    calibrate = Gtk.Button(label="Capture & auto-fit…")
    calibrate.connect("clicked", app.on_wheel_calibrate)
    diagnostics_box.pack_end(calibrate, False, False, 0)
    diagnostics.add(diagnostics_box)
    wheel_body.pack_start(diagnostics, False, False, 0)
    app._refresh_wheel_calibration_status()

    # ---- 4. Device visibility (collapsed cleanly) ----
    vis_exp = Gtk.Expander(label="Show or hide devices in tray & mixer")
    style(vis_exp, "g935-expander")
    vis_exp.set_expanded(False)
    vis_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    vis_box.set_margin_top(8)
    app.dev_toggle_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    vis_box.pack_start(app.dev_toggle_box, False, False, 0)
    vnote = Gtk.Label(xalign=0)
    style(vnote, "g935-caption")
    vnote.set_line_wrap(True)
    vnote.set_markup(
        "<small>Unchecked devices are hidden from the tray menu and mixer. "
        "easyeffects_* and monitor sources are always hidden.</small>")
    vis_box.pack_start(vnote, False, False, 2)
    vis_exp.add(vis_box)
    container.pack_start(vis_exp, False, False, 0)


def update_headset_vol_badge(app, pct: int | None, muted: bool | None = None) -> None:
    if pct is None:
        app.headset_vol_value.set_text("—")
        set_badge_tone(app.headset_vol_value, None)
    elif muted:
        app.headset_vol_value.set_text(f"{int(pct)}% · Muted")
        set_badge_tone(app.headset_vol_value, "bad")
    elif int(pct) > 100:
        app.headset_vol_value.set_text(f"{int(pct)}% · Boost")
        set_badge_tone(app.headset_vol_value, "boost")
    else:
        app.headset_vol_value.set_text(f"{int(pct)}%")
        set_badge_tone(app.headset_vol_value, "accent")

    mute_btn = getattr(app, "headset_mute_btn", None)
    if mute_btn is not None:
        mute_btn.set_label("Unmute" if muted else "Mute")


def build_volume_rows(app, container, snapshot=None) -> None:
    """Populate grouped Output / Input rows with radio defaults & mute buttons."""
    all_sinks, def_sink, all_sources, def_source = (
        snapshot if snapshot is not None else app._audio_snapshot()
    )
    for c in container.get_children():
        c.destroy()
    for section_title, kind, devs, hidden, default, setter in (
            ("Outputs", "sink", all_sinks,
             set(app.settings.data["hidden_sinks"]),
             def_sink, app.audio.set_default_sink),
            ("Inputs", "source", all_sources,
             set(app.settings.data["hidden_sources"]),
             def_source, app.audio.set_default_source)):
        visible = [(name, desc) for name, desc in devs if name not in hidden]
        if not visible:
            continue
        hdr = Gtk.Label(label=section_title, xalign=0)
        style(hdr, "g935-caption")
        hdr.set_margin_top(2)
        container.pack_start(hdr, False, False, 0)

        group = None
        if not any(name == default for name, _desc in visible):
            group = Gtk.RadioButton.new_from_widget(None)
            group.set_active(True)
        for name, desc in visible:
            row = Gtk.Box(spacing=8)
            rb = Gtk.RadioButton.new_from_widget(group)
            group = group or rb
            rb.set_active(name == default)
            rb.set_tooltip_text("Make default")
            rb.connect(
                "toggled",
                lambda b, n=name, f=setter: b.get_active() and f(n))
            row.pack_start(rb, False, False, 0)

            lbl = Gtk.Label(label=desc, xalign=0)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_size_request(180, -1)
            lbl.set_tooltip_text(desc)
            row.pack_start(lbl, True, True, 0)

            sc = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0, VOLUME_BOOST_MAX, 1)
            sc.set_digits(0)
            sc.set_size_request(210, -1)
            sc.set_value(app.audio.get_volume(kind, name) or 0)
            sc.set_value_pos(Gtk.PositionType.RIGHT)
            sc.add_mark(100, Gtk.PositionType.TOP, "100%")
            guard_slider_scroll(sc)
            sc.set_tooltip_text(
                "0–100% hardware mixer · 101–200% PipeWire software boost.")
            sc.connect("button-press-event", app._on_vol_press)
            sc.connect_after("button-release-event", app._on_vol_release)
            sc.connect("value-changed", app._on_vol_slider, kind, name)
            sc._g935_kind = kind
            sc._g935_name = name
            row._g935_scale = sc
            row.pack_start(sc, True, True, 0)

            muted = bool(app.audio.get_mute(kind, name))
            mbtn = Gtk.Button(label="🔇" if muted else "🔊")
            mbtn.set_tooltip_text("Toggle mute")
            mbtn.connect(
                "clicked",
                lambda _b, k=kind, n=name: app.audio.toggle_mute(k, n))
            row._g935_mute_btn = mbtn
            row.pack_start(mbtn, False, False, 0)

            container.pack_start(row, False, False, 0)
    container.show_all()
