"""Fail-closed workload and evidence contracts for the SQL card comparison."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.storage_v2.schema import benchmark_structural_card_reuse as benchmark


class StructuralCardBenchmarkTests(unittest.TestCase):
    def test_invalid_bounds_fail_before_database_setup(self):
        for repeats, calls in ((2, 500), (21, 500), (3, 0), (3, 5001), (True, 500), (3, 1.5)):
            with self.subTest(repeats=repeats, calls=calls), \
                    mock.patch.object(benchmark, 'implementation_identity') as identity:
                with self.assertRaises(ValueError):
                    benchmark.run_benchmark(repeats, calls)
                identity.assert_not_called()

    def test_external_database_and_dirty_implementation_are_rejected(self):
        with mock.patch.dict(os.environ, {'STORAGE_V2_TEST_SOCKET': 'synthetic-socket'}):
            with self.assertRaisesRegex(RuntimeError, 'own disposable'):
                benchmark.run_benchmark()
        with mock.patch.object(benchmark.subprocess, 'check_output', return_value=' M synthetic'):
            with self.assertRaisesRegex(RuntimeError, 'commit the benchmark'):
                benchmark.implementation_identity()

    def test_zero_or_partial_work_cannot_report_a_timing(self):
        for loops, rows in ((0, 0), (499, 1), (500, 0)):
            with self.subTest(loops=loops, rows=rows):
                case = mock.Mock()
                case.bulk_query.return_value = 'SELECT 1;'
                case.sql.return_value = json.dumps([{'Execution Time': 0.01, 'Plan': {
                    'Node Type': 'Aggregate', 'Plans': [{'Node Type': 'Function Scan',
                                                        'Actual Loops': loops, 'Actual Rows': rows}],
                }}])
                with self.assertRaisesRegex(RuntimeError, 'declared nonempty'):
                    benchmark.measure(case, {}, 500, cold=False)

    def test_evidence_and_telemetry_are_private_distinct_and_not_overwritten(self):
        result = {'status': 'PASS', 'semantic_rows_unchanged': True,
                  'medians': {'warm': {'before_ms': 10, 'after_ms': 5, 'speedup_ratio': 2}}}
        with tempfile.TemporaryDirectory(prefix='mainrag-card-comparison-') as temporary:
            output, metrics = Path(temporary) / 'evidence.json', Path(temporary) / 'metrics.json'
            with mock.patch.dict(os.environ, {'TM_KENNZAHLEN': str(metrics)}), \
                    mock.patch.object(benchmark, 'run_benchmark', return_value=result), \
                    mock.patch('sys.argv', ['comparison', '--output', str(output)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                benchmark.main()
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(set(json.loads(metrics.read_text())), {'card_reuse'})
            self.assertNotIn('warm_speedup_ratio', json.loads(metrics.read_text())['card_reuse'])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(metrics.stat().st_mode & 0o777, 0o600)
            fresh = Path(temporary) / 'fresh.json'
            for result_path, metric_path in ((output, fresh), (fresh, metrics), (fresh, fresh)):
                with self.subTest(result_path=result_path.name, metric_path=metric_path.name), \
                        mock.patch.dict(os.environ, {'TM_KENNZAHLEN': str(metric_path)}), \
                        mock.patch.object(benchmark, 'run_benchmark') as run, \
                        mock.patch('sys.argv', ['comparison', '--output', str(result_path)]), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        benchmark.main()
                    run.assert_not_called()
