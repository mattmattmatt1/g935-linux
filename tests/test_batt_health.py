import os
import tempfile
import unittest

from g935.battery import (
    BATT_CRITICAL_PCT,
    BATT_LOW_PCT,
    BatteryAlerts,
    HealthTracker,
    batt_percent,
    batt_state,
    build_drain_profile,
    build_insights,
    cell_mv,
    health_estimate,
    is_charge_path_mv,
    learned_full_runtime_h,
    live_drain_rate,
    merge_discharge_sessions,
    peak_charge_trend,
    projected_runtime_h,
    remaining_from_profile,
    remaining_runtime_h,
    runtime_analysis,
    sanitize_peaks,
    segment_rate,
    session_full_runtime_h,
    session_list_rows,
)
from g935.charts import build_history_points


class BattCurveTests(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(batt_percent(4200), 100)
        self.assertEqual(batt_percent(5000), 100)
        self.assertEqual(batt_percent(3500), 0)
        self.assertEqual(batt_percent(3000), 0)

    def test_midpoint(self):
        self.assertEqual(batt_percent(3870), 60)

    def test_flags(self):
        self.assertEqual(batt_state(0x00), ("unknown", None))
        self.assertEqual(batt_state(0x01), ("discharging", False))
        self.assertEqual(batt_state(0x03), ("charging", True))


class HealthEstimateTests(unittest.TestCase):
    def _seg(self, hours, pct_start, pct_end, t0=0, mv_start=4000, mv_end=3800):
        return {
            "type": "discharge",
            "t_start": t0,
            "t_end": t0 + int(hours * 3600),
            "pct_start": pct_start,
            "pct_end": pct_end,
            "mv_start": mv_start,
            "mv_end": mv_end,
            "samples": 20,
        }

    def test_segment_rate_gates(self):
        self.assertIsNone(segment_rate(self._seg(0.1, 100, 90)))
        self.assertIsNone(segment_rate(self._seg(2.0, 50, 48)))
        r = segment_rate(self._seg(2.0, 100, 50))
        self.assertAlmostEqual(r, 25.0)

    def test_projected_runtime(self):
        self.assertAlmostEqual(projected_runtime_h(self._seg(2.0, 100, 50)), 4.0)

    def test_health_estimate_median(self):
        segs = [self._seg(2.0, 100, 50, t0=i * 10000) for i in range(3)]
        est, n = health_estimate(segs, rated_runtime_h=8.0)
        self.assertEqual(n, 3)
        self.assertAlmostEqual(est, 50.0)

    def test_merge_short_sessions(self):
        # Three 25-min discharges with 5-min gaps, 6% each → 75 min / 18%
        segs = []
        t = 0
        pct = 90
        mv = 4000
        for _ in range(3):
            segs.append(self._seg(
                25 / 60, pct, pct - 6, t0=t, mv_start=mv, mv_end=mv - 30))
            t += int(25 * 60) + 5 * 60
            pct -= 6
            mv -= 30
        sessions = merge_discharge_sessions(segs)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["parts"], 3)
        full = session_full_runtime_h(sessions[0])
        self.assertIsNotNone(full)
        # 75 min = 1.25 h on-time for 18% → 100/18 * 1.25h
        self.assertAlmostEqual(full, 100.0 * 1.25 / 18.0, places=2)

    def test_learned_runtime_from_merges(self):
        segs = []
        t = 0
        for day in range(4):
            # one 1h session draining 20% each day
            segs.append(self._seg(1.0, 80, 60, t0=t, mv_start=4000, mv_end=3900))
            t += 86400
        learned, n = learned_full_runtime_h(segs)
        self.assertEqual(n, 4)
        self.assertAlmostEqual(learned, 5.0)  # 100/20 * 1h

    def test_health_tracker_persists(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "health.json")
            h = HealthTracker(path=path)
            t0 = 1_700_000_000
            h.add_sample(t0, 4000, False)
            h.add_sample(t0 + 10, 3990, False)
            h.add_sample(t0 + 20, 3980, False)
            h.close_segment("test")
            self.assertTrue(os.path.isfile(path))
            h2 = HealthTracker(path=path)
            self.assertEqual(len(h2.segments), 1)
            self.assertEqual(h2.segments[0]["reason"], "test")


