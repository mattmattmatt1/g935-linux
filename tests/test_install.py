import os
import unittest


class InstallScriptTests(unittest.TestCase):
    def _install_sh(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "install.sh")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_autostart_exec_is_absolute(self):
        # systemd-xdg-autostart-generator skips relative Exec= binaries
        # that are not on its early-session PATH (typically no ~/.local/bin).
        text = self._install_sh()
        self.assertIn("Exec=$BIN/g935-control --tray", text)
        self.assertIn("TryExec=$BIN/g935-control", text)
        self.assertNotIn("\nExec=g935-control --tray\n", text)

    def test_applications_exec_is_absolute(self):
        text = self._install_sh()
        self.assertIn("Exec=$BIN/g935-control\n", text)
        self.assertNotIn("\nExec=g935-control\n", text)
