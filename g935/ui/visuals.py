"""Custom Cairo centerpiece widgets: EQ response curve, RGB earcup preview, animated battery."""
from __future__ import annotations

import math
import time

import cairo
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk

from g935.ui.lighting_math import eval_breathing_waveform, eval_zone_color


def _round_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _theme_fg_bg(widget):
    fg = (0.88, 0.90, 0.93)
    bg = (0.14, 0.15, 0.18)
    try:
        ctx = widget.get_style_context()
        ok, c = ctx.lookup_color("theme_fg_color")
        if ok:
            fg = (c.red, c.green, c.blue)
        ok, c = ctx.lookup_color("theme_bg_color")
        if ok:
            bg = (c.red, c.green, c.blue)
    except Exception:
        pass
    return fg, bg


class EqCurveWidget(Gtk.DrawingArea):
    """Live smooth frequency-response curve for the 10-band Hardware EQ."""

    __gtype_name__ = "G935EqCurveWidget"

    def __init__(self, band_names=None):
        super().__init__()
        self._bands = list(band_names or ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"])
        self._gains = [0.0] * len(self._bands)
        self._min_db = -12.0
        self._max_db = 12.0
        self.set_size_request(260, 108)
        self.set_hexpand(True)
        self.connect("draw", self._on_draw)

    def set_gains(self, gains: list[float], band_names=None,
                  min_db: float = -12.0, max_db: float = 12.0) -> None:
        if band_names:
            self._bands = list(band_names)
        self._gains = [float(v) for v in gains]
        self._min_db = float(min_db)
        self._max_db = float(max_db)
        self.queue_draw()

    def _on_draw(self, widget, cr) -> bool:
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        if w < 60 or h < 40 or not self._gains:
            return False

        fg, bg = _theme_fg_bg(widget)
        cr.set_antialias(cairo.ANTIALIAS_GOOD)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        # Subtle sunken plot backdrop
        _round_rect(cr, 0.5, 0.5, w - 1, h - 1, 9)
        cr.set_source_rgba(bg[0] * 0.72, bg[1] * 0.75, bg[2] * 0.80, 0.55)
        cr.fill_preserve()
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.09)
        cr.set_line_width(1.0)
        cr.stroke()

        pad_l, pad_r, pad_t, pad_b = 34, 16, 12, 14
        pw = w - pad_l - pad_r
        ph = h - pad_t - pad_b
        span_db = max(1.0, self._max_db - self._min_db)

        def y_for(db: float) -> float:
            frac = (db - self._min_db) / span_db
            return pad_t + (1.0 - max(0.0, min(1.0, frac))) * ph

        y0 = y_for(0.0)

        # Horizontal dB gridlines
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(8.5)
        for db in (self._max_db, self._max_db / 2.0, 0.0, self._min_db / 2.0, self._min_db):
            yy = y_for(db)
            is_zero = abs(db) < 1e-6
            cr.set_source_rgba(fg[0], fg[1], fg[2], 0.24 if is_zero else 0.08)
            cr.set_line_width(1.1 if is_zero else 0.8)
            if not is_zero:
                cr.set_dash([3, 3])
            cr.move_to(pad_l, yy)
            cr.line_to(pad_l + pw, yy)
            cr.stroke()
            cr.set_dash([])
            if db in (self._max_db, 0.0, self._min_db):
                lbl = f"{int(db):+d}" if db != 0 else "0"
                ext = cr.text_extents(lbl)
                cr.set_source_rgba(fg[0], fg[1], fg[2], 0.50)
                cr.move_to(pad_l - 6 - ext.width, yy + 3)
                cr.show_text(lbl)

        n = len(self._gains)
        pts = [
            (pad_l + (i / max(1, n - 1)) * pw, y_for(g))
            for i, g in enumerate(self._gains)
        ]

        # Vertical subtle band guides
        for x, _y in pts:
            cr.set_source_rgba(fg[0], fg[1], fg[2], 0.05)
            cr.set_line_width(1.0)
            cr.move_to(x, pad_t)
            cr.line_to(x, pad_t + ph)
            cr.stroke()

        def trace_curve():
            cr.move_to(pts[0][0], pts[0][1])
            for i in range(len(pts) - 1):
                x0, py0 = pts[i]
                x1, py1 = pts[i + 1]
                cx = (x0 + x1) * 0.5
                cr.curve_to(cx, py0, cx, py1, x1, py1)

        # Gradient fill from curve to 0 dB line
        cr.save()
        cr.new_path()
        trace_curve()
        cr.line_to(pts[-1][0], y0)
        cr.line_to(pts[0][0], y0)
        cr.close_path()
        grad = cairo.LinearGradient(0, pad_t, 0, pad_t + ph)
        grad.add_color_stop_rgba(0.0, 0.22, 0.62, 0.98, 0.42)
        grad.add_color_stop_rgba(0.5, 0.22, 0.62, 0.98, 0.08)
        grad.add_color_stop_rgba(1.0, 0.45, 0.36, 0.95, 0.34)
        cr.set_source(grad)
        cr.fill()
        cr.restore()

        # Soft outer glow stroke
        cr.new_path()
        trace_curve()
        cr.set_source_rgba(0.28, 0.68, 0.98, 0.22)
        cr.set_line_width(6.5)
        cr.stroke()

        # Crisp main response line
        cr.new_path()
        trace_curve()
        line_grad = cairo.LinearGradient(pad_l, 0, pad_l + pw, 0)
        line_grad.add_color_stop_rgba(0.0, 0.30, 0.80, 0.95, 0.95)
        line_grad.add_color_stop_rgba(0.5, 0.25, 0.58, 0.98, 0.95)
        line_grad.add_color_stop_rgba(1.0, 0.56, 0.44, 0.98, 0.95)
        cr.set_source(line_grad)
        cr.set_line_width(2.4)
        cr.stroke()

        # Glowing band nodes
        for (x, y), g in zip(pts, self._gains):
            if abs(g) > 0.01:
                cr.set_source_rgba(0.32, 0.72, 1.0, 0.28)
                cr.arc(x, y, 5.4, 0, 2 * math.pi)
                cr.fill()
            cr.set_source_rgba(0.92, 0.96, 1.0, 0.95)
            cr.arc(x, y, 2.7, 0, 2 * math.pi)
            cr.fill()

        return False


