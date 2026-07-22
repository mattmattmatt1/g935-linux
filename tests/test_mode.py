import os
import tempfile
import unittest
from unittest import mock


class ModeTests(unittest.TestCase):
    def test_default_is_hardware_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}):
                # re-import with patched env
                import importlib
                import g935.mode as mode
                importlib.reload(mode)
                self.assertEqual(mode.load_mode(), "hardware")

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}):
                import importlib
                import g935.mode as mode
                importlib.reload(mode)
                mode.save_mode("ghub")
                self.assertEqual(mode.load_mode(), "ghub")
                mode.save_mode("hardware")
                self.assertEqual(mode.load_mode(), "hardware")
                path = mode.mode_file()
                self.assertTrue(os.path.isfile(path))
                # atomic: no leftover .tmp
                self.assertFalse(os.path.exists(path + ".tmp"))

    def test_corrupt_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}):
                import importlib
                import g935.mode as mode
                importlib.reload(mode)
                os.makedirs(os.path.dirname(mode.mode_file()), exist_ok=True)
                with open(mode.mode_file(), "w") as f:
                    f.write("not-a-mode\n")
                self.assertEqual(mode.load_mode(), "hardware")

    def test_invalid_save_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}):
                import importlib
                import g935.mode as mode
                importlib.reload(mode)
                with self.assertRaises(ValueError):
                    mode.save_mode("turbo")


if __name__ == "__main__":
    unittest.main()
