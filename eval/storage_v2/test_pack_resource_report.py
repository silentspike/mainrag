import copy
import itertools
import json
import unittest

from pack_resource_report import report, select_cohort


def fixture():
    rows = []
    for rep, large, pattern, codec, buffer in itertools.product((1, 2, 3), (1048576, 16777216), ("repeat", "random"), ("identity", "zstd"), (4096, 65536)):
        logical = large + 4096 + 262144
        rows.append(dict(schema="pack-resource-v1", scope="physical_pack_only", profile="debug",
                         repetition=rep, large_body_bytes=large, pattern=pattern, codec=codec, buffer_bytes=buffer,
                         logical_bytes=logical, source_stored_bytes=logical, stored_bytes=logical,
                         build_ms=10, rewrite_ms=100+rep, verify_ms=10,
                         rewrite_mib_s=logical/((100+rep)/1000)/1048576,
                         process_peak_rss_bytes=20000000, process_baseline_hwm_bytes=10000000,
                         integrity_passed=1, entry_count=3, sql_ms=None, device_io_bytes=None))
    return rows


def convert(rows):
    return report("\n".join("PACK_RESOURCE " + json.dumps(row) for row in rows)
                  + "\ntest result: ok. 1 passed; 0 failed; 0 ignored;", "a" * 40)


class PackResourceReportTests(unittest.TestCase):
    def test_complete_matrix_retains_raw_values_noise_and_no_default(self):
        result = convert(fixture())
        self.assertEqual(len(result["runs"]), 48)
        self.assertEqual(len(result["zustaende"]), 16)
        self.assertIsNone(result["selected_default"])
        self.assertEqual(result["qualification"], "diagnostic_only")
        for values in result["metrics"]["pack_resource.rewrite_ms"]["z"].values():
            self.assertEqual(values["median"], 102)
            self.assertEqual(values["n"], 3)
            self.assertGreater(values["streuung"], 0)

    def test_missing_and_duplicate_runs_fail(self):
        rows = fixture()
        for invalid in (rows[:-1], rows + rows[:1]):
            with self.assertRaises(ValueError):
                convert(invalid)

    def test_viewer_cohort_contains_only_comparable_settings(self):
        full = convert(fixture())
        selected = select_cohort(full, "random-size16777216")
        self.assertEqual(len(selected["runs"]), 12)
        self.assertEqual(len(selected["zustaende"]), 4)
        self.assertEqual(len(full["runs"]), 48)
        self.assertEqual(selected["validated_matrix_runs"], 48)
        for metric in selected["metrics"].values():
            self.assertEqual(len(metric["v"]), 12)
            self.assertEqual(len(metric["z"]), 4)
        with self.assertRaises(ValueError):
            select_cohort(full, "unknown")

    def test_invalid_observations_fail_closed(self):
        for field, value in (("integrity_passed", 0), ("rewrite_ms", None), ("rewrite_ms", float("nan")),
                             ("stored_bytes", True), ("process_peak_rss_bytes", 2**30),
                             ("process_baseline_hwm_bytes", 30000000), ("sql_ms", 0),
                             ("device_io_bytes", 0), ("rewrite_mib_s", 1), ("entry_count", 2),
                             ("profile", "release"), ("scope", "ingest")):
            rows = copy.deepcopy(fixture())
            rows[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                convert(rows)

    def test_exact_revision_is_required(self):
        with self.assertRaises(ValueError):
            report("", "main")

    def test_failed_parent_and_unexpected_public_fields_fail(self):
        with self.assertRaises(ValueError):
            report("\n".join("PACK_RESOURCE " + json.dumps(row) for row in fixture()), "a"*40)
        rows = fixture()
        rows[0]["private_context"] = "must not be exported"
        with self.assertRaises(ValueError):
            convert(rows)


if __name__ == "__main__":
    unittest.main()