class LiveRateAndProfileTests(unittest.TestCase):
    def _recent_discharge(self, n=30, t0=1_700_000_000, mv0=4100, drop_per_min=2):
        """n one-minute samples dropping drop_per_min mV each."""
        out = []
        for i in range(n):
            out.append([t0 + i * 60, mv0 - i * drop_per_min, 0])
        return out

    def test_live_drain_rate(self):
        recent = self._recent_discharge(n=40, drop_per_min=3)
        rate, n_pts, span = live_drain_rate(recent)
        self.assertIsNotNone(rate)
        self.assertGreater(n_pts, 8)
        self.assertGreater(span, 600)
        self.assertGreater(rate, 0)

    def test_live_rate_rejects_two_percent_curve_wobble(self):
        t0 = 1_700_000_000
        recent = [
            [t0 + i * 60, 3754 - round(5 * i / 20), 0]
            for i in range(21)
        ]
        rate, n_pts, span = live_drain_rate(recent)
        self.assertIsNone(rate)
        self.assertEqual(n_pts, 21)
        self.assertEqual(span, 20 * 60)

    def test_remaining_runtime_linear(self):
        # 50% at 20%/h → 2.5h
        self.assertAlmostEqual(remaining_runtime_h(3870, 20.0), 60 / 20.0)

    def test_build_profile_and_integrate(self):
        # Steady 120 mV/h drop over a wide range
        recent = self._recent_discharge(n=120, mv0=4150, drop_per_min=2)
        # 2 mV/min = 120 mV/h
        profile = build_drain_profile(recent)
        self.assertGreater(profile["n_pairs"], 10)
        self.assertTrue(profile["bins"] or profile["weak_bins"])
        rem = remaining_from_profile(4000, profile, empty_mv=3500)
        self.assertIsNotNone(rem)
        # 500 mV / 120 mV/h ≈ 4.17h
        self.assertGreater(rem, 3.0)
        self.assertLess(rem, 6.0)

    def test_runtime_analysis_bundle(self):
        recent = self._recent_discharge(n=40, mv0=4050, drop_per_min=2)
        segs = [{
            "type": "discharge",
            "t_start": recent[0][0],
            "t_end": recent[-1][0],
            "pct_start": batt_percent(recent[0][1]),
            "pct_end": batt_percent(recent[-1][1]),
            "mv_start": recent[0][1],
            "mv_end": recent[-1][1],
            "samples": len(recent),
        }]
        a = runtime_analysis(segs, recent, rated_runtime_h=8.0, mv=recent[-1][1],
                             settings={"design_capacity_mah": 1100,
                                       "full_mv": 4200, "empty_mv": 3500})
        self.assertIn("remain_best_h", a)
        self.assertIsNotNone(a["live_rate_pct_per_h"])
        self.assertIsNotNone(a["remain_expected_h"])
        self.assertIsNotNone(a.get("remain_vs_rated_pct"))
        self.assertIsNotNone(a.get("rated_rate_pct_per_h"))

    def test_mature_history_prevents_implausible_low_charge_eta(self):
        segs = []
        for i in range(8):
            t0 = 1_700_000_000 + i * 86400
            segs.append({
                "type": "discharge",
                "t_start": t0,
                "t_end": t0 + 3600,
                "pct_start": 80,
                "pct_end": 58,
                "mv_start": 3980,
                "mv_end": 3860,
                "samples": 60,
            })

        # A broad but too-slow voltage profile used to claim roughly four
        # hours remained around 26%, while the eight sessions established a
        # full runtime of only about 4.5 hours.
        t0 = 1_800_000_000
        recent = [[t0 + i * 60, 4050 - i, 0] for i in range(550)]
        a = runtime_analysis(
            segs, recent, rated_runtime_h=8.0, mv=3747,
            settings={"design_capacity_mah": 1100,
                      "full_mv": 4200, "empty_mv": 3500},
        )

        self.assertEqual(a["n_sessions"], 8)
        self.assertEqual(a["remain_source"], "session history")
        self.assertAlmostEqual(a["remain_best_h"], 26 / 100 * (100 / 22),
                               places=2)
        self.assertTrue(a["remain_profile_rejected"])
        self.assertIsNone(a["remain_profile_h"])
        self.assertGreater(a["remain_profile_raw_h"], a["remain_best_h"] * 2)


