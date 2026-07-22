#!/usr/bin/env python3
"""
g935-enable-v2.py - Replay G HUB's full connect sequence with reply checking.

v1 sent 6 SET commands blind. On a cold connect G HUB actually does a
discovery/GET phase then the enable burst. v2 sends the same order G HUB uses,
reads every reply, retries on error, and prints ACK / ERR for each step.

Usage:  python3 tools/g935-enable-v2.py [--dev /dev/hidrawN] [--tries N]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from g935.hidpp import G935_PID, find_device_by_pid, open_hidraw, transact
from g935.sequences import COLD_CONNECT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default=None, help="hidraw node (default: auto-detect)")
    ap.add_argument("--tries", type=int, default=3, help="retries per command on error")
    args = ap.parse_args()

    dev = args.dev or find_device_by_pid(G935_PID)
    if dev is None:
        sys.exit(
            "No G935 receiver found (no hidraw with 046D:0A87 + HID++ "
            "descriptor). Plug it in or pass --dev /dev/hidrawN."
        )
    try:
        fd = open_hidraw(dev)
    except OSError as e:
        sys.exit(
            f"open {dev}: {e}\n(permission denied? install the udev rule "
            "from the README, then replug the receiver)"
        )
    print(f"Opened {dev}. Sending {len(COLD_CONNECT)} commands with reply checking.\n")
    failures = []
    for hx, is_set, comment in COLD_CONNECT:
        for attempt in range(1, args.tries + 1):
            status, detail = transact(fd, hx)
            if status == "ACK":
                tag = "ACK "
                reply = f" reply={detail.hex()[:28]}" if detail else ""
            elif status == "ERR":
                tag = f"ERR {detail:#04x}" if detail is not None else "ERR"
                reply = ""
            else:
                tag = "no reply"
                reply = ""
            print(
                f"  {hx:<30} {tag:<9}{reply}   {comment}"
                + (f"  [attempt {attempt}]" if attempt > 1 else "")
            )
            if status == "ERR" and attempt < args.tries:
                time.sleep(0.5)
                continue
            break
        if status != "ACK" and is_set:
            failures.append((hx, comment, status, detail if status == "ERR" else None))
        time.sleep(0.06)
    os.close(fd)

    print()
    if failures:
        print("SET commands that did NOT get an ACK:")
        for hx, comment, status, err in failures:
            print(
                f"  {hx}  ({comment}): {status}"
                + (f" code {err:#04x}" if err is not None else "")
            )
        print(
            "\nIf the master enable (052b) errored, wait ~15s after headset "
            "power-on and rerun."
        )
    else:
        print(
            "All SET commands ACKed. Play audio - the full G HUB sound should be active."
        )


if __name__ == "__main__":
    main()
