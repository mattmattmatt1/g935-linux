import unittest

from g935.hidpp import ERROR_CODES, parse_reply, to_report


class FrameTests(unittest.TestCase):
    def test_to_report_pads_to_20(self):
        r = to_report("11ff052b01")
        self.assertEqual(len(r), 20)
        self.assertEqual(r[:5], bytes.fromhex("11ff052b01"))
        self.assertEqual(r[5:], b"\x00" * 15)

    def test_to_report_accepts_spaces(self):
        self.assertEqual(to_report("11 ff 05 2b 01"), to_report("11ff052b01"))

    def test_to_report_rejects_overlong(self):
        with self.assertRaises(ValueError):
            to_report("11" * 21)

    def test_parse_ack(self):
        sent = to_report("11ff052b01")
        # echo with payload
        buf = bytes([0x11, 0xFF, 0x05, 0x2B, 0x01]) + b"\x00" * 15
        status, detail = parse_reply(buf, sent[2], sent[3])
        self.assertEqual(status, "ACK")
        self.assertEqual(detail[2], 0x05)

    def test_parse_err(self):
        sent = to_report("11ff052b01")
        # HID++2.0 error: 11 ff ff feat fnsw err
        buf = bytes([0x11, 0xFF, 0xFF, 0x05, 0x2B, 0x05]) + b"\x00" * 14
        status, detail = parse_reply(buf, sent[2], sent[3])
        self.assertEqual(status, "ERR")
        self.assertEqual(detail, 0x05)
        self.assertIn(0x05, ERROR_CODES)

    def test_parse_unrelated(self):
        buf = bytes([0x11, 0xFF, 0x08, 0x0B, 0x00]) + b"\x00" * 15
        self.assertIsNone(parse_reply(buf, 0x05, 0x2B))

    def test_parse_short(self):
        self.assertIsNone(parse_reply(b"\x11\xff", 0x05, 0x2B))


if __name__ == "__main__":
    unittest.main()
