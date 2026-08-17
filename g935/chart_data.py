"""Pure helpers for battery-health charts (no GTK)."""
from __future__ import annotations

import time as _time

# Headset-off / panel-closed holes longer than this are separate wears, not
# a continuous discharge. Connecting them with a line invents drain.
HISTORY_GAP_S = 20 * 60


def resolve_recent_cells(recent, full_mv=4200):
    """[(t, cell_mv, charging)] with charge-rail SoC held at last rest."""
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
    return resolved


def split_history_runs(points, gap_s=HISTORY_GAP_S):
    """Split [(t, ...), ...] into contiguous runs. Empty / single-empty → []."""
    if not points:
        return []
    runs = [[points[0]]]
    for prev, pt in zip(points, points[1:]):
        if pt[0] - prev[0] > gap_s:
            runs.append([pt])
        else:
            runs[-1].append(pt)
    return runs


def _downsample(points, max_points, value_fn):
    if not points:
        return []
    if len(points) <= max_points:
        return [value_fn(p) for p in points]
    step = max(1, len(points) // max_points)
    out = [value_fn(points[i]) for i in range(0, len(points), step)]
    last = value_fn(points[-1])
    if not out or out[-1][0] != last[0]:
        out.append(last)
    return out


def build_history_runs(recent, batt_percent_fn, max_points=400, full_mv=4200,
                       gap_s=HISTORY_GAP_S):
    """Downsample recent samples into gap-split runs of [(t, pct, charging)].

    Each run is one sitting. Overnight / multi-hour holes are not connected.
    """
    resolved = resolve_recent_cells(recent, full_mv=full_mv)
    raw_runs = split_history_runs(resolved, gap_s=gap_s)
    total = sum(len(r) for r in raw_runs) or 1
    runs = []
    for raw in raw_runs:
        budget = max(2, int(round(max_points * len(raw) / total)))
        if len(raw) == 1:
            budget = 1
        else:
            duration = raw[-1][0] - raw[0][0]
            if duration >= 15 * 60:
                budget = max(budget, 16)
            if duration >= 40 * 60:
                budget = max(budget, 28)

        def as_pct(p, _fn=batt_percent_fn):
            t, cell, chg = p
            return (t, int(_fn(cell)), chg)

        runs.append(_downsample(raw, budget, as_pct))
    return runs


def build_history_points(recent, batt_percent_fn, max_points=400, full_mv=4200):
    """Flat downsample (tests / callers that do not need gap splits)."""
    runs = build_history_runs(
        recent, batt_percent_fn, max_points=max_points, full_mv=full_mv)
    return [pt for run in runs for pt in run]


# Charge sits can miss samples for a bit and still be one interpolation.
LEVEL_CONNECT_S = 3 * 3600
# Rest-after is "the unplug" only if it lands soon after the last charge sample.
# A morning sample after an overnight dock is the *result*, not the rise window.
CHARGE_REST_AFTER_S = 45 * 60
# Overnight dock: next rest may be many hours later. Trust it only when SoC
# actually rose and the hole is one night, not two days of unseen wear.
CHARGE_REST_TRUST_S = 16 * 3600
CHARGE_REST_MIN_DPCT = 15
# Logitech empty-to-full is ~2 h. Never draw a charge rise longer than this —
# leftover time until the next rest is blank (headset idle / panel closed).
CHARGE_RISE_MAX_S = 2 * 3600 + 30 * 60
# Holes longer than this stay flat at the last known %, then ease into the
# next sample. A 2-day diagonal is not a battery graph.
LEVEL_PROJECT_S = 8 * 3600
LEVEL_EASE_S = 45 * 60


def split_by_local_day(points):
    """One run per local calendar day (tests / callers that want date groups)."""
    if not points:
        return []
    runs = [[points[0]]]
    prev_day = _time.localtime(points[0][0])[:3]
    for pt in points[1:]:
        day = _time.localtime(pt[0])[:3]
        if day != prev_day:
            runs.append([pt])
            prev_day = day
        else:
            runs[-1].append(pt)
    return runs


def _charge_rise_points(t0, t1, start_pct, end_pct, step_s=120, charging=True):
    """Evenly spaced (t, pct, charging) from start_pct → end_pct over [t0, t1]."""
    t0, t1 = int(t0), int(t1)
    if t1 < t0:
        t1 = t0
    span = max(1, t1 - t0)
    times = list(range(t0, t1, step_s))
    if not times or times[-1] != t1:
        times.append(t1)
    out = []
    for tk in times:
        u = (tk - t0) / span
        out.append((tk, start_pct + u * (end_pct - start_pct), bool(charging)))
    return out


def _bridge_level_holes(series, hole_s=HISTORY_GAP_S,
                        rise_max_s=CHARGE_RISE_MAX_S,
                        rise_min_dpct=CHARGE_REST_MIN_DPCT,
                        trust_s=CHARGE_REST_TRUST_S,
                        project_s=LEVEL_PROJECT_S,
                        ease_s=LEVEL_EASE_S):
    """One continuous line: project short holes, hold+ease long ones.

    Overnight SoC gains: short green rise, then hold at the charged %.
    Same-day / overnight holes: project last known % → next known %.
    Multi-day holes: stay flat, then ease into the next sample so the
    line never lifts and never draws a 2-day diagonal.
    """
    if len(series) < 2:
        return series
    out = [series[0]]
    for prev, pt in zip(series, series[1:]):
        dt = pt[0] - prev[0]
        if dt <= hole_s:
            out.append(pt)
            continue
        dp = pt[1] - prev[1]
        if dp >= rise_min_dpct and dt <= trust_s:
            t_rise = prev[0] + min(int(rise_max_s), dt)
            out.extend(_charge_rise_points(
                prev[0], t_rise, prev[1], pt[1])[1:])
            if pt[0] > t_rise + 30:
                out.extend(_charge_rise_points(
                    t_rise, pt[0], pt[1], pt[1], charging=False)[1:-1])
            out.append(pt)
        elif dt <= project_s:
            out.extend(_charge_rise_points(
                prev[0], pt[0], prev[1], pt[1], charging=False)[1:-1])
            out.append(pt)
        else:
            t_ease = pt[0] - min(int(ease_s), max(60, dt // 4))
            if t_ease > prev[0] + 60:
                out.extend(_charge_rise_points(
                    prev[0], t_ease, prev[1], prev[1], charging=False)[1:-1])
            out.extend(_charge_rise_points(
                t_ease, pt[0], prev[1], pt[1], charging=False)[1:-1])
            out.append(pt)
    return out


def build_level_series(recent, batt_percent_fn, full_mv=4200,
                       window_s=7 * 86400, gap_s=LEVEL_CONNECT_S,
                       rest_after_s=CHARGE_REST_AFTER_S,
                       rest_trust_s=CHARGE_REST_TRUST_S,
                       rest_min_dpct=CHARGE_REST_MIN_DPCT,
                       rise_max_s=CHARGE_RISE_MAX_S,
                       split_gap_s=None):
    """Battery % over a recent window as one connected stroke.

    Charge sits rise from the last rest % to the rest % after unplug (the ADC
    rail is not used). The rise is drawn over the real unplug window when that
    rest lands soon, otherwise over at most ``rise_max_s`` starting at plug-in
    — never compressed into a 2-minute spike, and never stretched overnight.
    Holes stay on the same line so the week reads as one graph; ``split_gap_s``
    is accepted for callers that still want sitting splits.
    """
    resolved = resolve_recent_cells(recent, full_mv=full_mv)
    if not resolved:
        return []
    cutoff = resolved[-1][0] - window_s
    samples = [row for row in resolved if row[0] >= cutoff]
    if not samples:
        return []

    series = []
    last_rest_pct = None
    # Seed rest from anything just before the window so a charge at the
    # cutoff still has a starting %.
    for t, cell, chg in resolved:
        if t >= cutoff:
            break
        if not chg:
            last_rest_pct = int(batt_percent_fn(cell))

    i = 0
    n = len(samples)
    while i < n:
        t, cell, chg = samples[i]
        if not chg:
            last_rest_pct = int(batt_percent_fn(cell))
            series.append((int(t), last_rest_pct, False))
            i += 1
            continue
        j = i + 1
        while j < n and samples[j][2]:
            if samples[j][0] - samples[j - 1][0] > gap_s:
                break
            j += 1
        start_pct = last_rest_pct
        rest_after = None
        dt_rest = 0
        if j < n and not samples[j][2]:
            rest_after = samples[j]
        t0 = int(samples[i][0])
        t_last = int(samples[j - 1][0])

        if start_pct is None:
            # No rest baseline: hold the sanitized cell % (never the rail).
            for k in range(i, j):
                tk, cell_k, _ = samples[k]
                series.append((int(tk), int(batt_percent_fn(cell_k)), True))
            i = j
            continue

        if rest_after is not None:
            cand_pct = int(batt_percent_fn(rest_after[1]))
            dt_rest = int(rest_after[0]) - t_last
            gained = cand_pct - start_pct
            if dt_rest <= rest_after_s:
                end_pct = cand_pct
                t_target = int(rest_after[0])
            elif (dt_rest <= rest_trust_s and gained >= rest_min_dpct):
                # Overnight dock: we never saw the unplug, but morning SoC
                # is the charge result. Draw the rise over rise_max_s.
                end_pct = cand_pct
                t_target = t0 + int(rise_max_s)
            else:
                # Next rest is days later or didn't rise — unseen wear, not
                # this sit's unplug.
                end_pct = start_pct
                t_target = t_last
        else:
            end_pct = start_pct
            t_target = t_last

        # Rise at most rise_max_s (rated empty-to-full ~2 h). Dock-idle after
        # that is a flat hold, not a 7-hour climb to unplug.
        t_rise_end = t0 + min(int(rise_max_s), max(0, t_target - t0))
        if end_pct != start_pct and t_rise_end > t0:
            series.extend(_charge_rise_points(t0, t_rise_end, start_pct, end_pct))
            hold_from = t_rise_end
        else:
            series.append((t0, float(start_pct), True))
            hold_from = t0
        hold_until = t_last if rest_after is None or dt_rest > rest_after_s else t_target
        if hold_until > hold_from + 60:
            series.extend(_charge_rise_points(
                hold_from, hold_until, end_pct, end_pct))
        i = j
    if split_gap_s is not None:
        return split_history_runs(series, gap_s=split_gap_s)
    if not series:
        return []
    return [_bridge_level_holes(series)]


def build_day_summaries(recent, batt_percent_fn, full_mv=4200, max_days=21):
    """One row per calendar day: min / max / last SoC and whether it charged.

    Empty calendar days in the span are included so the x-axis is real time.
    """
    resolved = resolve_recent_cells(recent, full_mv=full_mv)
    if not resolved:
        return []
    by_day = {}
    for t, cell, chg in resolved:
        tm = _time.localtime(t)
        day = _time.strftime("%Y-%m-%d", tm)
        rec = by_day.setdefault(day, {"pcts": [], "charged": False, "last": None})
        pct = int(batt_percent_fn(cell))
        rec["pcts"].append(pct)
        rec["last"] = pct
        if chg:
            rec["charged"] = True
    first = min(by_day)
    last = max(by_day)
    start = _time.mktime(_time.strptime(first, "%Y-%m-%d"))
    end = _time.mktime(_time.strptime(last, "%Y-%m-%d"))
    days = []
    t = start
    while t <= end + 3600:
        key = _time.strftime("%Y-%m-%d", _time.localtime(t))
        if days and days[-1]["day"] == key:
            t += 86400
            continue
        rec = by_day.get(key)
        if rec:
            days.append({
                "day": key,
                "min_pct": min(rec["pcts"]),
                "max_pct": max(rec["pcts"]),
                "last_pct": rec["last"],
                "charged": rec["charged"],
                "samples": len(rec["pcts"]),
            })
        else:
            days.append({
                "day": key,
                "min_pct": None,
                "max_pct": None,
                "last_pct": None,
                "charged": False,
                "samples": 0,
            })
        t += 86400
    return days[-max_days:]


def build_day_ridges(recent, batt_percent_fn, full_mv=4200, max_days=21,
                     gap_s=HISTORY_GAP_S):
    """Group samples into calendar-day lobes for a ridgeline chart.

    Each day: ``{"day": "YYYY-MM-DD", "lobes": [[(hour, pct, charging), ...]]}``.
    ``hour`` is local clock time in [0, 24). Gaps longer than ``gap_s`` split
    a day into separate lobes so we never draw invented drain.
    """
    resolved = resolve_recent_cells(recent, full_mv=full_mv)
    by_day = {}
    order = []
    for t, cell, chg in resolved:
        tm = _time.localtime(t)
        day = _time.strftime("%Y-%m-%d", tm)
        hour = tm.tm_hour + tm.tm_min / 60.0 + tm.tm_sec / 3600.0
        if day not in by_day:
            by_day[day] = []
            order.append(day)
        by_day[day].append((hour, int(batt_percent_fn(cell)), bool(chg), int(t)))
    out = []
    for day in order[-max_days:]:
        pts = by_day[day]
        lobes = [[pts[0]]]
        for prev, pt in zip(pts, pts[1:]):
            if pt[3] - prev[3] > gap_s:
                lobes.append([pt])
            else:
                lobes[-1].append(pt)
        out.append({
            "day": day,
            "lobes": [[(h, p, c) for h, p, c, _t in lobe] for lobe in lobes],
        })
    return out


def build_expected_overlay(points, rated_runtime_h):
    """Dashed rated-discharge reference for a single contiguous sitting.

    Starts at the first discharge sample's % and declines at 100/rated %/h.
    Do not pass a multi-day flattened history — the slope is only meaningful
    inside one wear.
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
