"""Unit tests for g935.ui.visuals pure color/waveform math."""
from __future__ import annotations

import unittest

from g935.ui.lighting_math import eval_breathing_waveform, eval_zone_color


class TestVisualsMath(unittest.TestCase):
    def test_breathing_waveforms_in_unit_interval(self):
        for wf in range(8):
            for step in range(25):
                val = eval_breathing_waveform(step / 24.0, wf)
                self.assertGreaterEqual(val, 0.0, f"wf={wf} step={step}")
                self.assertLessEqual(val, 1.0, f"wf={wf} step={step}")

    def test_breathing_sine_peaks_at_half(self):
        self.assertAlmostEqual(eval_breathing_waveform(0.0, 1), 0.0, places=5)
        self.assertAlmostEqual(eval_breathing_waveform(0.5, 1), 1.0, places=5)
        self.assertAlmostEqual(eval_breathing_waveform(1.0, 1), 0.0, places=5)

    def test_zone_off_returns_zero_alpha(self):
        _r, _g, _b, a = eval_zone_color(0, (0, 255, 128), 5000, 100, 0, 1.23)
        self.assertEqual(a, 0.0)

    def test_zone_fixed_returns_exact_rgb_and_high_alpha(self):
        r, g, b, a = eval_zone_color(1, (0, 180, 255), 5000, 100, 0, 0.0)
        self.assertAlmostEqual(r, 0.0, places=4)
        self.assertAlmostEqual(g, 180 / 255.0, places=4)
        self.assertAlmostEqual(b, 1.0, places=4)
        self.assertGreater(a, 0.8)

    def test_zone_breathing_varies_over_period(self):
        _, _, _, a0 = eval_zone_color(2, (0, 200, 255), 4000, 100, 1, 0.0)
        _, _, _, a_mid = eval_zone_color(2, (0, 200, 255), 4000, 100, 1, 2.0)
        self.assertGreater(a_mid, a0)

    def test_zone_cycling_rotates_hue(self):
        c0 = eval_zone_color(3, (0, 0, 0), 6000, 100, 0, 0.0)
        c1 = eval_zone_color(3, (0, 0, 0), 6000, 100, 0, 2.0)
        self.assertNotAlmostEqual(c0[0], c1[0], places=2)
        self.assertGreater(c0[3], 0.5)


if __name__ == "__main__":
    unittest.main()
