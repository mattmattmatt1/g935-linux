"""Control page: EQ Hero + Live Curve at top, Sidetone, Mode + Mic row, Bindings."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from g935.bindings import (
    ACTIONS, KEYS, KEY_LABELS, LAYERS, LAYER_LABELS,
)
from g935.mode import load_mode
from g935.ui.theme import (
    guard_slider_scroll, make_card, set_badge_tone, style,
)
from g935.ui.visuals import EqCurveWidget

EQ_PRESET_CATEGORIES = (
    ("Reference", ("Flat", "G HUB baseline")),
    ("Tone", ("Bass boost", "Deep bass", "Warm", "V-shape",
              "Treble boost", "Late night")),
    ("Voice & games", ("Vocal clarity", "Podcast", "FPS footsteps",
                       "Cinematic", "Immersive")),
    ("Music", ("Rock", "Electronic", "Classical", "Jazz", "Acoustic")),
)


def build_control_page(app, container, read_host_mic_fn, set_host_mic_fn):
    """Build Control page: EQ + live response curve first, then Sidetone, Mode+Mic, Bindings."""
    # ---- 1. Hardware EQ Hero at the very top ----
    eq_header_actions = Gtk.Box(spacing=6)
    app.eq_flat_btn = Gtk.Button(label="Flat")
    app.eq_flat_btn.set_tooltip_text("Reset all 10 bands to 0 dB")
    app.eq_flat_btn.connect("clicked", lambda *_: app.on_eq_preset(None, "Flat"))
    eq_header_actions.pack_start(app.eq_flat_btn, False, False, 0)

    app.eq_preset_button = Gtk.MenuButton()
    app.eq_preset_button.set_label("Presets")
    app.eq_preset_button.set_size_request(155, -1)
    eq_header_actions.pack_start(app.eq_preset_button, False, False, 0)

    eq_card, eq = make_card(
        container,
        "Hardware EQ",
        subtitle="10-band on-device equalizer (±12 dB) with live curve",
        header_widget=eq_header_actions,
    )
    app.section_frames["eq"] = eq_card
    app.eq_box = eq
    app.eq_sliders = []
    app.eq_curve = EqCurveWidget(app.profile["eq_bands"])
    eq.pack_start(app.eq_curve, False, False, 0)
    if app.profile["eq_bands"]:
        build_eq_sliders(app, app.profile["eq_bands"])

    # ---- 2. Compact single-row Sidetone bar ----
    st_card = Gtk.Box(spacing=12)
    style(st_card, "g935-card")
    st_title = Gtk.Label(label="Sidetone", xalign=0)
    style(st_title, "g935-card-title")
    st_card.pack_start(st_title, False, False, 0)
    app.sidetone = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
    app.sidetone.set_draw_value(False)
    app.sidetone.set_hexpand(True)
    app.sidetone.set_value(100)
    guard_slider_scroll(app.sidetone)
    app._sidetone_pending = None
    app.sidetone.connect("value-changed", app.on_sidetone)
    st_card.pack_start(app.sidetone, True, True, 0)
    app.sidetone_badge = Gtk.Label(label="100%")
    app.sidetone_badge.set_size_request(52, -1)
    style(app.sidetone_badge, "g935-badge", "g935-badge-accent")
    st_card.pack_end(app.sidetone_badge, False, False, 0)
    container.pack_start(st_card, False, False, 0)
    app.section_frames["sidetone"] = st_card

    # ---- 3. 2-column row: Software mode (left) + Microphone (right) ----
    mid_pair = Gtk.Box(spacing=10)
    mid_pair.set_homogeneous(True)

    app.dsp_sw = Gtk.Switch()
    app.dsp_sw.set_active(load_mode() == "ghub")
    app.dsp_sw.set_tooltip_text(
        "Software mode enables the on-headset DSP soundstage and diverts G0–G3 + mic.")
    app.dsp_sw.connect("notify::active", app.on_dsp_toggle)

    gk_card, gk = make_card(
        mid_pair,
        "Software mode",
        subtitle="On-headset DSP soundstage + host controls",
        header_widget=app.dsp_sw,
    )
    app.mode_note = Gtk.Label(xalign=0)
    style(app.mode_note, "g935-caption")
    app.mode_note.set_line_wrap(True)
    gk.pack_start(app.mode_note, False, False, 0)

    key_row = Gtk.Box(spacing=6)
    app.gkey_labels = {}
    for name in KEYS:
        key = Gtk.Label(label=KEY_LABELS[name])
        style(key, "g935-key")
        key_row.pack_start(key, False, False, 0)
        app.gkey_labels[name] = key
    app.gkey_last = Gtk.Label(label="ready", xalign=0)
    style(app.gkey_last, "g935-caption")
    key_row.pack_start(app.gkey_last, True, True, 4)
    gk.pack_start(key_row, False, False, 2)
    app.section_frames["gkeys"] = gk_card
    app._update_mode_note()

    if app.profile["has_boom_mic"]:
        app.mic_badge = Gtk.Label(label="Reading…")
        style(app.mic_badge, "g935-badge")
        _mic_card, mic = make_card(
            mid_pair,
            "Microphone",
            subtitle="Boom position & ALSA capture",
            header_widget=app.mic_badge,
        )
        app.mic_big = Gtk.Label(xalign=0)
        style(app.mic_big, "g935-caption")
        app.mic_big.set_line_wrap(True)
        app.mic_big.set_markup("Reading boom position…")
        mic.pack_start(app.mic_big, False, False, 0)

        mic_row = Gtk.Box(spacing=8)
        mic_row.pack_start(Gtk.Label(label="Host capture", xalign=0), True, True, 0)
        app.hostmic_sw = Gtk.Switch()
        app.hostmic_sw.set_valign(Gtk.Align.CENTER)
        app.hostmic_sw.set_active(read_host_mic_fn() is not False)
        app.hostmic_sw.connect("notify::active", app.on_hostmic_toggle)
        mic_row.pack_start(app.hostmic_sw, False, False, 0)
        unstick = Gtk.Button(label="Unstick")
        unstick.set_tooltip_text("Force ALSA Mic Capture Switch ON")
        unstick.connect("clicked", lambda *_: (
            set_host_mic_fn(True), app._refresh_hostmic()))
        mic_row.pack_end(unstick, False, False, 0)
        mic.pack_start(mic_row, False, False, 0)

        app.mic_note = Gtk.Label(xalign=0)
        style(app.mic_note, "g935-caption")
        app.mic_note.set_line_wrap(True)
        mic.pack_start(app.mic_note, False, False, 0)
        app._update_mic_note()
    else:
        app.mic_label.set_no_show_all(True)
        app.mic_label.set_visible(False)

    container.pack_start(mid_pair, False, False, 0)

    # ---- 4. Collapsible G0–G3 button bindings ----
    bind_exp = Gtk.Expander(label="G0–G3 button bindings (tap & 0.45s hold)")
    style(bind_exp, "g935-expander")
    bind_exp.set_expanded(False)
    bind_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    bind_box.set_margin_top(8)
    bind_note = Gtk.Label(xalign=0)
    style(bind_note, "g935-caption")
    bind_note.set_line_wrap(True)
    bind_note.set_markup(
        "<small>Bottom→top: <b>G0</b> (earcup mute), then G1–G3. Active in "
        "software mode — works even with the panel closed when g935-dspd runs.</small>")
    bind_box.pack_start(bind_note, False, False, 0)

    grid = Gtk.Grid(column_spacing=10, row_spacing=6)
    grid.set_hexpand(True)
    for col, heading in enumerate(("Key", "Tap", "Hold (0.45s)")):
        h = Gtk.Label(label=heading, xalign=0)
        style(h, "g935-subtitle")
        grid.attach(h, col, 0, 1, 1)

    app._bind_combos = {}
    app._bind_cmds = {}
    for row_idx, key in enumerate(KEYS, start=1):
        k_lbl = Gtk.Label(label=KEY_LABELS[key], xalign=0)
        style(k_lbl, "g935-primary-title")
        k_lbl.set_size_request(34, -1)
        k_lbl.set_valign(Gtk.Align.START)
        k_lbl.set_margin_top(6)
        grid.attach(k_lbl, 0, row_idx, 1, 1)

        combos = {}
        cmds = {}
        for col_idx, layer in enumerate(LAYERS, start=1):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cell.set_hexpand(True)
            combo = Gtk.ComboBoxText()
            combo.set_hexpand(True)
            for action_id, label in ACTIONS:
                combo.append(action_id, label)
            slot = app._bindings[key].layer(layer)
            combo.set_active_id(slot.action)
            combo.connect("changed", app.on_binding_changed, key, layer)
            cell.pack_start(combo, False, False, 0)

            cmd = Gtk.Entry()
            cmd.set_placeholder_text(f"{LAYER_LABELS[layer]} command…")
            cmd.set_text(slot.command)
            cmd.connect("changed", app.on_binding_command, key, layer)
            cmd.set_no_show_all(True)
            cmd.set_visible(slot.action == "command")
            cell.pack_start(cmd, False, False, 0)

            grid.attach(cell, col_idx, row_idx, 1, 1)
            combos[layer] = combo
            cmds[layer] = cmd
        app._bind_combos[key] = combos
        app._bind_cmds[key] = cmds

    bind_box.pack_start(grid, False, False, 0)
    bind_exp.add(bind_box)
    container.pack_start(bind_exp, False, False, 0)


def set_gkey_pill_active(label: Gtk.Label, active: bool) -> None:
    ctx = label.get_style_context()
    if active:
        ctx.add_class("g935-key-active")
    else:
        ctx.remove_class("g935-key-active")


def update_mic_badge(app, boom_down: bool | None) -> None:
    badge = getattr(app, "mic_badge", None)
    if badge is None:
        return
    if boom_down is False:
        badge.set_text("Boom up")
        set_badge_tone(badge, "bad")
    elif boom_down is True:
        badge.set_text("Boom down")
        set_badge_tone(badge, "ok")
    else:
        badge.set_text("Unknown")
        set_badge_tone(badge, None)


def sync_eq_curve(app) -> None:
    curve = getattr(app, "eq_curve", None)
    if curve is None or not app.eq_sliders:
        return
    info = getattr(app, "_eq_info", None)
    lo = info.minimum_db if info else -12
    hi = info.maximum_db if info else 12
    labels = [lbl.get_text() for lbl in getattr(app, "eq_band_labels", [])]
    curve.set_gains(
        [s.get_value() for s in app.eq_sliders],
        band_names=labels or None,
        min_db=lo,
        max_db=hi,
    )


def build_eq_sliders(app, band_names) -> None:
    """Populate the EQ bank and Presets popover inside ``app.eq_box``."""
    sliders = Gtk.Box(spacing=4, homogeneous=True)
    app.eq_sliders = []
    app.eq_band_labels = []
    for name in band_names:
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        s = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -12, 12, 1)
        s.set_inverted(True)
        s.set_size_request(-1, 138)
        s.set_value(0)
        s.add_mark(0, Gtk.PositionType.LEFT, None)
        guard_slider_scroll(s)
        s.connect("value-changed", app._mark_eq_custom)
        col.pack_start(s, True, True, 0)
        band_label = Gtk.Label(label=name)
        style(band_label, "g935-caption")
        col.pack_start(band_label, False, False, 0)
        sliders.pack_start(col, True, True, 0)
        app.eq_sliders.append(s)
        app.eq_band_labels.append(band_label)
    app.eq_box.pack_start(sliders, False, False, 0)

    popover = Gtk.Popover.new(app.eq_preset_button)
    popover.set_position(Gtk.PositionType.BOTTOM)
    menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
    style(menu_box, "g935-preset-menu")
    menu_box.set_border_width(8)
    popover.add(menu_box)
    app.eq_preset_button.set_popover(popover)

    available = app.profile["eq_presets"]
    for category, names in EQ_PRESET_CATEGORIES:
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
            button.connect("clicked", app.on_eq_preset_menu, name, popover)
            grid.attach(button, index % 3, index // 3, 1, 1)
        menu_box.pack_start(grid, False, False, 0)
    menu_box.show_all()

    row = Gtk.Box(spacing=8)
    b = Gtk.Button(label="Apply EQ")
    b.get_style_context().add_class("suggested-action")
    b.connect("clicked", app.on_eq_apply)
    row.pack_start(b, True, True, 0)
    b = Gtk.Button(label="Read active")
    b.set_tooltip_text("Read active EQ from headset RAM")
    b.connect("clicked", lambda *_: app.send("eq", 2, "01", cb=app.got_eq))
    row.pack_start(b, True, True, 0)
    b = Gtk.Button(label="Load saved")
    b.set_tooltip_text("Load saved EQ from headset EEPROM")
    b.connect("clicked", lambda *_: app.send("eq", 2, "00", cb=app.got_eq))
    row.pack_start(b, True, True, 0)
    app.eq_box.pack_start(row, False, False, 0)
    app.eq_box.show_all()
    sync_eq_curve(app)
