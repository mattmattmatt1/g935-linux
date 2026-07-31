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
RECENT_RATE_MIN_DPCT = 3.0       # 1–2% is commonly ADC/curve quantization noise

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
ETA_HISTORY_MIN_SESSIONS = 3     # enough evidence to anchor remaining runtime
ETA_PROFILE_HISTORY_LO = 0.5     # reject profile ETA if it strongly disagrees
ETA_PROFILE_HISTORY_HI = 1.5     # with qualifying session history

# While charging, the 0x1f20 ADC often reads the *charger path* (charge rail /
# cell under charge current), not open-circuit cell voltage. Captures show jumps
# of 200–400 mV on plug and peaks to ~4370 mV — well above 4.20 V full. SoC and
# peak tracking must not treat those as resting cell voltage.
CHARGE_RAIL_ABOVE_FULL_MV = 40   # raw mV > full + this while charging → rail
POST_CHARGE_PEAK_SAMPLES = 5     # rest samples after unplug for true peak OCV

# Desktop low-battery notifications (discharging only)
BATT_LOW_PCT = 10
BATT_CRITICAL_PCT = 5
# Re-arm only after SoC rises this far above the threshold (ADC noise guard)
BATT_ALERT_HYSTERESIS = 5


def health_file() -> str:
    return os.path.join(config_dir(), "health.json")


class BatteryAlert:
    """One desktop notification to fire for a low-battery threshold cross."""

    __slots__ = ("level", "urgency", "summary", "body")

    def __init__(self, level: str, urgency: str, summary: str, body: str):
        self.level = level
        self.urgency = urgency
        self.summary = summary
        self.body = body

    def __repr__(self):
        return f"BatteryAlert({self.level!r}, {self.urgency!r})"


class BatteryAlerts:
    """Latch low/critical notifications while discharging.

    Rules:
      * Never alert while charging (or when charging state is unknown).
      * Fire once per level when SoC is at or below the threshold.
      * Critical suppresses a redundant low if both trip together.
      * On unplug (charge → discharge), re-arm and re-check immediately so
        unplugging at 8% still warns low, and unplugging at 4% warns critical.
      * Also re-arm after recovery past threshold + hysteresis (noise guard).
    """

    def __init__(
        self,
        low_pct: int = BATT_LOW_PCT,
        critical_pct: int = BATT_CRITICAL_PCT,
        hysteresis: int = BATT_ALERT_HYSTERESIS,
    ):
        self.low_pct = int(low_pct)
        self.critical_pct = int(critical_pct)
        self.hysteresis = int(hysteresis)
        self._latched_low = False
        self._latched_critical = False
        self._was_charging = False

    def reset(self):
        self._latched_low = False
        self._latched_critical = False

    def update(self, pct, charging) -> list:
        """Feed one battery poll. Returns zero or more BatteryAlert to show."""
        if charging is True:
            # Quiet on the rail; remember we were on charge so unplug re-arms.
            self._was_charging = True
            return []
        if pct is None or charging is not False:
            # Unknown SoC or unknown charge state — stay quiet, keep latches.
            return []

        # Charge → discharge edge: clear latches so a still-low pack alerts
        # right after unplug (e.g. unplug at 8% → low, at 4% → critical).
        if self._was_charging:
            self.reset()
            self._was_charging = False

        pct = int(pct)
        alerts = []

        if pct <= self.critical_pct and not self._latched_critical:
            self._latched_critical = True
            self._latched_low = True  # don't also fire low after critical
            alerts.append(BatteryAlert(
                level="critical",
                urgency="critical",
                summary="G935: battery critically low",
                body=(
                    f"{pct}% remaining — charge soon or the headset may "
                    f"power off."
                ),
            ))
        elif pct <= self.low_pct and not self._latched_low:
            self._latched_low = True
            alerts.append(BatteryAlert(
                level="low",
                urgency="normal",
                summary="G935: battery low",
                body=f"{pct}% remaining — plug in when you can.",
            ))

        # Re-arm only after clear recovery (noise near the threshold).
        if pct > self.low_pct + self.hysteresis:
            self._latched_low = False
        if pct > self.critical_pct + self.hysteresis:
            self._latched_critical = False

        return alerts


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


