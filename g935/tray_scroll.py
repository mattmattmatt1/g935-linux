"""Tray-icon scroll math.

Plasma sends Qt ``angleDelta.y``: positive when the wheel moves up. Ayatana
maps that positive vertical delta to ``Gdk.ScrollDirection.DOWN`` and passes
the magnitude. One mouse notch is 120 eighths of a degree.
"""

TRAY_SCROLL_UNIT = 120


def tray_scroll_notches(direction: str, delta, acc: int) -> tuple[int, int]:
    """Return ``(notches, leftover)``.

    ``direction`` is ``"down"`` (louder; wheel up on Plasma), ``"up"``
    (quieter), or anything else (ignored). ``delta`` is a magnitude.
    """
    try:
        amount = abs(int(delta))
    except (TypeError, ValueError):
        return 0, acc
    if direction == "down":
        signed = amount
    elif direction == "up":
        signed = -amount
    else:
        return 0, acc
    acc += signed
    notches = int(acc / TRAY_SCROLL_UNIT)
    if notches == 0:
        return 0, acc
    return notches, acc - notches * TRAY_SCROLL_UNIT
