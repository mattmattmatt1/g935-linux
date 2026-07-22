"""Battery voltage → % curve, discharge profile learning, and runtime estimates.

Only voltage + charging flag exist (no coulomb counter). Health is an honest
extrapolation: observed drain over sessions → projected full-to-empty runtime,
compared against the rated runtime spec. A learned per-voltage-bin drain
profile is built from dense datapoints over time for better remaining-ETA.
"""
from __future__ import annotations

import json
import os
import statistics
import time

from g935.paths import config_dir, ensure_config_dir

BATT_CURVE = [
    (4200, 100), (4060, 90), (3980, 80), (3920, 70), (3870, 60),
    (3820, 50), (3790, 40), (3760, 30), (3730, 20), (3670, 10),
    (3500, 0),
]

HEALTH_DEFAULTS = {
    "tracking": True,
    "design_capacity_mah": 1100,       # stock cell 533-000132
    "rated_runtime_h_rgb_on": 8.0,     # Logitech spec, default RGB, 50% vol
    "rated_runtime_h_rgb_off": 12.0,   # lighting off, 50% vol
    "full_mv": 4200,
    "empty_mv": 3500,
    "runtime_profile": "rgb_on",
}

# Segment bookkeeping
SEG_GAP_S = 30          # poll hole closes the open segment
SEG_DEBOUNCE = 2        # samples to believe a charging-flag flip
SEG_MIN_SAMPLES = 3     # ignore blips when closing

# Quality gates for a single segment used as a full-runtime evidence point
SEG_MIN_HOURS = 0.5
SEG_MIN_PCT = 10

# Merged usage-session gates (stitch short discharges across brief offs)
MERGE_GAP_S = 15 * 60   # headset-off gap still part of same usage day
MERGE_MIN_HOURS = 0.5   # 30 min on-time for a health evidence point
MERGE_MIN_PCT = 8       # need real drain, not ADC wobble

# Dense recent samples (1/min) — multi-day history for profile + live ETA
RECENT_DECIM_S = 60
RECENT_MAX = 10080      # ~7 days of 1/min samples
RECENT_RATE_WINDOW_S = 45 * 60   # look-back for live drain rate
RECENT_RATE_MIN_POINTS = 8
RECENT_RATE_MIN_SPAN_S = 10 * 60
RECENT_RATE_MIN_DPCT = 1.0       # need at least ~1% movement

# Voltage-bin drain profile (mV/h drop rate while discharging)
PROFILE_BIN_MV = 50
PROFILE_WINDOW_S = 15 * 60       # slope fit window per bin sample
PROFILE_MIN_POINTS = 6
PROFILE_MIN_DMV = 8              # ignore ADC noise
PROFILE_MIN_RATES_PER_BIN = 2
# Sanity band for full-runtime estimates vs rated (drop wild extrapolations)
RUNTIME_OUTLIER_LO = 0.25
RUNTIME_OUTLIER_HI = 1.75

SEG_MAX = 200
PEAKS_MAX = 50
SAVE_EVERY_S = 300
N_RECENT_SESSIONS = 8            # median window for learned full runtime


def health_file() -> str:
    return os.path.join(config_dir(), "health.json")


def batt_state(flags):
    """(text, charging?) from the flags byte of the 0x1f20 battery reply.

    bit0 = measurement valid, bit1 = charging. charging is None when invalid.
    """
    if not flags & 0x01:
        return ("unknown", None)
    return ("charging", True) if flags & 0x02 else ("discharging", False)


def batt_percent(mv: int) -> int:
    """Map mV → % using the stock LiPo curve (full/empty specs are for display only)."""
    if mv >= BATT_CURVE[0][0]:
        return 100
    if mv <= BATT_CURVE[-1][0]:
        return 0
    for (v1, p1), (v2, p2) in zip(BATT_CURVE, BATT_CURVE[1:]):
        if v2 <= mv <= v1:
            return round(p2 + (p1 - p2) * (mv - v2) / (v1 - v2))
    return 0