def is_charge_path_mv(mv, full_mv=None, charging=True):
    """True when a reading looks like charger-path voltage, not resting cell OCV."""
    if not charging:
        return False
    full = int(full_mv if full_mv is not None else BATT_CURVE[0][0])
    return int(mv) > full + CHARGE_RAIL_ABOVE_FULL_MV


def cell_mv(mv, charging, last_rest_mv=None, full_mv=None):
    """Best cell-voltage estimate for SoC.

    Off-charger: trust the ADC. On-charger: hold the last resting (discharge)
    reading so we never map charge-rail spikes (e.g. 4370 mV) to 100%.

    Returns None when charging with no rest baseline and the reading is a
    charge-rail spike (SoC unknown — do not invent 100%).
    """
    full = int(full_mv if full_mv is not None else BATT_CURVE[0][0])
    mv = int(mv)
    if not charging:
        return mv
    if last_rest_mv is not None:
        return int(last_rest_mv)
    # No rest baseline: refuse rail spikes; mild clamp otherwise
    if is_charge_path_mv(mv, full_mv=full, charging=True):
        return None
    return min(mv, full)


def sanitize_peaks(peaks, full_mv=None):
    """Drop peak samples that are clearly charger-rail, not cell OCV."""
    full = int(full_mv if full_mv is not None else BATT_CURVE[0][0])
    limit = full + CHARGE_RAIL_ABOVE_FULL_MV
    return [[int(t), int(mv)] for t, mv in peaks if int(mv) <= limit]


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


def peak_charge_trend(peaks, full_mv=4200, n_recent=8):
    """Summarize recent full-charge peaks for degradation signal.

    Returns dict with latest/median peak, vs-spec %, and direction, or None.
    """
    if not peaks:
        return None
    vals = [int(p[1]) for p in peaks if p and len(p) >= 2]
    if not vals:
        return None
    recent = vals[-n_recent:]
    latest = recent[-1]
    med = statistics.median(recent)
    vs_full = 100.0 * latest / full_mv if full_mv else None
    direction = None
    if len(recent) >= 4:
        mid = len(recent) // 2
        early = statistics.median(recent[:mid])
        late = statistics.median(recent[mid:])
        delta = late - early
        if abs(delta) >= 15:
            direction = "falling" if delta < 0 else "rising"
        else:
            direction = "stable"
    return {
        "latest_mv": latest,
        "median_mv": med,
        "n": len(recent),
        "vs_full_pct": vs_full,
        "full_mv": full_mv,
        "direction": direction,
        "all_recent": recent,
    }


