"""Costing workers are reused until shutdown, then recreated for later work."""

import unittest
from unittest.mock import patch

from sdbench.costing import costing


class CostingExecutorTests(unittest.TestCase):
    def test_transient_matcher_failure_is_retried_then_success_is_reused(self):
        matcher = object()
        config = object()  # No configuration is read by the mocked identity/build.
        with (patch.object(costing, "_cpt_matchers", {}),
              patch.object(costing.cost_cache, "pricing_identity", return_value="fixed"),
              patch.object(costing, "_build_cpt_fuzzy_matcher", side_effect=[None, matcher]) as build):
            self.assertIsNone(costing.get_cpt_fuzzy_matcher(config))
            self.assertIs(costing.get_cpt_fuzzy_matcher(config), matcher)
            self.assertIs(costing.get_cpt_fuzzy_matcher(config), matcher)
            self.assertEqual(build.call_count, 2)

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
