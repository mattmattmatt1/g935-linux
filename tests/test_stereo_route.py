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

    def test_parse_links_with_spaces_in_port_name(self):
        text = (
            "ee_soe_output_level:output_FL\n"
            "  |-> G935 Gaming Headset:playback_FL\n"
            "G935 Gaming Headset:playback_FL\n"
            "  |<- ee_soe_output_level:output_FL\n"
            "G935 Gaming Headset:playback_FR\n"
        )
        g = parse_pw_link_list(text)
        self.assertIn(
            "G935 Gaming Headset:playback_FL",
            g["ee_soe_output_level:output_FL"],
        )
        st = analyze_stereo(
            g, ["G935 Gaming Headset:playback_FL", "G935 Gaming Headset:playback_FR"])
        self.assertTrue(st.broken)
        self.assertEqual(
            st.missing,
            [("ee_soe_output_level:output_FR", "G935 Gaming Headset:playback_FR")],
        )

    def test_find_ports_ignores_surround_numeric_channels(self):
        ports = [
            "Logitech_G935:playback_FL",
            "Logitech_G935:playback_3",
        ]
        fl, fr = find_g935_playback_ports(ports)
        self.assertEqual(fl, "Logitech_G935:playback_FL")
        self.assertIsNone(fr)

    def test_notify_user_with_id_uses_print_id(self):
        from types import SimpleNamespace
        from unittest import mock
        from g935.stereo_route import notify_user_with_id

        with mock.patch("g935.stereo_route.shutil.which", return_value="/usr/bin/notify-send"), \
             mock.patch("g935.stereo_route.subprocess.run",
                        return_value=SimpleNamespace(returncode=0, stdout="42\n", stderr="")) as run:
            ok, nid = notify_user_with_id("Title", "Body", urgency="critical")
        self.assertTrue(ok)
        self.assertEqual(nid, 42)
        cmd = run.call_args[0][0]
        self.assertIn("--print-id", cmd)
        self.assertFalse(any(a.startswith("--replace-id=") for a in cmd))


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