def segment_rate(seg):
    """%/hour for a quality-gated discharge segment, else None."""
    if seg.get("type") != "discharge":
        return None
    dt_h = (seg["t_end"] - seg["t_start"]) / 3600.0
    dpct = seg["pct_start"] - seg["pct_end"]
    if dt_h < SEG_MIN_HOURS or dpct < SEG_MIN_PCT:
        return None
    return dpct / dt_h


def projected_runtime_h(seg):
    r = segment_rate(seg)
    return (100.0 / r) if r else None


def discharge_points(recent):
    """[(t, mv), ...] from recent samples while not charging."""
    return [(int(t), int(mv)) for t, mv, chg in recent if not chg]


def live_drain_rate(recent, now=None, window_s=RECENT_RATE_WINDOW_S):
    """Estimate current %/hour drain from recent discharge datapoints.

    Returns (pct_per_hour, n_points, span_s) or (None, 0, 0) if insufficient.
    """
    pts = discharge_points(recent)
    if len(pts) < RECENT_RATE_MIN_POINTS:
        return None, 0, 0
    now = now if now is not None else pts[-1][0]
    window = [(t, mv) for t, mv in pts if now - t <= window_s]
    if len(window) < RECENT_RATE_MIN_POINTS:
        return None, 0, 0
    t0, mv0 = window[0]
    t1, mv1 = window[-1]
    span = t1 - t0
    if span < RECENT_RATE_MIN_SPAN_S:
        return None, 0, 0
    # Use median of first/last few points to damp ADC wobble
    head = window[: min(3, len(window))]
    tail = window[-min(3, len(window)):]
    pct0 = statistics.median(batt_percent(mv) for _, mv in head)
    pct1 = statistics.median(batt_percent(mv) for _, mv in tail)
    dpct = pct0 - pct1
    if dpct < RECENT_RATE_MIN_DPCT:
        # Flat / charging recovery — no usable drain signal
        return None, len(window), span
    rate = dpct / (span / 3600.0)
    return rate, len(window), span


def remaining_runtime_h(mv, pct_per_hour, empty_mv=None):
    """Linear remaining time from current % at a constant %/h drain rate."""
    if not pct_per_hour or pct_per_hour <= 0:
        return None
    empty = empty_mv if empty_mv is not None else BATT_CURVE[-1][0]
    pct = batt_percent(mv)
    # Don't claim remaining if already at/below empty
    if mv <= empty or pct <= 0:
        return 0.0
    return pct / pct_per_hour


def expected_remaining_h(mv, rated_runtime_h, health_frac=1.0):
    """What remaining runtime *should* be at rated (or learned-health) capacity."""
    if not rated_runtime_h or rated_runtime_h <= 0:
        return None
    pct = batt_percent(mv)
    return (pct / 100.0) * rated_runtime_h * max(0.0, health_frac)


def merge_discharge_sessions(segments, gap_s=MERGE_GAP_S):
    """Stitch consecutive discharge segments across brief offs into usage sessions.

    Short headset-off gaps are normal during a day; treating them as one session
    lets us build full-runtime estimates without requiring a continuous 30+ min
    segment (the old quality gate was rarely met).
    """
    dis = [s for s in segments if s.get("type") == "discharge"]
    if not dis:
        return []
    sessions = []
    cur = None
    for s in dis:
        if cur is None:
            cur = _session_from_seg(s)
            continue
        gap = s["t_start"] - cur["t_end"]
        # Only merge if chronological and gap is small; also require voltage
        # continuity (no big upward jump = charge while "off")
        v_jump = s["mv_start"] - cur["mv_end"]
        if 0 <= gap <= gap_s and v_jump <= 30:
            cur["t_end"] = s["t_end"]
            cur["on_time_s"] += max(0, s["t_end"] - s["t_start"])
            cur["mv_end"] = s["mv_end"]
            cur["pct_end"] = s["pct_end"]
            cur["samples"] += s.get("samples", 0)
            cur["parts"] += 1
        else:
            sessions.append(cur)
            cur = _session_from_seg(s)
    if cur is not None:
        sessions.append(cur)
    return sessions