class ChargePathVoltageTests(unittest.TestCase):
    """ADC while charging often reports charger-path / rail, not cell OCV."""

    def test_rail_detection(self):
        self.assertTrue(is_charge_path_mv(4379, full_mv=4200, charging=True))
        self.assertFalse(is_charge_path_mv(4160, full_mv=4200, charging=True))
        self.assertFalse(is_charge_path_mv(4379, full_mv=4200, charging=False))

    def test_cell_mv_holds_rest_while_charging(self):
        # Plug-in jump 3929 → 4318 must not become the SoC voltage
        self.assertEqual(cell_mv(4318, True, last_rest_mv=3929), 3929)
        self.assertEqual(cell_mv(3929, False, last_rest_mv=4000), 3929)

    def test_cell_mv_unknown_on_rail_without_rest(self):
        self.assertIsNone(cell_mv(4370, True, last_rest_mv=None))

    def test_sanitize_peaks_drops_rail(self):
        peaks = [[1, 4165], [2, 4379], [3, 4160]]
        clean = sanitize_peaks(peaks, full_mv=4200)
        self.assertEqual(clean, [[1, 4165], [3, 4160]])

    def test_history_holds_soc_while_charging(self):
        # discharge at 72%, then charge path spikes to 100%
        recent = [
            [1000, 3929, 0],
            [1060, 3925, 0],
            [1120, 4167, 1],
            [1180, 4318, 1],
            [1240, 4379, 1],
        ]
        pts = build_history_points(recent, batt_percent, full_mv=4200)
        # All SoC while charging should stay near the rest baseline (~72%)
        chg_pcts = [p for t, p, c in pts if c]
        self.assertTrue(chg_pcts)
        for p in chg_pcts:
            self.assertLess(p, 80)
            self.assertGreater(p, 65)

    def test_tracker_ignores_rail_peak(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "health.json")
            h = HealthTracker(path=path)
            t0 = 1_700_000_000
            # rest at ~72%
            for i in range(5):
                h.add_sample(t0 + i * 5, 3929 - i, False)
            h.close_segment("test")
            # charge with rail spike — SoC must hold at pre-plug rest
            for i in range(10):
                h.add_sample(t0 + 100 + i * 5, 4200 + 50 + i * 10, True)
            r = h.reading(4370, True)
            self.assertLess(r["pct"], 80)
            self.assertTrue(r["path_is_rail"])
            self.assertAlmostEqual(r["cell_mv"], h.last_rest_mv)
            h.close_segment("test")
            # rail peak must not be stored as a capacity peak
            self.assertTrue(all(p[1] <= 4240 for p in h.peaks))
            self.assertTrue(h.pending_peak_from_rest)
            # unplug: capture rest peak from OCV, not rail
            for i in range(6):
                h.add_sample(t0 + 200 + i * 5, 4160 - i, False)
            self.assertFalse(h.pending_peak_from_rest)
            self.assertTrue(h.peaks)
            self.assertLessEqual(h.peaks[-1][1], 4240)


class InsightsAndPeaksTests(unittest.TestCase):
    def test_peak_trend_stable(self):
        peaks = [[i * 1000, 4160 + (i % 3)] for i in range(8)]
        t = peak_charge_trend(peaks, full_mv=4200)
        self.assertIsNotNone(t)
        self.assertEqual(t["n"], 8)
        self.assertEqual(t["direction"], "stable")

    def test_peak_trend_falling(self):
        peaks = [[i * 1000, 4200 - i * 10] for i in range(8)]
        t = peak_charge_trend(peaks, full_mv=4200)
        self.assertEqual(t["direction"], "falling")

    def test_insights_health_and_remain(self):
        a = {
            "health_pct": 89.0,
            "n_sessions": 4,
            "learned_full_runtime_h": 7.1,
            "rated_runtime_h": 8.0,
            "remain_best_h": 3.0,
            "remain_expected_h": 2.0,
            "live_rate_pct_per_h": 8.0,
            "profile_summary": {
                "ready": True, "hours_logged": 12.0,
                "bins_filled": 5, "span_mv": (3600, 4100),
            },
            "peak_summary": None,
        }
        ins = build_insights(a, state="discharging", charging=False)
        self.assertTrue(ins)
        texts = " ".join(i["text"] for i in ins)
        self.assertIn("health", texts.lower())
        self.assertIn("longer", texts.lower())

    def test_insights_charging(self):
        ins = build_insights({}, state="charging", charging=True)
        self.assertTrue(any("Charging" in i["text"] for i in ins))

    def test_session_list_rows(self):
        segs = []
        t = 0
        for _ in range(3):
            segs.append({
                "type": "discharge",
                "t_start": t,
                "t_end": t + 3600,
                "on_time_s": 3600,
                "mv_start": 4000,
                "mv_end": 3900,
                "pct_start": 80,
                "pct_end": 60,
                "samples": 20,
            })
            t += 86400
        # merge_discharge_sessions expects segment shape without on_time_s
        for s in segs:
            s.pop("on_time_s", None)
        rows = session_list_rows(segs, rated_runtime_h=8.0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["quality"], "solid")  # newest first
        self.assertIsNotNone(rows[0]["full_h"])


