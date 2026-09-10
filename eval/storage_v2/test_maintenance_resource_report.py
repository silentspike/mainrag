import itertools
import json
import unittest
from maintenance_resource_report import report


def fixtures():
    return [dict(schema="pack-maintenance-resource-v1", scope="integrated_pg_file_operator", profile="debug",
                 repetition=rep, buffer_bytes=buffer, repack_ms=100+rep, finish_ms=20+rep,
                 process_peak_rss_bytes=30000000, process_baseline_hwm_bytes=20000000,
                 moved_entries=2, logical_bytes=1114112, old_file_bytes=1245184, new_file_bytes=80,
                 dead_entry_bytes=131072, reclaimed_file_bytes=1245184, integrity_passed=1,
                 database_server_rss_bytes=None, device_io_bytes=None, sql_only_ms=None)
            for rep, buffer in itertools.product((1, 2, 3), (4096, 65536))]


def convert(rows):
    return report("\n".join("PACK_MAINTENANCE "+json.dumps(row) for row in rows)
                  + "\ntest result: ok. 1 passed; 0 failed; 0 ignored;", "a"*40)


class MaintenanceResourceTests(unittest.TestCase):
    def test_complete_report_preserves_accounting_and_variation(self):
        result = convert(fixtures())
        self.assertEqual(len(result["runs"]), 6)
        self.assertEqual(len(result["zustaende"]), 2)
        self.assertIsNone(result["selected_default"])
        self.assertEqual(result["metrics"]["pack_maintenance.repack_ms"]["z"]["maintenance-buf4096"]["median"], 102)

    def test_missing_duplicate_and_failed_parent_rejected(self):
        rows = fixtures()
        for invalid in (rows[:-1], rows+rows[:1]):
            with self.assertRaises(ValueError):
                convert(invalid)
        with self.assertRaises(ValueError):
            report("", "a"*40)

    def test_wrong_accounting_scope_and_invalid_metrics_rejected(self):
        for field, value in (("reclaimed_file_bytes", 131072), ("dead_entry_bytes", 1245184),
                             ("integrity_passed", 0), ("repack_ms", float("nan")), ("finish_ms", True),
                             ("process_peak_rss_bytes", 2**30), ("sql_only_ms", 0),
                             ("database_server_rss_bytes", 30000000), ("profile", "release"),
                             ("scope", "physical_pack_only"), ("private_context", "do not export")):
            rows = fixtures(); rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                convert(rows)
