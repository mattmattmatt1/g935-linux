import unittest

from g935.pulse import (
    PA_SUBSCRIPTION_EVENT_CARD,
    PA_SUBSCRIPTION_EVENT_CHANGE,
    PA_SUBSCRIPTION_EVENT_NEW,
    PA_SUBSCRIPTION_EVENT_REMOVE,
    PA_SUBSCRIPTION_EVENT_SERVER,
    PA_SUBSCRIPTION_EVENT_SINK,
    PA_SUBSCRIPTION_EVENT_SOURCE,
    PA_VOLUME_NORM,
    _pa_cvolume,
    classify_pa_event,
    pa_cvolume_to_pct,
    pct_to_pa_volume,
)


class PulseMathTests(unittest.TestCase):
    def test_pct_roundtrip(self):
        self.assertEqual(pct_to_pa_volume(0), 0)
        self.assertEqual(pct_to_pa_volume(100), PA_VOLUME_NORM)
        self.assertEqual(pct_to_pa_volume(200), PA_VOLUME_NORM * 2)
        cvol = _pa_cvolume()
        cvol.channels = 2
        cvol.values[0] = pct_to_pa_volume(75)
        cvol.values[1] = pct_to_pa_volume(75)
        self.assertEqual(pa_cvolume_to_pct(cvol), 75)

    def test_classify_pa_events(self):
        self.assertEqual(
            classify_pa_event(PA_SUBSCRIPTION_EVENT_SINK | PA_SUBSCRIPTION_EVENT_CHANGE),
            "levels",
        )
        self.assertEqual(
            classify_pa_event(PA_SUBSCRIPTION_EVENT_SINK | PA_SUBSCRIPTION_EVENT_NEW),
            "devices",
        )
        self.assertEqual(
            classify_pa_event(PA_SUBSCRIPTION_EVENT_SOURCE | PA_SUBSCRIPTION_EVENT_REMOVE),
            "devices",
        )
        self.assertEqual(
            classify_pa_event(PA_SUBSCRIPTION_EVENT_SERVER | PA_SUBSCRIPTION_EVENT_CHANGE),
            "devices",
        )
        self.assertEqual(
            classify_pa_event(PA_SUBSCRIPTION_EVENT_CARD | PA_SUBSCRIPTION_EVENT_CHANGE),
            "devices",
        )
        # sink-input / source-output (facility 2 / 3) must be ignored
        self.assertIsNone(classify_pa_event(0x0002 | PA_SUBSCRIPTION_EVENT_CHANGE))
        self.assertIsNone(classify_pa_event(0x0003 | PA_SUBSCRIPTION_EVENT_NEW))


if __name__ == "__main__":
    unittest.main()
