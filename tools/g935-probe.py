#!/usr/bin/env python3
"""Probe the G935 for undocumented HID++ features and functions.

Default is GET-only: IFeatureSet walk, curated getFeature IDs, then a
function sweep that skips known SET/DFU commands (zero-padded SETs would
write 0 — including software mode). Use --wide / --full for denser ID
sweeps, --unlock-hidden to try 0x1E00 after the GET pass.

Usage:
  python3 tools/g935-probe.py
  python3 tools/g935-probe.py --wide --out notes/evidence/probe-latest.md
  python3 tools/g935-probe.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from g935.features import parse_firmware_info
from g935.hidpp import G935_PID, find_device_by_pid, open_hidraw, transact
from g935.probe import (
    FunctionHit,
    ProbeReport,
    classify_function,
    dry_run_plan,
    enable_hidden_cmd,
    feature_name,
    featureset_count_cmd,
    featureset_id_cmd,
    followup_jobs,
    format_report,
    function_cmd,
    ids_for_mode,
    parse_featureset_count,
    parse_featureset_id,
    parse_get_feature,
    root_get_feature_cmd,
    root_protocol_cmd,
    skip_function_hit,
    skipped_functions,
)


def _ask(fd, hx: str, timeout: float, tries: int = 2):
    status, detail = "TIMEOUT", None
    for attempt in range(tries):
        status, detail = transact(fd, hx, timeout=timeout)
        if status == "GONE":
            return status, detail
        if status != "TIMEOUT":
            return status, detail
        if attempt + 1 < tries:
            time.sleep(0.05)
    return status, detail


def _progress(done: int, total: int, label: str) -> None:
    if total <= 0:
        return
    if done != total and done % 32 != 0 and done != 1:
        return
    sys.stderr.write(f"\r{label}: {done}/{total}")
    if done == total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _walk_featureset(fd, timeout: float, sleep_s: float, report: ProbeReport):
    status, reply = _ask(fd, featureset_count_cmd(), timeout)
    count = parse_featureset_count(status, reply)
    if count is None:
        report.notes.append(f"IFeatureSet getCount failed ({status})")
        return
    report.notes.append(f"IFeatureSet getCount={count}")
    # ROOT is index 0 and is often omitted from the count. Walk 0..count.
    for index in range(0, min(count, 32) + 1):
        status, reply = _ask(fd, featureset_id_cmd(index), timeout)
        slot = parse_featureset_id(index, status, reply)
        if slot is not None:
            report.slots.append(slot)
        elif status != "ERR":
            report.notes.append(
                f"IFeatureSet getFeatureID({index}) {status}"
            )
        time.sleep(sleep_s)


def _catalog_sweep(fd, ids, timeout: float, sleep_s: float, report: ProbeReport):
    total = len(ids)
    already = {slot.feature_id for slot in report.slots}
    for i, fid in enumerate(ids, 1):
        status, reply = _ask(fd, root_get_feature_cmd(fid), timeout)
        hit = parse_get_feature(fid, status, reply)
        report.catalog.append(hit)
        if hit.present and fid not in already:
            report.notes.append(
                f"getFeature `{fid:04X}` {feature_name(fid)} -> idx {hit.index}"
            )
        if status == "GONE":
            report.notes.append("hidraw gone during catalog sweep")
            return False
        _progress(i, total, "getFeature")
        time.sleep(sleep_s)
    return True


def _present_map(report: ProbeReport) -> dict[int, int]:
    """feature_id -> index, preferring IFeatureSet slots."""
    mapping: dict[int, int] = {}
    for hit in report.catalog:
        if hit.present:
            mapping[hit.feature_id] = hit.index
    for slot in report.slots:
        mapping[slot.feature_id] = slot.index
    mapping.setdefault(0x0000, 0)
    return mapping


def _function_sweep(fd, mapping, timeout: float, sleep_s: float, report: ProbeReport,
                    only_ids=None):
    items = sorted(mapping.items(), key=lambda kv: kv[1])
    if only_ids is not None:
        items = [(fid, idx) for fid, idx in items if fid in only_ids]
    planned = [(fid, idx, fn)
               for fid, idx in items
               for fn in range(16)]
    total = len(planned)
    done = 0
    consecutive_timeouts = 0
    last_fid = None
    for fid, idx, fn in planned:
        if fid != last_fid:
            consecutive_timeouts = 0
            last_fid = fid
        if consecutive_timeouts >= 3:
            report.functions.append(FunctionHit(
                idx, fid, fn, "SKIP", None, "",
                skipped=True,
                skip_reason="abandoned after 3 consecutive TIMEOUTs",
            ))
        elif fn in skipped_functions(fid):
            report.functions.append(skip_function_hit(idx, fid, fn))
        else:
            status, detail = _ask(fd, function_cmd(idx, fn), timeout)
            report.functions.append(
                classify_function(idx, fid, fn, status, detail))
            if status == "GONE":
                report.notes.append("hidraw gone during function sweep")
                return False
            if status == "TIMEOUT":
                consecutive_timeouts += 1
            else:
                consecutive_timeouts = 0
            time.sleep(sleep_s)
        done += 1
        _progress(done, total, "functions")
    return True


def _followup_sweep(fd, mapping, timeout: float, sleep_s: float, report: ProbeReport):
    jobs = followup_jobs(mapping)
    if not jobs:
        report.notes.append("no follow-up jobs (needed features missing)")
        return
    total = len(jobs)
    for i, (fid, idx, fn, params) in enumerate(jobs, 1):
        status, detail = _ask(fd, function_cmd(idx, fn, params), timeout)
        hit = classify_function(idx, fid, fn, status, detail)
        report.functions.append(hit)
        report.notes.append(
            f"follow-up `{fid:04X}` fn{fn} params={params or '∅'} "
            f"-> {hit.status} `{hit.payload_hex or '-'}`"
            + (f" ERR {hit.error:#04x}" if hit.error is not None else "")
        )
        if status == "GONE":
            report.notes.append("hidraw gone during follow-up")
            return
        _progress(i, total, "follow-up")
        time.sleep(sleep_s)


def _read_identity(fd, mapping, timeout: float, report: ProbeReport):
    status, reply = _ask(fd, root_protocol_cmd(), timeout)
    if status == "ACK" and reply:
        payload = bytes(reply[4:8]).hex()
        report.protocol = f"ACK payload {payload}"
    else:
        report.protocol = status

    idx = mapping.get(0x0003)
    if idx is None:
        return
    status, reply = _ask(fd, function_cmd(idx, 1, "00"), timeout)
    if status != "ACK" or not reply:
        return
    try:
        fw = parse_firmware_info(reply)
    except ValueError:
        report.firmware = bytes(reply[4:16]).hex()
        return
    marker = " active" if fw.active else ""
    report.firmware = (
        f"{fw.prefix or fw.entity_type} {fw.version} "
        f"build {fw.build:04d}{marker}"
    )


def _unlock_hidden(fd, mapping, timeout: float, sleep_s: float, report: ProbeReport):
    status, reply = _ask(fd, root_get_feature_cmd(0x1E00), timeout)
    hit = parse_get_feature(0x1E00, status, reply)
    report.catalog.append(hit)
    if not hit.present:
        report.hidden_attempts.append(
            f"0x1E00 not present ({status}, index {hit.index})"
        )
        return set()
    idx = hit.index
    mapping[0x1E00] = idx
    report.hidden_attempts.append(f"0x1E00 at index {idx}")
    before = {slot.feature_id for slot in report.slots}
    for fn in (0, 1):
        hx = enable_hidden_cmd(idx, fn)
        status, detail = _ask(fd, hx, timeout)
        tag = status
        if status == "ERR":
            tag = f"ERR {detail:#04x}" if detail is not None else "ERR"
        elif status == "ACK" and detail:
            tag = f"ACK {bytes(detail[4:8]).hex()}"
        report.hidden_attempts.append(f"enable fn{fn}=01 -> {tag}")
        time.sleep(sleep_s)
    report.slots.clear()
    _walk_featureset(fd, timeout, sleep_s, report)
    after = {slot.feature_id for slot in report.slots}
    new_ids = after - before
    if new_ids:
        names = ", ".join(f"`{fid:04X}` {feature_name(fid)}" for fid in sorted(new_ids))
        report.hidden_attempts.append(f"new IFeatureSet IDs: {names}")
    else:
        report.hidden_attempts.append("IFeatureSet unchanged after unlock attempts")
    return new_ids


def run_probe(fd, args) -> ProbeReport:
    report = ProbeReport(device=args.dev or "", mode=args.mode)
    _walk_featureset(fd, args.timeout, args.sleep, report)
    mapping = _present_map(report)
    _read_identity(fd, mapping, args.timeout, report)

    if args.followup:
        _followup_sweep(fd, mapping, args.timeout, args.sleep, report)
        if args.unlock_hidden:
            _unlock_hidden(fd, mapping, args.timeout, args.sleep, report)
        return report

    if not _catalog_sweep(fd, ids_for_mode(args.mode), args.timeout, args.sleep, report):
        return report
    mapping = _present_map(report)

    if args.functions:
        if not _function_sweep(fd, mapping, args.timeout, args.sleep, report):
            return report

    if args.unlock_hidden:
        new_ids = _unlock_hidden(fd, mapping, args.timeout, args.sleep, report)
        mapping = _present_map(report)
        if args.functions and new_ids:
            _function_sweep(
                fd, mapping, args.timeout, args.sleep, report, only_ids=new_ids)

    extra = report.unexpected_features()
    report.notes.append(
        f"{len(report.catalog)} getFeature queries, "
        f"{sum(1 for h in report.catalog if h.present)} present, "
        f"{len(extra)} unexpected feature(s)"
    )
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", default=None, help="hidraw node (default: auto-detect)")
    ap.add_argument(
        "--mode", choices=("catalog", "wide", "full"), default="catalog",
        help="getFeature ID set (default: curated catalog)")
    ap.add_argument("--wide", action="store_true", help="shorthand for --mode wide")
    ap.add_argument("--full", action="store_true", help="shorthand for --mode full")
    ap.add_argument("--no-functions", action="store_true",
                    help="skip the per-feature function sweep")
    ap.add_argument("--unlock-hidden", action="store_true",
                    help="after the GET pass, try Enable Hidden Features (0x1E00)")
    ap.add_argument("--followup", action="store_true",
                    help="only probe live leftover functions with small GET arg grids")
    ap.add_argument("--timeout", type=float, default=0.5)
    ap.add_argument("--sleep", type=float, default=0.03)
    ap.add_argument("--out", default=None, help="write markdown report to this path")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; do not open hidraw")
    args = ap.parse_args()
    if args.full:
        args.mode = "full"
    elif args.wide:
        args.mode = "wide"
    args.functions = not args.no_functions

    if args.dry_run:
        sys.stdout.write(dry_run_plan(args.mode, args.functions, args.unlock_hidden))
        return

    dev = args.dev or find_device_by_pid(G935_PID)
    if dev is None:
        sys.exit(
            "No G935 receiver found (no hidraw with 046D:0A87 + HID++ "
            "descriptor). Plug it in or pass --dev /dev/hidrawN."
        )
    args.dev = dev
    try:
        fd = open_hidraw(dev)
    except OSError as e:
        sys.exit(
            f"open {dev}: {e}\n(permission denied? install the udev rule "
            "from the README, then replug the receiver)"
        )

    n_ids = len(ids_for_mode(args.mode))
    print(
        f"Opened {dev}. mode={args.mode} ({n_ids} getFeature IDs), "
        f"functions={'on' if args.functions else 'off'}, "
        f"hidden={'on' if args.unlock_hidden else 'off'}.",
        file=sys.stderr,
    )
    try:
        report = run_probe(fd, args)
    finally:
        os.close(fd)

    text = format_report(report)
    sys.stdout.write(text)
    if args.out:
        parent = os.path.dirname(os.path.abspath(args.out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