class HeadsetLightingPreview(Gtk.DrawingArea):
    """Animated live preview of the Logitech G935 earcup Logo + front RGB Strip."""

    __gtype_name__ = "G935HeadsetLightingPreview"

    def __init__(self):
        super().__init__()
        self._zones = {
            0: {"mode": 2, "rgb": (0, 180, 255), "period_ms": 5000, "intensity": 100, "waveform": 0},
            1: {"mode": 2, "rgb": (0, 180, 255), "period_ms": 5000, "intensity": 100, "waveform": 0},
        }
        self._anim_id: int | None = None
        self._active = False
        self.set_size_request(360, 196)
        self.set_hexpand(True)
        self.connect("draw", self._on_draw)
        self.connect("destroy", lambda *_: self.set_animating(False))

    def set_zone_state(self, zone: int, mode: int, rgb: tuple[int, int, int],
                       period_ms: int, intensity: int, waveform: int) -> None:
        self._zones[int(zone)] = {
            "mode": int(mode),
            "rgb": tuple(int(c) for c in rgb),
            "period_ms": int(period_ms),
            "intensity": int(intensity),
            "waveform": int(waveform),
        }
        self.queue_draw()

    def set_animating(self, active: bool) -> None:
        active = bool(active)
        self._active = active
        if active and self._anim_id is None:
            self._anim_id = GLib.timeout_add(33, self._tick)
            self.queue_draw()
        elif not active and self._anim_id is not None:
            GLib.source_remove(self._anim_id)
            self._anim_id = None

    def _tick(self) -> bool:
        if not self._active:
            self._anim_id = None
            return False
        # Only redraw at 30fps if at least one zone is animated (Breathing / Cycling)
        if any(z["mode"] in (2, 3) for z in self._zones.values()):
            self.queue_draw()
        return True

    def _draw_g_logo(self, cr, cx: float, cy: float, r: float,
                     color: tuple[float, float, float, float]) -> None:
        rc, gc, bc, alpha = color
        if alpha > 0.02:
            glow = cairo.RadialGradient(cx, cy, r * 0.15, cx, cy, r * 2.5)
            glow.add_color_stop_rgba(0.0, rc, gc, bc, 0.55 * alpha)
            glow.add_color_stop_rgba(0.45, rc, gc, bc, 0.18 * alpha)
            glow.add_color_stop_rgba(1.0, rc, gc, bc, 0.0)
            cr.set_source(glow)
            cr.arc(cx, cy, r * 2.5, 0, 2 * math.pi)
            cr.fill()

        # Stylized geometric Logitech 'G' glyph
        cr.save()
        cr.set_line_width(max(2.6, r * 0.24))
        cr.set_line_cap(cairo.LINE_CAP_SQUARE)
        if alpha > 0.02:
            # Outer bloom stroke
            cr.set_source_rgba(rc, gc, bc, 0.35 * alpha)
            cr.set_line_width(max(5.5, r * 0.44))
            cr.new_sub_path()
            cr.arc(cx, cy, r, math.radians(15), math.radians(310))
            cr.stroke()
            cr.move_to(cx + r * 0.05, cy + r * 0.05)
            cr.line_to(cx + r * 0.92, cy + r * 0.05)
            cr.stroke()

            cr.set_source_rgba(
                min(1.0, rc * 0.8 + 0.2),
                min(1.0, gc * 0.8 + 0.2),
                min(1.0, bc * 0.8 + 0.2),
                0.25 + 0.75 * alpha,
            )
            cr.set_line_width(max(2.6, r * 0.23))
        else:
            cr.set_source_rgba(0.32, 0.35, 0.40, 0.55)
        cr.new_sub_path()
        cr.arc(cx, cy, r, math.radians(15), math.radians(310))
        cr.stroke()
        cr.move_to(cx + r * 0.05, cy + r * 0.05)
        cr.line_to(cx + r * 0.92, cy + r * 0.05)
        cr.stroke()
        cr.restore()

    def _on_draw(self, widget, cr) -> bool:
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        if w < 100 or h < 80:
            return False

        fg, _bg = _theme_fg_bg(widget)
        now = time.monotonic()
        z0 = self._zones.get(0, {})
        z1 = self._zones.get(1, {})
        c_logo = eval_zone_color(
            z0.get("mode", 0), z0.get("rgb", (0, 180, 255)),
            z0.get("period_ms", 5000), z0.get("intensity", 100),
            z0.get("waveform", 0), now)
        c_strip = eval_zone_color(
            z1.get("mode", 0), z1.get("rgb", (0, 180, 255)),
            z1.get("period_ms", 5000), z1.get("intensity", 100),
            z1.get("waveform", 0), now)

        cr.set_antialias(cairo.ANTIALIAS_GOOD)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        # Dark studio showcase stage
        _round_rect(cr, 0.5, 0.5, w - 1, h - 1, 12)
        stage = cairo.LinearGradient(0, 0, w, h)
        stage.add_color_stop_rgba(0.0, 0.09, 0.10, 0.13, 0.95)
        stage.add_color_stop_rgba(1.0, 0.05, 0.06, 0.08, 0.98)
        cr.set_source(stage)
        cr.fill_preserve()
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.10)
        cr.set_line_width(1.0)
        cr.stroke()

        # Ambient bounce light onto stage from Strip + Logo
        cx = w * 0.5
        cy = h * 0.52
        cup_h = min(h * 0.72, 134.0)
        cup_w = cup_h * 0.88

        if c_strip[3] > 0.02:
            amb = cairo.RadialGradient(
                cx + cup_w * 0.42, cy, 8,
                cx + cup_w * 0.42, cy, cup_h * 1.15)
            amb.add_color_stop_rgba(0.0, c_strip[0], c_strip[1], c_strip[2], 0.24 * c_strip[3])
            amb.add_color_stop_rgba(1.0, c_strip[0], c_strip[1], c_strip[2], 0.0)
            cr.save()
            _round_rect(cr, 1, 1, w - 2, h - 2, 12)
            cr.clip()
            cr.set_source(amb)
            cr.paint()
            cr.restore()

        if c_logo[3] > 0.02:
            amb0 = cairo.RadialGradient(
                cx - cup_w * 0.05, cy, 8,
                cx - cup_w * 0.05, cy, cup_h * 0.95)
            amb0.add_color_stop_rgba(0.0, c_logo[0], c_logo[1], c_logo[2], 0.18 * c_logo[3])
            amb0.add_color_stop_rgba(1.0, c_logo[0], c_logo[1], c_logo[2], 0.0)
            cr.save()
            _round_rect(cr, 1, 1, w - 2, h - 2, 12)
            cr.clip()
            cr.set_source(amb0)
            cr.paint()
            cr.restore()

        # Stylized yoke / headband stem at top
        cr.set_source_rgba(0.18, 0.20, 0.24, 1.0)
        _round_rect(cr, cx - cup_w * 0.14, 0, cup_w * 0.28, cy - cup_h * 0.36, 4)
        cr.fill()

        # Soft earpad rim behind left edge
        cr.set_source_rgba(0.13, 0.14, 0.17, 1.0)
        _round_rect(cr, cx - cup_w * 0.62, cy - cup_h * 0.48, cup_w * 0.30, cup_h * 0.96, 18)
        cr.fill()

        # Main G935 angular matte earcup shell
        shell_x = cx - cup_w * 0.44
        shell_y = cy - cup_h * 0.46
        shell_w = cup_w * 0.92
        shell_h = cup_h * 0.92
        _round_rect(cr, shell_x, shell_y, shell_w, shell_h, 20)
        shell_grad = cairo.LinearGradient(shell_x, shell_y, shell_x + shell_w, shell_y + shell_h)
        shell_grad.add_color_stop_rgb(0.0, 0.20, 0.22, 0.26)
        shell_grad.add_color_stop_rgb(0.55, 0.14, 0.15, 0.18)
        shell_grad.add_color_stop_rgb(1.0, 0.10, 0.11, 0.14)
        cr.set_source(shell_grad)
        cr.fill_preserve()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.09)
        cr.set_line_width(1.2)
        cr.stroke()

        # Subtle inner bevel plate
        _round_rect(cr, shell_x + 9, shell_y + 9, shell_w - 22, shell_h - 18, 14)
        cr.set_source_rgba(0.08, 0.09, 0.11, 0.55)
        cr.fill_preserve()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.04)
        cr.set_line_width(1.0)
        cr.stroke()

        # Zone 0: G-Logo
        logo_cx = shell_x + (shell_w - 16) * 0.48
        logo_cy = cy
        self._draw_g_logo(cr, logo_cx, logo_cy, cup_h * 0.16, c_logo)

        # Zone 1: Signature G935 curved/angled front LED strip along right edge
        sx = shell_x + shell_w - 11
        sy_top = shell_y + 15
        sy_bot = shell_y + shell_h - 15
        sr, sg, sb, sa = c_strip

        def trace_strip():
            cr.move_to(sx - 6, sy_top)
            cr.curve_to(sx + 4, sy_top + (sy_bot - sy_top) * 0.25,
                        sx + 4, sy_top + (sy_bot - sy_top) * 0.75,
                        sx - 6, sy_bot)

        if sa > 0.02:
            # Wide soft bloom
            cr.new_path()
            trace_strip()
            cr.set_source_rgba(sr, sg, sb, 0.22 * sa)
            cr.set_line_width(18.0)
            cr.stroke()

            cr.new_path()
            trace_strip()
            cr.set_source_rgba(sr, sg, sb, 0.48 * sa)
            cr.set_line_width(9.0)
            cr.stroke()

            cr.new_path()
            trace_strip()
            cr.set_source_rgba(
                min(1.0, sr * 0.72 + 0.28),
                min(1.0, sg * 0.72 + 0.28),
                min(1.0, sb * 0.72 + 0.28),
                0.35 + 0.65 * sa,
            )
            cr.set_line_width(4.2)
            cr.stroke()
        else:
            cr.new_path()
            trace_strip()
            cr.set_source_rgba(0.28, 0.30, 0.35, 0.6)
            cr.set_line_width(4.0)
            cr.stroke()

        # Subtle callout labels on left/right of stage
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10.0)
        z0_name = ("Off", "Fixed", "Breathing", "Cycling")[min(3, max(0, z0.get("mode", 0)))]
        z1_name = ("Off", "Fixed", "Breathing", "Cycling")[min(3, max(0, z1.get("mode", 0)))]

        # Left label: Logo
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.75)
        cr.move_to(16, h * 0.44)
        cr.show_text("Logo (Zone 0)")
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9.0)
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.50)
        cr.move_to(16, h * 0.44 + 14)
        cr.show_text(z0_name)

        # Right label: Strip
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10.0)
        lbl1 = "Strip (Zone 1)"
        ext1 = cr.text_extents(lbl1)
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.75)
        cr.move_to(w - 16 - ext1.width, h * 0.44)
        cr.show_text(lbl1)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9.0)
        ext1s = cr.text_extents(z1_name)
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.50)
        cr.move_to(w - 16 - ext1s.width, h * 0.44 + 14)
        cr.show_text(z1_name)

        return False


