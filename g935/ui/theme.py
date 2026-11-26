"""Modern GTK3 theme, card/chip builders, icon helpers, and scroll guards."""
from __future__ import annotations

import os

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from g935.app_icon import (
    FALLBACK_ICON_NAME, ICON_NAME, icon_file, tray_png_path,
)

APP_CSS = """
.g935-window {
    background-color: @theme_bg_color;
}
.g935-status {
    background-color: alpha(@theme_fg_color, 0.03);
    border-bottom: 1px solid alpha(@theme_fg_color, 0.09);
}
.g935-chip {
    background-color: alpha(@theme_fg_color, 0.065);
    border: 1px solid alpha(@theme_fg_color, 0.11);
    border-radius: 999px;
    padding: 4px 11px;
    font-size: 0.93em;
}
.g935-chip-ok {
    background-color: alpha(#2ec27e, 0.13);
    border-color: alpha(#2ec27e, 0.34);
}
.g935-chip-warn {
    background-color: alpha(#e5a50a, 0.14);
    border-color: alpha(#e5a50a, 0.36);
}
.g935-chip-bad {
    background-color: alpha(#e01b24, 0.14);
    border-color: alpha(#e01b24, 0.38);
}
.g935-card {
    background-color: alpha(@theme_fg_color, 0.032);
    border: 1px solid alpha(@theme_fg_color, 0.10);
    border-radius: 12px;
    padding: 14px 16px;
}
.g935-card-wear {
    background-color: alpha(#e5a50a, 0.07);
    border: 1px solid alpha(#e5a50a, 0.28);
    border-radius: 12px;
    padding: 14px 16px;
}
.g935-card-title {
    font-weight: 700;
    font-size: 1.04em;
}
.g935-primary-title {
    font-weight: 700;
    font-size: 1.04em;
}
.g935-subtitle {
    color: alpha(@theme_fg_color, 0.66);
    font-size: 0.92em;
}
.g935-caption {
    color: alpha(@theme_fg_color, 0.58);
    font-size: 0.90em;
}
.g935-badge {
    background-color: alpha(@theme_fg_color, 0.08);
    border: 1px solid alpha(@theme_fg_color, 0.12);
    border-radius: 999px;
    padding: 2px 10px;
    font-weight: 700;
    font-size: 0.88em;
}
.g935-badge-accent {
    background-color: alpha(#3584e4, 0.18);
    border-color: alpha(#3584e4, 0.45);
    color: #62a0ea;
}
.g935-badge-boost {
    background-color: alpha(#e5a50a, 0.18);
    border-color: alpha(#e5a50a, 0.48);
    color: #e5a50a;
}
.g935-badge-ok {
    background-color: alpha(#2ec27e, 0.16);
    border-color: alpha(#2ec27e, 0.42);
    color: #2ec27e;
}
.g935-badge-bad {
    background-color: alpha(#e01b24, 0.16);
    border-color: alpha(#e01b24, 0.42);
    color: #f66151;
}
.g935-key {
    background-color: alpha(@theme_fg_color, 0.06);
    border: 1px solid alpha(@theme_fg_color, 0.14);
    border-radius: 8px;
    padding: 4px 11px;
    font-weight: 700;
    color: alpha(@theme_fg_color, 0.55);
}
.g935-key-active {
    background-color: alpha(#3584e4, 0.28);
    border-color: #3584e4;
    color: @theme_fg_color;
}
.g935-zone {
    background-color: alpha(@theme_fg_color, 0.032);
    border: 1px solid alpha(@theme_fg_color, 0.09);
    border-radius: 10px;
    padding: 10px 12px;
}
.g935-kpi {
    background-color: alpha(@theme_fg_color, 0.035);
    border: 1px solid alpha(@theme_fg_color, 0.09);
    border-radius: 10px;
    padding: 10px 12px;
}
.g935-wear-copy {
    font-size: 0.92em;
    color: alpha(@theme_fg_color, 0.80);
}
.g935-expander {
    background-color: alpha(@theme_fg_color, 0.025);
    border: 1px solid alpha(@theme_fg_color, 0.10);
    border-radius: 12px;
    padding: 10px 14px;
}
.g935-preset-menu {
    padding: 12px;
}
.g935-preset-category {
    color: alpha(@theme_fg_color, 0.65);
    font-weight: 700;
    font-size: 0.88em;
    padding-top: 4px;
}
button.g935-preset-item {
    padding: 6px 12px;
}
.g935-info-key {
    color: alpha(@theme_fg_color, 0.60);
    font-size: 0.93em;
}
.g935-page {
    background-color: transparent;
}
.g935-card button {
    min-height: 28px;
}
.g935-divider {
    background-color: alpha(@theme_fg_color, 0.08);
    min-height: 1px;
}
"""


def install_app_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(APP_CSS.encode("utf-8"))
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def style(widget, *classes):
    ctx = widget.get_style_context()
    for css_class in classes:
        ctx.add_class(css_class)
    return widget


def set_chip_tone(widget, tone: str | None) -> None:
    ctx = widget.get_style_context()
    for name in ("g935-chip-ok", "g935-chip-warn", "g935-chip-bad"):
        ctx.remove_class(name)
    if tone in ("ok", "warn", "bad"):
        ctx.add_class(f"g935-chip-{tone}")


def set_badge_tone(widget, tone: str | None) -> None:
    ctx = widget.get_style_context()
    for name in ("g935-badge-accent", "g935-badge-boost",
                 "g935-badge-ok", "g935-badge-bad"):
        ctx.remove_class(name)
    if tone in ("accent", "boost", "ok", "bad"):
        ctx.add_class(f"g935-badge-{tone}")


