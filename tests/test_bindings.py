import unittest
from unittest import mock

from g935.bindings import (
    BINDINGS_VERSION, Binding, HoldTracker, KDE_MEDIA, bindings_to_data,
    cycle_name, describe, key_for_index, normalize_bindings, run_command,
)


class NormalizeTests(unittest.TestCase):
    def test_defaults_bottom_to_top_transport(self):
        got = normalize_bindings(None)
        self.assertEqual(got["g0"].tap.action, "next_track")
        self.assertEqual(got["g1"].tap.action, "play_pause")
        self.assertEqual(got["g2"].tap.action, "prev_track")
        self.assertEqual(got["g3"].tap.action, "mic_mute")

    def test_old_versions_take_new_defaults(self):
        got = normalize_bindings(
            {"g3": {"tap": {"action": "prev_track"}}}, version=2)
        self.assertEqual(got["g0"].tap.action, "next_track")
        self.assertEqual(got["g3"].tap.action, "mic_mute")

    def test_v3_keeps_explicit(self):
        got = normalize_bindings(
            {"g0": {"tap": {"action": "command", "command": "true"},
                    "hold": {"action": "none"}}},
            version=BINDINGS_VERSION)
        self.assertEqual(got["g0"].tap, Binding("command", "true"))

    def test_roundtrip(self):
        data = bindings_to_data(normalize_bindings(None))
        again = normalize_bindings(data, version=BINDINGS_VERSION)
        self.assertEqual(again["g1"].tap.action, "play_pause")
        self.assertEqual(again["g0"].tap.action, "next_track")


class HoldTrackerTests(unittest.TestCase):
    def test_short_press_is_tap(self):
        h = HoldTracker(0.45)
        h.press("g1", 1.0)
        self.assertEqual(h.due(1.2), [])
        self.assertEqual(h.release("g1"), "tap")

    def test_long_press_fires_hold_once_not_tap(self):
        h = HoldTracker(0.45)
        h.press("g1", 1.0)
        self.assertEqual(h.due(1.44), [])
        self.assertEqual(h.due(1.50), ["g1"])
        self.assertEqual(h.due(1.80), [])
        self.assertIsNone(h.release("g1"))

    def test_pending_while_waiting_for_hold(self):
        h = HoldTracker(0.45)
        h.press("g1", 1.0)
        self.assertTrue(h.pending())
        h.due(1.50)
        self.assertFalse(h.pending())

    def test_rejects_non_positive_hold(self):
        with self.assertRaises(ValueError):
            HoldTracker(0)


class HelperTests(unittest.TestCase):
    def test_hid_index_skips_g0(self):
        self.assertEqual(key_for_index(0), "g1")
        self.assertEqual(key_for_index(2), "g3")
        self.assertIsNone(key_for_index(3))

    def test_cycle_name(self):
        names = ["Flat", "Rock", "Jazz"]
        self.assertEqual(cycle_name(names, "Flat", 1), "Rock")
        self.assertEqual(cycle_name(names, "Jazz", 1), "Flat")
        self.assertIsNone(cycle_name([], "Flat", 1))

    def test_describe_command(self):
        self.assertEqual(describe(Binding("play_pause")), "Play / pause")
        self.assertEqual(describe(Binding("command", "true")), "true")

    def test_kde_media_shortcut_names(self):
        self.assertEqual(KDE_MEDIA["PlayPause"], "playpausemedia")
        self.assertEqual(KDE_MEDIA["Next"], "nextmedia")
        self.assertEqual(KDE_MEDIA["Previous"], "previousmedia")

    def test_run_command_empty(self):
        ok, detail = run_command("  ")
        self.assertFalse(ok)
        self.assertIn("empty", detail)

    def test_run_command_starts(self):
        with mock.patch("subprocess.Popen") as popen:
            ok, detail = run_command("true")
        self.assertTrue(ok)
        popen.assert_called_once()
        self.assertIn("true", detail)


class AppSettingsBindingsTests(unittest.TestCase):
    def test_old_ui_json_gets_defaults(self):
        import os
        import tempfile
        from unittest.mock import patch
        import importlib
        import g935.app_settings as app_settings

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": td}):
                importlib.reload(app_settings)
                settings = app_settings.AppSettings()
                self.assertEqual(
                    settings.data["bindings"]["g0"]["tap"]["action"],
                    "next_track")
                self.assertEqual(settings.data["bindings_version"], 3)
