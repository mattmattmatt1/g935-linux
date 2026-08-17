import unittest

from g935.probe import (
    DANGEROUS_FEATURES,
    FOLLOWUP_GRIDS,
    G935_ADVERTISED,
    catalog_ids,
    classify_function,
    dry_run_plan,
    enable_hidden_cmd,
    feature_name,
    featureset_count_cmd,
    featureset_id_cmd,
    followup_jobs,
    format_flags,
    format_report,
    function_cmd,
    functions_to_probe,
    ids_for_mode,
    parse_featureset_count,
    parse_featureset_id,
    parse_get_feature,
    root_get_feature_cmd,
    skip_function_hit,
    skipped_functions,
    wide_ids,
    ProbeReport,
)


class CatalogTests(unittest.TestCase):
    def test_known_g935_ids_are_named(self):
        for fid in G935_ADVERTISED:
            self.assertNotIn("unknown", feature_name(fid))

    def test_headset_out_and_hidden_are_in_catalog(self):
        ids = catalog_ids()
        self.assertIn(0x8320, ids)
        self.assertIn(0x1E00, ids)
        self.assertIn(0x00D0, ids)
        self.assertEqual(ids, sorted(set(ids)))

    def test_wide_covers_catalog_headset_range(self):
        wide = set(wide_ids())
        self.assertTrue(set(range(0x8300, 0x8330)).issubset(wide))
        self.assertTrue(set(range(0x1E00, 0x1E10)).issubset(wide))

    def test_mode_sizes(self):
        self.assertGreater(len(catalog_ids()), 80)
        self.assertLess(len(catalog_ids()), 800)
        self.assertGreater(len(wide_ids()), len(catalog_ids()))
        self.assertEqual(len(ids_for_mode("full")), 0x10000)
        with self.assertRaises(ValueError):
            ids_for_mode("nope")


class CommandTests(unittest.TestCase):
    def test_root_get_feature(self):
        self.assertEqual(root_get_feature_cmd(0x8320), "11ff000b8320")
        self.assertEqual(featureset_count_cmd(), "11ff010b")
        self.assertEqual(featureset_id_cmd(8), "11ff011b08")

    def test_function_cmd_matches_panel_swid(self):
        self.assertEqual(function_cmd(5, 2), "11ff052b")
        self.assertEqual(function_cmd(6, 3, "02"), "11ff063b02")
        self.assertEqual(enable_hidden_cmd(9, 0), "11ff090b01")

    def test_function_cmd_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            function_cmd(256, 0)
        with self.assertRaises(ValueError):
            function_cmd(0, 16)
        with self.assertRaises(ValueError):
            featureset_id_cmd(-1)


class SafetyTests(unittest.TestCase):
    def test_software_mode_set_is_skipped(self):
        self.assertIn(2, skipped_functions(0x8010))
        self.assertNotIn(2, functions_to_probe(0x8010))

    def test_eq_sidetone_lighting_sets_skipped(self):
        self.assertIn(3, skipped_functions(0x8310))
        self.assertIn(1, skipped_functions(0x8300))
        self.assertTrue({3, 5, 8, 13}.issubset(skipped_functions(0x8070)))
        self.assertIn(2, skipped_functions(0x1F20))
        self.assertNotIn(1, skipped_functions(0x1F20))

    def test_dfu_only_allows_fn0(self):
        for fid in DANGEROUS_FEATURES:
            self.assertEqual(functions_to_probe(fid), [0])

    def test_skip_hit_is_not_interesting(self):
        hit = skip_function_hit(5, 0x8010, 2)
        self.assertTrue(hit.skipped)
        self.assertFalse(hit.interesting)


class ParseTests(unittest.TestCase):
    def test_get_feature_absent(self):
        reply = bytes.fromhex("11ff000b000000")
        hit = parse_get_feature(0x8320, "ACK", reply)
        self.assertFalse(hit.present)
        self.assertEqual(hit.index, 0)

    def test_get_feature_root_index_zero_is_present(self):
        reply = bytes.fromhex("11ff000b000000")
        hit = parse_get_feature(0x0000, "ACK", reply)
        self.assertTrue(hit.present)

    def test_get_feature_present(self):
        reply = bytes.fromhex("11ff000b054000")
        hit = parse_get_feature(0x8010, "ACK", reply)
        self.assertTrue(hit.present)
        self.assertEqual(hit.index, 5)
        self.assertEqual(hit.flags, 0x40)

    def test_get_feature_timeout(self):
        hit = parse_get_feature(0x1E00, "TIMEOUT", None)
        self.assertFalse(hit.present)
        self.assertEqual(hit.status, "TIMEOUT")

    def test_featureset_parsers(self):
        self.assertEqual(
            parse_featureset_count("ACK", bytes.fromhex("11ff010b08")), 8)
        self.assertIsNone(parse_featureset_count("TIMEOUT", None))
        slot = parse_featureset_id(
            6, "ACK", bytes.fromhex("11ff011b83100001"))
        self.assertEqual(slot.feature_id, 0x8310)
        self.assertEqual(slot.flags, 0)
        self.assertEqual(slot.version, 1)

    def test_classify_ack_and_invalid_argument(self):
        ack = classify_function(
            6, 0x8310, 2, "ACK",
            bytes.fromhex("11ff062b0200000000000000000000000000"))
        self.assertTrue(ack.interesting)
        self.assertEqual(ack.payload_hex, "0200000000000000")
        need = classify_function(4, 0x8070, 1, "ERR", 0x02)
        self.assertTrue(need.interesting)
        missing = classify_function(4, 0x8070, 9, "ERR", 0x07)
        self.assertFalse(missing.interesting)

    def test_flag_format(self):
        self.assertEqual(format_flags(0), "-")
        self.assertEqual(format_flags(0x40), "hidden")
        self.assertIn("internal", format_flags(0x60))


class ReportTests(unittest.TestCase):
    def test_unexpected_feature_and_function(self):
        report = ProbeReport()
        report.catalog.append(parse_get_feature(
            0x8320, "ACK", bytes.fromhex("11ff000b090000")))
        report.functions.append(classify_function(
            6, 0x8310, 7, "ACK", bytes.fromhex("11ff067b01")))
        extra = report.unexpected_features()
        self.assertEqual(extra[0].feature_id, 0x8320)
        unexpected_fn = report.unexpected_functions()
        self.assertEqual(unexpected_fn[0].fn, 7)
        text = format_report(report)
        self.assertIn("8320", text)
        self.assertIn("Unexpected live functions", text)

    def test_dry_run_mentions_skips(self):
        plan = dry_run_plan("catalog", True, False)
        self.assertIn("getFeature queries=", plan)
        self.assertIn("skip [2]", plan)
        self.assertIn("11ff000b", plan)

    def test_followup_grid_covers_live_hits(self):
        self.assertIn((0x8010, 1), FOLLOWUP_GRIDS)
        self.assertNotIn((0x8070, 5), FOLLOWUP_GRIDS)
        self.assertNotIn((0x1F20, 2), FOLLOWUP_GRIDS)
        jobs = followup_jobs({0x8010: 5, 0x8070: 4, 0x1F20: 8})
        self.assertEqual(jobs, [(0x8010, 5, 1, "")])


if __name__ == "__main__":
    unittest.main()
