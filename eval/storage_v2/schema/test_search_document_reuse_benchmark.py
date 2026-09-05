"""Fail-closed and output-contract checks for the synthetic SQL reuse benchmark."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.storage_v2.schema import benchmark_search_document_reuse as benchmark


class SearchDocumentReuseBenchmarkTests(unittest.TestCase):
    def test_invalid_workload_is_rejected_before_database_setup(self) -> None:
        for repetitions, calls in ((2, 500), (21, 500), (3, 0), (3, 5001)):
            with self.subTest(repetitions=repetitions, calls=calls):
                with mock.patch.object(benchmark, "implementation_identity") as identity:
                    with self.assertRaises(ValueError):
                        benchmark.run_benchmark(repetitions, calls)
                    identity.assert_not_called()

    def test_external_socket_and_dirty_implementation_are_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"STORAGE_V2_TEST_SOCKET": "synthetic-socket"}):
            with self.assertRaisesRegex(RuntimeError, "own disposable"):
                benchmark.run_benchmark()
        with mock.patch.object(benchmark.subprocess, "check_output", return_value=" M fixture.py"):
            with self.assertRaisesRegex(RuntimeError, "commit the benchmark"):
                benchmark.implementation_identity()

    def test_missing_or_partial_function_work_cannot_report_a_timing(self) -> None:
        for loops, rows in ((0, 0), (499, 1), (500, 0)):
            with self.subTest(loops=loops, rows=rows):
                case = mock.Mock()
                case.sql.return_value = json.dumps([{
                    "Execution Time": 0.01,
                    "Plan": {"Node Type": "Aggregate", "Plans": [{
                        "Node Type": "Function Scan", "Actual Loops": loops, "Actual Rows": rows,
                    }]},
                }])
                with self.assertRaisesRegex(RuntimeError, "expected nonempty"):
                    benchmark.measure(case, "body", 500)

    def test_output_and_telemetry_are_private_and_distinct(self) -> None:
        result = {
            "status": "PASS", "corpus": {"documents": 10000, "postings": 20000},
            "medians": {"body": {"before_ms": 100, "after_ms": 10, "speedup_ratio": 10}},
        }
        with tempfile.TemporaryDirectory(prefix="mainrag-sql-reuse-output-") as temporary:
            output = Path(temporary) / "evidence.json"
            telemetry = Path(temporary) / "metrics.json"
            arguments = ["benchmark", "--output", str(output)]
            with mock.patch.dict(os.environ, {"TM_KENNZAHLEN": str(telemetry)}), \
                 mock.patch.object(benchmark, "run_benchmark", return_value=result), \
                 mock.patch("sys.argv", arguments), contextlib.redirect_stdout(io.StringIO()):
                benchmark.main()
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(set(json.loads(telemetry.read_text())), {"sql_reuse"})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(telemetry.stat().st_mode & 0o777, 0o600)
            for target in (output, Path(temporary) / "new-evidence.json"):
                with self.subTest(existing=target.exists()):
                    with mock.patch.dict(os.environ, {"TM_KENNZAHLEN": str(target)}), \
                         mock.patch.object(benchmark, "run_benchmark") as run, \
                         mock.patch("sys.argv", ["benchmark", "--output", str(target)]), \
                         contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            benchmark.main()
                        run.assert_not_called()
            self.assertEqual(json.loads(output.read_text()), result)
