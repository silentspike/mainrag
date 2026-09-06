"""Benchmark boundaries reject external databases and uncommitted work."""
import os
import unittest
from unittest import mock

from eval.storage_v2.schema import benchmark_empty_search_branches as benchmark


class EmptySearchBranchBenchmarkTests(unittest.TestCase):
    def test_invalid_bounds_never_start_a_fixture(self):
        for repetitions, views in ((2, 96), (21, 96), (3, 23), (3, 513), (True, 96), (3, 96.5)):
            with self.subTest(repetitions=repetitions, views=views), \
                    mock.patch.object(benchmark.search.EmptySearchBranchTests, 'setUpClass') as setup:
                with self.assertRaises(ValueError):
                    benchmark.run_benchmark(repetitions, views)
                setup.assert_not_called()

    def test_external_database_is_rejected(self):
        with mock.patch.dict(os.environ, {'STORAGE_V2_TEST_SOCKET': 'synthetic'}):
            with self.assertRaisesRegex(RuntimeError, 'own disposable'):
                benchmark.run_benchmark()

    def test_dirty_implementation_is_not_measurement_evidence(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(benchmark.subprocess, 'check_output', return_value=' M synthetic'):
            with self.assertRaisesRegex(RuntimeError, 'commit the benchmark'):
                benchmark.run_benchmark()
