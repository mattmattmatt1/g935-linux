import os
import tempfile
import unittest
from unittest import mock


class DaemonStatusTests(unittest.TestCase):
    def test_not_running_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": td}):
                import importlib
                import g935.daemon_status as ds
                importlib.reload(ds)
                self.assertFalse(ds.daemon_running())

    def test_acquire_and_detect(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": td}):
                import importlib
                import g935.daemon_status as ds
                importlib.reload(ds)
                fd = ds.acquire_daemon_lock()
                self.assertIsNotNone(fd)
                self.assertTrue(ds.daemon_running())
                # second acquire fails
                self.assertIsNone(ds.acquire_daemon_lock())
                os.close(fd)
                # after release, not running
                self.assertFalse(ds.daemon_running())


if __name__ == "__main__":
    unittest.main()
