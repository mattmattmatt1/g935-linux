"""Cairo chart widgets for battery health (no matplotlib dependency).

Battery-manager style: charge history area chart, expect-vs-actual bars,
session runtime bars, and drain-profile bars. Colors follow the GTK theme
when possible and fall back to a clean palette.
"""
from __future__ import annotations

import math
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


# Fallback palette (used when theme lookup fails)
_PAL = {
    "bg": (0.12, 0.13, 0.15),
    "plot": (0.16, 0.17, 0.20),
    "grid": (0.30, 0.32, 0.36),
    "axis": (0.55, 0.58, 0.62),
    "text": (0.85, 0.87, 0.90),
    "muted": (0.55, 0.58, 0.62),
    "actual": (0.30, 0.69, 0.91),       # blue — measured
    "actual_fill": (0.30, 0.69, 0.91, 0.28),
    "expected": (0.95, 0.62, 0.20),     # amber — rated/expected
    "expected_fill": (0.95, 0.62, 0.20, 0.18),
    "charge": (0.30, 0.80, 0.50),       # green — charging
    "charge_fill": (0.30, 0.80, 0.50, 0.25),
    "health_good": (0.30, 0.80, 0.50),
    "health_mid": (0.95, 0.75, 0.20),
    "health_bad": (0.90, 0.30, 0.30),
    "bar_bg": (0.22, 0.24, 0.28),
}


def _rgba(c, a=None):
    if len(c) == 4 and a is None:
        return c
    if a is None:
        return (*c[:3], 1.0)
    return (*c[:3], a)


def _set_source(cr, color, alpha=None):
    r, g, b, a = _rgba(color, alpha)
    cr.set_source_rgba(r, g, b, a)


def _pango_fit(cr, text, x, y, size=10, color=None, align="left"):
    """Simple cairo text (no pango dep beyond cairo)."""
    if color is not None:
        _set_source(cr, color)
    cr.select_font_face("Sans", 0, 0)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    if align == "center":
        x -= ext.width / 2
    elif align == "right":
        x -= ext.width
    cr.move_to(x, y)
    cr.show_text(text)


