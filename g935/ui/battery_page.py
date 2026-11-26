"""Battery Health page: dense top dashboard + charts first, text logs collapsed."""
from __future__ import annotations

import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from g935.battery import (
    HEALTH_DEFAULTS, batt_percent, build_insights,
    merge_discharge_sessions, session_full_runtime_h,
)
from g935.chart_data import build_level_series
from g935.charts import (
    ChargeHistoryChart, DrainProfileChart, ExpectActualChart,
    HealthGaugeChart, SessionRuntimeChart,
)
from g935.ui.theme import make_card, make_kpi_card, set_kpi, style
from g935.ui.visuals import AnimatedBatteryWidget


def fmt_dur(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    m = seconds // 60
    if m >= 60:
        return f"{m // 60}h{m % 60:02d}m"
    if m > 0:
        return f"{m}m"
    return f"{seconds}s"


def fmt_dur_h(hours) -> str:
    if hours is None:
        return "—"
    if hours <= 0:
        return "0m"
    total_m = int(round(hours * 60))
    if total_m >= 60:
        return f"{total_m // 60}h{total_m % 60:02d}m"
    return f"{total_m}m"


def _insight_color(tone: str) -> str:
    return {
        "good": "#2ec27e",
        "warn": "#e5a50a",
        "bad": "#e01b24",
        "info": "#62a0ea",
        "muted": "#9a9a9a",
    }.get(tone, "#9a9a9a")


def _remain_color(hours, charging: bool = False) -> str | None:
    if charging:
        return "#2ec27e"
    if hours is None:
        return None
    if hours < 0.75:
        return "#e01b24"
    if hours < 2.0:
        return "#e5a50a"
    return "#2ec27e"


def build_battery_page(app, container):
    """Build a dense, graph-first Battery Health page."""
    # ---- 1. Top unified Battery Dashboard (all key numbers above the fold) ----
    pwr_box = Gtk.Box(spacing=6)
    pwr_lbl = Gtk.Label(label="Auto-off")
    style(pwr_lbl, "g935-caption")
    pwr_box.pack_start(pwr_lbl, False, False, 0)
    app.power_timeout = Gtk.SpinButton.new_with_range(0, 255, 1)
    app.power_timeout.set_value(15)
    app.power_timeout.set_tooltip_text(
        "Auto power-off timeout in minutes (0 = stay on indefinitely)")
    pwr_box.pack_start(app.power_timeout, False, False, 0)
    unit = Gtk.Label(label="m")
    style(unit, "g935-caption")
    pwr_box.pack_start(unit, False, False, 0)
    apply_btn = Gtk.Button(label="Set")
    apply_btn.get_style_context().add_class("suggested-action")
    apply_btn.connect("clicked", app.on_power_timeout_apply)
    pwr_box.pack_start(apply_btn, False, False, 0)
    app.power_frame = pwr_box

    hero_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    style(hero_card, "g935-card")

    top_row = Gtk.Box(spacing=14)
    app.batt_graphic = AnimatedBatteryWidget()
    app.batt_graphic.set_valign(Gtk.Align.CENTER)
    top_row.pack_start(app.batt_graphic, False, False, 0)

    left_hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    left_hero.set_valign(Gtk.Align.CENTER)
    app.health_remain = Gtk.Label(xalign=0)
    app.health_remain.set_markup("<span size='xx-large' weight='bold'>—</span>")
    app.health_remain.set_line_wrap(True)
    left_hero.pack_start(app.health_remain, False, False, 0)

    app.health_status = Gtk.Label(xalign=0)
    app.health_status.set_markup("<span size='medium'>…</span>")
    app.health_status.set_line_wrap(True)
    left_hero.pack_start(app.health_status, False, False, 0)
    top_row.pack_start(left_hero, True, True, 0)

    pwr_box.set_valign(Gtk.Align.START)
    top_row.pack_end(pwr_box, False, False, 0)
    hero_card.pack_start(top_row, False, False, 0)

    # 5-column dense KPI strip: Health | Lost/charge | Full runtime | Charge-up | Live drain
    kpi_row = Gtk.Box(spacing=8)
    kpi_row.set_homogeneous(True)
    app.kpi_health = make_kpi_card(kpi_row, "Health")
    app.kpi_lost = make_kpi_card(kpi_row, "Lost / charge")
    app.kpi_full = make_kpi_card(kpi_row, "Full runtime")
    app.kpi_charge = make_kpi_card(kpi_row, "Charge-up")
    app.kpi_drain = make_kpi_card(kpi_row, "Live drain")
    hero_card.pack_start(kpi_row, False, False, 0)

    # Compact status/takeaway footer inside hero card
    app.health_session = Gtk.Label(xalign=0)
    style(app.health_session, "g935-caption")
    app.health_session.set_line_wrap(True)
    hero_card.pack_start(app.health_session, False, False, 0)

    app.health_insights = Gtk.Label(xalign=0)
    app.health_insights.set_line_wrap(True)
    app.health_insights.set_markup("")
    hero_card.pack_start(app.health_insights, False, False, 0)

    # Kept for compatibility (hidden unless explicitly used)
    app.health_wear_description = Gtk.Label(xalign=0)
    app.health_wear_description.set_no_show_all(True)
    app.health_wear_description.hide()

    container.pack_start(hero_card, False, False, 0)

    # ---- 2. Battery level 7-day curve (immediately visible above the fold) ----
    _hist_card, hist_f = make_card(
        container, "Battery level", subtitle="7-day charge & discharge curve")
    app.chart_history = ChargeHistoryChart()
    app.chart_history.set_size_request(200, 210)
    hist_f.pack_start(app.chart_history, False, False, 0)
    app.history_caption = Gtk.Label(xalign=0)
    style(app.history_caption, "g935-caption")
    app.history_caption.set_line_wrap(True)
    hist_f.pack_start(app.history_caption, False, False, 0)

    # ---- 3. 2-column chart grid: Health & Time Left + Sessions & Drain Profile ----
    _cmp_card, compare = make_card(container, "Health & remaining runtime")
    cmp_row = Gtk.Box(spacing=10)
    app.chart_gauge = HealthGaugeChart()
    app.chart_gauge.set_size_request(200, 148)
    app.chart_gauge.set_hexpand(False)
    cmp_row.pack_start(app.chart_gauge, False, False, 0)
    app.chart_expect = ExpectActualChart()
    app.chart_expect.set_size_request(200, 148)
    cmp_row.pack_start(app.chart_expect, True, True, 0)
    compare.pack_start(cmp_row, False, False, 0)
    app.health_detail = Gtk.Label(xalign=0)
    style(app.health_detail, "g935-caption")
    app.health_detail.set_line_wrap(True)
    compare.pack_start(app.health_detail, False, False, 0)

    charts_grid = Gtk.Box(spacing=10)
    charts_grid.set_homogeneous(True)
    _sess_card, sess_f = make_card(charts_grid, "Session runtimes")
    app.chart_sessions = SessionRuntimeChart()
    app.chart_sessions.set_size_request(180, 150)
    sess_f.pack_start(app.chart_sessions, False, False, 0)

    _prof_card, prof_f = make_card(charts_grid, "Drain profile (mV/h)")
    app.chart_profile = DrainProfileChart()
    app.chart_profile.set_size_request(180, 150)
    prof_f.pack_start(app.chart_profile, False, False, 0)
    app.health_profile = Gtk.Label(xalign=0)
    style(app.health_profile, "g935-caption")
    app.health_profile.set_line_wrap(True)
    app.health_profile.set_markup("<small>collecting discharge datapoints…</small>")
    prof_f.pack_start(app.health_profile, False, False, 0)
    container.pack_start(charts_grid, False, False, 0)

    # ---- 4. Detailed logs tucked into a single collapsed expander ----
    logs_exp = Gtk.Expander(label="Session & charge-up logs")
    style(logs_exp, "g935-expander")
    logs_exp.set_expanded(False)
    logs_grid = Gtk.Box(spacing=14)
    logs_grid.set_homogeneous(True)
    logs_grid.set_margin_top(8)

    s_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    s_hdr = Gtk.Label(label="Usage sessions", xalign=0)
    style(s_hdr, "g935-subtitle")
    s_col.pack_start(s_hdr, False, False, 0)
    app.health_hist = Gtk.Label(xalign=0)
    style(app.health_hist, "g935-caption")
    app.health_hist.set_line_wrap(True)
    app.health_hist.set_markup("<small>no sessions recorded</small>")
    s_col.pack_start(app.health_hist, False, False, 0)
    logs_grid.pack_start(s_col, True, True, 0)

    c_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    c_hdr = Gtk.Label(label="Charge-ups", xalign=0)
    style(c_hdr, "g935-subtitle")
    c_col.pack_start(c_hdr, False, False, 0)
    app.health_charges = Gtk.Label(xalign=0)
    style(app.health_charges, "g935-caption")
    app.health_charges.set_line_wrap(True)
    app.health_charges.set_markup("<small>no charge-ups recorded</small>")
    c_col.pack_start(app.health_charges, False, False, 0)
    logs_grid.pack_start(c_col, True, True, 0)

    logs_exp.add(logs_grid)
    container.pack_start(logs_exp, False, False, 0)

    # ---- 5. Tracking + methodology (collapsed) ----
    about = Gtk.Expander(label="Tracking & how these numbers work")
    style(about, "g935-expander")
    about.set_expanded(False)
    about_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    about_box.set_margin_top(8)
    about.add(about_box)
    container.pack_start(about, False, False, 0)

    track_row = Gtk.Box(spacing=8)
    track_lbl = Gtk.Label(xalign=0)
    track_lbl.set_markup(
        "<b>Track battery health</b>  (log voltage &amp; sessions to health.json)")
    track_row.pack_start(track_lbl, True, True, 0)
    app.track_sw = Gtk.Switch()
    app.track_sw.set_active(app.health.settings["tracking"])
    app.track_sw.connect("notify::active", app.on_track_toggle)
    track_row.pack_end(app.track_sw, False, False, 0)
    about_box.pack_start(track_row, False, False, 0)

    note = Gtk.Label(xalign=0)
    style(note, "g935-caption")
    note.set_line_wrap(True)
    note.set_markup(
        "<small>The G935 only reports <b>voltage + charging flag</b> — no coulomb "
        "counter. Health extrapolates solid discharge sessions (≥30 min, ≥8% drop; "
        "short hops merge across ≤15 min gaps) against the rated spec. While charging, "
        "SoC holds the last resting cell reading instead of charger-rail spikes "
        "(~4.3 V+).</small>")
    about_box.pack_start(note, False, False, 0)

    # ---- 6. Battery specification (collapsed for aftermarket cells) ----
    spec_exp = Gtk.Expander(
        label="Battery specification (stock G935 — edit for aftermarket cells)")
    style(spec_exp, "g935-expander")
    spec_exp.set_expanded(False)
    spec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    spec.set_margin_top(8)
    spec_exp.add(spec)
    container.pack_start(spec_exp, False, False, 0)

    spec_grid = Gtk.Grid(column_spacing=16, row_spacing=6)
    spec_grid.set_hexpand(True)
    app.spec_spins = {}
    spec_items = (
        ("design_capacity_mah", "Design capacity (mAh)", 300, 5000, 50),
        ("rated_runtime_h_rgb_on", "Rated runtime, RGB on (h)", 1, 40, 0.5),
        ("rated_runtime_h_rgb_off", "Rated runtime, RGB off (h)", 1, 40, 0.5),
        ("full_mv", "Full-charge voltage (mV)", 3900, 4400, 10),
        ("empty_mv", "Empty voltage (mV)", 3000, 3700, 10),
        ("rated_charge_h", "Rated empty-to-full (h)", 0.5, 8, 0.25),
    )
    for idx, (key, label, lo, hi, step) in enumerate(spec_items):
        r, c = idx // 2, (idx % 2) * 2
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_hexpand(True)
        spec_grid.attach(lbl, c, r, 1, 1)
        sp = Gtk.SpinButton.new_with_range(lo, hi, step)
        if step < 1:
            sp.set_digits(1)
        sp.set_value(app.health.settings[key])
        sp.connect("value-changed", app.on_spec_changed, key)
        spec_grid.attach(sp, c + 1, r, 1, 1)
        app.spec_spins[key] = sp
    spec.pack_start(spec_grid, False, False, 0)

    row = Gtk.Box(spacing=8)
    row.pack_start(Gtk.Label(label="Compare against", xalign=0), False, False, 0)
    app.profile_combo = Gtk.ComboBoxText()
    for t in ("RGB lighting on spec", "RGB lighting off spec"):
        app.profile_combo.append_text(t)
    app.profile_combo.set_active(
        0 if app.health.settings["runtime_profile"] == "rgb_on" else 1)
    app.profile_combo.connect("changed", app.on_profile_changed)
    row.pack_start(app.profile_combo, False, False, 0)
    reset_btn = Gtk.Button(label="Reset stock values")
    reset_btn.connect("clicked", app.on_spec_reset)
    row.pack_end(reset_btn, False, False, 0)
    spec.pack_start(row, False, False, 2)

    app.health_live = app.health_status
    app.health_est = app.kpi_health["value"]


def render_health_display(app, mv=None, pct=None, state="",
                          raw_mv=None, path_is_rail=False) -> None:
    """Populate all Battery Health KPIs, hero readout, and Cairo charts."""
    h = app.health
    rated = app._rated_runtime()
    charging = (state == "charging") if state else None
    if state in ("discharging",):
        charging = False
    a = h.analysis(
        mv=mv if mv is not None else raw_mv,
        rated_runtime_h=rated,
        charging=charging,
    )
    cell = a.get("cell_mv")
    raw = raw_mv if raw_mv is not None else a.get("raw_mv")
    if pct is None and cell is not None:
        pct = batt_percent(cell)

    if hasattr(app, "batt_graphic"):
        app.batt_graphic.set_state(
            pct, charging, offline=(state == "headset off"))

    # --- Hero remaining time ---
    remain = a.get("remain_best_h")
    if charging:
        remain_chg = a.get("remain_charge_h")
        if remain_chg is not None:
            hero = (
                "<span size='xx-large' weight='bold' foreground='#2ec27e'>"
                f"{fmt_dur_h(remain_chg)} to full</span>")
        else:
            hero = (
                "<span size='xx-large' weight='bold' foreground='#2ec27e'>"
                "Charging</span>")
    elif remain is not None:
        color = _remain_color(remain) or "#ffffff"
        src = a.get("remain_source") or "estimate"
        rel_s = ""
        if a.get("remain_vs_rated_pct") is not None:
            rel = a["remain_vs_rated_pct"]
            rel_s = f" · {'+' if rel >= 0 else '−'}{abs(rel):.0f}% vs rated"
        hero = (
            f"<span size='xx-large' weight='bold' foreground='{color}'>"
            f"{fmt_dur_h(remain)} left</span>  "
            f"<span size='small' foreground='#9a9a9a'>via {src}{rel_s}</span>")
    elif state == "headset off":
        hero = (
            "<span size='xx-large' weight='bold' foreground='#9a9a9a'>"
            "Headset off</span>")
    elif mv is not None and state == "discharging":
        hero = (
            "<span size='xx-large' weight='bold'>—</span>  "
            "<span size='small' foreground='#9a9a9a'>collecting live drain…</span>")
    else:
        hero = (
            "<span size='xx-large' weight='bold'>—</span>  "
            "<span size='small' foreground='#9a9a9a'>waiting for battery data</span>")
    app.health_remain.set_markup(hero)

    # --- Compact status line ---
    if cell is None and raw is None:
        app.health_status.set_markup(
            f"<span size='medium'>{GLib.markup_escape_text(state or '…')}</span>")
    elif charging:
        cell_s = f"{cell} mV rest" if cell is not None else "rest ?"
        path_s = f"{raw} mV rail" if path_is_rail else f"{raw} mV path"
        app.health_status.set_markup(
            f"<b>{pct if pct is not None else '—'}%</b> · {cell_s} · "
            f"↑ {GLib.markup_escape_text(state)} "
            f"<span foreground='#9a9a9a'>({GLib.markup_escape_text(path_s)})</span>")
    else:
        arrow = "↓" if state == "discharging" else "·"
        show_mv = cell if cell is not None else raw
        app.health_status.set_markup(
            f"<b>{pct}%</b> · {show_mv} mV · {arrow} "
            f"{GLib.markup_escape_text(state)}")

    # --- 5-column KPI strip ---
    if a.get("health_pct") is not None:
        hp = a["health_pct"]
        conf = "early" if a["n_sessions"] < 3 else f"{a['n_sessions']} sessions"
        hc = "#2ec27e" if hp >= 75 else "#e5a50a" if hp >= 55 else "#e01b24"
        set_kpi(
            app.kpi_health,
            f"<span size='x-large' weight='bold' foreground='{hc}'>≈{hp:.0f}%</span>",
            conf)
        learned = a.get("learned_full_runtime_h")
        lost_h = max(0.0, rated - learned) if learned is not None else None
        if lost_h is not None:
            lc = "#2ec27e" if lost_h < 0.25 else "#e5a50a" if lost_h < rated * 0.30 else "#e01b24"
            set_kpi(
                app.kpi_lost,
                f"<span size='x-large' weight='bold' foreground='{lc}'>"
                f"{'0m' if lost_h < 1.0/120.0 else '−' + fmt_dur_h(lost_h)}</span>",
                f"vs {rated:g}h spec")
        else:
            set_kpi(app.kpi_lost, "<span size='x-large' weight='bold'>—</span>", "need data")
    else:
        set_kpi(app.kpi_health, "<span size='x-large' weight='bold'>—</span>", "need ≥30m")
        set_kpi(app.kpi_lost, "<span size='x-large' weight='bold'>—</span>", "need ≥30m")

    if a.get("learned_full_runtime_h") is not None:
        set_kpi(
            app.kpi_full,
            f"<span size='large' weight='bold'>{fmt_dur_h(a['learned_full_runtime_h'])}</span>",
            f"of {rated:g}h rated")
    else:
        set_kpi(app.kpi_full, "<span size='large' weight='bold'>—</span>", f"rated {rated:g}h")

    rated_chg = a.get("rated_charge_h") or 2.0
    if a.get("learned_full_charge_h") is not None:
        chg_n = a.get("n_charges") or 0
        set_kpi(
            app.kpi_charge,
            f"<span size='large' weight='bold'>{fmt_dur_h(a['learned_full_charge_h'])}</span>",
            f"{chg_n} fill{'s' if chg_n != 1 else ''} · {rated_chg:g}h spec")
    else:
        set_kpi(app.kpi_charge, "<span size='large' weight='bold'>—</span>",
                f"rated {rated_chg:g}h")

    if a.get("live_rate_pct_per_h") is not None:
        set_kpi(
            app.kpi_drain,
            f"<span size='large' weight='bold'>−{a['live_rate_pct_per_h']:.1f}%/h</span>",
            f"over {fmt_dur(a['live_rate_span_s'])}")
    elif charging:
        set_kpi(app.kpi_drain, "<span size='large' weight='bold'>—</span>", "charging")
    else:
        set_kpi(app.kpi_drain, "<span size='large' weight='bold'>—</span>", "≥10m / 3%")

    # --- Open session + concise top 2 insights ---
    if h.cur:
        seg = h.cur
        dt = seg["t_end"] - seg["t_start"]
        line = (
            f"Current {seg['type']}: {fmt_dur(dt)} · "
            f"{seg['pct_start']}→{seg['pct_end']}% · "
            f"{seg['mv_start']}→{seg['mv_end']} mV")
        if seg["type"] == "discharge" and dt >= 600:
            rate = (seg["pct_start"] - seg["pct_end"]) / (dt / 3600.0)
            if rate > 0:
                line += f" · −{rate:.1f}%/h"
        app.health_session.set_markup(f"<small>{line}</small>")
    else:
        app.health_session.set_markup("<small>No active session</small>")

    insights = build_insights(a, state=state, charging=charging)[:2]
    if insights:
        parts = [
            f"<span foreground='{_insight_color(ins['tone'])}'>●</span> "
            f"{GLib.markup_escape_text(ins['text'])}"
            for ins in insights
        ]
        app.health_insights.set_markup("<small>" + "\n".join(parts) + "</small>")
    else:
        app.health_insights.set_markup("")

    # --- Charts ---
    update_health_charts(app, a, rated, mv=cell)

    app.history_caption.set_markup(
        "<small>Blue = using · Green = charging (rest SoC) · Gaps projected continuously.</small>"
        if len(h.recent) >= 2 else
        "<small>Samples log once per minute while tracking is enabled.</small>")

    details = []
    if a.get("remain_expected_adj_h") is not None and a.get("health_pct") is not None:
        details.append(f"Health-adjusted ETA: ~{fmt_dur_h(a['remain_expected_adj_h'])}.")
    ps_peak = a.get("peak_summary")
    if ps_peak and ps_peak.get("latest_mv"):
        details.append(
            f"Peak OCV: {ps_peak['latest_mv']} mV "
            f"(median {ps_peak['median_mv']:.0f} mV, {ps_peak['n']} charges).")
    app.health_detail.set_markup(
        "<small>" + GLib.markup_escape_text(" ".join(details) or " ") + "</small>")

    ps = a["profile_summary"]
    if not ps or ps.get("hours_logged", 0) <= 0:
        app.health_profile.set_markup("<small>Wear off-charger to build voltage-bin profile.</small>")
    else:
        span = ps.get("span_mv")
        span_s = f"{span[0]}–{span[1]} mV" if span else "—"
        app.health_profile.set_markup(
            f"<small><b>{ps['hours_logged']:.1f}h</b> logged · "
            f"<b>{ps.get('bins_filled', 0)}</b> bins ({span_s})</small>")

    # --- Collapsed text logs ---
    rows = a.get("session_rows") or []
    if not rows:
        app.health_hist.set_markup("<small>No usage sessions yet.</small>")
    else:
        lines = []
        for r in rows[:8]:
            when = time.strftime("%m/%d %H:%M", time.localtime(r["t_start"]))
            dur = fmt_dur(r["on_time_s"])
            drain = f"{r['pct_start']}→{r['pct_end']}%"
            if r["quality"] == "solid":
                bc = (
                    _insight_color("good") if (r.get("pct_of_rated") or 0) >= 85
                    else _insight_color("warn") if (r.get("pct_of_rated") or 0) >= 70
                    else _insight_color("bad"))
                tail = f"<span foreground='{bc}'>{fmt_dur_h(r['full_h'])} ({r['pct_of_rated']:.0f}%)</span>"
            else:
                tail = "<span foreground='#9a9a9a'>short</span>"
            lines.append(f"<b>{GLib.markup_escape_text(when)}</b>  {dur}  {drain}  {tail}")
        app.health_hist.set_markup("<small>" + "\n".join(lines) + "</small>")

    crows = a.get("charge_rows") or []
    if not crows:
        app.health_charges.set_markup("<small>No charge-ups yet.</small>")
    else:
        clines = []
        for r in crows[:8]:
            when = time.strftime("%m/%d %H:%M", time.localtime(r["t_start"]))
            dur = fmt_dur(r["on_time_s"])
            span = (
                f"{r['pct_start']}→{r['pct_end']}%"
                if r["pct_start"] is not None and r["pct_end"] is not None
                else "rest ?"
            )
            if r["quality"] == "solid":
                tail = f"<span foreground='#2ec27e'>→ {fmt_dur_h(r['full_h'])}</span>"
            elif r["quality"] == "charging":
                tail = "<span foreground='#2ec27e'>charging</span>"
            else:
                tail = "<span foreground='#9a9a9a'>short/idle</span>"
            clines.append(f"<b>{GLib.markup_escape_text(when)}</b>  {dur}  {span}  {tail}")
        app.health_charges.set_markup("<small>" + "\n".join(clines) + "</small>")


def update_health_charts(app, analysis, rated, mv=None) -> None:
    h = app.health
    full = int(h.settings.get("full_mv", HEALTH_DEFAULTS["full_mv"]))
    app.chart_history.set_data({
        "series": build_level_series(h.recent, batt_percent, full_mv=full),
    })
    app.chart_gauge.set_data({
        "health_pct": analysis.get("health_pct"),
        "learned_h": analysis.get("learned_full_runtime_h"),
        "rated_h": rated,
        "n_sessions": analysis.get("n_sessions"),
        "effective_mah": analysis.get("effective_mah"),
        "design_mah": analysis.get("design_mah"),
    })
    app.chart_expect.set_data({
        "expected_h": analysis.get("remain_expected_h"),
        "actual_h": analysis.get("remain_best_h"),
        "delta_pct": analysis.get("remain_vs_rated_pct"),
    })
    sess_rows = []
    for sess in merge_discharge_sessions(h.segments):
        full_rt = session_full_runtime_h(sess)
        if full_rt is None:
            continue
        label = time.strftime("%m/%d", time.localtime(sess["t_start"]))
        sess_rows.append({"label": label, "hours": full_rt})
    app.chart_sessions.set_data({
        "sessions": sess_rows[-16:],
        "rated_h": rated,
    })
    profile = analysis.get("profile") or {}
    app.chart_profile.set_data({
        "bins": profile.get("bins") or {},
        "weak_bins": profile.get("weak_bins") or {},
        "current_mv": mv,
        "bin_mv": profile.get("bin_mv"),
    })
