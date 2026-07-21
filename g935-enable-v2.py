#!/usr/bin/env python3
"""
g935-enable-v2.py - Replay G HUB's full connect sequence with reply checking.

v1 sent 6 SET commands blind. On a cold connect G HUB actually does:
  1. a discovery/GET phase (features 02, 08, 04 per-channel reads)
  2. THEN the enable burst - and its own first early attempt at the master enable
     (11ff052b01) gets REJECTED with error 05 before discovery/settling.
So v2 sends the same order G HUB uses, reads every reply, retries on error, and
prints ACK / ERR for each step so we can see exactly what the headset accepts.

Reply format:  11 ff <feat> <funcSwId> <params...>       = ACK (echo)
               11 ff ff <feat> <funcSwId> <errCode>      = HID++2.0 error

Usage:  python3 g935-enable-v2.py [--dev /dev/hidrawN] [--tries N]
        (no --dev: auto-detects the G935's HID++ hidraw node)
"""
import os, sys, glob, time, select, argparse

VID_PID = "046D:00000A87"

def find_device():
    """The G935 receiver's hidraw node whose report descriptor carries the
    Logitech vendor usage page (06 43 ff) - the HID++ interface."""
    for d in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            uevent = open(os.path.join(d, "device", "uevent")).read()
            if VID_PID not in uevent:
                continue
            rdesc = open(os.path.join(d, "device", "report_descriptor"), "rb").read()
        except OSError:
            continue
        if b"\x06\x43\xff" in rdesc:
            return "/dev/" + os.path.basename(d)
    return None

# (hex-without-spaces, is_set, comment) - the order G HUB uses on a cold connect
SEQUENCE = [
    ("11ff021b", 0, "GET feat02 fn1: device info"),
    ("11ff080b", 0, "GET feat08 fn0"),
    ("11ff040b", 0, "GET feat04 fn0: DSP caps"),
    ("11ff041b00", 0, "GET feat04 fn1 chan0"),
    ("11ff042b0000", 0, "GET feat04 fn2 chan0 p0"),
    ("11ff042b0001", 0, "GET feat04 fn2 chan0 p1"),
    ("11ff042b0002", 0, "GET feat04 fn2 chan0 p2"),
    ("11ff042b0003", 0, "GET feat04 fn2 chan0 p3"),
    ("11ff041b01", 0, "GET feat04 fn1 chan1"),
    ("11ff042b0100", 0, "GET feat04 fn2 chan1 p0"),
    ("11ff042b0101", 0, "GET feat04 fn2 chan1 p1"),
    ("11ff042b0102", 0, "GET feat04 fn2 chan1 p2"),
    ("11ff042b0103", 0, "GET feat04 fn2 chan1 p3"),
    ("11ff052b01", 1, "SET feat05: MASTER ENABLE = 01  <-- the soundstage switch"),
    ("11ff048b0101", 1, "SET feat04 fn8 = 01 01"),
    ("11ff070b", 0, "GET feat07 fn0: level"),
    ("11ff071b64", 1, "SET feat07 fn1: level = 100"),
    ("11ff043b010200b4ff1388000700", 1, "SET feat04 fn3 chan1 DSP config"),
    ("11ff043b000200b4ff1388000800", 1, "SET feat04 fn3 chan0 DSP config"),
    ("11ff044b0001", 1, "SET feat04 fn4 = 00 01"),
    ("11ff081b", 0, "GET feat08 fn1"),
    ("11ff062b", 0, "GET feat06 fn2"),
    # G HUB repeats these three at the end of the burst
    ("11ff044b0001", 1, "SET feat04 fn4 = 00 01 (repeat)"),
    ("11ff081b", 0, "GET feat08 fn1 (repeat)"),
    ("11ff062b", 0, "GET feat06 fn2 (repeat)"),
]

def to_report(hx, size=20):
    b = bytes.fromhex(hx)
    return b + b"\x00" * (size - len(b))

def read_reply(fd, sent, timeout=1.0):
    """Read input reports until we see the echo/error for `sent`, or time out."""
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
        # unrelated notification - keep reading
    return ("TIMEOUT", None, None)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default=None, help="hidraw node (default: auto-detect)")
    ap.add_argument("--tries", type=int, default=3, help="retries per command on error")
    args = ap.parse_args()

    dev = args.dev or find_device()
    if dev is None:
        sys.exit("No G935 receiver found (no hidraw with 046D:0A87 + HID++ "
                 "descriptor). Plug it in or pass --dev /dev/hidrawN.")
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        sys.exit(f"open {dev}: {e}\n(permission denied? install the udev rule "
                 "from the README, then replug the receiver)")
    args.dev = dev
    print(f"Opened {args.dev}. Sending {len(SEQUENCE)} commands with reply checking.\n")
    failures = []
    for hx, is_set, comment in SEQUENCE:
        rpt = to_report(hx)
        for attempt in range(1, args.tries + 1):
            os.write(fd, rpt)
            status, err, buf = read_reply(fd, rpt)
            tag = {"ACK": "ACK ", "ERR": f"ERR {err:#04x}" if err is not None else "ERR",
                   "TIMEOUT": "no reply"}[status]
            reply = f" reply={buf.hex()[:28]}" if buf else ""
            print(f"  {hx:<30} {tag:<9}{reply}   {comment}"
                  + (f"  [attempt {attempt}]" if attempt > 1 else ""))
            if status == "ERR" and attempt < args.tries:
                time.sleep(0.5)
                continue
            break
        if status != "ACK" and is_set:
            failures.append((hx, comment, status, err))
        time.sleep(0.06)
    os.close(fd)

    print()
    if failures:
        print("SET commands that did NOT get an ACK:")
        for hx, comment, status, err in failures:
            print(f"  {hx}  ({comment}): {status}"
                  + (f" code {err:#04x}" if err is not None else ""))
        print("\nIf the master enable (052b) errored, wait ~15s after headset power-on and rerun.")
    else:
        print("All SET commands ACKed. Play audio - the full G HUB sound should be active.")

if __name__ == "__main__":
    main()