def build_insights(analysis, state=None, charging=None):
    """Short, human-readable takeaways for the Battery Health hero strip.

    Returns a list of insight dicts: {tone, text} where tone is
    'good' | 'warn' | 'bad' | 'info' | 'muted'.
    """
    insights = []
    a = analysis or {}
    health = a.get("health_pct")
    n_sess = a.get("n_sessions") or 0
    learned = a.get("learned_full_runtime_h")
    rated = a.get("rated_runtime_h")
    remain = a.get("remain_best_h")
    expect = a.get("remain_expected_h")
    live_rate = a.get("live_rate_pct_per_h")
    ps = a.get("profile_summary") or {}
    peaks = a.get("peak_summary")

    # Charging-first messaging
    if charging is True or state == "charging":
        insights.append({
            "tone": "good",
            "text": "Charging — remaining runtime updates once you're off the dock.",
        })
    elif state == "headset off" or state == "unknown":
        insights.append({
            "tone": "muted",
            "text": "Headset offline. Open this page while wearing it to keep logging.",
        })

    # Health narrative
    if health is None:
        if n_sess == 0:
            insights.append({
                "tone": "info",
                "text": "Health needs longer use: stitch ≥30 min off-charger with ≥8% drop "
                        "(short sessions merge across brief offs).",
            })
    else:
        conf = ("early read" if n_sess < 3
                else f"median of {n_sess} sessions")
        if health >= 90:
            tone, vibe = "good", "in great shape"
        elif health >= 75:
            tone, vibe = "good", "holding up well"
        elif health >= 55:
            tone, vibe = "warn", "showing wear"
        else:
            tone, vibe = "bad", "well below rated"
        learned_s = _fmt_h_short(learned)
        rated_s = f"{rated:g}h" if rated else "—"
        insights.append({
            "tone": tone,
            "text": f"Battery is {vibe}: ~{health:.0f}% health · {learned_s} real full "
                    f"runtime vs {rated_s} rated ({conf}).",
        })

    # Live remaining vs rated at this level
    if remain is not None and expect is not None and expect > 0 and charging is not True:
        rel = 100.0 * (remain / expect - 1.0)
        if rel >= 15:
            insights.append({
                "tone": "good",
                "text": f"Right now lasting {abs(rel):.0f}% longer than rated at this "
                        f"charge level ({_fmt_h_short(remain)} left vs "
                        f"{_fmt_h_short(expect)} expected).",
            })
        elif rel <= -20:
            insights.append({
                "tone": "warn",
                "text": f"Draining faster than rated (−{abs(rel):.0f}% at this level). "
                        f"Heavy audio/RGB load, or the cell is aging.",
            })
        elif remain < 0.75:
            insights.append({
                "tone": "warn",
                "text": f"About {_fmt_h_short(remain)} left at the current drain rate — "
                        f"plug in soon if you need a long session.",
            })

    # Live rate context: compare to rated average drain
    if live_rate is not None and rated and rated > 0 and charging is not True:
        rated_rate = 100.0 / rated
        if live_rate > rated_rate * 1.4:
            insights.append({
                "tone": "info",
                "text": f"Live drain −{live_rate:.1f}%/h is heavier than the "
                        f"−{rated_rate:.1f}%/h rated average.",
            })
        elif live_rate < rated_rate * 0.7:
            insights.append({
                "tone": "good",
                "text": f"Light use right now (−{live_rate:.1f}%/h vs "
                        f"−{rated_rate:.1f}%/h rated average).",
            })

    # Profile readiness
    if ps.get("ready"):
        span = ps.get("span_mv")
        span_s = f"{span[0]}–{span[1]} mV" if span else "the voltage range"
        if a.get("remain_profile_rejected"):
            insights.append({
                "tone": "info",
                "text": f"ETA is anchored to the median of {n_sess} sessions; "
                        "the voltage-bin estimate disagreed and was ignored.",
            })
        else:
            insights.append({
                "tone": "info",
                "text": f"Drain profile learned: {ps.get('hours_logged', 0):.1f} h "
                        f"off-charger, {ps.get('bins_filled', 0)} voltage bins "
                        f"across {span_s}.",
            })
    elif ps.get("hours_logged", 0) > 0:
        insights.append({
            "tone": "muted",
            "text": f"Building drain profile… {ps.get('hours_logged', 0):.1f} h logged, "
                    f"{ps.get('bins_filled', 0)} solid bins so far.",
        })

    # Peak charge degradation
    if peaks and peaks.get("latest_mv") and peaks.get("full_mv"):
        latest = peaks["latest_mv"]
        full = peaks["full_mv"]
        # Ignore ADC spikes well above full (charging noise)
        if latest <= full + 50:
            shortfall = full - latest
            if shortfall >= 80:
                insights.append({
                    "tone": "warn",
                    "text": f"Recent full charges top out at {latest} mV "
                            f"({peaks['vs_full_pct']:.1f}% of {full} mV spec).",
                })
            elif peaks.get("direction") == "falling":
                insights.append({
                    "tone": "info",
                    "text": f"Charge peaks trending down (latest {latest} mV, "
                            f"median {peaks['median_mv']:.0f} mV over "
                            f"{peaks['n']} charges).",
                })

    # Cap so the UI stays scannable
    return insights[:4]


def _fmt_h_short(hours):
    if hours is None:
        return "—"
    if hours <= 0:
        return "0m"
    total_m = int(round(hours * 60))
    if total_m >= 60:
        return f"{total_m // 60}h{total_m % 60:02d}m"
    return f"{total_m}m"


