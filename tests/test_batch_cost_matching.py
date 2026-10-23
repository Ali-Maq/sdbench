"""Scripted pricing responses must not assign one test's cost to another."""

import unittest
from unittest.mock import Mock, patch

from openai.types.chat import ChatCompletion

from sdbench.costing.llm_batch_estimator import cost_tests_batch


class BatchCostMatchingTests(unittest.TestCase):
    def estimate(self, names, response):
        completion = ChatCompletion.model_validate({
            "id": "cost-test", "object": "chat.completion", "created": 0,
            "model": "scripted", "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": response},
            }],
        })
        llm = Mock(return_value=completion)
        results = cost_tests_batch(names, config=None, llm=llm)
        llm.assert_called_once()
        return results

    def test_exact_names_match_ignoring_case_and_outer_whitespace(self):
        results = self.estimate([" CBC ", "MRI"],
                                "Test: cbc\nCost: $35.50\n\nTest: MRI\nCost: $1,200")
        self.assertEqual(results["cbc"]["cost"], 35.50)
        self.assertEqual(results["mri"]["cost"], 1200)
        self.assertEqual(results["cbc"]["batch_test_name"], "cbc")

    def test_equivalent_requests_are_sent_once_and_remain_usable_by_the_caller(self):
        names = [" CBC ", "cbc", "MRI", "mri ", "CBC"]
        results = self.estimate(names, "Test: CBC\nCost: $35\n\nTest: MRI\nCost: $1,200")
        prompt = results["cbc"]["messages"][1]["content"]
        requested_lines = [line for line in prompt.splitlines() if line[:1].isdigit()]
        self.assertEqual(requested_lines, ["1. CBC", "2. MRI"])
        self.assertIn("following 2 medical tests", prompt)
        self.assertEqual(set(results), {"cbc", "mri"})
        for original_name in names:
            key = original_name.lower().strip()
            self.assertEqual(results[key]["cost"], 35 if key == "cbc" else 1200)

    def test_reordered_responses_keep_costs_with_the_correct_test(self):
        results = self.estimate(["CBC", "MRI"],
                                "Test: MRI\nCost: $1,200\n\nTest: CBC\nCost: $35")
        self.assertEqual(results["cbc"]["cost"], 35)
        self.assertEqual(results["mri"]["cost"], 1200)

    def test_missing_test_does_not_shift_the_remaining_price(self):
        results = self.estimate(["CBC", "MRI"], "Test: MRI\nCost: $1,200")
        self.assertIn("error", results["cbc"])
        self.assertNotIn("cost", results["cbc"])
        self.assertEqual(results["mri"]["cost"], 1200)

    def test_unrequested_or_renamed_test_is_not_a_price_for_requested_test(self):
        results = self.estimate(["CBC"], "Test: Another blood test\nCost: $35")
        self.assertEqual(set(results), {"cbc"})
        self.assertIn("error", results["cbc"])
        self.assertNotIn("cost", results["cbc"])

    def test_duplicate_name_fails_instead_of_choosing_one_price(self):
        results = self.estimate(["CBC", "MRI"],
                                "Test: CBC\nCost: $35\n\nTest: cbc\nCost: $55\n\n"
                                "Test: MRI\nCost: $1,200")
        self.assertIn("error", results["cbc"])
        self.assertNotIn("cost", results["cbc"])
        self.assertEqual(results["mri"]["cost"], 1200)

    def test_missing_price_cannot_borrow_from_adjacent_block_without_blank_line(self):
        results = self.estimate(["CBC", "MRI"],
                                "Test: CBC\nExplanation: unavailable\nTest: MRI\nCost: $1,200")
        self.assertIn("error", results["cbc"])
        self.assertNotIn("cost", results["cbc"])
        self.assertEqual(results["mri"]["cost"], 1200)

    def test_invalid_or_ambiguous_price_does_not_become_single_estimate(self):
        for value in ("$10-20", "$1,20", "unknown", "$10\nCost: $20"):
            with self.subTest(value=value):
                results = self.estimate(["CBC"], f"Test: CBC\nCost: {value}")
                self.assertIn("error", results["cbc"])
                self.assertNotIn("cost", results["cbc"])

    def test_formatted_prices_fail_the_declared_plain_amount_contract(self):
        for value in ("**$35**", "USD $35", "$35 (estimate)", "$35 USD", "$35/$55"):
            with self.subTest(value=value):
                results = self.estimate(["CBC"], f"Test: CBC\nCost: {value}")
                self.assertIn("error", results["cbc"])
                self.assertNotIn("cost", results["cbc"])

    def test_empty_request_makes_no_model_call(self):
        llm = Mock()
        self.assertEqual(cost_tests_batch([], config=None, llm=llm), {})
        llm.assert_not_called()

    def test_numeric_overflow_is_an_error_not_an_infinite_cost(self):
        results = self.estimate(["CBC"], "Test: CBC\nCost: $" + "9" * 400)
        self.assertIn("error", results["cbc"])
        self.assertNotIn("cost", results["cbc"])

    def test_provider_failure_keeps_normalized_keys_and_explicit_errors(self):
        llm = Mock(side_effect=RuntimeError("scripted unavailable"))
        with patch("builtins.print"):
            results = cost_tests_batch([" CBC ", "MRI"], config=None, llm=llm)
        self.assertEqual(set(results), {"cbc", "mri"})
        for estimate in results.values():
            self.assertIn("error", estimate)
            self.assertNotIn("cost", estimate)


if __name__ == "__main__":
    unittest.main()
