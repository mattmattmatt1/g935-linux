#!/usr/bin/env python3
"""
g935-enable.py  -  Reproduce G HUB's connect-time DSP enable on Linux.

The Logitech G935's "open soundstage" is an ON-DEVICE DSP state that G HUB sets over
HID++ when it connects, and the headset holds it until it loses power. This script sends
that same HID++ enable sequence directly over the headset's raw HID interface, so you get
the G HUB sound on Linux with no G HUB and no software audio processing.

Prior art for the sibling G933: https://github.com/ashkitten/g933-utils

Usage:
    sudo python3 g935-enable.py            # find the device and send the enable sequence
    sudo python3 g935-enable.py --list     # just list matching HID interfaces
    sudo python3 g935-enable.py --dev /dev/hidrawN   # force a specific hidraw node

Needs:  pip install hid          (or: apt install python3-hid)
Run as root, or add a udev rule granting your user access to the hidraw node.
"""
import sys, time, glob, argparse

VID, PID = 0x046D, 0x0A87   # Logitech G935 receiver

# The connect-time SET commands, in order. Each is padded to 20 bytes when sent.
ENABLE_SEQUENCE = [
    "11 ff 05 2b 01",                              # feature 05: master enable
    "11 ff 04 8b 01 01",                           # feature 04 func 8
    "11 ff 07 1b 64",                              # feature 07: level 100
    "11 ff 04 3b 01 02 00 b4 ff 13 88 00 07",      # feature 04 func 3: channel 1
    "11 ff 04 3b 00 02 00 b4 ff 13 88 00 08",      # feature 04 func 3: channel 0
    "11 ff 04 4b 00 01",                           # feature 04 func 4
]

def to_report(hexstr, size=20):
    b = bytes(int(x, 16) for x in hexstr.split())
    return b + b"\x00" * (size - len(b))

def find_interfaces():
    """Return hidapi device_info dicts for the Logitech HID++ vendor interface(s)."""
    import hid
    hits = []
    for d in hid.enumerate(VID, PID):
        # HID++ lives on the vendor collection (usage page >= 0xff00), not the
        # keyboard/consumer/lamparray collections.
        if d.get("usage_page", 0) >= 0xff00:
            hits.append(d)
    return hits

def try_raw_send(path, reports):
    """Fallback: write reports straight to a /dev/hidraw* node."""
    with open(path, "wb", buffering=0) as f:
        for r in reports:
            f.write(r)
            time.sleep(0.05)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dev", help="force a /dev/hidraw* node")
    args = ap.parse_args()

    reports = [to_report(h) for h in ENABLE_SEQUENCE]

    if args.dev:
        print(f"Sending {len(reports)} reports to {args.dev} ...")
        try_raw_send(args.dev, reports)
        print("Done. Play audio and listen for the soundstage to open up.")
        return

    try:
        import hid
    except ImportError:
        sys.exit("Install the hid package first:  pip install hid   (or apt install python3-hid)\n"
                 "Or point at the node directly:  sudo python3 g935-enable.py --dev /dev/hidrawN")

    ifaces = find_interfaces()
    if not ifaces:
        sys.exit("No Logitech G935 HID++ interface found. Is the headset ON and connected?\n"
                 "List candidates:  ls -l /dev/hidraw*  and try --dev on each.")

    for d in ifaces:
        print(f"found HID++ iface: path={d['path'].decode(errors='replace')} "
              f"usage_page={d.get('usage_page'):#06x} usage={d.get('usage'):#06x} "
              f"iface={d.get('interface_number')}")
    if args.list:
        return

    dev = hid.Device(path=ifaces[0]["path"])
    print(f"\nSending {len(reports)} enable reports ...")
    for h, r in zip(ENABLE_SEQUENCE, reports):
        dev.write(r)
        print("  ->", h)
        time.sleep(0.05)
    dev.close()
    print("\nDone. The DSP state now persists until the headset loses power.")
    print("Play audio and confirm the open soundstage is present.")

if __name__ == "__main__":
    main()
