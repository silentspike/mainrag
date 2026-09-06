"""Reject partial/empty work and unsafe benchmark targets before measuring."""
import json
import os
import unittest
from unittest import mock

from eval.storage_v2.schema import benchmark_intelligence_commands as benchmark


class IntelligenceCommandBenchmarkTests(unittest.TestCase):
    def test_bounds_and_external_database_fail_before_setup(self):
        for repetitions, cards in ((2, 32), (11, 32), (3, 0), (3, 5001), (True, 32), (3, 32.5)):
            with mock.patch.object(benchmark, 'implementation_identity') as identity:
                with self.assertRaises(ValueError):
                    benchmark.run_benchmark(repetitions, cards)
                identity.assert_not_called()
        with mock.patch.dict(os.environ, {'STORAGE_V2_TEST_SOCKET': 'synthetic'}):
            with self.assertRaisesRegex(RuntimeError, 'own disposable'):
                benchmark.run_benchmark()

    def test_dirty_implementation_is_not_performance_evidence(self):
        with mock.patch.object(benchmark.subprocess, 'check_output', return_value=' M synthetic'):
            with self.assertRaisesRegex(RuntimeError, 'commit the benchmark'):
                benchmark.implementation_identity()

    def test_partial_empty_or_unhashed_result_is_not_timing_evidence(self):
        for result in ({'record_count': 0, 'result_sha256': 'a' * 64},
                       {'record_count': 2, 'result_sha256': 'a' * 64},
                       {'record_count': 1, 'result_sha256': None},
                       {'record_count': 1, 'result_sha256': 'z' * 64}):
            case = mock.Mock()
            case.sql.return_value = json.dumps([{'Execution Time': 1}]) + '\n' + json.dumps(result)
            with self.assertRaisesRegex(RuntimeError, 'complete nonempty'):
                benchmark.measure(case, 1, 'card', {}, False, 1)
