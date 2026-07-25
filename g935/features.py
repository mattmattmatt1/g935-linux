"""Pure HID++ feature decoders/builders used by the G935 control panel."""
from __future__ import annotations

from dataclasses import dataclass


FEATURE_LABELS = {
    0x0001: "Feature Set",
    0x0003: "Device Information",
    0x0005: "Device Name / Type",
    0x1F20: "ADC / Battery",
    0x8010: "Gaming G-keys",
    0x8070: "Color LED Effects",
    0x8300: "Sidetone",
    0x8310: "Equalizer",
}

DEVICE_TYPES = {
    0: "Keyboard", 1: "Remote control", 2: "Numpad", 3: "Mouse",
    4: "Trackpad", 5: "Trackball", 6: "Presenter", 7: "Receiver",
    8: "Headset", 9: "Webcam", 10: "Steering wheel", 11: "Joystick",
    12: "Gamepad", 13: "Dock", 14: "Speaker", 15: "Microphone",
    16: "Illumination light", 17: "Programmable controller",
    18: "Simulator pedals", 19: "Adapter",
}

ENTITY_TYPES = {
    0: "Main application", 1: "Bootloader", 2: "Hardware",
    3: "Touchpad", 4: "Optical sensor", 5: "Bluetooth SoftDevice",
    6: "RF companion MCU", 7: "Factory application",
    8: "RGB custom effect", 9: "Motor drive",
}

LED_LOCATIONS = {
    0x0000: "Unknown", 0x0001: "Primary", 0x0002: "Logo",
    0x0003: "Left side", 0x0004: "Right side", 0x0005: "Combined",
    0x0006: "Primary 1", 0x0007: "Primary 2", 0x0008: "Primary 3",
    0x0009: "Primary 4", 0x000A: "Primary 5", 0x000B: "Primary 6",
}

LED_EFFECTS = {
    0x0000: "Off",
    0x0001: "Fixed",
    0x0002: "Pulse",
    0x0003: "Cycling",
    0x0004: "Wave",
    0x0008: "Boot",
    0x0009: "Demo",
    0x000A: "Breathing",
}

LIGHT_MODES = ("Off", "Fixed", "Breathing", "Cycling")


def _payload(reply: bytes, minimum: int = 0) -> bytes:
    if len(reply) < 4 + minimum:
        raise ValueError("short HID++ reply")
    return reply[4:]


def _bcd(byte: int) -> int:
    hi, lo = byte >> 4, byte & 0x0F
    if hi > 9 or lo > 9:
        raise ValueError("invalid packed BCD")
    return hi * 10 + lo


@dataclass(frozen=True)
class DeviceInfo:
    entity_count: int
    unit_id: str
    transport_mask: int
    transports: tuple[str, ...]
    model_id: str
    extended_model_id: int
    serial_supported: bool


def parse_device_info(reply: bytes) -> DeviceInfo:
    p = _payload(reply, 15)
    transport_names = ("Bluetooth", "Bluetooth LE", "eQuad", "USB")
    transports = tuple(name for bit, name in enumerate(transport_names)
                       if p[6] & (1 << bit))
    return DeviceInfo(
        entity_count=p[0],
        unit_id=p[1:5].hex().upper(),
        transport_mask=p[6],
        transports=transports,
        model_id=p[7:13].hex().upper(),
        extended_model_id=p[13],
        serial_supported=bool(p[14] & 1),
    )


@dataclass(frozen=True)
class FirmwareInfo:
    entity_type: str
    prefix: str
    version: str
    build: int
    active: bool
    transport_pid: int
    extra_version: str


def parse_firmware_info(reply: bytes) -> FirmwareInfo:
    p = _payload(reply, 16)
    prefix = p[1:4].decode("ascii", "replace").rstrip("\0 ")
    return FirmwareInfo(
        entity_type=ENTITY_TYPES.get(p[0], f"Type {p[0]}"),
        prefix=prefix,
        version=f"{_bcd(p[4]):02d}.{_bcd(p[5]):02d}",
        build=_bcd(p[6]) * 100 + _bcd(p[7]),
        active=bool(p[8] & 1),
        transport_pid=(p[9] << 8) | p[10],
        extra_version=p[11:16].hex().upper(),
    )