def session_list_rows(segments, rated_runtime_h, limit=10):
    """Rows for the recent-sessions list: label, duration, drain, full runtime, quality."""
    rows = []
    sessions = merge_discharge_sessions(segments)
    for sess in reversed(sessions[-limit:]):
        full = session_full_runtime_h(sess)
        dpct = sess["pct_start"] - sess["pct_end"]
        quality = "solid" if full is not None else "short"
        pct_of_rated = None
        if full is not None and rated_runtime_h and rated_runtime_h > 0:
            pct_of_rated = min(120.0, 100.0 * full / rated_runtime_h)
        rows.append({
            "t_start": sess["t_start"],
            "on_time_s": sess["on_time_s"],
            "parts": sess["parts"],
            "pct_start": sess["pct_start"],
            "pct_end": sess["pct_end"],
            "mv_start": sess["mv_start"],
            "mv_end": sess["mv_end"],
            "dpct": dpct,
            "full_h": full,
            "pct_of_rated": pct_of_rated,
            "quality": quality,
        })
    return rows


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
    remain_profile_raw = None
    remain_expected = None
    if mv is not None:
        if live_rate is not None:
            remain_live = remaining_runtime_h(mv, live_rate, empty_mv=empty_mv)
        remain_profile_raw = remaining_from_profile(
            mv, profile, empty_mv=empty_mv)
        remain_profile = remain_profile_raw
        remain_expected = expected_remaining_h(mv, rated_runtime_h, health_frac=1.0)
        remain_expected_adj = expected_remaining_h(
            mv, rated_runtime_h, health_frac=health_frac)
    else:
        remain_expected_adj = None

    # Once several qualifying sessions exist, their median full runtime is the
    # most stable remaining-time anchor. A 1–2% move on the G935 voltage curve
    # can otherwise turn an ADC plateau into wildly optimistic 3%/h live rates.
    # Keep a genuinely heavier live drain conservative, but never let a brief
    # slow/flat window extend a well-supported historical ETA.
    history_ready = (
        n_sess >= ETA_HISTORY_MIN_SESSIONS
        and remain_expected_adj is not None
    )
    remain_history = remain_expected_adj if history_ready else None

    # The voltage-bin profile is useful before session history matures, but it
    # can overvalue the flat part of a LiPo curve. Hide it as an ETA cross-check
    # when it substantially contradicts established full-runtime evidence.
    profile_eta_rejected = False
    if (history_ready and remain_profile is not None
            and remain_history is not None and remain_history > 0):
        ratio = remain_profile / remain_history
        if not ETA_PROFILE_HISTORY_LO <= ratio <= ETA_PROFILE_HISTORY_HI:
            remain_profile = None
            profile_eta_rejected = True

    remain_best = None
    remain_source = None
    if history_ready:
        remain_best = remain_history
        remain_source = "session history"
        if remain_live is not None and remain_live < remain_history:
            remain_best = remain_live
            remain_source = "live rate"
    elif remain_live is not None:
        remain_best = remain_live
        remain_source = "live rate"
    elif psum["ready"] and remain_profile is not None:
        remain_best = remain_profile
        remain_source = "profile"

    design = int(settings.get("design_capacity_mah", HEALTH_DEFAULTS["design_capacity_mah"]))
    effective_mah = (
        int(health_pct / 100.0 * design) if health_pct is not None else None
    )

    # Rated average drain (%/h) for live-rate comparison in the UI
    rated_rate = (100.0 / rated_runtime_h) if rated_runtime_h else None
    # Delta of measured remaining vs rated remaining at this voltage
    remain_vs_rated_pct = None
    if remain_best is not None and remain_expected is not None and remain_expected > 0:
        remain_vs_rated_pct = 100.0 * (remain_best / remain_expected - 1.0)

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
        "rated_rate_pct_per_h": rated_rate,
        "remain_live_h": remain_live,
        "remain_profile_h": remain_profile,
        "remain_profile_raw_h": remain_profile_raw,
        "remain_profile_rejected": profile_eta_rejected,
        "remain_history_h": remain_history,
        "remain_expected_h": remain_expected,
        "remain_expected_adj_h": remain_expected_adj,
        "remain_best_h": remain_best,
        "remain_source": remain_source,
        "remain_vs_rated_pct": remain_vs_rated_pct,
        "profile": profile,
        "profile_summary": psum,
    }


