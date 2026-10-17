import unittest

from g935.audio import classify_pactl_event
from g935.stereo_route import is_g935_sink


class ClassifyPactlEventTests(unittest.TestCase):
    def test_stream_events_are_ignored(self):
        self.assertIsNone(
            classify_pactl_event("Event 'change' on sink-input #42"))
        self.assertIsNone(
            classify_pactl_event("Event 'new' on source-output #7"))
        self.assertIsNone(
            classify_pactl_event("Event 'remove' on client #3"))

    def test_volume_change_is_levels_only(self):
        self.assertEqual(
            classify_pactl_event("Event 'change' on sink #108"), "levels")
        self.assertEqual(
            classify_pactl_event("Event 'change' on source #109"), "levels")

    def test_device_topology(self):
        self.assertEqual(
            classify_pactl_event("Event 'new' on sink #200"), "devices")
        self.assertEqual(
            classify_pactl_event("Event 'remove' on source #3"), "devices")
        self.assertEqual(
            classify_pactl_event("Event 'change' on server"), "devices")
        self.assertEqual(
            classify_pactl_event("Event 'change' on card #1"), "devices")

    def test_blank_and_unknown(self):
        self.assertIsNone(classify_pactl_event(""))
        self.assertIsNone(classify_pactl_event("not an event"))


class G935SinkMatchTests(unittest.TestCase):
    def test_usb_node(self):
        self.assertTrue(is_g935_sink(
            "alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo",
            "G935 Gaming Headset Analog Stereo"))

    def test_other_devices(self):
        self.assertFalse(is_g935_sink(
            "alsa_output.pci-0000_0a_00.1.hdmi-stereo", "HDMI"))
        self.assertFalse(is_g935_sink("easyeffects_sink", "Easy Effects Sink"))
