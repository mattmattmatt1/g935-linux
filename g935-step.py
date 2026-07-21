#!/usr/bin/env python3
"""
g935-step.py - Step through the G HUB connect sequence ONE command at a time.

Goal: find which command actually enables the real DSP. Commands persist on the
headset until power loss, so the procedure is:

  1. Power-cycle the headset (start from the raw/cold state).
  2. Play music continuously.
  3. Run this script and press Enter to send each numbered step.
  4. Note the step number where the sound changes ("opens up").
  5. Power-cycle again and send ONLY that step (type its number) to confirm it
     works alone, or whether it needs the steps before it.

Interactive keys:
  Enter    send the next step
  <N>      send step N only (jumps there; next Enter continues from N+1)
  l        list all steps (sent steps marked)
  g        send all remaining GETs up to the next SET (fast-forward discovery)
  q        quit

One-shot mode (for driving from another program/chat):
  g935-step.py --send N          send step N and exit
  g935-step.py --send N --thru   send steps 1..N and exit

Note: step marked [SIDETONE] changes mic monitoring - an audible change there
is sidetone, not the DSP. Don't count it unless the *music* changes.
"""
import os, sys, time, glob, select, argparse

VID_PID = "046D:00000A87"

# (hex, is_set, comment) - same order as g935-enable-v2.py
SEQUENCE = [
    ("11ff021b",                     0, "GET feat02 fn1: device info"),
    ("11ff080b",                     0, "GET feat08 fn0"),
    ("11ff040b",                     0, "GET feat04 fn0: DSP caps"),
    ("11ff041b00",                   0, "GET feat04 fn1 chan0"),
    ("11ff042b0000",                 0, "GET feat04 fn2 chan0 p0"),
    ("11ff042b0001",                 0, "GET feat04 fn2 chan0 p1"),
    ("11ff042b0002",                 0, "GET feat04 fn2 chan0 p2"),
    ("11ff042b0003",                 0, "GET feat04 fn2 chan0 p3"),
    ("11ff041b01",                   0, "GET feat04 fn1 chan1"),
    ("11ff042b0100",                 0, "GET feat04 fn2 chan1 p0"),
    ("11ff042b0101",                 0, "GET feat04 fn2 chan1 p1"),
    ("11ff042b0102",                 0, "GET feat04 fn2 chan1 p2"),
    ("11ff042b0103",                 0, "GET feat04 fn2 chan1 p3"),
    ("11ff052b01",                   1, "SET feat05: master enable = 01"),
    ("11ff048b0101",                 1, "SET feat04 fn8 = 01 01"),
    ("11ff070b",                     0, "GET feat07 fn0: sidetone level"),
    ("11ff071b64",                   1, "SET feat07 fn1: sidetone = 100  [SIDETONE]"),
    ("11ff043b010200b4ff1388000700", 1, "SET feat04 fn3 chan1 DSP config"),
    ("11ff043b000200b4ff1388000800", 1, "SET feat04 fn3 chan0 DSP config"),
    ("11ff044b0001",                 1, "SET feat04 fn4 = 00 01"),
    ("11ff081b",                     0, "GET feat08 fn1"),
    ("11ff062b",                     0, "GET feat06 fn2"),
    ("11ff044b0001",                 1, "SET feat04 fn4 = 00 01 (repeat)"),
]

def find_device():
    for d in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            uevent = open(os.path.join(d, "device", "uevent")).read()
            if VID_PID not in uevent:
                continue
            rdesc = open(os.path.join(d, "device", "report_descriptor"), "rb").read()
        except OSError:
            continue
        # only the interface with the Logitech vendor usage page speaks HID++
        if b"\x06\x43\xff" in rdesc:
            return "/dev/" + os.path.basename(d)
    return None

def to_report(hx, size=20):
    b = bytes.fromhex(hx)
    return b + b"\x00" * (size - len(b))

def read_reply(fd, sent, timeout=1.0):
    feat, fnsw = sent[2], sent[3]
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], deadline - time.time())
        if not r:
            break
        buf = os.read(fd, 64)
        if len(buf) < 5 or buf[0] != 0x11:
            continue
        if buf[2] == 0xFF and buf[3] == feat and buf[4] == fnsw:
            return ("ERR", buf[5], buf)
        if buf[2] == feat and buf[3] == fnsw:
            return ("ACK", None, buf)
    return ("TIMEOUT", None, None)

def send_step(fd, i, sent_flags):
    hx, is_set, comment = SEQUENCE[i]
    rpt = to_report(hx)
    os.write(fd, rpt)
    status, err, buf = read_reply(fd, rpt)
    tag = {"ACK": "ACK", "ERR": f"ERR {err:#04x}" if err is not None else "ERR",
           "TIMEOUT": "no reply"}[status]
    reply = f" reply={buf.hex()[:28]}" if buf else ""
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

    dev = args.dev or find_device()
    if not dev:
        sys.exit("G935 hidraw node not found - is the headset on?")
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        sys.exit(f"open {dev}: {e}\n(permission denied? install the udev rule "
                 "from the README, then replug the receiver)")
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
            cmd = input(f"[Enter]=send step {nxt+1}, N=send step N, l=list, g=gets, q=quit > ").strip().lower()
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
