import unittest

from g935.features import (
    build_light_params, format_frequency, parse_device_info, parse_eq_info,
    parse_firmware_info, parse_frequency_page, parse_gkey_mask,
    parse_led_effect_info, parse_led_state, parse_led_zone_info,
)


class FeatureDecoderTests(unittest.TestCase):
    def test_device_info_from_live_g935_reply(self):
        reply = bytes.fromhex(
            "11ff020b01ffffffff0003000000000a87000000")
        info = parse_device_info(reply)
        self.assertEqual(info.entity_count, 1)
        self.assertEqual(info.unit_id, "FFFFFFFF")
        self.assertEqual(info.model_id, "000000000A87")
        self.assertEqual(info.transport_mask, 0x03)
        self.assertEqual(info.transports, ("Bluetooth", "Bluetooth LE"))

    def test_firmware_from_live_g935_reply(self):
        reply = bytes.fromhex(
            "11ff021b0055312029000012010a870000000000")
        fw = parse_firmware_info(reply)
        self.assertEqual(fw.entity_type, "Main application")
        self.assertEqual(fw.prefix, "U1")
        self.assertEqual(fw.version, "29.00")
        self.assertEqual(fw.build, 12)
        self.assertTrue(fw.active)
        self.assertEqual(fw.transport_pid, 0x0A87)

    def test_eq_info_and_frequency_page(self):
        info = parse_eq_info(bytes.fromhex("11ff060b0a0c010000"))
        self.assertEqual((info.bands, info.minimum_db, info.maximum_db),
                         (10, -12, 12))
        page = bytes.fromhex(
            "11ff061b0000200040007d00fa01f403e807d0")
        self.assertEqual(
            parse_frequency_page(page, 7, 0),
            [32, 64, 125, 250, 500, 1000, 2000],
        )
        self.assertEqual(format_frequency(1000), "1k")
        self.assertEqual(format_frequency(125), "125")

    def test_gkey_mask_is_little_endian(self):
        report = bytes.fromhex("11ff050001000000")
        self.assertEqual(parse_gkey_mask(report), 1)
        report = bytes.fromhex("11ff050004000000")
        self.assertEqual(parse_gkey_mask(report), 4)

    def test_live_led_info_layouts(self):
        self.assertEqual(
            parse_led_zone_info(bytes.fromhex("11ff041b0000020400")),
            (0, "Logo", 4),
        )
        self.assertEqual(
            parse_led_effect_info(bytes.fromhex("11ff042b00030003c0050006")),
            (0, 3, 3, 0xC005, 6),
        )
        state = parse_led_state(bytes.fromhex(
            "11ff04eb0102ff00001f400664000000"))
        self.assertEqual(state.zone, 1)
        self.assertEqual(state.mode, 2)
        self.assertEqual(state.rgb, (255, 0, 0))
        self.assertEqual(state.period_ms, 8000)
        self.assertEqual(state.waveform, 6)
        self.assertEqual(state.intensity, 100)


class LightingBuilderTests(unittest.TestCase):
    def test_off(self):
        self.assertEqual(build_light_params(1, 0),
                         "0100" + "00" * 10 + "01")

    def test_fixed(self):
        self.assertEqual(
            build_light_params(0, 1, (0x12, 0x34, 0x56), ramp=2),
            "000112345602" + "00" * 6 + "01",
        )

    def test_breathing(self):
        self.assertEqual(
            build_light_params(
                1, 2, (0, 0xB4, 0xFF), 5000, 7, waveform=0),
            "010200b4ff13880007" + "00" * 3 + "01",
        )

    def test_cycling(self):
        self.assertEqual(
            build_light_params(1, 3, period_ms=5000, intensity=100),
            "0103" + "00" * 5 + "138864" + "00" * 2 + "01",
        )

    def test_invalid_values(self):
        with self.assertRaises(ValueError):
            build_light_params(0, 3, intensity=101)


if __name__ == "__main__":
    unittest.main()
