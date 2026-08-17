"""App id and icon paths for the window, tray, and .desktop file.

Plasma 6 on Wayland matches the GTK app-id to <id>.desktop. The id must be
set before Gtk is imported, and a themed icon named the same as the id must
exist or the taskbar tile stays empty.
"""
from __future__ import annotations

import os

from g935.paths import runtime_dir

APP_ID = "g935-control"
ICON_NAME = "g935-control"
FALLBACK_ICON_NAME = "audio-headset"
ICON_FILENAME = "g935-control.svg"


def icon_file_candidates() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    return [
        os.path.join(root, "icons", ICON_FILENAME),
        os.path.join(data_home, "g935-linux", "icons", ICON_FILENAME),
        os.path.join(data_home, "icons", "hicolor", "scalable", "apps",
                     ICON_FILENAME),
    ]


def icon_file() -> str | None:
    for path in icon_file_candidates():
        if os.path.isfile(path):
            return path
    return None


def icon_theme_dir() -> str | None:
    path = icon_file()
    return os.path.dirname(path) if path else None


def tray_png_path() -> str:
    return os.path.join(runtime_dir(), "g935-tray-icon.png")