class AnimatedBatteryWidget(Gtk.DrawingArea):
    """Animated liquid-fill & charging-pulse battery graphic for Battery Health."""

    __gtype_name__ = "G935AnimatedBatteryWidget"

    def __init__(self):
        super().__init__()
        self._pct: int | None = None
        self._charging: bool | None = None
        self._offline = False
        self._anim_id: int | None = None
        self._active = False
        self.set_size_request(116, 62)
        self.connect("draw", self._on_draw)
        self.connect("destroy", lambda *_: self.set_animating(False))

    def set_state(self, pct: int | None, charging: bool | None,
                  offline: bool = False) -> None:
        self._pct = None if pct is None else max(0, min(100, int(pct)))
        self._charging = charging
        self._offline = bool(offline)
        self.queue_draw()

    def set_animating(self, active: bool) -> None:
        active = bool(active)
        self._active = active
        if active and self._anim_id is None:
            self._anim_id = GLib.timeout_add(40, self._tick)
            self.queue_draw()
        elif not active and self._anim_id is not None:
            GLib.source_remove(self._anim_id)
            self._anim_id = None

    def _tick(self) -> bool:
        if not self._active:
            self._anim_id = None
            return False
        if not self._offline and self._pct is not None:
            self.queue_draw()
        return True

    def _fill_color(self):
        if self._offline or self._pct is None:
            return (0.45, 0.48, 0.52)
        if self._charging:
            return (0.18, 0.78, 0.49)
        if self._pct <= 10:
            return (0.88, 0.18, 0.20)
        if self._pct <= 25:
            return (0.92, 0.65, 0.12)
        if self._pct <= 50:
            return (0.24, 0.60, 0.94)
        return (0.18, 0.76, 0.49)

    def _on_draw(self, widget, cr) -> bool:
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        if w < 60 or h < 36:
            return False

        fg, _bg = _theme_fg_bg(widget)
        now = time.monotonic()
        cr.set_antialias(cairo.ANTIALIAS_GOOD)

        cap_w = 7.0
        pad = 3.0
        bx = pad
        by = pad + 2.0
        bw = w - pad * 2 - cap_w - 2.0
        bh = h - (pad + 2.0) * 2
        r = 10.0

        # Terminal cap on right
        cap_h = bh * 0.42
        _round_rect(cr, bx + bw + 2.0, by + (bh - cap_h) / 2.0, cap_w, cap_h, 3.0)
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.35)
        cr.fill()

        # Shell background
        _round_rect(cr, bx, by, bw, bh, r)
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.06)
        cr.fill()

        # Fill bar clipped inside shell
        col = self._fill_color()
        if not self._offline and self._pct is not None and self._pct > 0:
            inset = 4.0
            ix, iy = bx + inset, by + inset
            iw, ih = bw - inset * 2, bh - inset * 2
            fill_w = max(6.0, iw * (self._pct / 100.0))

            cr.save()
            _round_rect(cr, ix, iy, iw, ih, max(4.0, r - 3.0))
            cr.clip()

            # Animated liquid meniscus edge on right side of fill
            pulse = 1.0
            if self._pct <= 10 and not self._charging:
                pulse = 0.65 + 0.35 * (0.5 + 0.5 * math.sin(now * 6.0))
            elif self._charging:
                pulse = 0.88 + 0.12 * (0.5 + 0.5 * math.sin(now * 3.5))

            wave_amp = 3.2 if (self._pct < 98) else 0.0
            cr.new_path()
            cr.move_to(ix, iy)
            cr.line_to(ix + fill_w, iy)
            steps = 12
            for k in range(steps + 1):
                frac = k / steps
                yy = iy + frac * ih
                wx = wave_amp * math.sin(frac * math.pi * 2.0 + now * 4.2)
                cr.line_to(ix + fill_w + wx, yy)
            cr.line_to(ix, iy + ih)
            cr.close_path()

            grad = cairo.LinearGradient(ix, iy, ix + fill_w, iy + ih)
            grad.add_color_stop_rgba(0.0, col[0], col[1], col[2], 0.85 * pulse)
            grad.add_color_stop_rgba(1.0, min(1.0, col[0] + 0.12), min(1.0, col[1] + 0.12), min(1.0, col[2] + 0.10), 0.96 * pulse)
            cr.set_source(grad)
            cr.fill()

            # Animated charging shimmer sweep
            if self._charging:
                sweep_x = ix - 28.0 + ((now * 68.0) % (fill_w + 56.0))
                shimmer = cairo.LinearGradient(sweep_x - 20, 0, sweep_x + 20, 0)
                shimmer.add_color_stop_rgba(0.0, 1.0, 1.0, 1.0, 0.0)
                shimmer.add_color_stop_rgba(0.5, 1.0, 1.0, 1.0, 0.32)
                shimmer.add_color_stop_rgba(1.0, 1.0, 1.0, 1.0, 0.0)
                cr.set_source(shimmer)
                cr.rectangle(ix, iy, fill_w, ih)
                cr.fill()

            cr.restore()

        # Crisp shell border
        _round_rect(cr, bx, by, bw, bh, r)
        border_a = 0.55 if self._charging else 0.28
        bc = col if (self._charging or (self._pct is not None and self._pct <= 10 and not self._offline)) else fg
        cr.set_source_rgba(bc[0], bc[1], bc[2], border_a)
        cr.set_line_width(1.8)
        cr.stroke()

        # Center percentage text (+ bolt when charging)
        label = "—" if (self._offline or self._pct is None) else (
            f"⚡{self._pct}%" if self._charging else f"{self._pct}%"
        )
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(14.0)
        ext = cr.text_extents(label)
        tx = bx + (bw - ext.width) / 2.0 - ext.x_bearing
        ty = by + (bh - ext.height) / 2.0 - ext.y_bearing
        # Subtle shadow for contrast over bright fill
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.45)
        cr.move_to(tx, ty + 1.0)
        cr.show_text(label)
        cr.set_source_rgba(0.98, 0.99, 1.0, 0.96 if not self._offline else 0.45)
        cr.move_to(tx, ty)
        cr.show_text(label)

        return False
