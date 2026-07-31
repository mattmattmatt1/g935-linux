import unittest

from g935.stereo_route import (
    analyze_stereo,
    find_g935_playback_ports,
    parse_pw_link_list,
    parse_pw_link_ports,
)


# Minimal fixture matching the post-resume bug (FL linked, FR not)
BROKEN_L = """
effect_output.dts71gaming:output_FL
  |-> alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
effect_output.dts71gaming:output_FR
ee_soe_output_level:output_FL
  |-> alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
ee_soe_output_level:output_FR
alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
  |<- effect_output.dts71gaming:output_FL
  |<- ee_soe_output_level:output_FL
alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_2
Firefox:output_FL
  |-> easyeffects_sink:playback_FL
Firefox:output_FR
  |-> easyeffects_sink:playback_FR
"""

FIXED_L = """
ee_soe_output_level:output_FL
  |-> alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
ee_soe_output_level:output_FR
  |-> alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_2
effect_output.dts71gaming:output_FL
  |-> alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
effect_output.dts71gaming:output_FR
  |-> alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_2
alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
  |<- ee_soe_output_level:output_FL
  |<- effect_output.dts71gaming:output_FL
alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_2
  |<- ee_soe_output_level:output_FR
  |<- effect_output.dts71gaming:output_FR
"""

INPUTS = """
alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_2
easyeffects_sink:playback_FL
easyeffects_sink:playback_FR
"""


class ParseTests(unittest.TestCase):
    def test_parse_links(self):
        g = parse_pw_link_list(BROKEN_L)
        fl = "alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL"
        self.assertIn(
            fl,
            g["ee_soe_output_level:output_FL"],
        )
        # reverse edge recorded
        self.assertIn("ee_soe_output_level:output_FL", g[fl] or
                      [s for s, ds in g.items() if fl in ds])

    def test_find_ports(self):
        ports = parse_pw_link_ports(INPUTS)
        fl, fr = find_g935_playback_ports(ports)
        self.assertTrue(fl.endswith(":playback_FL"))
        self.assertTrue(fr.endswith(":playback_2"))


class AnalyzeTests(unittest.TestCase):
    def test_broken_detects_missing_fr(self):
        g = parse_pw_link_list(BROKEN_L)
        inputs = parse_pw_link_ports(INPUTS)
        st = analyze_stereo(g, inputs)
        self.assertTrue(st.g935_present)
        self.assertFalse(st.ok)
        self.assertTrue(st.broken)
        self.assertTrue(st.missing)
        srcs = {s for s, _ in st.missing}
        self.assertIn("ee_soe_output_level:output_FR", srcs)
        self.assertIn("effect_output.dts71gaming:output_FR", srcs)
        for _, dst in st.missing:
            self.assertTrue(dst.endswith(":playback_2"))

    def test_fixed_is_ok(self):
        g = parse_pw_link_list(FIXED_L)
        inputs = parse_pw_link_ports(INPUTS)
        st = analyze_stereo(g, inputs)
        self.assertTrue(st.g935_present)
        self.assertTrue(st.ok)
        self.assertFalse(st.broken)
        self.assertEqual(st.missing, [])

    def test_no_g935(self):
        g = parse_pw_link_list("foo:output_FL\n  |-> bar:playback_FL\n")
        st = analyze_stereo(g, ["bar:playback_FL"])
        self.assertFalse(st.g935_present)
        self.assertTrue(st.ok)

    def test_mono_only_not_broken(self):
        text = """
speech-dispatcher-dummy:output_FL
  |-> alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_FL
  |<- speech-dispatcher-dummy:output_FL
alsa_output.usb-Logitech_G935_Gaming_Headset-00.analog-stereo:playback_2
"""
        g = parse_pw_link_list(text)
        inputs = parse_pw_link_ports(INPUTS)
        st = analyze_stereo(g, inputs)
        self.assertTrue(st.ok)


if __name__ == "__main__":
    unittest.main()
