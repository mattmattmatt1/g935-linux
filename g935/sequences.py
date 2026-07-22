"""Authoritative cold-connect HID++ sequence (hex without spaces).

Shared by tools/g935-enable-v2.py and tools/g935-step.py so they cannot drift.
Each entry: (hex, is_set, comment).
"""

# Order G HUB uses on a cold connect (captured 2026-07-20/21).
COLD_CONNECT = [
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
    ("11ff070b", 0, "GET feat07 fn0: sidetone level"),
    ("11ff071b64", 1, "SET feat07 fn1: sidetone = 100  [SIDETONE]"),
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

# Minimal SET-only enable (what actually sticks the soundstage on a warm device)
ENABLE_SETS = [
    "11ff052b01",
    "11ff048b0101",
    "11ff071b64",
    "11ff043b010200b4ff1388000700",
    "11ff043b000200b4ff1388000800",
    "11ff044b0001",
]
