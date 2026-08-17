"""G-key action bindings. Persist as ui.json['bindings'].

Each G-key has a tap and a long-hold action. The earcup mic button is not a
G-key and is not stored here.
"""
from __future__ import annotations

from dataclasses import dataclass

KEYS = ("g0", "g1", "g2", "g3")
KEY_LABELS = {"g0": "G0", "g1": "G1", "g2": "G2", "g3": "G3"}
# 0x8010 bits 0..2 are the three printed G-keys (G1–G3). G0 is the earcup mute.
HID_KEYS = ("g1", "g2", "g3")
LAYERS = ("tap", "hold")
LAYER_LABELS = {"tap": "Tap", "hold": "Hold"}

# (id, label) — order is the combo order
ACTIONS = (
    ("none", "Do nothing"),
    ("play_pause", "Play / pause"),
    ("next_track", "Next track"),
    ("prev_track", "Previous track"),
    ("vol_up", "Volume up"),
    ("vol_down", "Volume down"),
    ("mute_output", "Mute / unmute output"),
    ("mic_mute", "Toggle host mic mute"),
    ("show_panel", "Show control panel"),
    ("next_eq", "Next EQ preset"),
    ("prev_eq", "Previous EQ preset"),
    ("toggle_mode", "Toggle software mode"),
    ("command", "Run a command"),
)
ACTION_IDS = frozenset(a for a, _ in ACTIONS)
ACTION_LABELS = dict(ACTIONS)

# Physical bottom→top: G0 (earcup mute button), G1, G2, G3.
DEFAULT_BINDINGS = {
    "g0": {
        "tap": {"action": "next_track", "command": ""},
        "hold": {"action": "none", "command": ""},
    },
    "g1": {
        "tap": {"action": "play_pause", "command": ""},
        "hold": {"action": "show_panel", "command": ""},
    },
    "g2": {
        "tap": {"action": "prev_track", "command": ""},
        "hold": {"action": "vol_up", "command": ""},
    },
    "g3": {
        "tap": {"action": "mic_mute", "command": ""},
        "hold": {"action": "vol_down", "command": ""},
    },
}
BINDINGS_VERSION = 3
DEFAULT_HOLD_S = 0.45

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_IFACE = "org.mpris.MediaPlayer2.Player"


@dataclass(frozen=True)
class Binding:
    action: str
    command: str = ""

    def as_dict(self) -> dict:
        return {"action": self.action, "command": self.command}


@dataclass(frozen=True)
class KeyBinding:
    tap: Binding
    hold: Binding

    def as_dict(self) -> dict:
        return {"tap": self.tap.as_dict(), "hold": self.hold.as_dict()}

    def layer(self, name: str) -> Binding:
        return self.hold if name == "hold" else self.tap


def _parse_binding(value, fallback: Binding) -> Binding:
    action = fallback.action
    command = fallback.command
    if isinstance(value, str) and value in ACTION_IDS:
        return Binding(value, "")
    if not isinstance(value, dict):
        return fallback
    cand = value.get("action", action)
    if cand in ACTION_IDS:
        action = cand
    if isinstance(value.get("command"), str):
        command = value["command"]
    return Binding(action, command)


def fire_headless(binding: Binding) -> tuple[bool, str]:
    """Actions the daemon can run without the GTK panel."""
    if binding.action == "none":
        return True, "none"
    if binding.action == "play_pause":
        return media_call("PlayPause")
    if binding.action == "next_track":
        return media_call("Next")
    if binding.action == "prev_track":
        return media_call("Previous")
    if binding.action == "command":
        return run_command(binding.command)
    return False, f"{binding.action} needs the panel"


def normalize_bindings(raw, version: int = BINDINGS_VERSION) -> dict[str, KeyBinding]:
    src = raw if isinstance(raw, dict) else {}
    if version < 3:
        # New G0 + bottom-to-top transport order.
        src = {}
    out: dict[str, KeyBinding] = {}
    for key in KEYS:
        default = DEFAULT_BINDINGS[key]
        value = src.get(key, default)
        tap_fb = _parse_binding(default["tap"], Binding("none"))
        hold_fb = _parse_binding(default["hold"], Binding("none"))
        if isinstance(value, dict) and (
                "tap" in value or "hold" in value):
            tap = _parse_binding(value.get("tap"), tap_fb)
            hold = _parse_binding(value.get("hold"), hold_fb)
        else:
            # v1: {action, command} or a bare action string
            tap = _parse_binding(value, tap_fb)
            hold = hold_fb
        out[key] = KeyBinding(tap, hold)
    return out


def bindings_to_data(bindings: dict[str, KeyBinding]) -> dict:
    return {key: bindings[key].as_dict() for key in KEYS}


