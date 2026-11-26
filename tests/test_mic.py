import unittest
from unittest import mock

from g935.mic import BOOM_DOWN, BOOM_UP, BUTTON, MIC_REPORT, MicHandler, UNMUTE_HOLD_S


class FakeSwitch:
    def __init__(self, on=True):
        self.on = on
        self.writes = []

    def read(self, usbid=None, switch_name=None):
        return self.on

    def set(self, on, usbid=None, switch_name=None):
        self.on = bool(on)
        self.writes.append(self.on)


class MicHandlerTests(unittest.TestCase):
    def _handler(self, boom_down=True, switch_on=True, mode="ghub"):
        switch = FakeSwitch(switch_on)
        boom = {"down": boom_down}
        h = MicHandler(mode_loader=lambda: mode)
        h._read_boom = lambda fd: boom["down"]
        h._read_switch = switch.read
        h._set_switch = switch.set
        return h, switch, boom

    def test_poll_does_not_sleep(self):
        h, switch, boom = self._handler(boom_down=True, switch_on=True)
        h._needs_unmute = True
        h.poll(fd=None)
        self.assertEqual(switch.writes, [False])
        self.assertIsNotNone(h._pulse_deadline)
        self.assertEqual(switch.on, False)

    def test_pulse_turns_on_only_after_hold(self):
        h, switch, boom = self._handler(boom_down=True)
        h._needs_unmute = True
        h.poll(fd=None)
        # Use the real monotonic deadline (independent of host uptime)
        deadline = h._pulse_deadline
        self.assertIsNotNone(deadline)
        h.tick(fd=None, now=deadline - 0.5)
        self.assertEqual(switch.on, False)
        self.assertIsNotNone(h._pulse_deadline)
        h.tick(fd=None, now=deadline - 0.01)
        self.assertEqual(switch.on, False)
        h.tick(fd=None, now=deadline)
        self.assertEqual(switch.on, True)
        self.assertIsNone(h._pulse_deadline)

    def test_start_pulse_uses_injected_now(self):
        h, switch, boom = self._handler(boom_down=True)
        h.start_unmute_pulse(now=10.0, hold_s=1.5)
        self.assertEqual(switch.writes, [False])
        h.tick(fd=None, now=11.49)
        self.assertEqual(switch.on, False)
        h.tick(fd=None, now=11.5)
        self.assertEqual(switch.on, True)

    def test_boom_up_mid_pulse_stays_muted(self):
        h, switch, boom = self._handler(boom_down=True)
        h.start_unmute_pulse(now=0.0, hold_s=1.5)
        boom["down"] = False
        h.tick(fd=None, now=1.5)
        self.assertEqual(switch.on, False)
        self.assertIsNone(h._pulse_deadline)

    def test_handle_report_stale_flag_starts_pulse(self):
        h, switch, boom = self._handler(boom_down=True, switch_on=False)
        h._needs_unmute = True
        h._boom_down = True
        h.handle_report(bytes((MIC_REPORT, BUTTON)), fd=None)
        self.assertEqual(switch.writes, [False])
        self.assertIsNotNone(h._pulse_deadline)

    def test_button_gate_skips_mute(self):
        h, switch, boom = self._handler()
        h.button_gate = lambda: True
        h.handle_report(bytes((MIC_REPORT, BUTTON)), fd=None)
        self.assertEqual(switch.writes, [])

    def test_g0_next_does_not_toggle_mute(self):
        from g935.bindings import Binding
        h, switch, boom = self._handler()
        h.button_binding = lambda: Binding("next_track")
        with mock.patch("g935.bindings.fire_headless",
                        return_value=(True, "ok")) as fire:
            h.handle_report(bytes((MIC_REPORT, BUTTON)), fd=None)
        fire.assert_called_once()
        self.assertEqual(switch.writes, [])

    def test_hardware_mode_ignores_reports(self):
        h, switch, boom = self._handler(mode="hardware")
        h.handle_report(bytes((MIC_REPORT, BOOM_UP)), fd=None)
        self.assertEqual(switch.writes, [])
        h._needs_unmute = True
        h.poll(fd=None)
        self.assertEqual(switch.writes, [])

    def test_seconds_until_tick(self):
        h, switch, boom = self._handler()
        self.assertIsNone(h.seconds_until_tick(now=0))
        h.start_unmute_pulse(now=5.0, hold_s=UNMUTE_HOLD_S)
        self.assertAlmostEqual(h.seconds_until_tick(now=5.0), UNMUTE_HOLD_S)
        self.assertEqual(h.seconds_until_tick(now=20.0), 0.0)


if __name__ == "__main__":
    unittest.main()