def _session_from_seg(s):
    return {
        "t_start": s["t_start"],
        "t_end": s["t_end"],
        "on_time_s": max(0, s["t_end"] - s["t_start"]),
        "mv_start": s["mv_start"],
        "mv_end": s["mv_end"],
        "pct_start": s["pct_start"],
        "pct_end": s["pct_end"],
        "samples": s.get("samples", 0),
        "parts": 1,
    }


def session_full_runtime_h(session, min_hours=MERGE_MIN_HOURS, min_pct=MERGE_MIN_PCT):
    """Extrapolate a merged usage session to full-to-empty hours, or None."""
    dt_h = session["on_time_s"] / 3600.0
    dpct = session["pct_start"] - session["pct_end"]
    if dt_h < min_hours or dpct < min_pct:
        return None
    return 100.0 * dt_h / dpct


def _filter_runtime_outliers(runtimes, rated_runtime_h):
    """Drop wild extrapolations that are implausible vs the rated spec."""
    if not runtimes:
        return []
    if not rated_runtime_h or rated_runtime_h <= 0:
        return list(runtimes)
    lo = rated_runtime_h * RUNTIME_OUTLIER_LO
    hi = rated_runtime_h * RUNTIME_OUTLIER_HI
    kept = [r for r in runtimes if lo <= r <= hi]
    return kept if kept else list(runtimes)  # if all outliers, keep raw


def learned_full_runtime_h(segments, n_recent=N_RECENT_SESSIONS,
                           rated_runtime_h=None):
    """(median full runtime h, n_sessions) from merged discharge sessions.

    Falls back to strict single-segment projections if merges don't qualify.
    """
    runtimes = []
    for sess in merge_discharge_sessions(segments):
        r = session_full_runtime_h(sess)
        if r is not None:
            runtimes.append(r)
    if not runtimes:
        runtimes = [h for h in (projected_runtime_h(s) for s in segments) if h]
    runtimes = _filter_runtime_outliers(runtimes, rated_runtime_h)
    if not runtimes:
        return None, 0
    used = runtimes[-n_recent:]
    return statistics.median(used), len(used)


def health_estimate(segments, rated_runtime_h, n_recent=N_RECENT_SESSIONS):
    """(health% capped at 120, sessions used) or (None, 0).

    health% = learned_full_runtime / rated_runtime * 100.
    """
    if not rated_runtime_h or rated_runtime_h <= 0:
        return None, 0
    learned, n = learned_full_runtime_h(
        segments, n_recent=n_recent, rated_runtime_h=rated_runtime_h)
    if learned is None:
        return None, 0
    return min(120.0, 100.0 * learned / rated_runtime_h), n


