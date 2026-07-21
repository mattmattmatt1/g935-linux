#!/usr/bin/env python3
"""
g935-dspd.py - G935 housekeeping daemon: keep the chosen mode across power-ons.

11 ff 05 2b 01 = "G HUB mode" (DSP soundstage ON + mic handling moves from
firmware to host); 00 = stock hardware mode (flat sound, mic fully
self-managed). The setting lives on the headset and resets on power loss.
The hidraw node belongs to the USB receiver (always present), so udev can't
see headset power-ons. We poll the battery GET every few seconds; a no-reply
-> reply transition means the headset just came up, and if the configured
mode (~/.config/g935/mode, written by g935-control.py) is "ghub" the enable
is re-sent (with retries - the headset can reject commands with error 05
right after power-on).

Boom position is taken from the device's own current value of input report
0x08 (ioctl HIDIOCGINPUT: bit 0x10 = boom up, bit 0x20 = boom down), polled
4x/s, NOT from the 0x08 event stream. The events are unreliable as a source of
truth: our own ALSA mute writes provoke identical-looking ones, so the old
event+dead-time scheme both chased its own echoes and went deaf to real boom
moves for 4 s after every unmute.

Runs fine alongside g935-control.py (each hidraw fd gets its own copy of
input reports).

Usage: python3 g935-dspd.py   (see g935-dsp.service for systemd --user setup)
"""
import os, glob, time, select, subprocess, fcntl

VID_PID = "046D:00000A87"
ALSA_USBID = "046d:0a87"
MIC_SWITCH_NAME = "Mic Capture Switch"

def find_alsa_card():
    """ALSA card index whose USB id matches the headset, or None. By-index +
    by-name addressing survives enumeration order; numids shift across kernels
    and the literal card id 'Headset' collides with other USB headsets."""
    for p in glob.glob("/proc/asound/card*/usbid"):
        try:
            if open(p).read().strip().lower() == ALSA_USBID:
                return os.path.basename(os.path.dirname(p))[4:]
        except OSError:
            continue
    return None
MODE_FILE = os.path.expanduser("~/.config/g935/mode")
POLL_S = 5
ENABLE = "11ff052b01"
BATTERY_GET = "11ff080b"

def load_mode():
    """'ghub' = DSP soundstage + software mic handling; 'hardware' = stock.
    Re-read on every power-on so GUI changes take effect without a restart."""
    try:
        return open(MODE_FILE).read().strip() or "ghub"
    except OSError:
        return "ghub"

def log(msg):
    print(msg, flush=True)

def find_device():
    for d in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            uevent = open(os.path.join(d, "device", "uevent")).read()
            if VID_PID not in uevent:
                continue
            rdesc = open(os.path.join(d, "device", "report_descriptor"), "rb").read()
        except OSError:
            continue
        # The receiver exposes several HID interfaces; only the one whose
        # report descriptor declares the Logitech vendor usage page (06 43 ff)
        # speaks HID++. Glob order is not stable across machines.
        if b"\x06\x43\xff" in rdesc:
            return "/dev/" + os.path.basename(d)
    return None

