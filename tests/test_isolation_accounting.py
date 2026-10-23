"""Accounting edge cases using fictional prices and provider-shaped usage fixtures."""

from types import SimpleNamespace
import unittest

from sdbench.isolation.accounting import RateCard, normalize_usage, summarize_usage, usage_entry


class AccountingTests(unittest.TestCase):
    def card(self):
        return RateCard("2026-10-22", 2, 8, .5, "fictional-test-rate-card")

    def entry(self, **kwargs):
        values = dict(role="gatekeeper", model="test-model", attempt=1, api_mode="live",
                      rate_card=self.card(), usage={"prompt_tokens": 1000, "completion_tokens": 200,
                                                   "prompt_tokens_details": {"cached_tokens": 800},
                                                   "completion_tokens_details": {"reasoning_tokens": 150}})
        values.update(kwargs)
        return usage_entry(**values)

    def test_cached_and_reasoning_tokens_are_subsets_not_extra_billed_tokens(self):
        entry = self.entry()
        self.assertEqual(entry["usage"]["total_tokens"], 1200)
        self.assertEqual(entry["usage"]["reasoning_tokens"], 150)
        # (200 ordinary input * 2 + 800 cached input * .5 + 200 output * 8)/1e6
        self.assertAlmostEqual(entry["estimated_cost_usd"], .0024)
        self.assertIsNone(entry["actual_paid_usd"])

    def test_supports_responses_usage_objects_and_zero_is_distinct_from_missing(self):
        result = normalize_usage(SimpleNamespace(input_tokens=10, output_tokens=0,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
            output_tokens_details=SimpleNamespace(reasoning_tokens=0)))
        self.assertEqual(result["total_tokens"], 10)
        self.assertEqual(result["output_tokens"], 0)
        missing = normalize_usage(None)
        self.assertIsNone(missing["total_tokens"])
        self.assertIsNone(missing["output_tokens"])
        self.assertFalse(missing["usage_available"])

    def test_unknown_cached_usage_gives_bounds_not_a_guessed_exact_bill(self):
        entry = self.entry(usage={"input_tokens": 10, "output_tokens": 4})
        self.assertIsNone(entry["estimated_cost_usd"])
        self.assertEqual(entry["cost_bounds_usd"], [37 / 1_000_000, 52 / 1_000_000])
        no_discount = self.entry(usage={"input_tokens": 10, "output_tokens": 4},
                                 rate_card=RateCard("2026-10-22", 2, 8))
        self.assertAlmostEqual(no_discount["estimated_cost_usd"], 52 / 1_000_000)

    def test_missing_retry_usage_does_not_disappear_from_ledger(self):
        failed = self.entry(call_id="attempt-1", attempt=1, usage=None, status="failed")
        success = self.entry(call_id="attempt-2", attempt=2)
        summary = summarize_usage([failed, success])
        self.assertEqual(summary["calls"], 2)
        self.assertIsNone(summary["total_tokens"])
        self.assertEqual(summary["known_total_tokens"], 1200)
        self.assertEqual(summary["unknown_total_tokens_calls"], 1)
        self.assertIsNone(summary["estimated_cost_usd"])
        self.assertAlmostEqual(summary["known_estimated_cost_usd"], .0024)
        self.assertEqual(summary["unknown_live_cost_calls"], 1)
        self.assertIsNone(summary["actual_paid_usd"])

    def test_mock_simulation_is_not_actual_or_estimated_live_spend(self):
        mock = self.entry(api_mode="mock", call_id="mock-call")
        live = self.entry(role="judge", call_id="live-call")
        self.assertEqual(mock["cost_kind"], "simulation")
        self.assertIsNone(mock["estimated_cost_usd"])
        self.assertAlmostEqual(mock["simulated_cost_usd"], .0024)
        summary = summarize_usage([mock, live])
        self.assertAlmostEqual(summary["estimated_cost_usd"], .0024)
        self.assertAlmostEqual(summary["simulated_cost_usd"], .0024)
        self.assertEqual(summary["by_role"]["judge"]["live_calls"], 1)
        self.assertEqual(summary["by_role"]["gatekeeper"]["mock_calls"], 1)

    def test_inconsistent_provider_subcounts_are_not_trusted(self):
        entry = self.entry(usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 100,
                                  "input_tokens_details": {"cached_tokens": 20},
                                  "output_tokens_details": {"reasoning_tokens": 5}})
        self.assertEqual(entry["usage"]["total_tokens"], 14)
        self.assertIsNone(entry["usage"]["cached_input_tokens"])
        self.assertIsNone(entry["usage"]["reasoning_tokens"])
        self.assertEqual(len(entry["usage"]["warnings"]), 3)
        self.assertIsNone(entry["estimated_cost_usd"])

    def test_total_only_usage_cannot_estimate_input_output_charges(self):
        entry = self.entry(usage={"total_tokens": 1000})
        self.assertEqual(entry["usage"]["total_tokens"], 1000)
        self.assertIsNone(entry["estimated_cost_usd"])
        self.assertIsNone(entry["cost_bounds_usd"])

    def test_no_rate_card_does_not_invent_model_prices(self):
        entry = self.entry(rate_card=None)
        self.assertIsNone(entry["estimated_cost_usd"])
        self.assertEqual(entry["usage"]["total_tokens"], 1200)

    def test_invalid_prices_and_duplicate_calls_fail_loudly(self):
        for args in (("not-a-date", 2, 8), ("2026-10-22", -1, 8),
                     ("2026-10-22", float("nan"), 8), ("2026-10-22", True, 8)):
            with self.subTest(args=args), self.assertRaises((ValueError, TypeError)):
                RateCard(*args)
        entry = self.entry(call_id="same-call")
        with self.assertRaises(ValueError):
            summarize_usage([entry, entry])
        with self.assertRaises(ValueError):
            self.entry(attempt=0)


if __name__ == "__main__":
    unittest.main()