def build_drain_profile(recent, bin_mv=PROFILE_BIN_MV):
    """Learn mV/h drop rate per voltage bin from windowed discharge slopes.

    Adjacent 1-minute pairs are too noisy (±10 mV ADC). Instead, for each
    point take a ~15 min forward window and fit mV/h, attributed to the
    window's midpoint voltage bin.
    """
    pts = discharge_points(recent)
    if len(pts) < PROFILE_MIN_POINTS:
        return {
            "bins": {}, "weak_bins": {}, "n_pairs": 0,
            "bins_filled": 0, "span_mv": None, "total_discharge_s": 0,
            "bin_mv": bin_mv,
        }

    rates_by_bin = {}
    total_s = 0
    min_mv = max_mv = pts[0][1]
    n_windows = 0

    # Walk with stride ~2 min to avoid massively correlated windows
    stride_s = 120
    i = 0
    while i < len(pts) - PROFILE_MIN_POINTS:
        t0, mv0 = pts[i]
        # find end of window
        j = i + 1
        while j < len(pts) and pts[j][0] - t0 < PROFILE_WINDOW_S:
            j += 1
        if j - i < PROFILE_MIN_POINTS:
            i += 1
            continue
        j -= 1
        t1, mv1 = pts[j]
        dt = t1 - t0
        if dt < PROFILE_WINDOW_S * 0.5:
            i += 1
            continue
        # median endpoints damp ADC spikes
        head = pts[i:i + min(3, j - i + 1)]
        tail = pts[max(i, j - 2):j + 1]
        mv_a = statistics.median(m for _, m in head)
        mv_b = statistics.median(m for _, m in tail)
        dmv = mv_a - mv_b
        min_mv = min(min_mv, mv_a, mv_b)
        max_mv = max(max_mv, mv_a, mv_b)
        if dmv >= PROFILE_MIN_DMV:
            mid = (mv_a + mv_b) / 2.0
            bin_c = int(round(mid / bin_mv) * bin_mv)
            rate = dmv / (dt / 3600.0)
            rates_by_bin.setdefault(bin_c, []).append(rate)
            total_s += dt
            n_windows += 1
        # advance by stride
        t_target = t0 + stride_s
        i += 1
        while i < len(pts) and pts[i][0] < t_target:
            i += 1

    bins = {
        b: statistics.median(rs)
        for b, rs in rates_by_bin.items()
        if len(rs) >= PROFILE_MIN_RATES_PER_BIN
    }
    weak = {
        b: statistics.median(rs)
        for b, rs in rates_by_bin.items()
        if 1 <= len(rs) < PROFILE_MIN_RATES_PER_BIN and b not in bins
    }
    return {
        "bins": bins,
        "weak_bins": weak,
        "n_pairs": n_windows,
        "bins_filled": len(bins),
        "span_mv": (int(min_mv), int(max_mv)) if rates_by_bin else None,
        "total_discharge_s": total_s,
        "bin_mv": bin_mv,
    }


def remaining_from_profile(mv, profile, empty_mv=None):
    """Integrate time from current mV down to empty using learned bin rates.

    Falls back to None if the profile can't cover the path.
    """
    if not profile or not profile.get("bins"):
        return None
    empty = empty_mv if empty_mv is not None else BATT_CURVE[-1][0]
    if mv <= empty:
        return 0.0

    bins = profile["bins"]
    weak = profile.get("weak_bins") or {}
    bin_mv = profile.get("bin_mv") or PROFILE_BIN_MV
    # Walk downward in bin steps
    total_h = 0.0
    covered = 0.0
    needed = float(mv - empty)
    v = float(mv)
    # safety: max steps
    for _ in range(200):
        if v <= empty:
            break
        b = int(round(v / bin_mv) * bin_mv)
        rate = bins.get(b) or weak.get(b) or _nearest_rate(b, bins) or _nearest_rate(b, weak)
        if rate is None or rate <= 0:
            # can't integrate this step
            if covered <= 0:
                return None
            # extrapolate remaining with last known average rate
            avg = (mv - v) / total_h if total_h > 0 else None
            if avg and avg > 0:
                total_h += (v - empty) / avg
                return total_h
            return None
        step = min(bin_mv, v - empty)
        total_h += step / rate
        v -= step
        covered += step
    return total_h if covered > 0 else None


def _nearest_rate(bin_c, bins):
    if not bins:
        return None
    if bin_c in bins:
        return bins[bin_c]
    nearest = min(bins.keys(), key=lambda b: abs(b - bin_c))
    # Only use if within 2 bins
    if abs(nearest - bin_c) <= 2 * PROFILE_BIN_MV:
        return bins[nearest]
    return None


