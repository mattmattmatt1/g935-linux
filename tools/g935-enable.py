#!/usr/bin/env python3
"""
g935-enable.py  -  Reproduce G HUB's connect-time DSP enable on Linux.

Minimal SET-only burst. Prefer g935-enable-v2.py for cold-connect with
reply checking. For day-to-day use prefer g935-dspd / the control panel.

Usage:
    python3 tools/g935-enable.py
    python3 tools/g935-enable.py --list
    python3 tools/g935-enable.py --dev /dev/hidrawN

Needs:  pip install hid  (or apt install python3-hid) for auto-detect via
hidapi; --dev works without it.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from g935.hidpp import G935_PID, find_device_by_pid, to_report
from g935.sequences import ENABLE_SETS

VID, PID = 0x046D, G935_PID


def find_interfaces():
    import hid
    hits = []
    for d in hid.enumerate(VID, PID):
        if d.get("usage_page", 0) >= 0xff00:
            hits.append(d)
    return hits


def try_raw_send(path, reports):
    with open(path, "wb", buffering=0) as f:
        for r in reports:
            f.write(r)
            time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dev", help="force a /dev/hidraw* node")
    args = ap.parse_args()

    reports = [to_report(h) for h in ENABLE_SETS]

    if args.dev:
        print(f"Sending {len(reports)} reports to {args.dev} ...")
        try_raw_send(args.dev, reports)
        print("Done. Play audio and listen for the soundstage to open up.")
        return

    try:
        import hid  # noqa: F401
    except ImportError:
        path = find_device_by_pid(PID)
        if path:
            print(f"hid package missing; using sysfs path {path}")
            try_raw_send(path, reports)
            print("Done.")
            return
        sys.exit(
            "Install the hid package:  pip install hid   (or apt install python3-hid)\n"
            "Or point at the node:  python3 tools/g935-enable.py --dev /dev/hidrawN"
        )

    ifaces = find_interfaces()
    if not ifaces:
        sys.exit(
            "No Logitech G935 HID++ interface found. Is the headset ON and connected?\n"
            "List candidates:  ls -l /dev/hidraw*  and try --dev on each."
        )

    for d in ifaces:
        print(
            f"found HID++ iface: path={d['path'].decode(errors='replace')} "
            f"usage_page={d.get('usage_page'):#06x} usage={d.get('usage'):#06x} "
            f"iface={d.get('interface_number')}"
        )
    if args.list:
        return

    import hid
    dev = hid.Device(path=ifaces[0]["path"])
    print(f"\nSending {len(reports)} enable reports ...")
    for h, r in zip(ENABLE_SETS, reports):
        dev.write(r)
        print("  ->", h)
        time.sleep(0.05)
    dev.close()
    print("\nDone. The DSP state now persists until the headset loses power.")


if __name__ == "__main__":
    main()