class HealthTracker:
    """Charge/discharge session recorder fed one sample per battery poll.

    Dense `recent` datapoints drive the learned drain profile and live ETA.
    Closed `segments` (and merges of them) drive full-runtime / health %.

    While charging, raw ADC mV is treated as *path* voltage (often the charge
    rail). SoC % and peak OCV use last resting cell voltage instead.
    """

    def __init__(self, path=None):
        self.path = path or health_file()
        data = self._load()
        self.settings = data["settings"]
        self.segments = data["segments"]
        full = int(self.settings.get("full_mv", HEALTH_DEFAULTS["full_mv"]))
        # Drop historical charger-rail peaks so health UI isn't poisoned
        raw_peaks = data["peak_charge_mv"]
        self.peaks = sanitize_peaks(raw_peaks, full_mv=full)
        self._peaks_scrubbed = len(self.peaks) != len(raw_peaks)
        self.recent = data["recent"]
        self.cur = None
        self.cur_mvs = []
        self.pending_flip = 0
        self.last_t = 0
        self.last_recent_t = 0
        self.last_save = time.time()
        self.dirty = False
        self.last_rest_mv = self._infer_last_rest()
        # After a charge that only saw rail voltages, capture peak from rest
        self.pending_peak_from_rest = False
        self.post_charge_mvs = []
        if self._peaks_scrubbed:
            self.save()

    def _full_mv(self):
        return int(self.settings.get("full_mv", HEALTH_DEFAULTS["full_mv"]))

    def _infer_last_rest(self):
        for _t, mv, chg in reversed(self.recent):
            if not chg:
                return int(mv)
        return None

    def reading(self, mv, charging):
        """Interpret a raw ADC sample for display / SoC.

        Returns dict: raw_mv, cell_mv, pct, charging, last_rest_mv, path_is_rail.
        """
        full = self._full_mv()
        raw = int(mv)
        chg = bool(charging)
        rest = self.last_rest_mv
        c_mv = cell_mv(raw, chg, last_rest_mv=rest, full_mv=full)
        return {
            "raw_mv": raw,
            "cell_mv": c_mv,
            "pct": batt_percent(c_mv) if c_mv is not None else None,
            "charging": chg,
            "last_rest_mv": rest,
            "path_is_rail": is_charge_path_mv(raw, full_mv=full, charging=chg),
        }

    def add_sample(self, t, mv, charging):
        if not self.settings["tracking"]:
            return
        if self.last_t and t - self.last_t > SEG_GAP_S:
            self.close_segment("gap")
        self.last_t = t
        mv = int(mv)
        charging = bool(charging)
        full = self._full_mv()
        seg_type = "charge" if charging else "discharge"

        # Update resting baseline only off-charger (true cell / OCV path)
        if not charging:
            self._maybe_capture_post_charge_peak(t, mv)
            # Smoothed rest update happens below after median; seed raw first
            if self.last_rest_mv is None:
                self.last_rest_mv = mv

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
        c_mv = cell_mv(smoothed, charging, self.last_rest_mv, full_mv=full)
        self.cur["t_end"] = int(t)
        # Keep raw path mV in the segment (useful diagnostics); % from cell
        self.cur["mv_end"] = smoothed
        if c_mv is not None:
            self.cur["pct_end"] = batt_percent(c_mv)
            self.cur["cell_mv_end"] = c_mv
        self.cur["samples"] += 1
        if self.cur["type"] == "charge":
            self.cur["mv_peak"] = max(self.cur.get("mv_peak") or 0, mv)
        elif not charging:
            self.last_rest_mv = smoothed

        if t - self.last_recent_t >= RECENT_DECIM_S:
            # Store raw ADC always; charts/SoC sanitize charging samples
            self.recent.append([int(t), int(mv), 1 if charging else 0])
            del self.recent[:-RECENT_MAX]
            self.last_recent_t = t
            self.dirty = True
        if self.dirty and t - self.last_save > SAVE_EVERY_S:
            self.save()

    def _maybe_capture_post_charge_peak(self, t, mv):
        """After a rail-only charge, learn peak OCV from early rest samples."""
        if not self.pending_peak_from_rest:
            return
        self.post_charge_mvs.append(int(mv))
        if len(self.post_charge_mvs) < POST_CHARGE_PEAK_SAMPLES:
            return
        # First sample right after unplug is still elevated; use the rest window
        window = self.post_charge_mvs[1:] or self.post_charge_mvs
        peak = int(statistics.median(window))
        full = self._full_mv()
        if peak <= full + CHARGE_RAIL_ABOVE_FULL_MV:
            self.peaks.append([int(t), peak])
            del self.peaks[:-PEAKS_MAX]
            self.dirty = True
        self.pending_peak_from_rest = False
        self.post_charge_mvs = []

    def _open(self, seg_type, t, mv):
        full = self._full_mv()
        charging = seg_type == "charge"
        c_mv = cell_mv(mv, charging, self.last_rest_mv, full_mv=full)
        # Fall back for segment bookkeeping only if SoC truly unknown
        pct = batt_percent(c_mv) if c_mv is not None else (
            batt_percent(self.last_rest_mv) if self.last_rest_mv is not None else 0)
        self.cur = {
            "type": seg_type, "t_start": int(t), "t_end": int(t),
            "mv_start": mv, "mv_end": mv,
            "pct_start": pct, "pct_end": pct,
            "cell_mv_start": c_mv, "cell_mv_end": c_mv,
            "samples": 1, "mv_peak": mv if charging else None,
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
            if seg["type"] == "charge" and seg.get("mv_peak"):
                self._record_charge_peak(seg)
            self.segments.append(seg)
            del self.segments[:-SEG_MAX]
        self.save()

    def _record_charge_peak(self, seg):
        """Record a full-charge OCV peak; never store charger-rail spikes."""
        full = self._full_mv()
        peak = int(seg.get("mv_peak") or 0)
        if peak <= 0:
            return
        if is_charge_path_mv(peak, full_mv=full, charging=True):
            # Peak was the rail — capture true OCV after unplug instead
            self.pending_peak_from_rest = True
            self.post_charge_mvs = []
            return
        # Plausible cell voltage under/near CV
        self.peaks.append([int(seg["t_end"]), peak])
        del self.peaks[:-PEAKS_MAX]

    def mark_offline(self):
        self.close_segment("headset off")
        self.last_t = 0
        # Don't clear last_rest_mv — still the best SoC anchor when back on

    def analysis(self, mv=None, rated_runtime_h=None, charging=None):
        """Expect-vs-real + remaining runtime package for the UI.

        When `charging` is True, `mv` is treated as path voltage and replaced
        with the cell estimate for remaining-runtime math.
        """
        if rated_runtime_h is None:
            key = (
                "rated_runtime_h_rgb_on"
                if self.settings.get("runtime_profile") == "rgb_on"
                else "rated_runtime_h_rgb_off"
            )
            rated_runtime_h = float(self.settings[key])
        cell = None
        if mv is not None:
            cell = cell_mv(
                mv, bool(charging), self.last_rest_mv, full_mv=self._full_mv())
        result = runtime_analysis(
            self.segments, self.recent, rated_runtime_h,
            mv=cell, settings=self.settings,
        )
        result["raw_mv"] = int(mv) if mv is not None else None
        result["cell_mv"] = cell
        result["last_rest_mv"] = self.last_rest_mv
        if self.peaks:
            result["peak_recent_mv"] = max(p[1] for p in self.peaks[-10:])
            result["peaks"] = list(self.peaks)
            result["peak_summary"] = peak_charge_trend(
                self.peaks, full_mv=self._full_mv())
        else:
            result["peak_recent_mv"] = None
            result["peaks"] = []
            result["peak_summary"] = None
        result["session_rows"] = session_list_rows(
            self.segments, result.get("rated_runtime_h"))
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
