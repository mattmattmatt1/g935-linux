"""Pure-Python lighting waveform & RGB color math (no GTK/Cairo dependency)."""
from __future__ import annotations

import colorsys
import math


def eval_breathing_waveform(phase: float, waveform: int) -> float:
    """Return brightness multiplier in [0, 1] for phase in [0, 1)."""
    p = phase % 1.0
    if waveform in (0, 1):  # Default / Sine
        return 0.5 - 0.5 * math.cos(2.0 * math.pi * p)
    if waveform == 2:       # Square
        return 1.0 if p < 0.5 else 0.08
    if waveform == 3:       # Triangle
        return 2.0 * p if p < 0.5 else 2.0 * (1.0 - p)
    if waveform == 4:       # Sawtooth
        return p
    if waveform == 5:       # Shark fin (fast rise, slow decay)
        return (p / 0.22) if p < 0.22 else max(0.0, (1.0 - p) / 0.78) ** 1.6
    if waveform == 6:       # Exponential
        tri = 2.0 * p if p < 0.5 else 2.0 * (1.0 - p)
        return tri ** 2.6
    return 0.5 - 0.5 * math.cos(2.0 * math.pi * p)


def eval_zone_color(mode: int, rgb: tuple[int, int, int], period_ms: int,
                    intensity: int, waveform: int, now_s: float) -> tuple[float, float, float, float]:
    """Evaluate a lighting zone at ``now_s`` -> (r, g, b, glow_alpha) in [0, 1]."""
    if mode == 0:
        return (0.15, 0.17, 0.20, 0.0)
    r, g, b = rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
    period_s = max(0.4, float(period_ms or 5000) / 1000.0)
    # G935 uses 0–100 (or legacy 7–8 on default); scale gently so low intensity still previews
    scale = (
        max(0.18, min(1.0, float(intensity) / 100.0))
        if intensity > 10
        else max(0.35, min(1.0, float(intensity) / 10.0))
    )
    if mode == 1:  # Fixed
        return (r, g, b, 0.92)
    if mode == 2:  # Breathing
        level = eval_breathing_waveform((now_s / period_s) % 1.0, waveform)
        alpha = (0.10 + 0.90 * level) * scale
        return (r, g, b, alpha)
    if mode == 3:  # Cycling
        hue = (now_s / period_s) % 1.0
        cr, cg, cb = colorsys.hsv_to_rgb(hue, 0.92, 1.0)
        return (cr, cg, cb, 0.90 * scale)
    return (r, g, b, 0.0)