@dataclass(frozen=True)
class EqInfo:
    bands: int
    minimum_db: int
    maximum_db: int
    stored_as_gains: bool


def parse_eq_info(reply: bytes) -> EqInfo:
    p = _payload(reply, 5)
    minimum = p[3] - 256 if p[3] > 127 else p[3]
    maximum = p[4] - 256 if p[4] > 127 else p[4]
    if minimum == maximum == 0:
        minimum, maximum = -p[1], p[1]
    return EqInfo(p[0], minimum, maximum, bool(p[2] & 1))


def parse_frequency_page(reply: bytes, count: int, expected_start: int) -> list[int]:
    p = _payload(reply, 1 + count * 2)
    if p[0] != expected_start:
        raise ValueError("EQ frequency page index mismatch")
    return [(p[1 + i * 2] << 8) | p[2 + i * 2] for i in range(count)]


def format_frequency(hz: int) -> str:
    if hz >= 1000 and hz % 1000 == 0:
        return f"{hz // 1000}k"
    return str(hz)


def parse_gkey_mask(report: bytes) -> int:
    p = _payload(report, 4)
    return int.from_bytes(p[:4], "little")


def build_light_params(zone: int, mode: int, rgb=(0, 180, 255),
                       period_ms: int = 5000, intensity: int = 100,
                       waveform: int = 0, ramp: int = 2,
                       persistence: int = 1) -> str:
    """Build setZoneEffect params (zone, effect slot, 10 bytes, persistence).

    The G935 advertises slots 0/1/2/3 as disabled, fixed, breathing, cycling.
    """
    if not 0 <= zone <= 255:
        raise ValueError("invalid lighting zone")
    if mode not in range(len(LIGHT_MODES)):
        raise ValueError("invalid lighting mode")
    if any(not 0 <= value <= 255 for value in rgb):
        raise ValueError("invalid RGB color")
    if not 0 <= period_ms <= 0xFFFF:
        raise ValueError("invalid lighting period")
    if not 0 <= intensity <= 100:
        raise ValueError("invalid lighting intensity")
    if not 0 <= waveform <= 6 or not 0 <= ramp <= 2:
        raise ValueError("invalid lighting option")
    if persistence not in (0, 1, 2):
        raise ValueError("invalid lighting persistence")

    params = bytearray(10)
    if mode == 1:  # fixed: color @0, ramp @3
        params[0:3] = bytes(rgb)
        params[3] = ramp
    elif mode == 2:  # breathing: color @0, period @3, form @5, intensity @6
        params[0:3] = bytes(rgb)
        params[3:5] = period_ms.to_bytes(2, "big")
        params[5] = waveform
        params[6] = intensity
    elif mode == 3:  # cycle: period @5, intensity @7
        params[5:7] = period_ms.to_bytes(2, "big")
        params[7] = intensity
    return (bytes((zone, mode)) + params + bytes((persistence,))).hex()


@dataclass(frozen=True)
class LedState:
    zone: int
    mode: int
    rgb: tuple[int, int, int]
    period_ms: int
    intensity: int
    waveform: int
    ramp: int


def parse_led_state(reply: bytes) -> LedState:
    p = _payload(reply, 12)
    zone, mode = p[0], p[1]
    params = p[2:12]
    rgb = tuple(params[0:3]) if mode in (1, 2) else (0, 0, 0)
    period = 0
    intensity = 0
    waveform = 0
    ramp = 0
    if mode == 1:
        ramp = params[3]
    elif mode == 2:
        period = int.from_bytes(params[3:5], "big")
        waveform = params[5]
        intensity = params[6]
    elif mode == 3:
        period = int.from_bytes(params[5:7], "big")
        intensity = params[7]
    return LedState(
        zone, mode, rgb, period, intensity, waveform, ramp)


def parse_led_zone_info(reply: bytes) -> tuple[int, str, int]:
    p = _payload(reply, 5)
    location = (p[1] << 8) | p[2]
    return p[0], LED_LOCATIONS.get(location, f"Location 0x{location:04X}"), p[3]


def parse_led_effect_info(reply: bytes) -> tuple[int, int, int, int, int]:
    p = _payload(reply, 8)
    return (
        p[0], p[1], (p[2] << 8) | p[3],
        (p[4] << 8) | p[5], (p[6] << 8) | p[7],
    )