def profile_summary(profile):
    """Human-readable stats for the learned drain profile."""
    if not profile or profile.get("n_pairs", 0) == 0:
        return {
            "hours_logged": 0.0,
            "bins_filled": 0,
            "span_mv": None,
            "ready": False,
        }
    hours = profile["total_discharge_s"] / 3600.0
    return {
        "hours_logged": hours,
        "bins_filled": profile.get("bins_filled", 0),
        "span_mv": profile.get("span_mv"),
        "n_pairs": profile.get("n_pairs", 0),
        "ready": profile.get("bins_filled", 0) >= 2 and hours >= 0.25,
    }


def runtime_analysis(segments, recent, rated_runtime_h, mv=None, settings=None):
    """Bundle expect-vs-real health + live remaining estimates.

    Returns a dict consumed by the UI.
    """
    settings = settings or {}
    empty_mv = int(settings.get("empty_mv", BATT_CURVE[-1][0]))
    full_mv = int(settings.get("full_mv", BATT_CURVE[0][0]))

    learned_h, n_sess = learned_full_runtime_h(
        segments, rated_runtime_h=rated_runtime_h)
    health_pct = None
    if learned_h is not None and rated_runtime_h and rated_runtime_h > 0:
        health_pct = min(120.0, 100.0 * learned_h / rated_runtime_h)
    health_frac = (health_pct / 100.0) if health_pct is not None else 1.0

    profile = build_drain_profile(recent)
    psum = profile_summary(profile)

    live_rate, n_pts, span_s = live_drain_rate(recent)
    remain_live = None
    remain_profile = None
    remain_expected = None
    if mv is not None:
        if live_rate is not None:
            remain_live = remaining_runtime_h(mv, live_rate, empty_mv=empty_mv)
        remain_profile = remaining_from_profile(mv, profile, empty_mv=empty_mv)
        remain_expected = expected_remaining_h(mv, rated_runtime_h, health_frac=1.0)
        remain_expected_adj = expected_remaining_h(
            mv, rated_runtime_h, health_frac=health_frac)
    else:
        remain_expected_adj = None

    # Prefer live %/h for "time left now" (stable); profile is the
    # voltage-shaped cross-check once enough bins are filled.
    remain_best = None
    remain_source = None
    if remain_live is not None:
        remain_best = remain_live
        remain_source = "live rate"
    elif psum["ready"] and remain_profile is not None:
        remain_best = remain_profile
        remain_source = "profile"

    design = int(settings.get("design_capacity_mah", HEALTH_DEFAULTS["design_capacity_mah"]))
    effective_mah = (
        int(health_pct / 100.0 * design) if health_pct is not None else None
    )

    # Peak charge degradation signal
    peaks = []  # filled by caller if needed

    return {
        "learned_full_runtime_h": learned_h,
        "n_sessions": n_sess,
        "rated_runtime_h": rated_runtime_h,
        "health_pct": health_pct,
        "health_frac": health_frac,
        "effective_mah": effective_mah,
        "design_mah": design,
        "full_mv": full_mv,
        "empty_mv": empty_mv,
        "live_rate_pct_per_h": live_rate,
        "live_rate_points": n_pts,
        "live_rate_span_s": span_s,
        "remain_live_h": remain_live,
        "remain_profile_h": remain_profile,
        "remain_expected_h": remain_expected,
        "remain_expected_adj_h": remain_expected_adj,
        "remain_best_h": remain_best,
        "remain_source": remain_source,
        "profile": profile,
        "profile_summary": psum,
    }


