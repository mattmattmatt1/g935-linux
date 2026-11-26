"""Cairo chart widgets for battery health (no matplotlib dependency).

Battery-manager style: charge history area chart, expect-vs-actual bars,
session runtime bars, and drain-profile bars. Colors follow the GTK theme
when possible and fall back to a clean palette.
"""
from __future__ import annotations

import datetime
import math
import time

import cairo
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
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    if align == "center":
        x -= ext.width / 2
    elif align == "right":
        x -= ext.width
    cr.move_to(x, y)
    cr.show_text(text)


# Sampling holes shorter than this stay on the same stroke; we just do not
# invent a smooth curve through them (a straight connector fills the gap).
_HOLE_S = 20 * 60


def _smooth_cluster(run, step_s=90):
    """Resample + two-pass EMA so a sitting reads as a curve, not stairs."""
    if len(run) < 2:
        return [(float(t), float(p), c) for t, p, c in run]
    t0, t1 = float(run[0][0]), float(run[-1][0])
    if len(run) < 8:
        grid = [(float(t), float(p), c) for t, p, c in run]
    else:
        grid = []
        t = t0
        j = 0
        while t <= t1:
            while j + 1 < len(run) and run[j + 1][0] < t:
                j += 1
            if j + 1 < len(run) and run[j + 1][0] != run[j][0]:
                u = (t - run[j][0]) / (run[j + 1][0] - run[j][0])
                pct = run[j][1] + u * (run[j + 1][1] - run[j][1])
                chg = run[j][2] or run[j + 1][2]
            else:
                pct, chg = float(run[j][1]), run[j][2]
            grid.append((t, pct, chg))
            t += step_s
        if grid[-1][0] < t1:
            grid.append((float(run[-1][0]), float(run[-1][1]), run[-1][2]))

    alpha = 0.22

    def ema(pts):
        s = pts[0][1]
        out = []
        for t, p, c in pts:
            s = alpha * p + (1.0 - alpha) * s
            out.append((t, s, c))
        return out

    fwd = ema(grid)
    rev = ema(list(reversed(fwd)))
    rev.reverse()
    # Pin endpoints so a charge rise / last sample is not EMA-lagged.
    rev[0] = (rev[0][0], float(run[0][1]), run[0][2])
    rev[-1] = (rev[-1][0], float(run[-1][1]), run[-1][2])
    return rev


def _cluster_run(run, hole_s=_HOLE_S):
    """Split a sitting on headset-off holes. Never invent samples across them."""
    if not run:
        return []
    clusters = [[run[0]]]
    for pt in run[1:]:
        if pt[0] - clusters[-1][-1][0] > hole_s:
            clusters.append([pt])
        else:
            clusters[-1].append(pt)
    return clusters


def _display_curve(run, step_s=90, hole_s=_HOLE_S):
    """Flattened _display_clusters (tests / callers that want one list)."""
    return [pt for cl in _display_clusters(run, step_s=step_s, hole_s=hole_s)
            for pt in cl]


def _display_clusters(run, step_s=90, hole_s=_HOLE_S):
    """Like _display_curve but keeps each sit as its own list (lift the pen)."""
    out = []
    for cl in _cluster_run(run, hole_s=hole_s):
        if len(cl) == 1:
            t, p, c = cl[0]
            out.append([(float(t), float(p), c)])
        else:
            out.append(_smooth_cluster(cl, step_s))
    return out


def _trace_poly(cr, pts):
    """Polyline through every vertex. Round caps hide the joints."""
    if not pts:
        return
    cr.move_to(*pts[0])
    for pt in pts[1:]:
        cr.line_to(*pt)


def _trace_smooth(cr, pts, max_dx=28):
    """Mid-point spline on close points; long jumps stay a straight segment.

    A spline across a multi-hour hole bows into empty space and looks like
    invented drain. Only curve when neighbours share a few pixels of x.
    """
    if not pts:
        return
    if len(pts) < 3:
        _trace_poly(cr, pts)
        return
    cr.move_to(*pts[0])
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if abs(x1 - x0) > max_dx:
            cr.line_to(x1, y1)
            continue
        cx = (x0 + x1) / 2.0
        cr.curve_to(cx, y0, cx, y1, x1, y1)


def _decimate_xy(curve, flags, min_dx=1.25):
    """Drop points that collapse into the same pixel, keep color flips."""
    if len(curve) <= 3:
        return curve, flags
    out_c = [curve[0]]
    out_f = [flags[0]]
    for i in range(1, len(curve) - 1):
        if (abs(curve[i][0] - out_c[-1][0]) >= min_dx
                or bool(flags[i]) != bool(out_f[-1])):
            out_c.append(curve[i])
            out_f.append(flags[i])
    out_c.append(curve[-1])
    out_f.append(flags[-1])
    return out_c, out_f


