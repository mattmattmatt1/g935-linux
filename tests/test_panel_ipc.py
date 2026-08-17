import os
import tempfile
import unittest
from unittest import mock

from g935.panel_ipc import bind_listener, notify_running_instance


class PanelIpcTests(unittest.TestCase):
    def test_notify_without_listener_is_false(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": td}):
                self.assertFalse(notify_running_instance())

    def test_notify_reaches_listener(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": td}):
                sock = bind_listener()
                try:
                    self.assertTrue(notify_running_instance())
                    data, _addr = sock.recvfrom(64)
                    self.assertEqual(data, b"present")
                finally:
                    sock.close()
