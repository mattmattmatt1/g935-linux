"""Earcup volume-wheel raw capture & auto-fit calibration dialog."""
from __future__ import annotations

import fcntl
import json
import os
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from g935.paths import config_dir, ensure_config_dir
from g935.ui.theme import style
from g935.volume_wheel import (
    EVIOCGRAB, INPUT_EVENT, KEY_VOLUMEDOWN, KEY_VOLUMEUP,
    analyze_calibration, find_wheel_device, parse_key_events,
)


def _capture_file() -> str:
    return os.path.join(config_dir(), "wheel-capture.json")


class WheelCalibrationWizard:
    """Modal wizard that grabs the wheel evdev node and fits timing parameters."""

    STAGES = (
        ("slow_up", "Slow upward calibration",
         "Make 5 small, deliberate upward rolls—the finest movements you "
         "would use near your target volume. Then click Next.",
         5, KEY_VOLUMEUP),
        ("slow_down", "Slow downward calibration",
         "Make 5 small, deliberate downward rolls. Pause between them as you "
         "would for fine adjustment, then click Next.",
         5, KEY_VOLUMEDOWN),
        ("fast_up", "Fast upward calibration",
         "Make 3 separate fast upward sweeps toward 100%. Let the receiver "
         "release fully between sweeps, then click Next.",
         3, KEY_VOLUMEUP),
        ("fast_down", "Fast downward calibration",
         "Make 3 separate fast downward sweeps toward 0%. Let the receiver "
         "release fully between sweeps, then click Next.",
         3, KEY_VOLUMEDOWN),
    )

    def __init__(self, app):
        self.app = app
        self.dialog: Gtk.Dialog | None = None
        self._fd: int | None = None
        self._watch: int | None = None
        self._stage: str | None = None
        self._target = 0
        self._expected = KEY_VOLUMEUP
        self._samples = {
            "slow_up": [], "slow_down": [],
            "fast_up": [], "fast_down": [],
        }
        self._pending: dict = {}
        self._raw: list[dict] = []
        self._saved = False
        self._widgets: dict = {}

    def cancel(self) -> None:
        if self.dialog is not None:
            self.dialog.response(Gtk.ResponseType.CANCEL)

    def run(self) -> None:
        app = self.app
        # Prefer daemon IPC pause so a crash never leaves wheel_enabled=False.
        used_ipc_pause = app.worker.pause_wheel(True)
        original_enabled = app.settings.data["wheel_enabled"]
        if not used_ipc_pause:
            app.settings.data["wheel_enabled"] = False
            app.settings.save()
            app.wheel_sw.handler_block_by_func(app.on_wheel_setting_changed)
            app.wheel_sw.set_active(False)
            app.wheel_sw.handler_unblock_by_func(app.on_wheel_setting_changed)

        dialog = Gtk.Dialog(
            title="Calibrate G935 volume wheel",
            transient_for=app, modal=True)
        dialog.set_default_size(620, 460)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        next_button = dialog.add_button("Next", Gtk.ResponseType.APPLY)
        next_button.set_sensitive(False)
        self.dialog = dialog

        box = dialog.get_content_area()
        box.set_spacing(12)
        box.set_border_width(16)

        heading = Gtk.Label(xalign=0)
        style(heading, "g935-primary-title")
        heading.set_line_wrap(True)
        box.pack_start(heading, False, False, 0)

        instruction = Gtk.Label(xalign=0)
        instruction.set_line_wrap(True)
        box.pack_start(instruction, False, False, 0)

        progress = Gtk.ProgressBar()
        progress.set_show_text(True)
        box.pack_start(progress, False, False, 0)

        live = Gtk.Label(xalign=0)
        live.set_selectable(True)
        live.set_line_wrap(True)
        live.set_size_request(-1, 125)
        style(live, "g935-zone")
        box.pack_start(live, False, False, 0)

        target_row = Gtk.Box(spacing=8)
        target_row.pack_start(
            Gtk.Label(label="Target fast rolls from 0–100%", xalign=0),
            True, True, 0)
        target_rolls = Gtk.SpinButton.new_with_range(3, 5, 1)
        target_rolls.set_value(3)
        target_row.pack_end(target_rolls, False, False, 0)
        box.pack_start(target_row, False, False, 0)

        foot = Gtk.Label(xalign=0)
        style(foot, "g935-caption")
        foot.set_line_wrap(True)
        foot.set_text(
            "The daemon pauses the earcup wheel while this wizard is open. "
            "Volume will not change during capture.")
        box.pack_start(foot, False, False, 0)

        self._widgets = {
            "heading": heading, "instruction": instruction,
            "progress": progress, "live": live, "next": next_button,
        }

        GLib.timeout_add(150, self._try_open)
        result = None
        applied = False
        try:
            for key, title, copy, target, expected_code in self.STAGES:
                self._stage = key
                self._target = target
                self._expected = expected_code
                heading.set_text(title)
                instruction.set_text(copy)
                next_button.set_label("Next")
                self._update_live()
                dialog.show_all()
                if dialog.run() != Gtk.ResponseType.APPLY:
                    break
            else:
                self._stage = None
                result = analyze_calibration(
                    self._samples, int(target_rolls.get_value()))
                capture_path = self._save_capture(result)
                counts = result["counts"]
                fast_ms = [round(v * 1000) for v in result["fast_gaps"]]
                slow_ms = [round(v * 1000) for v in result["slow_gaps"]]
                slow_holds_ms = [round(v * 1000) for v in result["slow_holds"]]
                fast_holds_ms = [round(v * 1000) for v in result["fast_holds"]]
                heading.set_text("Calibration result")
                instruction.set_text(
                    "Review the measured data below. Apply saves this profile "
                    "and returns the wheel to the background service.")
                live.set_text(
                    "Headset-reported rolls\n"
                    f"  Slow up {counts['slow_up']}/5 · "
                    f"Slow down {counts['slow_down']}/5\n"
                    f"  Fast up {counts['fast_up']}/3 · "
                    f"Fast down {counts['fast_down']}/3\n"
                    f"  Opposite-direction reports: {result['wrong_directions']}\n\n"
                    f"Raw input events captured: {len(self._raw)}\n"
                    f"Neutral gaps · fast {fast_ms or ['none']} ms · "
                    f"slow {slow_ms or ['none']} ms\n"
                    f"Fine holds: {slow_holds_ms or ['none']} ms\n"
                    f"Fast holds: {fast_holds_ms or ['none']} ms\n"
                    f"Duration decoder · first {result['wheel_fine_step']}% immediately · "
                    f"gentle steps every {result['wheel_fine_interval_ms']} ms before "
                    f"{result['wheel_hold_delay_ms']} ms · then fast rate every "
                    f"{result['wheel_hold_interval_ms']} ms\n"
                    f"Maximum per sweep: {result['wheel_step']}%\n\n"
                    f"Full event trace saved to {capture_path}")
                progress.set_fraction(1)
                progress.set_text("Ready to apply")
                next_button.set_label("Apply calibration")
                next_button.set_sensitive(True)
                dialog.show_all()
                if dialog.run() == Gtk.ResponseType.APPLY:
                    for key, value in result.items():
                        if key.startswith("wheel_"):
                            app.settings.data[key] = value
                    applied = True
        finally:
            if self._raw and not self._saved:
                self._save_capture(result)
            self._cleanup()
            dialog.destroy()
            self.dialog = None
            if used_ipc_pause:
                app.worker.pause_wheel(False)
            else:
                app.settings.data["wheel_enabled"] = original_enabled
                app.wheel_sw.handler_block_by_func(app.on_wheel_setting_changed)
                app.wheel_sw.set_active(original_enabled)
                app.wheel_sw.handler_unblock_by_func(app.on_wheel_setting_changed)
            app.settings.save()
            if applied and result is not None:
                app._sync_wheel_feel_controls()
            app._refresh_wheel_calibration_status()

    def _try_open(self) -> bool:
        if self.dialog is None or self._fd is not None:
            return False
        path = find_wheel_device()
        if path is None:
            self._widgets["live"].set_text("Waiting for the G935 input device…")
            return True
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            self._widgets["live"].set_text(
                f"Waiting for the daemon to release {path}…\n{exc}")
            return True
        self._fd = fd
        self._watch = GLib.io_add_watch(
            fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_io)
        self._widgets["next"].set_sensitive(True)
        self._update_live()
        return False

    def _on_io(self, fd, condition) -> bool:
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            self._watch = None
            self._widgets["live"].set_text(
                "Wheel device disconnected. Reconnect it and restart calibration.")
            return False
        try:
            data = os.read(fd, INPUT_EVENT.size * 64)
        except (BlockingIOError, OSError):
            return True
        stage = self._stage
        if stage is None:
            return True
        events = self._samples[stage]
        for timestamp, code, value in parse_key_events(data):
            self._raw.append({
                "stage": stage, "timestamp": timestamp,
                "code": code, "value": value,
            })
            if value == 1:
                event = {"press": timestamp, "release": None, "code": code}
                events.append(event)
                self._pending[code] = event
            elif value == 0:
                event = self._pending.pop(code, None)
                if event is not None:
                    event["release"] = timestamp
        self._update_live()
        return True

    def _update_live(self) -> None:
        stage = self._stage
        if not self._widgets or stage is None:
            return
        events = self._samples[stage]
        expected = self._expected
        correct = sum(e["code"] == expected for e in events)
        opposite = len(events) - correct
        holds = [
            (e["release"] - e["press"]) * 1000
            for e in events if e["release"] is not None
        ]
        direction = "UP" if expected == KEY_VOLUMEUP else "DOWN"
        last = events[-1] if events else None
        last_text = (
            ("UP" if last["code"] == KEY_VOLUMEUP else "DOWN")
            if last else "waiting")
        stage_raw = [e for e in self._raw if e["stage"] == stage]
        origin = stage_raw[0]["timestamp"] if stage_raw else 0
        state_names = {0: "release", 1: "press", 2: "repeat"}
        trace = "\n".join(
            f"+{(e['timestamp'] - origin) * 1000:6.0f} ms  "
            f"{'UP  ' if e['code'] == KEY_VOLUMEUP else 'DOWN'} "
            f"{state_names.get(e['value'], str(e['value']))}"
            for e in stage_raw[-8:]
        )
        self._widgets["progress"].set_fraction(
            min(1, correct / max(1, self._target)))
        self._widgets["progress"].set_text(
            f"{correct} of {self._target} "
            f"{'sweeps' if stage.startswith('fast_') else 'movements'} "
            f"reported {direction}")
        self._widgets["live"].set_text(
            f"Expected direction: {direction}\n"
            f"Reported: {correct} expected · {opposite} opposite · "
            f"{len(events)} total\n"
            f"Last report: {last_text}\n"
            f"Completed hold durations: "
            f"{', '.join(f'{v:.0f} ms' for v in holds[-6:]) or '—'}\n"
            f"Raw timeline ({len(stage_raw)} events):\n{trace or 'waiting'}")

    def _save_capture(self, result=None) -> str:
        origin = self._raw[0]["timestamp"] if self._raw else 0
        state_names = {0: "release", 1: "press", 2: "repeat"}
        payload = {
            "version": 1,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "device": find_wheel_device(),
            "events": [
                {
                    "t_ms": round((e["timestamp"] - origin) * 1000, 3),
                    "stage": e["stage"],
                    "direction": "up" if e["code"] == KEY_VOLUMEUP else "down",
                    "state": state_names.get(e["value"], str(e["value"])),
                    "code": e["code"],
                    "value": e["value"],
                }
                for e in self._raw
            ],
            "gestures": self._samples,
            "analysis": result,
        }
        ensure_config_dir()
        path = _capture_file()
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
            self._saved = True
            return path
        except OSError as exc:
            self.app.log(f"--- could not save wheel capture: {exc} ---")
            return f"capture could not be saved ({exc})"

    def _cleanup(self) -> None:
        if self._watch is not None:
            try:
                GLib.source_remove(self._watch)
            except Exception:
                pass
            self._watch = None
        if self._fd is not None:
            try:
                fcntl.ioctl(self._fd, EVIOCGRAB, 0)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
