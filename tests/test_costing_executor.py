"""Costing workers are reused until shutdown, then recreated for later work."""

import unittest
from unittest.mock import patch

from sdbench.costing import costing


class CostingExecutorTests(unittest.TestCase):
    def test_shutdown_discards_cached_executor_before_next_request(self):
        with patch.object(costing, "_executor_cache", {}):
            try:
                first = costing._get_executor(False, 1)
                self.assertIs(costing._get_executor(False, 1), first)
                self.assertEqual(first.submit(lambda: "first").result(timeout=2), "first")

                costing._shutdown_all_executors()
                self.assertEqual(costing._executor_cache, {})

                second = costing._get_executor(False, 1)
                self.assertIsNot(second, first)
                self.assertEqual(second.submit(lambda: "second").result(timeout=2), "second")
            finally:
                costing._shutdown_all_executors()


if __name__ == "__main__":
    unittest.main()