class BatteryAlertTests(unittest.TestCase):
    def test_no_alert_above_low(self):
        a = BatteryAlerts()
        self.assertEqual(a.update(50, False), [])
        self.assertEqual(a.update(BATT_LOW_PCT + 1, False), [])

    def test_low_once_while_discharging(self):
        a = BatteryAlerts()
        alerts = a.update(BATT_LOW_PCT, False)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, "low")
        self.assertEqual(alerts[0].urgency, "normal")
        self.assertIn(f"{BATT_LOW_PCT}%", alerts[0].body)
        # latched — no spam on every poll
        self.assertEqual(a.update(BATT_LOW_PCT - 1, False), [])
        self.assertEqual(a.update(BATT_LOW_PCT, False), [])

    def test_critical_once_and_skips_redundant_low(self):
        a = BatteryAlerts()
        alerts = a.update(BATT_CRITICAL_PCT, False)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, "critical")
        self.assertEqual(alerts[0].urgency, "critical")
        # already at critical; further polls stay quiet
        self.assertEqual(a.update(BATT_CRITICAL_PCT - 1, False), [])

    def test_low_then_critical(self):
        a = BatteryAlerts()
        low = a.update(8, False)
        self.assertEqual([x.level for x in low], ["low"])
        crit = a.update(4, False)
        self.assertEqual([x.level for x in crit], ["critical"])
        self.assertEqual(a.update(3, False), [])

    def test_no_alert_while_charging(self):
        a = BatteryAlerts()
        self.assertEqual(a.update(3, True), [])
        self.assertEqual(a.update(BATT_LOW_PCT, True), [])
        # still quiet after charging poll even if latches were set earlier
        a.update(4, False)
        self.assertEqual(a.update(4, True), [])

    def test_charging_rearms_for_next_cycle(self):
        a = BatteryAlerts()
        self.assertEqual(len(a.update(9, False)), 1)
        # plug in is quiet
        self.assertEqual(a.update(9, True), [])
        # unplug still low → notify again
        again = a.update(9, False)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].level, "low")

    def test_unplug_at_8_percent_warns_low(self):
        """Stay on charge at 8%, then unplug → one low alert."""
        a = BatteryAlerts()
        for _ in range(5):
            self.assertEqual(a.update(8, True), [])
        alerts = a.update(8, False)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, "low")
        self.assertIn("8%", alerts[0].body)
        # still discharging at 8% — latched, no spam
        self.assertEqual(a.update(8, False), [])

    def test_unplug_at_4_percent_warns_critical(self):
        """Stay on charge at 4%, then unplug → one critical alert."""
        a = BatteryAlerts()
        for _ in range(5):
            self.assertEqual(a.update(4, True), [])
        alerts = a.update(4, False)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, "critical")
        self.assertIn("4%", alerts[0].body)
        self.assertEqual(a.update(4, False), [])

    def test_unplug_after_prior_alert_still_warns(self):
        """Alert while draining, charge a bit, unplug still low → alert again."""
        a = BatteryAlerts()
        self.assertEqual(a.update(7, False)[0].level, "low")
        self.assertEqual(a.update(7, True), [])
        self.assertEqual(a.update(6, True), [])
        again = a.update(6, False)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].level, "low")

    def test_unknown_state_stays_quiet(self):
        a = BatteryAlerts()
        self.assertEqual(a.update(5, None), [])
        self.assertEqual(a.update(None, False), [])

    def test_hysteresis_prevents_boundary_spam(self):
        a = BatteryAlerts()
        self.assertEqual(len(a.update(10, False)), 1)
        # noise just above low threshold must not re-arm
        self.assertEqual(a.update(12, False), [])
        self.assertEqual(a.update(10, False), [])
        # clear recovery re-arms low
        self.assertEqual(a.update(16, False), [])
        self.assertEqual(len(a.update(10, False)), 1)

    def test_thresholds_match_defaults(self):
        self.assertEqual(BATT_LOW_PCT, 10)
        self.assertEqual(BATT_CRITICAL_PCT, 5)


if __name__ == "__main__":
    unittest.main()
