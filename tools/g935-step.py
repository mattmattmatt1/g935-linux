#!/usr/bin/env python3
"""
g935-step.py - Step through the G HUB connect sequence ONE command at a time.

Uses the same COLD_CONNECT sequence as g935-enable-v2.py (shared constant).

Interactive keys:
  Enter    send the next step
  <N>      send step N only (jumps there; next Enter continues from N+1)
  l        list all steps (sent steps marked)
  g        send all remaining GETs up to the next SET (fast-forward discovery)
  q        quit

One-shot mode:
  g935-step.py --send N          send step N and exit
  g935-step.py --send N --thru   send steps 1..N and exit
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

SEQUENCE = COLD_CONNECT


def send_step(fd, i, sent_flags):
    hx, is_set, comment = SEQUENCE[i]
    status, detail = transact(fd, hx)
    if status == "ACK":
        tag = "ACK"
        reply = f" reply={detail.hex()[:28]}" if detail else ""
    elif status == "ERR":
        tag = f"ERR {detail:#04x}" if detail is not None else "ERR"
        reply = ""
    else:
        tag = "no reply"
        reply = ""
    kind = "SET" if is_set else "get"
    print(f"  [{i+1:2}] {kind} {hx:<30} {tag:<9}{reply}  {comment}")
    sent_flags[i] = True
    return status


def list_steps(sent_flags, nxt):
    print()
    for i, (hx, is_set, comment) in enumerate(SEQUENCE):
        mark = "*" if sent_flags[i] else (">" if i == nxt else " ")
        kind = "SET" if is_set else "get"
        print(f" {mark}[{i+1:2}] {kind}  {hx:<30} {comment}")
    print(" (* = sent this run, > = next)\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default=None)
    ap.add_argument("--send", type=int, help="one-shot: send step N (1-based) and exit")
    ap.add_argument("--thru", action="store_true", help="with --send: send steps 1..N")
    args = ap.parse_args()

    dev = args.dev or find_device_by_pid(G935_PID)
    if not dev:
        sys.exit("G935 hidraw node not found - is the headset on?")
    try:
        fd = open_hidraw(dev)
    except OSError as e:
        sys.exit(
            f"open {dev}: {e}\n(permission denied? install the udev rule "
            "from the README, then replug the receiver)"
        )
    print(f"Opened {dev}. {len(SEQUENCE)} steps.")

    sent_flags = [False] * len(SEQUENCE)

    if args.send:
        n = args.send
        if not 1 <= n <= len(SEQUENCE):
            sys.exit(f"step must be 1..{len(SEQUENCE)}")
        rng = range(n) if args.thru else [n - 1]
        for i in rng:
            send_step(fd, i, sent_flags)
            time.sleep(0.06)
        os.close(fd)
        return

    print("Power-cycle the headset first so it starts cold, and keep music playing.")
    list_steps(sent_flags, 0)
    nxt = 0
    while True:
        try:
            cmd = input(
                f"[Enter]=send step {nxt+1}, N=send step N, l=list, g=gets, q=quit > "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd == "q":
            break
        elif cmd == "l":
            list_steps(sent_flags, nxt)
        elif cmd == "g":
            while nxt < len(SEQUENCE) and SEQUENCE[nxt][1] == 0:
                send_step(fd, nxt, sent_flags)
                time.sleep(0.06)
                nxt += 1
        elif cmd == "":
            if nxt >= len(SEQUENCE):
                print("Done - all steps sent.")
                continue
            send_step(fd, nxt, sent_flags)
            nxt += 1
        elif cmd.isdigit():
            n = int(cmd)
            if 1 <= n <= len(SEQUENCE):
                send_step(fd, n - 1, sent_flags)
                nxt = n
            else:
                print(f"step must be 1..{len(SEQUENCE)}")
        else:
            print("unrecognized input")
    os.close(fd)


if __name__ == "__main__":
    main()