def window_icon_name() -> str:
    theme = Gtk.IconTheme.get_default()
    if theme.has_icon(ICON_NAME):
        return ICON_NAME
    if theme.has_icon(FALLBACK_ICON_NAME):
        return FALLBACK_ICON_NAME
    return ICON_NAME


def apply_window_icon(window) -> None:
    theme = Gtk.IconTheme.get_default()
    path = icon_file()
    if theme.has_icon(ICON_NAME):
        window.set_icon_name(ICON_NAME)
        return
    if path:
        try:
            window.set_icon_from_file(path)
            return
        except Exception:
            pass
    window.set_icon_name(FALLBACK_ICON_NAME)


def sni_icon_name() -> str:
    """StatusNotifierItem.IconName: absolute PNG path, else a Breeze icon."""
    src = icon_file()
    dest = tray_png_path()
    if src:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(src, 64, 64)
            pixbuf.savev(dest, "png", [], [])
            return dest
        except Exception:
            if os.path.isabs(src):
                return src
    return FALLBACK_ICON_NAME


def brand_image() -> Gtk.Image:
    path = icon_file()
    if path:
        image = Gtk.Image.new_from_file(path)
        image.set_pixel_size(20)
        return image
    return Gtk.Image.new_from_icon_name(
        window_icon_name(), Gtk.IconSize.BUTTON)


def make_card(parent, title: str, subtitle: str | None = None,
              header_widget=None, card_class: str = "g935-card"):
    """Modern headerless rounded card returning (card_box, body_box)."""
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    style(card, card_class)

    header = Gtk.Box(spacing=10)
    title_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    title_lbl = Gtk.Label(label=title, xalign=0)
    style(title_lbl, "g935-card-title")
    title_col.pack_start(title_lbl, False, False, 0)
    if subtitle:
        sub_lbl = Gtk.Label(label=subtitle, xalign=0)
        style(sub_lbl, "g935-subtitle")
        sub_lbl.set_line_wrap(True)
        title_col.pack_start(sub_lbl, False, False, 0)
    header.pack_start(title_col, True, True, 0)
    if header_widget is not None:
        header_widget.set_valign(Gtk.Align.CENTER)
        header.pack_end(header_widget, False, False, 0)
    card.pack_start(header, False, False, 0)

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    card.pack_start(body, False, False, 0)
    parent.pack_start(card, False, False, 0)
    return card, body


def make_info_row(parent, title: str) -> Gtk.Label:
    row = Gtk.Box(spacing=10)
    key = Gtk.Label(label=title, xalign=0)
    style(key, "g935-info-key")
    key.set_size_request(105, -1)
    value = Gtk.Label(label="—", xalign=0)
    value.set_selectable(True)
    value.set_line_wrap(True)
    row.pack_start(key, False, False, 0)
    row.pack_start(value, True, True, 0)
    parent.pack_start(row, False, False, 0)
    return value


def make_kpi_card(parent, title: str) -> dict:
    """Compact metric card: title + big value + small subtitle."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    style(box, "g935-kpi")
    title_lbl = Gtk.Label(xalign=0)
    style(title_lbl, "g935-subtitle")
    title_lbl.set_markup(f"<small>{GLib.markup_escape_text(title)}</small>")
    value_lbl = Gtk.Label(xalign=0)
    value_lbl.set_markup("<span size='large' weight='bold'>—</span>")
    sub_lbl = Gtk.Label(xalign=0)
    style(sub_lbl, "g935-caption")
    sub_lbl.set_markup("<small> </small>")
    sub_lbl.set_line_wrap(True)
    box.pack_start(title_lbl, False, False, 0)
    box.pack_start(value_lbl, False, False, 0)
    box.pack_start(sub_lbl, False, False, 0)
    parent.pack_start(box, True, True, 0)
    return {"frame": box, "title": title_lbl, "value": value_lbl, "sub": sub_lbl}


def set_kpi(card: dict, value_markup: str, sub_text: str = "") -> None:
    card["value"].set_markup(value_markup)
    esc = GLib.markup_escape_text(sub_text) if sub_text else " "
    card["sub"].set_markup(f"<small>{esc}</small>")


def _focus_slider(scale, _event):
    scale.grab_focus()
    return False


def _on_guarded_slider_scroll(scale, event):
    if scale.has_focus():
        return False
    parent = scale.get_parent()
    while parent is not None and not isinstance(parent, Gtk.ScrolledWindow):
        parent = parent.get_parent()
    if parent is None:
        return True
    adj = parent.get_vadjustment()
    step = max(float(adj.get_step_increment()), 36.0)
    direction = event.direction
    if direction == Gdk.ScrollDirection.UP:
        delta = -step
    elif direction == Gdk.ScrollDirection.DOWN:
        delta = step
    elif direction == Gdk.ScrollDirection.SMOOTH:
        ok, _dx, dy = event.get_scroll_deltas()
        if not ok:
            return True
        delta = dy * step
    else:
        return True
    upper = max(adj.get_lower(), adj.get_upper() - adj.get_page_size())
    adj.set_value(min(upper, max(adj.get_lower(), adj.get_value() + delta)))
    return True


def guard_slider_scroll(scale) -> None:
    """Let the page scroll over a slider until that slider is clicked."""
    scale.set_can_focus(True)
    scale.set_tooltip_text(
        "Click once to focus this slider before adjusting it with the wheel.")
    scale.connect("button-press-event", _focus_slider)
    scale.connect("scroll-event", _on_guarded_slider_scroll)