def read_host_mic_switch():
    """True = capture on, False = host-muted, None = unknown (incl. no amixer)."""
    card = find_alsa_card()
    if card is None:
        return None
    try:
        out = subprocess.run(["amixer", "-c", card, "cget", "name=" + MIC_SWITCH_NAME],
                             capture_output=True, text=True,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    except FileNotFoundError:
        return None
    for line in out.splitlines():
        if ": values=" in line:
            return line.strip().endswith("=on")
    return None

def set_switch(on):
    card = find_alsa_card()
    if card is None:
        return
    try:
        subprocess.run(["amixer", "-q", "-c", card, "cset",
                        "name=" + MIC_SWITCH_NAME, "on" if on else "off"], check=False,
                       env={**os.environ, "LC_ALL": "C"})
    except FileNotFoundError:
        log("amixer not found - install alsa-utils for mic handling")

# G HUB-mode mic state (the boom-mic mute mechanism):
# boom-up: the headset mutes itself (sets its internal boom flag) and reports
# 0810. Boom-down reports 0820 but clears NOTHING - the host must send a UAC
# mute pulse (off, HOLD >=1.5s, on - both writes AFTER the boom-down) to merge
# the flag away. Found by automated RMS-verified method hunt 2026-07-21; a
# 0.3s hold does NOT work, nor does pre-positioning the off write at boom-up.
MIC_REPORT = 0x08
# Measured 2026-07-21 with the daemon stopped and the host switch forced on, so
# the only thing muting was the headset itself: boom up reads 0x11 and goes
# silent, boom down reads 0x21 and is live.
BOOM_UP, BOOM_DOWN, BUTTON = 0x10, 0x20, 0x01
MIC_POLL_S = 0.25

_boom_down = None         # last observed boom position (None = not read yet)
_needs_unmute = False     # a boom-up happened; the next boom-down owes a pulse

def hidiocginput(length):
    """_IOC(_IOC_READ|_IOC_WRITE, 'H', 0x0A, length)"""
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x0A

def read_boom(fd):
    """Ask the headset where the boom is right now: True=down, False=up.
    Level truth - it cannot be missed the way an event can, and it is immune to
    the echoes our own ALSA writes provoke (those never move it)."""
    buf = bytearray(2)
    buf[0] = MIC_REPORT
    try:
        fcntl.ioctl(fd, hidiocginput(len(buf)), buf, True)
    except OSError:
        return None
    if buf[1] & BOOM_DOWN:
        return True
    if buf[1] & BOOM_UP:
        return False
    return None

def unmute_pulse(fd):
    log("unmute pulse: switch off, hold 1.5s, on")
    set_switch(False)
    time.sleep(1.5)
    if read_boom(fd) is False:
        # Raised during the hold: don't hand back a live mic, however briefly.
        # The device flag stays set, which costs nothing - the boom is up, and
        # coming back down owes a fresh pulse anyway.
        log("boom went up mid-pulse: staying muted")
        return
    set_switch(True)

def poll_mic(fd):
    """Track boom position by level and settle the mic to match it."""
    global _boom_down, _needs_unmute
    down = read_boom(fd)
    if down is None:
        return
    changed = down != _boom_down
    if changed and _boom_down is not None:
        log(f"boom {'down' if down else 'up'}")
    _boom_down = down
    if load_mode() != "ghub":
        return
    if not down:
        if changed:
            _needs_unmute = True       # device muted itself; we owe it a pulse
            # Boom up must mean muted, and the device flag alone doesn't
            # guarantee that: an unmute pulse merges the flag away whatever the
            # boom is doing, which leaves a raised boom live. Hold the host
            # layer down instead - that one can't be cleared behind our back.
            if read_host_mic_switch():
                log("boom up: host switch off")
                set_switch(False)
        return
    if not _needs_unmute:
        return
    # Cleared before the pulse, not after: a boom-up during those 1.5 blind
    # seconds sets it again and we simply run another pulse next time round.
    _needs_unmute = False
    unmute_pulse(fd)
    poll_mic(fd)                       # resync position after the blind window

def handle_report(buf, fd):
    """The G HUB role: in G HUB mode the headset only self-mutes on boom-up;
    everything else (boom-down unmute, button toggle) is the host's job.
    In hardware mode the headset self-manages - log only.

    Boom decisions live in poll_mic(); an 0810 here is only a hint that a
    boom-up may have slipped between two polls, and is trusted only when the
    device's own level agrees. That check is what keeps our mute writes from
    echoing back in as fake boom-ups and pulsing forever."""
    global _needs_unmute
    if len(buf) < 2 or buf[0] != MIC_REPORT:
        return
    bits = buf[1]
    log(f"mic event: {buf[:2].hex()}")
    if load_mode() != "ghub":
        return
    if bits & BOOM_UP and read_boom(fd) is False:
        _needs_unmute = True
    elif bits & BUTTON and not bits & (BOOM_UP | BOOM_DOWN):
        cur = read_host_mic_switch()   # the button does nothing on-device here
        if cur is None:
            return
        if cur:
            log("button: muting (host switch off)")
            set_switch(False)
        elif not _boom_down:
            log("button: ignored, boom is up")
        elif _needs_unmute:
            log("button: unmuting (stale boom flag, full pulse)")
            _needs_unmute = False
            unmute_pulse(fd)
        else:
            log("button: unmuting (host switch on)")
            set_switch(True)

def transact(fd, hx, timeout=1.0):
    rpt = bytes.fromhex(hx) + b"\x00" * (20 - len(hx) // 2)
    os.write(fd, rpt)
    feat, fnsw = rpt[2], rpt[3]
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], deadline - time.time())
        if not r:
            break
        buf = os.read(fd, 64)
        if len(buf) < 2:
            continue
        if buf[0] != 0x11:
            handle_report(buf, fd)
            continue
        if buf[2] == 0xFF and buf[3] == feat and buf[4] == fnsw:
            return "ERR", buf[5]
        if buf[2] == feat and buf[3] == fnsw:
            return "ACK", buf
    return "TIMEOUT", None

def enable_dsp(fd):
    for attempt in range(5):
        status, detail = transact(fd, ENABLE)
        if status == "ACK":
            log("DSP soundstage enabled")
            return True
        log(f"enable attempt {attempt + 1}: {status}"
            + (f" code {detail:#04x}" if status == "ERR" else ""))
        time.sleep(2)
    log("giving up until next power-on")
    return False

def main():
    global _boom_down, _needs_unmute
    connected = False   # start pessimistic: first successful poll triggers enable
    fd = None
    next_poll = 0.0
    while True:
        if fd is None:
            dev = find_device()
            if dev is None:
                connected = False
                time.sleep(POLL_S)
                continue
            try:
                fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
                log(f"opened {dev}")
            except OSError as e:
                log(f"open {dev}: {e}")
                time.sleep(POLL_S)
                continue
        try:
            now = time.time()
            if now >= next_poll:
                status, _ = transact(fd, BATTERY_GET)
                next_poll = now + POLL_S
                if status == "ACK":
                    if not connected:
                        mode = load_mode()
                        log(f"headset detected (power-on), mode={mode}")
                        if mode == "ghub":
                            time.sleep(2)
                            enable_dsp(fd)
                            # A headset that powered on with the boom up carries
                            # the flag into this session; owe it a pulse.
                            _needs_unmute = True
                        else:
                            log("hardware mode - leaving headset stock")
                        connected = True
                else:
                    connected = False
                    _boom_down = None
            # between polls: react to unsolicited reports immediately, and keep
            # sampling the boom level so a missed event can never strand the mic
            wait = max(0.0, min(next_poll, time.time() + MIC_POLL_S) - time.time())
            r, _, _ = select.select([fd], [], [], wait)
            if r:
                buf = os.read(fd, 64)
                if len(buf) >= 2 and buf[0] != 0x11:
                    handle_report(buf, fd)
            if connected:
                poll_mic(fd)
        except OSError:            # receiver unplugged
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
            connected = False
            _boom_down = None
            log("receiver gone, rescanning")
            time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
