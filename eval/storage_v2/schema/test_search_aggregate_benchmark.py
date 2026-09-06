"""The search optimization comparison cannot accept empty or partial work."""
import json
import os
import unittest
from unittest import mock

from eval.storage_v2.schema import benchmark_search_aggregates as benchmark


class SearchAggregateBenchmarkTests(unittest.TestCase):
    def test_bounds_and_external_database_are_rejected_before_setup(self):
        for repetitions, views in ((2, 96), (21, 96), (3, 0), (3, 513), (True, 96), (3, 96.5)):
            with self.subTest(repetitions=repetitions, views=views), \
                    mock.patch.object(benchmark, 'implementation_identity') as identity:
                with self.assertRaises(ValueError):
                    benchmark.run_benchmark(repetitions, views)
                identity.assert_not_called()
        with mock.patch.dict(os.environ, {'STORAGE_V2_TEST_SOCKET': 'synthetic'}):
            with self.assertRaisesRegex(RuntimeError, 'own disposable'):
                benchmark.run_benchmark()

    def test_dirty_implementation_cannot_be_benchmark_evidence(self):
        with mock.patch.object(benchmark.subprocess, 'check_output', return_value=' M synthetic'):
            with self.assertRaisesRegex(RuntimeError, 'commit the benchmark'):
                benchmark.implementation_identity()

    def test_partial_work_cannot_report_performance(self):
        definition = '    WITH RECURSIVE synthetic AS (SELECT 1) SELECT 1 INTO v_result;'
        for result in ({}, {'fully_scored_views': 96, 'total': 95, 'results': [None] * 10},
                       {'fully_scored_views': 96, 'total': 96, 'results': []}):
            case = mock.Mock()
            case.sql.return_value = json.dumps([{'Execution Time': 1, 'Planning Time': 1}]) + '\n' + json.dumps(result)
            with self.subTest(result=result), self.assertRaisesRegex(RuntimeError, 'omitted'):
                benchmark.measure(case, definition, {'type': 'term', 'value': 'alpha'}, 96)