class HealthTracker:
    """Charge/discharge session recorder fed one sample per battery poll.

    Dense `recent` datapoints drive the learned drain profile and live ETA.
    Closed `segments` (and merges of them) drive full-runtime / health %.
    """

    def __init__(self, path=None):
        self.path = path or health_file()
        data = self._load()
        self.settings = data["settings"]
        self.segments = data["segments"]
        self.peaks = data["peak_charge_mv"]
        self.recent = data["recent"]
        self.cur = None
        self.cur_mvs = []
        self.pending_flip = 0
        self.last_t = 0
        self.last_recent_t = 0
        self.last_save = time.time()
        self.dirty = False

    def add_sample(self, t, mv, charging):
        if not self.settings["tracking"]:
            return
        if self.last_t and t - self.last_t > SEG_GAP_S:
            self.close_segment("gap")
        self.last_t = t
        seg_type = "charge" if charging else "discharge"

        if self.cur is None:
            self._open(seg_type, t, mv)
        elif seg_type != self.cur["type"]:
            self.pending_flip += 1
            if self.pending_flip >= SEG_DEBOUNCE:
                self.close_segment("flag flip")
                self._open(seg_type, t, mv)
        else:
            self.pending_flip = 0

        self.cur_mvs = (self.cur_mvs + [mv])[-5:]
        smoothed = int(statistics.median(self.cur_mvs))
        self.cur["t_end"] = int(t)
        self.cur["mv_end"] = smoothed
        self.cur["pct_end"] = batt_percent(smoothed)
        self.cur["samples"] += 1
        if self.cur["type"] == "charge":
            self.cur["mv_peak"] = max(self.cur.get("mv_peak") or 0, mv)

        if t - self.last_recent_t >= RECENT_DECIM_S:
            self.recent.append([int(t), int(mv), 1 if charging else 0])
            del self.recent[:-RECENT_MAX]
            self.last_recent_t = t
            self.dirty = True
        if self.dirty and t - self.last_save > SAVE_EVERY_S:
            self.save()

    def _open(self, seg_type, t, mv):
        self.cur = {
            "type": seg_type, "t_start": int(t), "t_end": int(t),
            "mv_start": mv, "mv_end": mv,
            "pct_start": batt_percent(mv), "pct_end": batt_percent(mv),
            "samples": 1, "mv_peak": mv if seg_type == "charge" else None,
        }
        self.cur_mvs = [mv]
        self.pending_flip = 0

    def close_segment(self, reason):
        if self.cur is None:
            return
        seg, self.cur, self.cur_mvs = self.cur, None, []
        self.pending_flip = 0
        seg["reason"] = reason
        if seg["samples"] >= SEG_MIN_SAMPLES:
            if seg["type"] == "charge" and seg["mv_peak"]:
                self.peaks.append([seg["t_end"], seg["mv_peak"]])
                del self.peaks[:-PEAKS_MAX]
            self.segments.append(seg)
            del self.segments[:-SEG_MAX]
        self.save()

    def mark_offline(self):
        self.close_segment("headset off")
        self.last_t = 0

    def analysis(self, mv=None, rated_runtime_h=None):
        """Expect-vs-real + remaining runtime package for the UI."""
        if rated_runtime_h is None:
            key = (
                "rated_runtime_h_rgb_on"
                if self.settings.get("runtime_profile") == "rgb_on"
                else "rated_runtime_h_rgb_off"
            )
            rated_runtime_h = float(self.settings[key])
        result = runtime_analysis(
            self.segments, self.recent, rated_runtime_h,
            mv=mv, settings=self.settings,
        )
        if self.peaks:
            result["peak_recent_mv"] = max(p[1] for p in self.peaks[-10:])
            result["peaks"] = list(self.peaks)
        else:
            result["peak_recent_mv"] = None
            result["peaks"] = []
        return result

    def save(self):
        ensure_config_dir()
        data = {
            "version": 2,
            "settings": self.settings,
            "segments": self.segments,
            "peak_charge_mv": self.peaks,
            "recent": self.recent,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)
        self.last_save = time.time()
        self.dirty = False

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        recent = data.get("recent", [])
        # Cap on load in case file was written with a larger RECENT_MAX earlier
        if len(recent) > RECENT_MAX:
            recent = recent[-RECENT_MAX:]
        return {
            "settings": {**HEALTH_DEFAULTS, **data.get("settings", {})},
            "segments": data.get("segments", []),
            "peak_charge_mv": data.get("peak_charge_mv", []),
            "recent": recent,
        }