class _ChartBase(Gtk.DrawingArea):
    """Shared chrome: background, title, padding, theme-aware colors."""

    __gtype_name__ = "G935ChartBase"

    def __init__(self, title="", height=160):
        super().__init__()
        self._title = title
        self._data = None
        self.set_size_request(200, height)
        self.set_hexpand(True)
        self.connect("draw", self._on_draw)

    def set_data(self, data):
        self._data = data
        self.queue_draw()

    def _colors(self, widget):
        """Blend theme fg/bg with our accent palette."""
        pal = dict(_PAL)
        try:
            ctx = widget.get_style_context()
            # background
            ok, bg = ctx.lookup_color("theme_bg_color")
            if not ok:
                ok, bg = ctx.lookup_color("theme_base_color")
            if ok:
                pal["bg"] = (bg.red, bg.green, bg.blue)
                # slightly elevated plot surface
                pal["plot"] = (
                    min(1.0, bg.red * 0.92 + 0.04),
                    min(1.0, bg.green * 0.92 + 0.04),
                    min(1.0, bg.blue * 0.92 + 0.05),
                )
            ok, fg = ctx.lookup_color("theme_fg_color")
            if not ok:
                ok, fg = ctx.lookup_color("theme_text_color")
            if ok:
                pal["text"] = (fg.red, fg.green, fg.blue)
                pal["muted"] = (fg.red * 0.65, fg.green * 0.65, fg.blue * 0.65)
                pal["axis"] = pal["muted"]
                pal["grid"] = (fg.red * 0.25, fg.green * 0.25, fg.blue * 0.25)
        except Exception:
            pass
        return pal

    def _on_draw(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        pal = self._colors(widget)
        # outer bg
        _set_source(cr, pal["bg"])
        cr.rectangle(0, 0, w, h)
        cr.fill()
        # plot card
        pad = 8
        _set_source(cr, pal["plot"])
        self._round_rect(cr, pad, pad, w - 2 * pad, h - 2 * pad, 8)
        cr.fill()

        # title
        if self._title:
            _pango_fit(cr, self._title, pad + 10, pad + 16, size=11, color=pal["text"])

        # Leave room under the plot for x-axis labels / bar categories.
        bottom = getattr(self, "_bottom_label_h", 18)
        top = 28 if self._title else 12
        inner = {
            "x": pad + 40,
            "y": pad + top,
            "w": w - 2 * pad - 52,
            "h": h - 2 * pad - top - bottom,
        }
        if inner["w"] < 40 or inner["h"] < 30:
            return False
        self._draw_chart(cr, inner, pal)
        return False

    def _draw_chart(self, cr, box, pal):
        raise NotImplementedError

    @staticmethod
    def _round_rect(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _grid_y(self, cr, box, pal, y_min, y_max, ticks=4, fmt=None):
        fmt = fmt or (lambda v: f"{v:.0f}")
        for i in range(ticks + 1):
            frac = i / ticks
            val = y_min + (y_max - y_min) * (1 - frac)
            y = box["y"] + box["h"] * frac
            _set_source(cr, pal["grid"], 0.6)
            cr.set_line_width(1)
            cr.move_to(box["x"], y)
            cr.line_to(box["x"] + box["w"], y)
            cr.stroke()
            _pango_fit(cr, fmt(val), box["x"] - 6, y + 3, size=9,
                       color=pal["muted"], align="right")

    def _axis_frame(self, cr, box, pal):
        _set_source(cr, pal["axis"], 0.5)
        cr.set_line_width(1)
        cr.rectangle(box["x"], box["y"], box["w"], box["h"])
        cr.stroke()


class ChargeHistoryChart(_ChartBase):
    """Charge % over time with charging/discharging coloring — battery manager classic."""

    __gtype_name__ = "G935ChargeHistoryChart"

    def __init__(self):
        super().__init__(title="Charge level over time", height=200)
        self._bottom_label_h = 22

    def _draw_chart(self, cr, box, pal):
        data = self._data or {}
        points = data.get("points") or []  # [(t, pct, charging), ...]
        if len(points) < 2:
            _pango_fit(cr, "Collecting samples… wear the headset with tracking on",
                       box["x"] + box["w"] / 2,
                       box["y"] + box["h"] / 2, size=12, color=pal["muted"],
                       align="center")
            return

        t0, t1 = points[0][0], points[-1][0]
        span = max(1, t1 - t0)
        y_min, y_max = 0, 100
        self._grid_y(cr, box, pal, y_min, y_max, ticks=4, fmt=lambda v: f"{v:.0f}%")

        def xy(t, pct):
            x = box["x"] + (t - t0) / span * box["w"]
            y = box["y"] + (1 - (pct - y_min) / (y_max - y_min)) * box["h"]
            return x, y

        # Area fill under the curve
        cr.new_path()
        x0, y0 = xy(points[0][0], points[0][1])
        cr.move_to(x0, box["y"] + box["h"])
        cr.line_to(x0, y0)
        for t, pct, _chg in points[1:]:
            x, y = xy(t, pct)
            cr.line_to(x, y)
        x_last, _ = xy(points[-1][0], points[-1][1])
        cr.line_to(x_last, box["y"] + box["h"])
        cr.close_path()
        _set_source(cr, pal["actual_fill"])
        cr.fill()

        # Line segments colored by charge/discharge
        cr.set_line_width(2.2)
        cr.set_line_join(1)  # ROUND
        for i in range(1, len(points)):
            t0p, pct0, chg0 = points[i - 1]
            t1p, pct1, chg1 = points[i]
            charging = chg0 or chg1
            color = pal["charge"] if charging else pal["actual"]
            _set_source(cr, color)
            x0, y0 = xy(t0p, pct0)
            x1, y1 = xy(t1p, pct1)
            cr.move_to(x0, y0)
            cr.line_to(x1, y1)
            cr.stroke()

        # Expected dashed reference: linear discharge from first point at rated rate
        expected = data.get("expected_points")
        if expected and len(expected) >= 2:
            cr.set_line_width(1.5)
            cr.set_dash([5, 4])
            _set_source(cr, pal["expected"], 0.9)
            x, y = xy(expected[0][0], expected[0][1])
            cr.move_to(x, y)
            for t, pct in expected[1:]:
                x, y = xy(t, pct)
                cr.line_to(x, y)
            cr.stroke()
            cr.set_dash([])

        # Current level marker
        last_t, last_pct, _ = points[-1]
        lx, ly = xy(last_t, last_pct)
        _set_source(cr, pal["text"])
        cr.arc(lx, ly, 3.5, 0, 2 * math.pi)
        cr.fill()
        _pango_fit(cr, f"{last_pct}%", lx - 8, ly - 8, size=10, color=pal["text"],
                   align="right")

        self._axis_frame(cr, box, pal)

        # X labels: start, mid, end — date-aware for multi-day spans
        mid_t = t0 + span / 2
        _pango_fit(cr, _fmt_axis_time(t0, span), box["x"], box["y"] + box["h"] + 14,
                   size=9, color=pal["muted"])
        _pango_fit(cr, _fmt_axis_time(mid_t, span), box["x"] + box["w"] / 2,
                   box["y"] + box["h"] + 14, size=9, color=pal["muted"], align="center")
        _pango_fit(cr, _fmt_axis_time(t1, span), box["x"] + box["w"],
                   box["y"] + box["h"] + 14, size=9, color=pal["muted"], align="right")

        # Legend
        ly = box["y"] - 14
        self._legend_swatch(cr, box["x"] + box["w"] - 200, ly, pal["actual"],
                            "actual", pal["muted"])
        self._legend_swatch(cr, box["x"] + box["w"] - 130, ly, pal["charge"],
                            "charging", pal["muted"])
        if expected:
            self._legend_swatch(cr, box["x"] + box["w"] - 55, ly, pal["expected"],
                                "rated", pal["muted"])

    def _legend_swatch(self, cr, x, y, color, label, text_color=None):
        _set_source(cr, color)
        cr.rectangle(x, y - 6, 10, 3)
        cr.fill()
        _pango_fit(cr, label, x + 14, y, size=9,
                   color=text_color or _PAL["muted"])


class ExpectActualChart(_ChartBase):
    """Side-by-side remaining runtime: expected (rated) vs actual (measured)."""

    __gtype_name__ = "G935ExpectActualChart"

    def __init__(self):
        super().__init__(title="Time left now · expected vs measured", height=175)
        self._bottom_label_h = 36

    def _draw_chart(self, cr, box, pal):
        data = self._data or {}
        expected = data.get("expected_h")
        actual = data.get("actual_h")
        delta_pct = data.get("delta_pct")  # actual vs expected %

        if expected is None and actual is None:
            _pango_fit(cr, "Discharge ~10 min off-charger for a live estimate",
                       box["x"] + box["w"] / 2, box["y"] + box["h"] / 2,
                       size=11, color=pal["muted"], align="center")
            return

        # bars
        items = []
        if expected is not None:
            items.append(("Rated\nexpect", expected, pal["expected"]))
        if actual is not None:
            items.append(("Measured\nnow", actual, pal["actual"]))

        max_h = max((v for _, v, _ in items), default=1) * 1.15 or 1
        n = len(items)
        gap = 28
        bar_w = min(90, (box["w"] - gap * (n + 1)) / max(n, 1))
        total = n * bar_w + (n + 1) * gap
        x0 = box["x"] + (box["w"] - total) / 2 + gap

        self._grid_y(cr, box, pal, 0, max_h, ticks=4, fmt=lambda v: _fmt_hours(v))

        for i, (label, val, color) in enumerate(items):
            x = x0 + i * (bar_w + gap)
            bh = (val / max_h) * box["h"] if max_h else 0
            y = box["y"] + box["h"] - bh
            # track
            _set_source(cr, pal["bar_bg"])
            self._round_rect(cr, x, box["y"], bar_w, box["h"], 6)
            cr.fill()
            # value bar
            _set_source(cr, color)
            if bh > 4:
                self._round_rect(cr, x, y, bar_w, bh, 6)
                cr.fill()
            # value label on top
            _pango_fit(cr, _fmt_hours(val), x + bar_w / 2, y - 6,
                       size=12, color=pal["text"], align="center")
            # category under
            for li, line in enumerate(label.split("\n")):
                _pango_fit(cr, line, x + bar_w / 2,
                           box["y"] + box["h"] + 12 + li * 12,
                           size=9, color=pal["muted"], align="center")

        # delta badge (more useful than re-stating health)
        if delta_pct is not None:
            sign = "+" if delta_pct >= 0 else "−"
            if delta_pct >= 10:
                hc = pal["health_good"]
            elif delta_pct <= -15:
                hc = pal["health_bad"]
            else:
                hc = pal["muted"]
            badge = f"{sign}{abs(delta_pct):.0f}% vs rated"
            _pango_fit(cr, badge, box["x"] + box["w"], box["y"] - 2,
                       size=11, color=hc, align="right")

        self._axis_frame(cr, box, pal)


class SessionRuntimeChart(_ChartBase):
    """Per-session projected full runtime vs rated horizontal reference."""

    __gtype_name__ = "G935SessionRuntimeChart"

    def __init__(self):
        super().__init__(title="Full-runtime from each solid session", height=165)
        self._bottom_label_h = 22

    def _draw_chart(self, cr, box, pal):
        data = self._data or {}
        # Prefer solid (qualifying) sessions only for a cleaner story
        all_sessions = data.get("sessions") or []  # [{label, hours, ...}]
        rated = data.get("rated_h")
        sessions = [s for s in all_sessions if s.get("hours") is not None]
        n_short = len(all_sessions) - len(sessions)

        if not sessions:
            msg = "No solid sessions yet"
            if n_short:
                msg = f"{n_short} short session(s) — need ≥30 min / ≥8% drop"
            _pango_fit(cr, msg,
                       box["x"] + box["w"] / 2, box["y"] + box["h"] / 2,
                       size=11, color=pal["muted"], align="center")
            return

        vals = [s["hours"] for s in sessions]
        if rated:
            vals.append(rated)
        y_max = max(vals) * 1.2 if vals else 1

        self._grid_y(cr, box, pal, 0, y_max, ticks=4, fmt=lambda v: _fmt_hours(v))

        # rated reference line
        if rated is not None and y_max > 0:
            y = box["y"] + box["h"] * (1 - rated / y_max)
            cr.set_line_width(1.2)
            cr.set_dash([4, 3])
            _set_source(cr, pal["expected"], 0.9)
            cr.move_to(box["x"], y)
            cr.line_to(box["x"] + box["w"], y)
            cr.stroke()
            cr.set_dash([])
            _pango_fit(cr, f"rated {rated:g}h", box["x"] + box["w"] - 4, y - 4,
                       size=9, color=pal["expected"], align="right")

        n = len(sessions)
        gap = 10
        bar_w = max(10, min(48, (box["w"] - gap * (n + 1)) / max(n, 1)))
        total = n * bar_w + (n + 1) * gap
        x0 = box["x"] + max(0, (box["w"] - total) / 2) + gap

        for i, s in enumerate(sessions):
            hours = s["hours"]
            x = x0 + i * (bar_w + gap)
            bh = (hours / y_max) * box["h"]
            y = box["y"] + box["h"] - bh
            # color by vs rated
            if rated and hours < rated * 0.7:
                color = pal["health_bad"]
            elif rated and hours < rated * 0.9:
                color = pal["health_mid"]
            else:
                color = pal["actual"]
            _set_source(cr, color)
            if bh > 2:
                self._round_rect(cr, x, y, bar_w, bh, 4)
                cr.fill()
            # value on / above bar
            label_y = y - 4 if bh > 22 else y + 12
            lc = pal["text"] if bh > 22 else color
            _pango_fit(cr, _fmt_hours(hours), x + bar_w / 2, label_y,
                       size=9, color=lc, align="center")

        # x labels
        step = max(1, n // 8)
        for i, s in enumerate(sessions):
            if i % step != 0 and i != n - 1:
                continue
            x = x0 + i * (bar_w + gap) + bar_w / 2
            _pango_fit(cr, s.get("label", ""), x, box["y"] + box["h"] + 14,
                       size=8, color=pal["muted"], align="center")

        self._axis_frame(cr, box, pal)


class DrainProfileChart(_ChartBase):
    """mV/h drain rate by voltage bin — the learned profile."""

    __gtype_name__ = "G935DrainProfileChart"

    def __init__(self):
        super().__init__(title="How hard it drains by voltage (mV/h)", height=155)
        self._bottom_label_h = 22

    def _draw_chart(self, cr, box, pal):
        data = self._data or {}
        bins = data.get("bins") or {}  # {mv: rate}
        weak = data.get("weak_bins") or {}
        current_mv = data.get("current_mv")

        all_bins = sorted(set(bins) | set(weak))
        if not all_bins:
            _pango_fit(cr, "Wear the headset off-charger to build the profile",
                       box["x"] + box["w"] / 2, box["y"] + box["h"] / 2,
                       size=11, color=pal["muted"], align="center")
            return

        rates = [bins.get(b) or weak.get(b) or 0 for b in all_bins]
        y_max = max(rates) * 1.25 if rates else 1
        self._grid_y(cr, box, pal, 0, y_max, ticks=3, fmt=lambda v: f"{v:.0f}")

        n = len(all_bins)
        gap = 4
        bar_w = max(4, (box["w"] - gap * (n + 1)) / n)
        bin_mv = data.get("bin_mv") or 50
        cur_bin = None
        if current_mv is not None:
            cur_bin = int(round(current_mv / bin_mv) * bin_mv)

        for i, b in enumerate(all_bins):
            rate = bins.get(b) or weak.get(b) or 0
            solid = b in bins
            x = box["x"] + gap + i * (bar_w + gap)
            bh = (rate / y_max) * box["h"] if y_max else 0
            y = box["y"] + box["h"] - bh
            # Highlight the bin matching current voltage
            if cur_bin is not None and abs(b - cur_bin) < bin_mv / 2:
                color = pal["expected"]
                alpha = 1.0
            else:
                color = pal["actual"]
                alpha = 0.95 if solid else 0.35
            _set_source(cr, color, alpha)
            if bh > 1:
                self._round_rect(cr, x, y, bar_w, bh, 2)
                cr.fill()

        # x labels ends + mid (full → empty reads left→right if bins descend)
        for idx in sorted({0, n // 2, n - 1}):
            b = all_bins[idx]
            x = box["x"] + gap + idx * (bar_w + gap) + bar_w / 2
            _pango_fit(cr, f"{b}", x, box["y"] + box["h"] + 14,
                       size=8, color=pal["muted"], align="center")

        # Hint under title area
        if cur_bin is not None:
            _pango_fit(cr, "amber = now", box["x"] + box["w"], box["y"] - 2,
                       size=9, color=pal["expected"], align="right")

        self._axis_frame(cr, box, pal)


class HealthGaugeChart(_ChartBase):
    """Arc gauge for overall health %."""

    __gtype_name__ = "G935HealthGaugeChart"

    def __init__(self):
        super().__init__(title="Battery health", height=150)

    def _draw_chart(self, cr, box, pal):
        data = self._data or {}
        health = data.get("health_pct")
        learned = data.get("learned_h")
        rated = data.get("rated_h")
        n_sess = data.get("n_sessions")
        mah = data.get("effective_mah")
        design = data.get("design_mah")

        cx = box["x"] + box["w"] / 2
        cy = box["y"] + box["h"] * 0.68
        radius = min(box["w"] / 2 - 10, box["h"] * 0.78)

        # background arc (180°)
        cr.set_line_width(14)
        cr.set_line_cap(1)  # ROUND
        _set_source(cr, pal["bar_bg"])
        cr.arc(cx, cy, radius, math.pi, 2 * math.pi)
        cr.stroke()

        if health is None:
            _pango_fit(cr, "—", cx, cy - radius * 0.4, size=24,
                       color=pal["muted"], align="center")
            _pango_fit(cr, "need solid sessions", cx, cy - 6, size=10,
                       color=pal["muted"], align="center")
            return

        # value arc
        frac = max(0.0, min(1.0, health / 100.0))
        color = (pal["health_good"] if health >= 80
                 else pal["health_mid"] if health >= 55
                 else pal["health_bad"])
        _set_source(cr, color)
        cr.arc(cx, cy, radius, math.pi, math.pi + math.pi * frac)
        cr.stroke()

        _pango_fit(cr, f"{health:.0f}%", cx, cy - radius * 0.42, size=24,
                   color=pal["text"], align="center")
        if learned is not None and rated is not None:
            sub = f"{_fmt_hours(learned)} real · {rated:g}h rated"
            _pango_fit(cr, sub, cx, cy - 8, size=10, color=pal["muted"],
                       align="center")
        conf = None
        if n_sess is not None:
            conf = f"{n_sess} session{'s' if n_sess != 1 else ''}"
        if mah is not None and design is not None:
            conf = (f"{conf} · ~{mah}/{design} mAh" if conf
                    else f"~{mah}/{design} mAh")
        if conf:
            _pango_fit(cr, conf, cx, cy + 8, size=9, color=pal["muted"],
                       align="center")


def _fmt_time(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _fmt_axis_time(ts, span_s):
    """Axis label: include date when the window spans more than ~18 hours."""
    if span_s >= 18 * 3600:
        return time.strftime("%m/%d %H:%M", time.localtime(ts))
    return time.strftime("%H:%M", time.localtime(ts))


def _fmt_hours(h):
    if h is None:
        return "—"
    if h < 0:
        h = 0
    total_m = int(round(h * 60))
    if total_m >= 60:
        return f"{total_m // 60}h{total_m % 60:02d}m"
    return f"{total_m}m"


def build_history_points(recent, batt_percent_fn, max_points=400, full_mv=4200):
    """Downsample recent samples to chart points [(t, pct, charging), ...].

    While charging, the ADC often reports charger-path voltage (spikes to
    ~4.3 V+). Hold the last resting cell voltage for SoC so the curve does
    not jump to a fake 100% on plug-in.
    """
    if not recent:
        return []
    # First pass: resolve cell mV for every sample (charging holds last rest)
    resolved = []
    last_rest = None
    for t, mv, chg in recent:
        mv = int(mv)
        chg = bool(chg)
        if not chg:
            last_rest = mv
            cell = mv
        else:
            cell = last_rest if last_rest is not None else min(mv, int(full_mv))
        resolved.append((int(t), cell, chg))

    step = max(1, len(resolved) // max_points)
    pts = []
    for i in range(0, len(resolved), step):
        t, cell, chg = resolved[i]
        pts.append((t, int(batt_percent_fn(cell)), chg))
    # always include last
    t, cell, chg = resolved[-1]
    if not pts or pts[-1][0] != t:
        pts.append((t, int(batt_percent_fn(cell)), chg))
    return pts


def build_expected_overlay(points, rated_runtime_h):
    """Dashed rated-discharge reference from the first non-charge stretch.

    Starts at the first discharge sample's % and declines at 100/rated %/h.
    """
    if not points or not rated_runtime_h or rated_runtime_h <= 0:
        return None
    # find first discharge point
    start = None
    for t, pct, chg in points:
        if not chg:
            start = (t, pct)
            break
    if start is None:
        return None
    t0, pct0 = start
    rate = 100.0 / rated_runtime_h  # %/h
    out = []
    t_end = points[-1][0]
    # sample every ~5 min across the window
    step = max(60, (t_end - t0) // 40) if t_end > t0 else 300
    t = t0
    while t <= t_end:
        hours = (t - t0) / 3600.0
        pct = max(0.0, pct0 - rate * hours)
        out.append((t, pct))
        if pct <= 0:
            break
        t += step
    if not out or out[-1][0] != t_end:
        hours = (t_end - t0) / 3600.0
        out.append((t_end, max(0.0, pct0 - rate * hours)))
    return out
