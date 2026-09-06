"""Fail-closed inputs, complete work, and private comparison evidence."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.storage_v2.schema import benchmark_intelligence_export as benchmark


class IntelligenceExportBenchmarkTests(unittest.TestCase):
    def test_invalid_bounds_and_external_database_fail_before_setup(self):
        for repetitions, cards in ((2, 5000), (21, 5000), (3, 0), (3, 20001), (True, 1), (3, 1.5)):
            with self.subTest(repetitions=repetitions, cards=cards), \
                    mock.patch.object(benchmark, 'implementation_identity') as identity:
                with self.assertRaises(ValueError):
                    benchmark.run_benchmark(repetitions, cards)
                identity.assert_not_called()
        with mock.patch.dict(os.environ, {'STORAGE_V2_TEST_SOCKET': 'synthetic'}):
            with self.assertRaisesRegex(RuntimeError, 'own disposable'):
                benchmark.run_benchmark()

    def test_dirty_implementation_is_not_evidence(self):
        with mock.patch.object(benchmark.subprocess, 'check_output', return_value=' M synthetic'):
            with self.assertRaisesRegex(RuntimeError, 'commit the benchmark'):
                benchmark.implementation_identity()

    def test_zero_partial_or_missing_digest_cannot_report_timing(self):
        complete = {'cards': 5, 'payload_sha256': 'a' * 64, 'protected_payload_sha256': 'b' * 64}
        for result in ({**complete, 'cards': 0}, {**complete, 'cards': 4},
                       {**complete, 'payload_sha256': None}, {**complete, 'protected_payload_sha256': 'z' * 64}):
            case = mock.Mock()
            case.sql.return_value = json.dumps(result)
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                benchmark.measure(case, 1, 'public', False, 5)

    def test_outputs_are_private_distinct_and_not_overwritten(self):
        result = {'status': 'PASS', 'medians': {'public': {'before_client_ms': 20, 'after_client_ms': 10}}}
        with tempfile.TemporaryDirectory(prefix='mainrag-export-benchmark-') as temporary:
            output, metrics = Path(temporary) / 'evidence.json', Path(temporary) / 'metrics.json'
            with mock.patch.dict(os.environ, {'TM_KENNZAHLEN': str(metrics)}), \
                    mock.patch.object(benchmark, 'run_benchmark', return_value=result), \
                    mock.patch('sys.argv', ['comparison', '--output', str(output)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                benchmark.main()
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(set(json.loads(metrics.read_text())), {'intelligence_export'})
            for path in (output, metrics):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            fresh = Path(temporary) / 'fresh.json'
            for result_path, metric_path in ((output, fresh), (fresh, metrics), (fresh, fresh)):
                with mock.patch.dict(os.environ, {'TM_KENNZAHLEN': str(metric_path)}), \
                        mock.patch.object(benchmark, 'run_benchmark') as run, \
                        mock.patch('sys.argv', ['comparison', '--output', str(result_path)]), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        benchmark.main()
                    run.assert_not_called()