def _color_runs(curve, flags):
    """Inclusive [i, j] slices of the same charge/use color. Shared vertices."""
    if len(curve) < 2:
        return []
    runs = []
    i = 0
    while i < len(curve) - 1:
        charging = bool(flags[i] and flags[i + 1])
        j = i + 1
        while j < len(curve) - 1 and bool(flags[j] and flags[j + 1]) == charging:
            j += 1
        runs.append((i, j, charging))
        i = j
    return runs


class _ChartBase(Gtk.DrawingArea):
    """Shared chrome: background, title, padding, theme-aware colors."""

    __gtype_name__ = "G935ChartBase"

    def __init__(self, title="", height=160, framed=True):
        super().__init__()
        self._title = title
        self._framed = framed
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
        cr.set_antialias(cairo.ANTIALIAS_GOOD)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if self._framed:
            _set_source(cr, pal["bg"])
            cr.rectangle(0, 0, w, h)
            cr.fill()
            pad = 8
            _set_source(cr, pal["plot"])
            self._round_rect(cr, pad, pad, w - 2 * pad, h - 2 * pad, 8)
            cr.fill()
            if self._title:
                _pango_fit(cr, self._title, pad + 10, pad + 16,
                           size=11, color=pal["text"])
            bottom = getattr(self, "_bottom_label_h", 18)
            top = 28 if self._title else 12
            inner = {
                "x": pad + 40,
                "y": pad + top,
                "w": w - 2 * pad - 52,
                "h": h - 2 * pad - top - bottom,
            }
        else:
            # Sit inside a section card — no second box, no second title.
            bottom = getattr(self, "_bottom_label_h", 20)
            top = 8 if self._title else 4
            inner = {
                "x": 36,
                "y": top,
                "w": w - 44,
                "h": h - top - bottom,
            }
            if self._title:
                _pango_fit(cr, self._title, inner["x"], 12,
                           size=10, color=pal["muted"])
                inner["y"] = 20
                inner["h"] = h - 20 - bottom
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
            _set_source(cr, pal["grid"], 0.28 if i < ticks else 0.42)
            cr.set_line_width(1.0)
            cr.move_to(box["x"], y)
            cr.line_to(box["x"] + box["w"], y)
            cr.stroke()
            _pango_fit(cr, fmt(val), box["x"] - 8, y + 3, size=8.5,
                       color=pal["muted"], align="right")

    def _axis_frame(self, cr, box, pal):
        # Hairline baseline only — a full box fights the section card.
        _set_source(cr, pal["axis"], 0.28)
        cr.set_line_width(1.0)
        cr.move_to(box["x"], box["y"] + box["h"])
        cr.line_to(box["x"] + box["w"], box["y"] + box["h"])
        cr.stroke()


