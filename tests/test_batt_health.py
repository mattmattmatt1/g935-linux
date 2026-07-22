import os
import tempfile
import unittest

from g935.battery import (
    HealthTracker,
    batt_percent,
    batt_state,
    build_drain_profile,
    health_estimate,
    learned_full_runtime_h,
    live_drain_rate,
    merge_discharge_sessions,
    projected_runtime_h,
    remaining_from_profile,
    remaining_runtime_h,
    runtime_analysis,
    segment_rate,
    session_full_runtime_h,
)


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


if __name__ == "__main__":
    unittest.main()
