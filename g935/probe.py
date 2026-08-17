"""HID++ feature/function probe catalog and report helpers.

Live I/O lives in tools/g935-probe.py. This module is offline-testable.
GET-only by default: empty-param function sweeps skip known SET/DFU IDs so
zero-padded reports cannot flip software mode or write EQ/lighting/sidetone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from g935.hidpp import ERROR_CODES

SW_ID = 0x0B

# Advertised on the G935 via IFeatureSet (2026-07 live walk).
G935_ADVERTISED = (
    0x0000, 0x0001, 0x0003, 0x0005, 0x8070, 0x8010, 0x8310, 0x8300, 0x1F20,
)

FEATURE_NAMES: dict[int, str] = {
    0x0000: "IRoot",
    0x0001: "IFeatureSet",
    0x0002: "Feature Info",
    0x0003: "Device Information",
    0x0004: "Device Unit ID",
    0x0005: "Device Name / Type",
    0x0006: "Device Groups",
    0x0007: "Device Friendly Name",
    0x0008: "Keep Alive",
    0x0011: "Property Access",
    0x0020: "Config Change / Reset",
    0x0021: "Crypto ID",
    0x0030: "Target Software",
    0x0080: "Wireless Signal Strength",
    0x00C0: "DFU Control (legacy)",
    0x00C1: "DFU Control (unsigned)",
    0x00C2: "DFU Control (signed)",
    0x00C3: "DFU Control",
    0x00D0: "DFU",
    0x1000: "Battery Status",
    0x1001: "Battery Voltage",
    0x1004: "Unified Battery",
    0x1010: "Charging Control",
    0x1300: "LED Control",
    0x1500: "Force Pairing",
    0x1800: "Generic Test",
    0x1802: "Device Reset",
    0x1805: "OOB State",
    0x1806: "Config Device Props",
    0x1814: "Change Host",
    0x1815: "Hosts Info",
    0x1D4B: "Wireless Device Status",
    0x1DF0: "Remaining Pairing",
    0x1E00: "Enable Hidden Features",
    0x1F1F: "Firmware Properties",
    0x1F20: "ADC / Battery",
    0x8010: "Gaming G-keys",
    0x8020: "M-keys",
    0x8030: "MR",
    0x8040: "Brightness Control",
    0x8060: "Report Rate",
    0x8061: "Extended Report Rate",
    0x8070: "Color LED Effects",
    0x8071: "RGB Effects",
    0x8090: "Mode Status",
    0x8100: "Onboard Profiles",
    0x8110: "Mouse Button Spy",
    0x8111: "Latency Monitoring",
    0x8300: "Sidetone",
    0x8310: "Equalizer",
    0x8320: "Headset Out",
    # Centurion-era headset IDs — cheap to ask, unlikely on G935 firmware.
    0x0100: "Centurion Device Info",
    0x0200: "Headset Volume",
    0x0201: "Headset EQ",
    0x020D: "Headset Advanced Para EQ",
    0x020E: "Headset Mic Test",
    0x0213: "Headset EQ Styles",
    0x0600: "Headset RGB Effects",
    0x0601: "Headset Mic Mute",
    0x0602: "Headset Mic SNR",
    0x0604: "Headset Audio Sidetone",
    0x0607: "Headset Host Switch",
    0x0609: "Headset Mix",
    0x060B: "Headset Tones",
    0x060D: "Headset Noise Exposure",
    0x060E: "Headset AI Noise Reduction",
    0x0611: "Headset Mic Gain",
    0x0618: "Headset Battery Saver",
    0x0636: "Headset Onboard EQ",
    0x0800: "Mixer Audio",
    0x0801: "Mixer Mic",
    0x0900: "LogiVoice",
}

# Feature IDs where any non-info write can brick or factory-reset.
DANGEROUS_FEATURES = frozenset({
    0x0020, 0x00C0, 0x00C1, 0x00C2, 0x00C3, 0x00D0, 0x1802, 0x1805,
})

# Empty-param sweeps pad with zeros. Skip known SETs so we do not write 0.
KNOWN_SET_FUNCTIONS: dict[int, frozenset[int]] = {
    0x8010: frozenset({2}),          # software / G HUB mode
    0x8300: frozenset({1}),          # sidetone level
    0x8310: frozenset({3}),          # EQ gains
    0x8070: frozenset({3, 5, 8, 13}),  # zone effect, boot SET, connect fn8, preamp
    0x1E00: frozenset({0, 1}),       # hidden-feature enable (opt-in)
    0x1F20: frozenset({2}),          # SET auto power-off minutes
}

KNOWN_FUNCTIONS: dict[int, dict[int, str]] = {
    0x0000: {0: "getFeature", 1: "getProtocolVersion"},
    0x0001: {0: "getCount", 1: "getFeatureID"},
    0x0003: {0: "getDeviceInfo", 1: "getFirmwareInfo"},
    0x0005: {0: "getNameCount", 1: "getName", 2: "getType"},
    0x8010: {0: "getCount", 1: "getControl", 2: "setSoftwareControl"},
    0x8070: {
        0: "getInfo", 1: "getZoneInfo", 2: "getEffectInfo",
        3: "setZoneEffect", 4: "getBootEffect", 5: "setBootEffect",
        8: "set (connect fn8)",
        13: "setVisualizerGain", 14: "getZoneEffect",
    },
    0x8300: {0: "getLevel", 1: "setLevel"},
    0x8310: {0: "getInfo", 1: "getFrequencies", 2: "getGains", 3: "setGains"},
    0x1F20: {0: "getMeasurement", 1: "getPowerTimeout", 2: "setPowerTimeout"},
    0x1E00: {0: "setEnabled?", 1: "setEnabled?"},
}

FEATURE_FLAG_NAMES = (
    (0x20, "internal"),
    (0x40, "hidden"),
    (0x80, "obsolete"),
)

WIDE_RANGES = (
    (0x0000, 0x0200),
    (0x0800, 0x0C00),
    (0x1000, 0x2000),
    (0x8000, 0x8400),
)

# Curated getFeature list: named IDs plus neighbors of headset/hidden/DFU.
_NEIGHBOR_SPANS = (
    (0x0000, 0x0038),
    (0x00C0, 0x00D8),
    (0x1000, 0x1018),
    (0x1800, 0x1820),
    (0x1DF0, 0x1E10),
    (0x1F18, 0x1F28),
    (0x8008, 0x8130),
    (0x82F8, 0x8330),
    (0x01F8, 0x0220),
    (0x05F8, 0x0640),
    (0x07F8, 0x0810),
    (0x08F8, 0x0910),
)


def feature_name(feature_id: int) -> str:
    return FEATURE_NAMES.get(feature_id, f"unknown:{feature_id:04X}")


def format_flags(flags: int) -> str:
    names = [name for bit, name in FEATURE_FLAG_NAMES if flags & bit]
    extra = flags & ~0xE0
    if extra:
        names.append(f"raw=0x{flags:02X}")
    elif flags and not names:
        names.append(f"0x{flags:02X}")
    return ",".join(names) if names else "-"


def function_name(feature_id: int, fn: int) -> str:
    return KNOWN_FUNCTIONS.get(feature_id, {}).get(fn, "")


def is_dangerous_feature(feature_id: int) -> bool:
    return feature_id in DANGEROUS_FEATURES


def skipped_functions(feature_id: int) -> frozenset[int]:
    """Function IDs the empty-param sweep must not send."""
    if is_dangerous_feature(feature_id):
        return frozenset(range(1, 16))
    return KNOWN_SET_FUNCTIONS.get(feature_id, frozenset())


def catalog_ids() -> list[int]:
    ids = set(FEATURE_NAMES)
    ids.update(G935_ADVERTISED)
    for start, end in _NEIGHBOR_SPANS:
        ids.update(range(start, end))
    return sorted(ids)


def wide_ids() -> list[int]:
    ids: set[int] = set()
    for start, end in WIDE_RANGES:
        ids.update(range(start, end))
    return sorted(ids)


def full_ids() -> list[int]:
    return list(range(0x10000))


def ids_for_mode(mode: str) -> list[int]:
    if mode == "catalog":
        return catalog_ids()
    if mode == "wide":
        return wide_ids()
    if mode == "full":
        return full_ids()
    raise ValueError(f"unknown probe mode {mode!r}")


def root_get_feature_cmd(feature_id: int) -> str:
    return f"11ff000b{feature_id:04x}"


def root_protocol_cmd() -> str:
    return "11ff001b"


def featureset_count_cmd() -> str:
    return "11ff010b"


def featureset_id_cmd(index: int) -> str:
    if not 0 <= index <= 255:
        raise ValueError("feature index out of range")
    return f"11ff011b{index:02x}"


def function_cmd(feat_index: int, fn: int, params: str = "") -> str:
    if not 0 <= feat_index <= 255:
        raise ValueError("feature index out of range")
    if not 0 <= fn <= 15:
        raise ValueError("function out of range")
    return f"11ff{feat_index:02x}{(fn << 4) | SW_ID:02x}{params}"


def enable_hidden_cmd(feat_index: int, fn: int = 0) -> str:
    return function_cmd(feat_index, fn, "01")


@dataclass(frozen=True)
class FeaturePresence:
    feature_id: int
    index: int
    flags: int
    version: int
    present: bool
    status: str


def parse_get_feature(feature_id: int, status: str, reply) -> FeaturePresence:
    if status != "ACK" or not reply or len(reply) < 5:
        return FeaturePresence(feature_id, 0, 0, 0, False, status)
    index = reply[4]
    flags = reply[5] if len(reply) > 5 else 0
    version = reply[6] if len(reply) > 6 else 0
    # IRoot is always index 0; every other ID uses 0 as "absent".
    present = bool(index) or feature_id == 0x0000
    return FeaturePresence(feature_id, index, flags, version, present, status)


@dataclass(frozen=True)
class FeatureSlot:
    index: int
    feature_id: int
    flags: int
    version: int


def parse_featureset_count(status: str, reply) -> int | None:
    if status != "ACK" or not reply or len(reply) < 5:
        return None
    return reply[4]


def parse_featureset_id(index: int, status: str, reply) -> FeatureSlot | None:
    if status != "ACK" or not reply or len(reply) < 6:
        return None
    feature_id = (reply[4] << 8) | reply[5]
    flags = reply[6] if len(reply) > 6 else 0
    version = reply[7] if len(reply) > 7 else 0
    return FeatureSlot(index, feature_id, flags, version)


@dataclass(frozen=True)
class FunctionHit:
    feat_index: int
    feature_id: int
    fn: int
    status: str
    error: int | None
    payload_hex: str
    skipped: bool = False
    skip_reason: str = ""

    @property
    def interesting(self) -> bool:
        if self.skipped:
            return False
        if self.status == "ACK":
            return True
        if self.status == "ERR" and self.error in (0x02, 0x03, 0x05, 0x08, 0x09):
            return True
        return False


def classify_function(
        feat_index: int, feature_id: int, fn: int,
        status: str, detail) -> FunctionHit:
    error = None
    payload = ""
    if status == "ERR":
        error = int(detail) if detail is not None else None
    elif status == "ACK" and detail:
        # Keep leading zeros — rstrip hid getFeatureID(0) and empty GETs.
        payload = bytes(detail[4:12]).hex()
    return FunctionHit(feat_index, feature_id, fn, status, error, payload)


def skip_function_hit(feat_index: int, feature_id: int, fn: int) -> FunctionHit:
    if is_dangerous_feature(feature_id) and fn != 0:
        reason = "dangerous feature (GET fn0 only)"
    else:
        reason = "known SET (empty params would write 0)"
    return FunctionHit(
        feat_index, feature_id, fn, "SKIP", None, "",
        skipped=True, skip_reason=reason,
    )


def functions_to_probe(feature_id: int) -> list[int]:
    skip = skipped_functions(feature_id)
    return [fn for fn in range(16) if fn not in skip]


# Targeted GET-style args for functions the empty sweep marked live.
FOLLOWUP_GRIDS: dict[tuple[int, int], tuple[str, ...]] = {
    (0x8010, 1): ("",),
}


def followup_jobs(mapping: dict[int, int]) -> list[tuple[int, int, int, str]]:
    """(feature_id, feat_index, fn, params) for live functions that need args."""
    jobs = []
    for (fid, fn), params_list in FOLLOWUP_GRIDS.items():
        idx = mapping.get(fid)
        if idx is None:
            continue
        for params in params_list:
            jobs.append((fid, idx, fn, params))
    return jobs


def error_name(code: int | None) -> str:
    if code is None:
        return ""
    return ERROR_CODES.get(code, f"code 0x{code:02x}")


@dataclass
class ProbeReport:
    device: str = ""
    mode: str = "catalog"
    protocol: str = ""
    firmware: str = ""
    slots: list[FeatureSlot] = field(default_factory=list)
    catalog: list[FeaturePresence] = field(default_factory=list)
    functions: list[FunctionHit] = field(default_factory=list)
    hidden_attempts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def unexpected_features(self) -> list[FeaturePresence | FeatureSlot]:
        known = set(G935_ADVERTISED)
        extra: list[FeaturePresence | FeatureSlot] = []
        seen: set[int] = set()
        for slot in self.slots:
            if slot.feature_id not in known and slot.feature_id not in seen:
                extra.append(slot)
                seen.add(slot.feature_id)
        for hit in self.catalog:
            if hit.present and hit.feature_id not in known and hit.feature_id not in seen:
                extra.append(hit)
                seen.add(hit.feature_id)
        return extra

    def unexpected_functions(self) -> list[FunctionHit]:
        out = []
        for hit in self.functions:
            if not hit.interesting:
                continue
            if function_name(hit.feature_id, hit.fn):
                continue
            out.append(hit)
        return out


def format_report(report: ProbeReport) -> str:
    lines = [
        "# G935 HID++ probe",
        "",
        f"- device: {report.device or 'unknown'}",
        f"- mode: {report.mode}",
        f"- protocol: {report.protocol or 'n/a'}",
        f"- firmware: {report.firmware or 'n/a'}",
        "",
        "## IFeatureSet",
        "",
    ]
    if not report.slots:
        lines.append("(no slots)")
    else:
        lines.append("| idx | id | flags | ver | name |")
        lines.append("|---:|---|---|---:|---|")
        for slot in report.slots:
            lines.append(
                f"| {slot.index} | `{slot.feature_id:04X}` | "
                f"{format_flags(slot.flags)} | {slot.version} | "
                f"{feature_name(slot.feature_id)} |"
            )
    lines.extend(["", "## getFeature catalog", ""])
    present = [h for h in report.catalog if h.present]
    absent_named = [
        h for h in report.catalog
        if not h.present and h.feature_id in FEATURE_NAMES and h.status == "ACK"
    ]
    other = [
        h for h in report.catalog
        if not h.present and h.status != "ACK"
    ]
    lines.append(f"queried {len(report.catalog)}, present {len(present)}.")
    if present:
        lines.extend(["", "| id | idx | flags | ver | name |", "|---|---:|---|---:|---|"])
        for hit in present:
            lines.append(
                f"| `{hit.feature_id:04X}` | {hit.index} | "
                f"{format_flags(hit.flags)} | {hit.version} | "
                f"{feature_name(hit.feature_id)} |"
            )
    if other:
        lines.extend(["", "Non-ACK catalog queries:", ""])
        for hit in other:
            lines.append(
                f"- `{hit.feature_id:04X}` {feature_name(hit.feature_id)}: {hit.status}"
            )
    if absent_named:
        lines.append("")
        lines.append(
            f"{len(absent_named)} named IDs returned index 0 (absent)."
        )
    extra = report.unexpected_features()
    lines.extend(["", "## Unexpected features", ""])
    if extra:
        for item in extra:
            fid = item.feature_id
            lines.append(f"- `{fid:04X}` {feature_name(fid)}")
    else:
        lines.append("None beyond the known G935 advertised set.")

    lines.extend(["", "## Function sweep", ""])
    if not report.functions:
        lines.append("(not run)")
    else:
        lines.append("| feat | id | fn | name | result | payload |")
        lines.append("|---:|---|---:|---|---|---|")
        for hit in report.functions:
            name = function_name(hit.feature_id, hit.fn) or "-"
            if hit.skipped:
                result = f"SKIP ({hit.skip_reason})"
            elif hit.status == "ERR":
                result = f"ERR {error_name(hit.error)}"
            else:
                result = hit.status
            payload = hit.payload_hex or "-"
            lines.append(
                f"| {hit.feat_index} | `{hit.feature_id:04X}` | {hit.fn} | "
                f"{name} | {result} | `{payload}` |"
            )

    unexpected_fn = report.unexpected_functions()
    lines.extend(["", "## Unexpected live functions", ""])
    if unexpected_fn:
        for hit in unexpected_fn:
            result = hit.status
            if hit.status == "ERR":
                result = f"ERR {error_name(hit.error)}"
            lines.append(
                f"- `{hit.feature_id:04X}` fn {hit.fn} @ idx {hit.feat_index}: "
                f"{result} payload=`{hit.payload_hex or '-'}`"
            )
    else:
        lines.append(
            "No ACKs or argument-errors outside the already-mapped functions."
        )

    if report.hidden_attempts:
        lines.extend(["", "## Hidden-feature unlock", ""])
        lines.extend(f"- {line}" for line in report.hidden_attempts)

    if report.notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {line}" for line in report.notes)
    lines.append("")
    return "\n".join(lines)


def dry_run_plan(mode: str, do_functions: bool, unlock_hidden: bool) -> str:
    ids = ids_for_mode(mode)
    lines = [
        f"mode={mode} getFeature queries={len(ids)}",
        f"IFeatureSet walk + IRoot protocol version",
        f"function sweep={'yes' if do_functions else 'no'} "
        f"(skips known SETs and DFU writes)",
        f"unlock hidden={'yes' if unlock_hidden else 'no'}",
        "",
        "first catalog IDs:",
    ]
    for fid in ids[:16]:
        lines.append(f"  {root_get_feature_cmd(fid)}  {feature_name(fid)}")
    if do_functions:
        lines.append("")
        lines.append("example function cmds on advertised G935 map:")
        assumed = (
            (0, 0x0000), (1, 0x0001), (2, 0x0003), (3, 0x0005),
            (4, 0x8070), (5, 0x8010), (6, 0x8310), (7, 0x8300), (8, 0x1F20),
        )
        for idx, fid in assumed:
            probed = ",".join(str(fn) for fn in functions_to_probe(fid))
            skipped = ",".join(str(fn) for fn in sorted(skipped_functions(fid))) or "-"
            lines.append(
                f"  idx {idx} `{fid:04X}` probe [{probed}] skip [{skipped}]"
            )
    return "\n".join(lines) + "\n"
