import unittest

from g935.sequences import COLD_CONNECT, ENABLE_SETS


class SequenceTests(unittest.TestCase):
    def test_cold_connect_length(self):
        self.assertEqual(len(COLD_CONNECT), 25)

    def test_master_enable_present(self):
        sets = [hx for hx, is_set, _ in COLD_CONNECT if is_set]
        self.assertIn("11ff052b01", sets)
        self.assertIn("11ff052b01", ENABLE_SETS)

    def test_hex_valid(self):
        for hx, is_set, comment in COLD_CONNECT:
            bytes.fromhex(hx)  # must not raise
            self.assertIn(is_set, (0, 1))
            self.assertTrue(comment)


if __name__ == "__main__":
    unittest.main()
