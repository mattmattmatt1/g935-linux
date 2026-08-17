import os
import unittest
import xml.etree.ElementTree as ET

from g935.app_icon import (
    APP_ID, ICON_FILENAME, ICON_NAME, icon_file, icon_file_candidates,
)


class AppIconTests(unittest.TestCase):
    def test_ids_match_desktop_basename(self):
        self.assertEqual(APP_ID, "g935-control")
        self.assertEqual(ICON_NAME, APP_ID)

    def test_packaged_svg_exists_and_is_svg(self):
        path = icon_file()
        self.assertIsNotNone(path, icon_file_candidates())
        self.assertTrue(path.endswith(ICON_FILENAME))
        tree = ET.parse(path)
        self.assertTrue(tree.getroot().tag.endswith("svg"))

    def test_repo_desktop_uses_app_icon(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        desktop = os.path.join(root, "g935-control.desktop")
        with open(desktop, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(f"Icon={ICON_NAME}", text)
        self.assertIn(f"StartupWMClass={APP_ID}", text)
