import os
import time
import unittest
from types import SimpleNamespace

from g935.volume_wheel import (
    DEFAULT_CEILING, EV_KEY, INPUT_EVENT, KEY_VOLUMEDOWN, KEY_VOLUMEUP,
    REVERSAL_BURST_LIMIT, VolumeWheel, accelerated_step, analyze_calibration,
    clamp_volume_boost, key_caps_has, parse_evdev_key_caps, parse_key_events,
)


class KeyCapsTests(unittest.TestCase):
    def test_live_g935_volume_bits(self):
        # /proc/bus/input/devices KEY=c000000000000 0 on this receiver
        text = "c000000000000 0"
        self.assertTrue(key_caps_has(text, KEY_VOLUMEDOWN, KEY_VOLUMEUP))
        self.assertFalse(key_caps_has(text, 1))

    def test_empty_and_32bit(self):
        self.assertFalse(key_caps_has("", KEY_VOLUMEUP))
        # bit 1 set in a single 32-bit word
        self.assertTrue(key_caps_has("2", 1))
        self.assertEqual(parse_evdev_key_caps("2"), 2)


class ParseAndMathTests(unittest.TestCase):
    def test_parse_key_events(self):
        payload = (
            INPUT_EVENT.pack(1, 0, EV_KEY, KEY_VOLUMEUP, 1)
            + INPUT_EVENT.pack(1, 5000, 0, 0, 0)
            + INPUT_EVENT.pack(1, 10000, EV_KEY, KEY_VOLUMEUP, 0)
        )
        events = list(parse_key_events(payload))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][1], KEY_VOLUMEUP)
        self.assertEqual(events[0][2], 1)
        self.assertEqual(events[1][2], 0)

    def test_accelerated_step_endpoints(self):
        self.assertEqual(accelerated_step(48, None), 1)
        self.assertEqual(accelerated_step(48, 2.0), 1)
        self.assertEqual(accelerated_step(48, 0.1), 48)

    def test_analyze_calibration_uses_holds(self):
        stages = {
            "slow_up": [
                {"press": 0.0, "release": 0.08, "code": KEY_VOLUMEUP},
                {"press": 0.4, "release": 0.48, "code": KEY_VOLUMEUP},
            ],
            "slow_down": [
                {"press": 1.0, "release": 1.08, "code": KEY_VOLUMEDOWN},
            ],
            "fast_up": [
                {"press": 2.0, "release": 3.1, "code": KEY_VOLUMEUP},
            ],
            "fast_down": [
                {"press": 4.0, "release": 5.0, "code": KEY_VOLUMEDOWN},
            ],
        }
        result = analyze_calibration(stages)
        self.assertGreaterEqual(result["wheel_step"], 10)
        self.assertTrue(result["wheel_calibrated"])
        self.assertEqual(result["wrong_directions"], 0)


class FakePactl:
    def __init__(self, vol=40):
        self.vol = vol
        self.sets = []
        self.targets = []
        self.sinks = ""
        self.failures = 0

    def __call__(self, *args):
        if args[0] == "list":
            return SimpleNamespace(
                returncode=0, stdout=self.sinks, stderr="")
        if self.failures:
            self.failures -= 1
            return SimpleNamespace(returncode=1, stdout="", stderr="fail")
        if args[0] == "get-sink-volume":
            self.targets.append(args[1])
            return SimpleNamespace(
                returncode=0, stdout=f"Volume: {self.vol}% / {self.vol}%",
                stderr="")
        if args[0] == "set-sink-volume":
            self.targets.append(args[1])
            self.vol = int(str(args[2]).rstrip("%"))
            self.sets.append(self.vol)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown")


def _key_event(ts, code, value):
    sec = int(ts)
    usec = int(round((ts - sec) * 1_000_000))
    return INPUT_EVENT.pack(sec, usec, EV_KEY, code, value)


class VolumeScaleTests(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp_volume_boost(80), 100)
        self.assertEqual(clamp_volume_boost(150), 150)
        self.assertEqual(clamp_volume_boost(250), 200)
        self.assertEqual(clamp_volume_boost("nope"), 150)


