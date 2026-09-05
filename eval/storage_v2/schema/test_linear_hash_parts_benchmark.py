"""Prevent empty work or censored baseline timings from becoming speed claims."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.storage_v2.schema import benchmark_linear_hash_parts as benchmark


class LinearHashBenchmarkTests(unittest.TestCase):
    def test_bounds_external_database_and_dirty_code_fail_closed(self):
        for value in (2, 6, True, 3.5):
            with self.assertRaises(ValueError):
                benchmark.run_benchmark(value)
        with mock.patch.dict(os.environ, {'STORAGE_V2_TEST_SOCKET': 'synthetic'}):
            with self.assertRaisesRegex(RuntimeError, 'own disposable'):
                benchmark.run_benchmark()
        with mock.patch.object(benchmark.subprocess, 'check_output', return_value=' M synthetic'):
            with self.assertRaisesRegex(RuntimeError, 'commit the benchmark'):
                benchmark.implementation_identity()

    def test_timeout_is_not_a_zero_time_and_other_errors_are_not_timeouts(self):
        case = mock.Mock()
        case.command.return_value = mock.Mock(returncode=1, stdout='',
            stderr='ERROR: 57014: canceling statement due to statement timeout')
        self.assertEqual(benchmark.measure(case, 130908, 'a' * 64),
                         {'status': 'TIMEOUT', 'timeout_ms': 30000})
        case.command.return_value.stderr = 'ERROR: permission denied'
        with self.assertRaisesRegex(RuntimeError, 'unexpected'):
            benchmark.measure(case, 130908, 'a' * 64)

    def test_missing_digest_match_duplicate_rows_or_writes_are_rejected(self):
        for rows, loops, wal in ((0, 1, 0), (2, 1, 0), (1, 0, 0), (1, 1, 1)):
            case = mock.Mock()
            case.command.return_value = mock.Mock(returncode=0, stdout=json.dumps([{
                'Execution Time': 1, 'Plan': {'Actual Rows': rows, 'Actual Loops': loops, 'WAL Records': wal},
            }]))
            with self.assertRaises(RuntimeError):
                benchmark.measure(case, 130908, 'a' * 64)

    def test_private_distinct_evidence_preserves_null_time_and_failure(self):
        result = {'status': 'FAIL', 'medians': {'130908': {'before_ms': None, 'before_timeouts': 3,
                                                        'after_ms': None, 'after_timeouts': 3}}}
        with tempfile.TemporaryDirectory(prefix='mainrag-hash-comparison-') as temporary:
            output, metrics = Path(temporary) / 'result.json', Path(temporary) / 'metrics.json'
            with mock.patch.dict(os.environ, {'TM_KENNZAHLEN': str(metrics)}), \
                    mock.patch.object(benchmark, 'run_benchmark', return_value=result), \
                    mock.patch('sys.argv', ['comparison', '--output', str(output)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    benchmark.main()
                self.assertEqual(failure.exception.code, 1)
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(json.loads(metrics.read_text()), {'hash_parts': {
                'parts_130908_before_timeouts': 3, 'parts_130908_after_timeouts': 3}})
            for path in (output, metrics):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            fresh = Path(temporary) / 'fresh.json'
            for a, b in ((output, fresh), (fresh, metrics), (fresh, fresh)):
                with mock.patch.dict(os.environ, {'TM_KENNZAHLEN': str(b)}), \
                        mock.patch.object(benchmark, 'run_benchmark') as run, \
                        mock.patch('sys.argv', ['comparison', '--output', str(a)]), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        benchmark.main()
                    run.assert_not_called()