class ChargeHistoryChart(_ChartBase):
    """Battery % over the last week as one connected line."""

    __gtype_name__ = "G935ChargeHistoryChart"

    def __init__(self):
        super().__init__(title="", height=232, framed=False)
        self._bottom_label_h = 26

    def _draw_chart(self, cr, box, pal):
        data = self._data or {}
        lobes = data.get("series") or []
        lobes = [lobe for lobe in lobes if lobe]
        if not lobes:
            _pango_fit(cr, "Collecting samples… wear the headset with tracking on",
                       box["x"] + box["w"] / 2,
                       box["y"] + box["h"] / 2, size=12, color=pal["muted"],
                       align="center")
            return

        t0 = min(lobe[0][0] for lobe in lobes)
        t1 = max(lobe[-1][0] for lobe in lobes)
        # Pad the window so a single sit is not stretched edge-to-edge.
        span = max(3600, t1 - t0)
        pad = min(3 * 3600, span * 0.04)
        t0 -= pad
        t1 += pad
        span = t1 - t0

        self._grid_y(cr, box, pal, 0, 100, ticks=4, fmt=lambda v: f"{v:.0f}%")
        self._axis_frame(cr, box, pal)

        def xy(t, pct):
            x = box["x"] + (t - t0) / span * box["w"]
            y = box["y"] + (1 - max(0.0, min(100.0, pct)) / 100.0) * box["h"]
            return x, y

        # Flatten every sitting onto one polyline so nights / off-hours stay
        # on the same stroke. Color still flips at charge/use vertices.
        flat = [pt for lobe in lobes for pt in lobe]
        smooth = _display_curve(flat)
        curve = [xy(t, p) for t, p, _c in smooth]
        flags = [c for _t, _p, c in smooth]
        curve, flags = _decimate_xy(curve, flags)
        if len(curve) == 1:
            x, y = curve[0]
            _set_source(cr, pal["charge"] if flags[0] else pal["actual"])
            cr.arc(x, y, 2.8, 0, 2 * math.pi)
            cr.fill()
        else:
            cr.new_path()
            _trace_smooth(cr, curve)
            cr.line_to(curve[-1][0], box["y"] + box["h"])
            cr.line_to(curve[0][0], box["y"] + box["h"])
            cr.close_path()
            _set_source(cr, pal["actual_fill"])
            cr.fill()
            # One glow under the whole polyline so color joints never break.
            cr.new_path()
            _trace_smooth(cr, curve)
            _set_source(cr, pal["actual"], 0.16)
            cr.set_line_width(6.5)
            cr.stroke()
            for i, j, charging in _color_runs(curve, flags):
                piece = curve[i:j + 1]
                if len(piece) < 2:
                    continue
                cr.new_path()
                _trace_smooth(cr, piece)
                _set_source(cr, pal["charge"] if charging else pal["actual"])
                cr.set_line_width(2.3)
                cr.stroke()

        last_t, last_pct, last_chg = lobes[-1][-1]
        lx, ly = xy(last_t, last_pct)
        _set_source(cr, pal["bg"], 0.9)
        cr.arc(lx, ly, 4.6, 0, 2 * math.pi)
        cr.fill()
        _set_source(cr, pal["charge"] if last_chg else pal["actual"])
        cr.arc(lx, ly, 3.2, 0, 2 * math.pi)
        cr.fill()
        # Keep the label inside the plot so an isolated "now" dot isn't orphaned
        # in the right-hand margin.
        label = f"{last_pct:.0f}% now"
        label_x = min(lx - 8, box["x"] + box["w"] - 4)
        label_y = ly - 10 if ly > box["y"] + 18 else ly + 16
        if label_x < box["x"] + 56:
            label_x = min(lx + 10, box["x"] + box["w"] - 4)
            _pango_fit(cr, label, label_x, label_y, size=10, color=pal["text"])
        else:
            _pango_fit(cr, label, label_x, label_y,
                       size=10, color=pal["text"], align="right")

        # Midnight ticks for each local calendar day in the window (DST-safe).
        lt0 = time.localtime(t0)
        day = datetime.date(lt0.tm_year, lt0.tm_mon, lt0.tm_mday)
        midnight = time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))
        if midnight < t0:
            day += datetime.timedelta(days=1)
            midnight = time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))
        while midnight <= t1:
            x = box["x"] + (midnight - t0) / span * box["w"]
            _set_source(cr, pal["grid"], 0.2)
            cr.set_line_width(1.0)
            cr.move_to(x, box["y"])
            cr.line_to(x, box["y"] + box["h"])
            cr.stroke()
            label = time.strftime("%a %d", time.localtime(midnight)).replace(" 0", " ")
            _pango_fit(cr, label, x + 4, box["y"] + box["h"] + 16,
                       size=8.5, color=pal["muted"])
            day += datetime.timedelta(days=1)
            midnight = time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))

        self._legend_swatch(
            cr, box["x"] + box["w"] - 150, box["y"] + 10,
            pal["actual"], "using", pal["muted"])
        self._legend_swatch(
            cr, box["x"] + box["w"] - 78, box["y"] + 10,
            pal["charge"], "charging", pal["muted"])

    def _legend_swatch(self, cr, x, y, color, label, text_color=None):
        _set_source(cr, color)
        cr.rectangle(x, y - 3, 11, 2.2)
        cr.fill()
        _pango_fit(cr, label, x + 15, y + 1, size=8.5,
                   color=text_color or _PAL["muted"])


class ExpectActualChart(_ChartBase):
    """Side-by-side remaining runtime: expected (rated) vs actual (measured)."""

    __gtype_name__ = "G935ExpectActualChart"

    def __init__(self):
        super().__init__(title="", height=168, framed=False)
        self._bottom_label_h = 34

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
            _set_source(cr, pal["bar_bg"], 0.55)
            self._round_rect(cr, x, box["y"], bar_w, box["h"], 8)
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
            sign = "+" if delta_pct >= 0 else "-"
            if delta_pct >= 10:
                hc = pal["health_good"]
            elif delta_pct <= -15:
                hc = pal["health_bad"]
            else:
                hc = pal["muted"]
            badge = f"{sign}{abs(delta_pct):.0f}% vs rated"
            _pango_fit(cr, badge, box["x"] + box["w"] - 4, box["y"] + 12,
                       size=10.5, color=hc, align="right")

        self._axis_frame(cr, box, pal)


class SessionRuntimeChart(_ChartBase):
    """Per-session projected full runtime vs rated horizontal reference."""

    __gtype_name__ = "G935SessionRuntimeChart"

    def __init__(self):
        super().__init__(title="", height=168, framed=False)
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
        super().__init__(title="", height=150, framed=False)
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
        super().__init__(title="", height=156, framed=False)

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