class VolumeWheelTests(unittest.TestCase):
    def _wheel(self, vol=40, max_step=20):
        pactl = FakePactl(vol)
        w = VolumeWheel(
            settings_loader=lambda: (True, max_step),
            calibration_loader=lambda: {
                "fine_step": 1,
                "fine_interval": 0.083,
                "fast_gap": 0.75,
                "medium_gap": 1.50,
                "direction_guard": 0.0,
                "hold_delay": 0.25,
                "hold_interval": 0.025,
            },
            command_runner=pactl,
        )
        r, wr = os.pipe()
        os.set_blocking(r, False)
        w.fd = r
        w.path = "/dev/null"
        w._write = wr
        w._pactl = pactl
        return w

    def _feed(self, wheel, *chunks):
        os.write(wheel._write, b"".join(chunks))
        wheel.handle_ready()

    def _flush(self, wheel, steps=80):
        now = time.monotonic()
        for i in range(steps):
            wheel.tick(now=now + i * 0.025)
        return wheel._pactl.sets

    def tearDown(self):
        # handle_ready / close may already have closed the pipe
        pass

    def test_wheel_targets_g935_sink_when_present(self):
        w = self._wheel(40)
        w._pactl.sinks = (
            "179576\talsa_output.pci-0000_0a_00.1.hdmi-stereo\tPipeWire\t"
            "s32le 2ch 48000Hz\tRUNNING\n"
            "108\talsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo"
            "\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
        )
        try:
            self._feed(
                w,
                _key_event(1.0, KEY_VOLUMEUP, 1),
                _key_event(1.05, KEY_VOLUMEUP, 0),
            )
            self._flush(w)
            self.assertEqual(w._pactl.sets, [41])
            self.assertTrue(w._pactl.targets)
            self.assertTrue(all(
                "Logitech_G935" in name for name in w._pactl.targets))
        finally:
            os.close(w._write)
            w.close()

    def test_failed_sink_list_keeps_known_g935_name(self):
        w = self._wheel(40)
        w._pactl.sinks = (
            "108\talsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo"
            "\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
        )
        try:
            self.assertIn("Logitech_G935", w._volume_target())
            w._sink_until = 0

            def list_fails(*args):
                if args[0] == "list":
                    return SimpleNamespace(
                        returncode=1, stdout="", stderr="fail")
                return w._pactl(*args)

            w.command_runner = list_fails
            self.assertIn("Logitech_G935", w._volume_target())
            w._sink_until = 0
            w.command_runner = w._pactl
            w._pactl.sinks = (
                "179576\talsa_output.pci-0000_0a_00.1.hdmi-stereo\tPipeWire\t"
                "s32le 2ch 48000Hz\tRUNNING\n"
            )
            self.assertEqual(w._volume_target(), "@DEFAULT_SINK@")
        finally:
            os.close(w._write)
            w.close()

    def test_fine_press_changes_one_percent(self):
        w = self._wheel(40)
        try:
            self._feed(
                w,
                _key_event(1.0, KEY_VOLUMEUP, 1),
                _key_event(1.05, KEY_VOLUMEUP, 0),
            )
            self._flush(w)
            self.assertEqual(w._pactl.sets, [41])
        finally:
            os.close(w._write)
            w.close()

    def test_hold_adds_progressive_steps(self):
        w = self._wheel(40, max_step=5)
        try:
            self._feed(w, _key_event(1.0, KEY_VOLUMEUP, 1))
            now = time.monotonic()
            for i in range(8):
                w.tick(now=now + 0.08 * (i + 1))
            self.assertGreaterEqual(len(w._pactl.sets), 2)
            self.assertLessEqual(w._pactl.vol, 45)
            self.assertEqual(w._pactl.sets[-1], w._pactl.vol)
        finally:
            os.close(w._write)
            w.close()

    def test_wheel_up_crosses_hardware_100(self):
        w = self._wheel(100)
        try:
            self._feed(
                w,
                _key_event(1.0, KEY_VOLUMEUP, 1),
                _key_event(1.05, KEY_VOLUMEUP, 0),
            )
            self._flush(w)
            self.assertEqual(w._pactl.sets, [101])
            self.assertEqual(w._pactl.vol, 101)
        finally:
            os.close(w._write)
            w.close()

    def test_wheel_down_from_boost(self):
        w = self._wheel(150)
        try:
            self._feed(
                w,
                _key_event(1.0, KEY_VOLUMEDOWN, 1),
                _key_event(1.05, KEY_VOLUMEDOWN, 0),
            )
            self._flush(w)
            self.assertEqual(w._pactl.sets, [149])
            self.assertEqual(w._pactl.vol, 149)
        finally:
            os.close(w._write)
            w.close()

    def test_wheel_up_stops_at_ceiling(self):
        w = self._wheel(DEFAULT_CEILING)
        try:
            self._feed(
                w,
                _key_event(1.0, KEY_VOLUMEUP, 1),
                _key_event(1.05, KEY_VOLUMEUP, 0),
            )
            self._flush(w)
            self.assertEqual(w._pactl.sets, [])
            self.assertEqual(w._pactl.vol, DEFAULT_CEILING)
        finally:
            os.close(w._write)
            w.close()

    def test_reversal_cancels_queued_ramp(self):
        w = self._wheel(50, max_step=30)
        try:
            self._feed(w, _key_event(1.0, KEY_VOLUMEUP, 1))
            now = time.monotonic()
            w.tick(now=now + 0.3)
            # opposite edge before release of the previous direction
            self._feed(
                w,
                _key_event(1.4, KEY_VOLUMEDOWN, 1),
                _key_event(1.45, KEY_VOLUMEDOWN, 0),
            )
            after = list(w._pactl.sets)
            self._flush(w)
            # down 1% from wherever the ramp currently is; no leftover up queue
            self.assertTrue(w._pactl.sets)
            self.assertLessEqual(w._pactl.vol, after[-1] if after else 51)
        finally:
            os.close(w._write)
            w.close()

    def test_reversal_burst_suppresses(self):
        w = self._wheel(50)
        try:
            t = 2.0
            for i in range(REVERSAL_BURST_LIMIT + 1):
                code = KEY_VOLUMEUP if i % 2 == 0 else KEY_VOLUMEDOWN
                self._feed(w, _key_event(t, code, 1), _key_event(t + 0.01, code, 0))
                t += 0.03
            self.assertGreater(w.suppressed_until, 0)
        finally:
            os.close(w._write)
            w.close()

    def test_three_failures_release_grab(self):
        w = self._wheel(40)
        try:
            w._pactl.failures = 3
            for i in range(3):
                t = 1.0 + i
                self._feed(
                    w,
                    _key_event(t, KEY_VOLUMEUP, 1),
                    _key_event(t + 0.05, KEY_VOLUMEUP, 0),
                )
            self.assertIsNone(w.fd)
        finally:
            try:
                os.close(w._write)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
