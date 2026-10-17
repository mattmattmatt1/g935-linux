import unittest

from g935.tray_scroll import TRAY_SCROLL_UNIT, tray_scroll_notches


class TrayScrollTests(unittest.TestCase):
    def test_wheel_up_is_louder(self):
        # Plasma's positive angleDelta.y arrives as "down".
        notches, acc = tray_scroll_notches("down", 120, 0)
        self.assertEqual((notches, acc), (1, 0))

    def test_wheel_down_is_quieter(self):
        notches, acc = tray_scroll_notches("up", 120, 0)
        self.assertEqual((notches, acc), (-1, 0))

    def test_touchpad_deltas_accumulate(self):
        acc = 0
        total = 0
        for _ in range(3):
            notches, acc = tray_scroll_notches("down", 40, acc)
            total += notches
        self.assertEqual(total, 1)
        self.assertEqual(acc, 0)

    def test_horizontal_is_ignored(self):
        self.assertEqual(tray_scroll_notches("other", 120, 7), (0, 7))

    def test_bad_delta_is_ignored(self):
        self.assertEqual(tray_scroll_notches("down", None, 4), (0, 4))

    def test_unit_is_one_mouse_notch(self):
        self.assertEqual(TRAY_SCROLL_UNIT, 120)
