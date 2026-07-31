"""Detect and repair left-only audio on the G935 after sleep/resume.

After suspend, PipeWire/EasyEffects often re-links only FL into the headset
while FR stays unconnected (the right port may appear as ``playback_2`` instead
of ``playback_FR``). Symptoms: audio only in the left cup; volumes still show
stereo 100%/100%.

Uses ``pw-link`` (PipeWire). Pure helpers accept text fixtures for unit tests.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Iterable

log = logging.getLogger("g935.stereo")

# Sink name fragments that identify the G935 USB playback device
G935_SINK_MARKERS = (
    "Logitech_G935",
    "G935_Gaming_Headset",
    "G935 Gaming Headset",
)

# Effect / path outputs that should feed both ears when present
PRIMARY_FR_SOURCES = (
    "ee_soe_output_level:output_FR",
    "effect_output.dts71gaming:output_FR",
)

def _is_stereo_path_source(port: str) -> bool:
    """True for EasyEffects / filter-chain outputs — not Firefox, speechd, etc."""
    base = port.split(":")[0] if ":" in port else port
    if base.startswith("ee_soe_") or base.startswith("ee_sie_"):
        return port.endswith("_FL") or port.endswith("_FR")
    if base.startswith("effect_output."):
        return True
    if base.startswith("DTS_") or "dts71" in base.lower():
        return True
    if base == "easyeffects_sink" and "monitor_" in port:
        # EE monitor feeds convolver; not the final device hop
        return False
    return False


@dataclass
class StereoStatus:
    """Result of a stereo-route inspection."""

    ok: bool
    g935_present: bool = False
    fl_port: str | None = None
    fr_port: str | None = None
    fl_links_in: list[str] = field(default_factory=list)
    fr_links_in: list[str] = field(default_factory=list)
    missing: list[tuple[str, str]] = field(default_factory=list)  # (src, dst)
    detail: str = ""
    pw_link_available: bool = True

    @property
    def broken(self) -> bool:
        return self.g935_present and not self.ok and bool(self.missing)


def _is_g935_port(name: str) -> bool:
    return any(m in name for m in G935_SINK_MARKERS)


def parse_pw_link_list(text: str) -> dict[str, list[str]]:
    """Parse ``pw-link -l`` into {port: [linked_peer, ...]} (both directions).

    Format::

        node:port_a
          |-> node:port_b
          |<- node:port_c
    """
    graph: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith(" ") or line.startswith("\t"):
            s = line.strip()
            m = re.match(r"\|->\s+(\S+)", s)
            if m and current:
                peer = m.group(1)
                graph.setdefault(current, []).append(peer)
                graph.setdefault(peer, [])
                continue
            m = re.match(r"\|<-\s+(\S+)", s)
            if m and current:
                peer = m.group(1)
                graph.setdefault(peer, []).append(current)
                graph.setdefault(current, [])
            continue
        current = line.strip()
        graph.setdefault(current, [])
    return graph


def parse_pw_link_ports(text: str) -> list[str]:
    """Parse ``pw-link -i`` / ``pw-link -o`` (one port per line)."""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def find_g935_playback_ports(input_ports: Iterable[str]) -> tuple[str | None, str | None]:
    """Return (fl_port, fr_port) for the G935 sink inputs.

    Right channel may be named ``playback_FR`` or orphaned ``playback_2``.
    Prefer an explicit ``playback_FR`` when both exist.
    """
    fl = None
    fr = None
    fr_fallback = None
    for p in input_ports:
        if not _is_g935_port(p):
            continue
        if p.endswith(":playback_FL"):
            fl = p
        elif p.endswith(":playback_FR"):
            fr = p
        elif p.endswith(":playback_2") or re.search(r":playback_\d+$", p):
            # Numeric playback_N (N!= typical FL name) — treat as right candidate
            if not p.endswith(":playback_FL"):
                fr_fallback = p
    if fr is None:
        fr = fr_fallback
    return fl, fr


def _flip_channel(port: str) -> str | None:
    """Map ``…:output_FL`` ↔ ``…:output_FR`` (and playback_FL ↔ playback_FR)."""
    if port.endswith("_FL"):
        return port[:-3] + "_FR"
    if port.endswith("_FR"):
        return port[:-3] + "_FL"
    return None


def analyze_stereo(graph: dict[str, list[str]],
                   input_ports: list[str] | None = None) -> StereoStatus:
    """Decide if G935 stereo routing is healthy and what links are missing.

    Broken when something feeds FL that has a FR twin which is not linked into
    the headset's right port (the classic post-resume EasyEffects failure).
    """
    if input_ports is None:
        # Derive input candidates from graph keys that look like playback ports
        input_ports = [p for p in graph if ":playback_" in p and _is_g935_port(p)]

    fl, fr = find_g935_playback_ports(input_ports)
    if not fl:
        return StereoStatus(
            ok=True,
            g935_present=False,
            detail="G935 playback sink not in the PipeWire graph",
        )

    # Who feeds FL? graph may list either direction depending on parse;
    # collect nodes that link *to* fl.
    fl_in = []
    fr_in = []
    for src, dests in graph.items():
        if fl in dests:
            fl_in.append(src)
        if fr and fr in dests:
            fr_in.append(src)
    # Also: if fl lists |<- peers we stored as reverse edges into graph[peer]->fl
    # Already covered by walking dests.

    # Dedup preserve order
    def _uniq(xs):
        seen = set()
        out = []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    fl_in = _uniq(fl_in)
    fr_in = _uniq(fr_in)

    if not fr:
        return StereoStatus(
            ok=False,
            g935_present=True,
            fl_port=fl,
            fr_port=None,
            fl_links_in=fl_in,
            fr_links_in=fr_in,
            detail="G935 has no right-channel playback port",
        )

    missing: list[tuple[str, str]] = []
    for src in fl_in:
        if not _is_stereo_path_source(src):
            continue
        twin = _flip_channel(src)
        if twin is None or twin in fr_in:
            continue
        if "monitor_" in src:
            continue
        missing.append((twin, fr))

    # Always ensure primary known sources if FL is linked from their twin
    for primary_fr in PRIMARY_FR_SOURCES:
        primary_fl = _flip_channel(primary_fr)
        if primary_fl and primary_fl in fl_in and primary_fr not in fr_in:
            pair = (primary_fr, fr)
            if pair not in missing:
                missing.append(pair)

    if missing:
        srcs = ", ".join(s for s, _ in missing)
        return StereoStatus(
            ok=False,
            g935_present=True,
            fl_port=fl,
            fr_port=fr,
            fl_links_in=fl_in,
            fr_links_in=fr_in,
            missing=missing,
            detail=f"Right ear unlinked — missing: {srcs}",
        )

    return StereoStatus(
        ok=True,
        g935_present=True,
        fl_port=fl,
        fr_port=fr,
        fl_links_in=fl_in,
        fr_links_in=fr_in,
        detail="Stereo route OK" if (fl_in or fr_in) else "G935 present, no active links",
    )


def _run_pw(*args: str, timeout: float = 3.0) -> tuple[int, str, str]:
    if not shutil.which("pw-link"):
        return 127, "", "pw-link not found"
    try:
        r = subprocess.run(
            ["pw-link", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def inspect_stereo() -> StereoStatus:
    """Live check against the running PipeWire graph."""
    if not shutil.which("pw-link"):
        return StereoStatus(
            ok=True,
            g935_present=False,
            pw_link_available=False,
            detail="pw-link not installed — stereo check skipped",
        )
    rc, out_l, err = _run_pw("-l")
    if rc != 0 and not out_l:
        return StereoStatus(
            ok=True,
            g935_present=False,
            detail=f"pw-link -l failed: {err.strip() or rc}",
        )
    rc_i, out_i, _ = _run_pw("-i")
    inputs = parse_pw_link_ports(out_i) if rc_i == 0 else None
    graph = parse_pw_link_list(out_l)
    return analyze_stereo(graph, inputs)


def fix_stereo(status: StereoStatus | None = None) -> tuple[bool, str]:
    """Create missing FR→G935 links. Returns (ok, message)."""
    st = status if status is not None else inspect_stereo()
    if not st.pw_link_available:
        return False, st.detail
    if not st.g935_present:
        return True, "G935 not in graph — nothing to fix"
    if st.ok and not st.missing:
        return True, st.detail or "Stereo already OK"

    # Re-inspect if missing empty but marked broken
    if not st.missing:
        st = inspect_stereo()
    if st.ok:
        return True, st.detail or "Stereo already OK"
    if not st.missing:
        return False, st.detail or "Broken stereo but no auto-fix links known"

    linked = []
    failed = []
    for src, dst in st.missing:
        rc, _out, err = _run_pw(src, dst)
        if rc == 0:
            linked.append(f"{src} → {dst}")
            log.info("stereo fix: linked %s -> %s", src, dst)
        else:
            # pw-link returns error if already linked — treat as ok
            err_l = (err or "").lower()
            if "exists" in err_l or "already" in err_l:
                linked.append(f"{src} → {dst} (already)")
            else:
                failed.append(f"{src}: {(err or 'failed').strip()}")
                log.warning("stereo fix failed %s -> %s: %s", src, dst, err)

    # Verify
    after = inspect_stereo()
    if after.ok:
        msg = "Restored right channel: " + "; ".join(linked)
        return True, msg
    if linked and not failed:
        return True, "Linked " + "; ".join(linked) + " (recheck may need a moment)"
    parts = []
    if linked:
        parts.append("linked " + "; ".join(linked))
    if failed:
        parts.append("failed " + "; ".join(failed))
    return False, after.detail + (" — " + "; ".join(parts) if parts else "")


def notify_user(summary: str, body: str = "", urgency: str = "normal",
                replace_id: int | None = None) -> bool:
    """Desktop notification via notify-send when available.

    Returns True if notify-send ran successfully. Failures are logged (the
    previous silent swallow made missing battery alerts hard to debug).
    """
    if not shutil.which("notify-send"):
        log.warning("notify-send not found — install libnotify-bin for desktop alerts")
        return False
    cmd = [
        "notify-send",
        f"--urgency={urgency}",
        "--app-name=G935 Control",
        "--icon=audio-headset",
        "--category=device",
        # Helps KDE/Plasma associate the toast with the desktop entry
        "--hint=string:desktop-entry:g935-control",
        summary,
    ]
    if body:
        cmd.append(body)
    if replace_id is not None:
        cmd[1:1] = [f"--replace-id={int(replace_id)}"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3, text=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("notify-send failed: %s", e)
        return False
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        log.warning("notify-send failed: %s", err)
        return False
    return True