def key_for_index(index: int) -> str | None:
    """HID++ G-key bit index → g1/g2/g3 (not g0)."""
    if 0 <= index < len(HID_KEYS):
        return HID_KEYS[index]
    return None


def cycle_name(names: list[str], current: str, step: int) -> str | None:
    if not names:
        return None
    try:
        index = names.index(current)
    except ValueError:
        index = -1 if step > 0 else 0
    return names[(index + step) % len(names)]


class HoldTracker:
    """Classify G-key down/up into tap vs hold. Hold fires once while held."""

    def __init__(self, hold_s: float = DEFAULT_HOLD_S):
        if hold_s <= 0:
            raise ValueError("hold_s must be positive")
        self.hold_s = hold_s
        self._down_at: dict[str, float] = {}
        self._held: set[str] = set()

    def press(self, key: str, now: float) -> None:
        self._down_at[key] = now
        self._held.discard(key)

    def release(self, key: str) -> str | None:
        if key not in self._down_at:
            return None
        del self._down_at[key]
        if key in self._held:
            self._held.discard(key)
            return None
        return "tap"

    def due(self, now: float) -> list[str]:
        fired = []
        for key, started in self._down_at.items():
            if key in self._held:
                continue
            if now - started >= self.hold_s:
                self._held.add(key)
                fired.append(key)
        return fired

    def pending(self) -> bool:
        return any(k not in self._held for k in self._down_at)


KDE_MEDIA = {
    "PlayPause": "playpausemedia",
    "Next": "nextmedia",
    "Previous": "previousmedia",
}


def kde_media(method: str) -> tuple[bool, str]:
    """Invoke Plasma's media keys (same as the keyboard media cluster)."""
    shortcut = KDE_MEDIA.get(method)
    if not shortcut:
        return False, f"no KDE shortcut for {method}"
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.kde.kglobalaccel", "/component/mediacontrol",
            "org.kde.kglobalaccel.Component", "invokeShortcut",
            GLib.Variant("(s)", (shortcut,)), None,
            Gio.DBusCallFlags.NONE, 400, None)
    except Exception as exc:
        return False, f"KDE media failed ({exc})"
    return True, f"KDE {shortcut}"


def mpris_call(method: str) -> tuple[bool, str]:
    """Call PlayPause / Next / Previous on a session MPRIS player."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception as exc:
        return False, f"MPRIS unavailable ({exc})"
    if method not in ("PlayPause", "Next", "Previous"):
        return False, f"bad MPRIS method {method}"
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "ListNames", None,
            GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 400, None)
        names = [n for n in reply.unpack()[0] if n.startswith(MPRIS_PREFIX)]
    except Exception as exc:
        return False, f"MPRIS list failed ({exc})"
    if not names:
        return False, "no media player"
    chosen = names[0]
    for name in names:
        try:
            prop = bus.call_sync(
                name, MPRIS_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (MPRIS_IFACE, "PlaybackStatus")),
                GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 300, None)
            status = prop.unpack()[0]
            if status == "Playing":
                chosen = name
                break
        except Exception:
            continue
    try:
        bus.call_sync(
            chosen, MPRIS_PATH, MPRIS_IFACE, method, None, None,
            Gio.DBusCallFlags.NONE, 400, None)
    except Exception as exc:
        return False, f"{method} failed ({exc})"
    short = chosen[len(MPRIS_PREFIX):]
    return True, f"{method} → {short}"


def media_call(method: str) -> tuple[bool, str]:
    """Prefer Plasma media shortcuts, then MPRIS, then playerctl."""
    ok, detail = kde_media(method)
    if ok:
        return ok, detail
    ok2, detail2 = mpris_call(method)
    if ok2:
        return ok2, detail2
    import shutil
    import subprocess
    playerctl = shutil.which("playerctl")
    if playerctl:
        flag = {"PlayPause": "play-pause", "Next": "next",
                "Previous": "previous"}.get(method)
        if flag:
            try:
                subprocess.run(
                    [playerctl, flag], check=False, timeout=1,
                    capture_output=True)
                return True, f"playerctl {flag}"
            except Exception as exc:
                return False, f"playerctl failed ({exc})"
    return False, f"{detail}; {detail2}"


def run_command(command: str) -> tuple[bool, str]:
    text = (command or "").strip()
    if not text:
        return False, "empty command"
    import subprocess
    try:
        subprocess.Popen(text, shell=True, start_new_session=True)
    except OSError as exc:
        return False, str(exc)
    return True, f"ran {text!r}"


def describe(binding: Binding) -> str:
    if binding.action == "command":
        return binding.command.strip() or "command (empty)"
    return ACTION_LABELS.get(binding.action, binding.action)
