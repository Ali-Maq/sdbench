"""Clinical pricing failures must remain visible without changing diagnostic scores."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdbench.cli import costing as costing_cli, recost
from sdbench.config import CostingConfig, HarnessConfig, SDBenchConfig
from sdbench.costing import costing
from sdbench.costing.cost_types import cost_summary
from sdbench import reporting


def sd_config(**kwargs):
    return SDBenchConfig(
        max_turns=3, gatekeeper_prompt_version="v6_fixed", gatekeeper_model="fixture",
        judge_prompt_version="v3_reasoning", judge_model="fixture", **kwargs,
    )


def price(name, cost, method="llm_only"):
    return {"test_name": name, "cost": cost, "method": method,
            "cpt_estimate": None, "llm": None, "explanation": "Scripted price"}


def legacy_analysis(details):
    # Previous files silently treated failed prices as zero in these totals.
    total = sum(detail["cost"] or 0 for detail in details)
    return {"method": "llm_only", "total_unique_tests": len(details), "cost_details": details,
            "totals": {"tests_only": total, "clinician_visits": 300,
                       "total_with_visits": total + 300}}


def diagnosed(analysis=None, case_id="fixture"):
    result = {"case_id": case_id, "model_diagnosis": "Inherited error of metabolism",
              "actual_diagnosis": "Inherited error of metabolism", "diagnosis_score": 5,
              "correct": True, "check_diagnosis_output": "<score>5</score>",
              "case_set": "train", "requested_tests_only": ["CBC", "MRI"],
              "num_test_rounds": 1}
    if analysis is not None:
        result["cost_analysis_llm_only"] = analysis
    return result


class CostEstimationTests(unittest.TestCase):
    def config(self, method="llm_only", **kwargs):
        return CostingConfig.from_sdbench_config(sd_config(costing_method=method, **kwargs))

    def test_unique_names_deduplicate_across_all_pricing_modes(self):
        for method in ("llm_only", "batch_llm_only", "unified_cpt_llm"):
            with self.subTest(method=method):
                matcher = Mock()
                matcher.cost_test.side_effect = lambda name: {"cost": 10, "cpt_codes": ["fixture"]}
                with (patch.object(costing.llm_estimater, "cost_test", return_value={"cost": 10}) as llm,
                      patch.object(costing.llm_batch_estimator, "cost_tests_batch",
                                   return_value={"cbc": {"cost": 10}, "mri": {"cost": 10}}) as batch,
                      patch.object(costing, "get_cpt_fuzzy_matcher", return_value=matcher)):
                    result = costing.estimate_costs([" CBC ", "cbc", "MRI", "mri "], 1,
                                                    self.config(method, costing_parallelism=1))
                self.assertEqual(result["total_requests"], 4)
                self.assertEqual(result["total_unique_tests"], 2)
                self.assertEqual(result["totals"]["total_with_visits"], 320)
                self.assertEqual(result["pricing_status"], "complete")
                calls = batch.call_args.args[0] if method == "batch_llm_only" else [
                    call.args[0] for call in (matcher.cost_test if method == "unified_cpt_llm" else llm).call_args_list]
                self.assertEqual(calls, ["CBC", "MRI"])

    def test_partial_and_all_failed_are_not_complete_totals(self):
        for values, status, subtotal, unpriced in (
            ({"cbc": {"cost": 25}, "mri": {"error": "missing"}}, "partial", 25, 1),
            ({"cbc": {"error": "missing"}, "mri": {"error": "missing"}}, "unknown", 0, 2),
        ):
            with self.subTest(status=status), patch.object(costing.llm_batch_estimator, "cost_tests_batch", return_value=values):
                result = costing.estimate_costs(["CBC", "MRI"], 2, self.config("batch_llm_only"))
            self.assertEqual(result["pricing_status"], status)
            self.assertEqual(result["unpriced_test_count"], unpriced)
            for field in ("tests_only", "total_with_visits", "average_per_test"):
                self.assertIsNone(result["totals"][field])
            self.assertEqual(result["totals"]["known_tests_subtotal"], subtotal)
            self.assertEqual(result["totals"]["known_total_with_visits"], subtotal + 600)
            json.dumps(result, allow_nan=False)

    def test_zero_costs_and_zero_tests_are_complete(self):
        with patch.object(costing.llm_estimater, "cost_test", return_value={"cost": 0}) as pricer:
            zero = costing.estimate_costs(["CBC"], 0, self.config())
            none = costing.estimate_costs([], 0, self.config())
        pricer.assert_called_once()
        for result in (zero, none):
            self.assertEqual(result["pricing_status"], "complete")
            self.assertEqual(result["totals"]["total_with_visits"], 0)
        self.assertEqual(none["method"], "no_tests")

    def test_invalid_single_and_batch_prices_are_failures_and_not_cached(self):
        for value in (-1, float("nan"), float("inf"), True, "25", None):
            for method in ("llm_only", "batch_llm_only"):
                with (self.subTest(value=value, method=method),
                      patch.object(costing.llm_estimater, "cost_test", return_value={"cost": value}),
                      patch.object(costing.llm_batch_estimator, "cost_tests_batch", return_value={"cbc": {"cost": value}}),
                      patch.object(costing.cost_cache, "set_cached_cost") as cache):
                    result = costing.estimate_costs(["CBC"], 1, self.config(method))
                self.assertEqual(result["cost_details"][0]["method"], "failure")
                self.assertIsNone(result["totals"]["total_with_visits"])
                cache.assert_not_called()
                json.dumps(result, allow_nan=False)

    def test_invalid_cpt_price_can_fall_back_to_valid_llm_price(self):
        matcher = Mock()
        matcher.cost_test.return_value = {"cost": -100}
        with (patch.object(costing, "get_cpt_fuzzy_matcher", return_value=matcher),
              patch.object(costing.llm_estimater, "cost_test", return_value={"cost": 40})):
            result = costing.estimate_costs(["CBC"], 1, self.config("unified_cpt_llm"))
        self.assertEqual(result["cost_details"][0]["method"], "llm_fallback")
        self.assertEqual(result["totals"]["tests_only"], 40)

    def test_parallel_estimation_retains_partial_status(self):
        with patch.object(costing.llm_estimater, "cost_test", side_effect=lambda name, config: {"cost": 10} if name == "CBC" else {"error": "missing"}):
            result = costing.estimate_costs(["CBC", "MRI"], 1, self.config(costing_parallelism=2))
        self.assertEqual(result["pricing_status"], "partial")
        self.assertIsNone(result["totals"]["total_with_visits"])

    def test_invalid_visit_inputs_are_rejected_before_pricing(self):
        with patch.object(costing.llm_estimater, "cost_test") as pricer:
            for rounds in (-1, True, 1.5):
                with self.assertRaises(ValueError):
                    costing.estimate_costs(["CBC"], rounds, self.config())
            with self.assertRaises(ValueError):
                costing.estimate_costs(["CBC"], 1, self.config(clinician_cost=-1))
        pricer.assert_not_called()

    def test_matcher_reuses_identity_not_unhashable_config_and_reloads_changed_source(self):
        config = self.config("unified_cpt_llm", costing_model_args={"extra_body": {"provider": {"order": ["fixture"]}}})
        with (patch.dict(costing._cpt_matchers, {}, clear=True),
              patch.object(costing.cost_cache, "pricing_identity", side_effect=["a", "a", "b"]),
              patch.object(costing, "_build_cpt_fuzzy_matcher", side_effect=["first", "changed"]) as build):
            self.assertEqual(costing.get_cpt_fuzzy_matcher(config), "first")
            self.assertEqual(costing.get_cpt_fuzzy_matcher(config), "first")
            self.assertEqual(costing.get_cpt_fuzzy_matcher(config), "changed")
            self.assertEqual(build.call_count, 2)

    def test_missing_matcher_source_returns_none_for_preflight(self):
        with (patch.object(costing.cost_cache, "pricing_identity", side_effect=FileNotFoundError("fixture.xlsx")),
              redirect_stdout(io.StringIO()) as output):
            self.assertIsNone(costing.get_cpt_fuzzy_matcher(self.config("unified_cpt_llm")))
        self.assertIn("Could not identify CPT pricing inputs", output.getvalue())


class CostReportingTests(unittest.TestCase):
    def setUp(self):
        self.partial = legacy_analysis([price("CBC", 25), price("MRI", None, "failure")])
        self.complete = legacy_analysis([price("CBC", 25)])
        self.failed = legacy_analysis([price("MRI", None, "failure")])

    def test_legacy_partial_and_missing_details_do_not_supply_pareto_cost(self):
        self.assertIsNone(reporting.case_cost(diagnosed(self.partial), "llm_only"))
        self.assertIsNone(reporting.case_cost(diagnosed({"totals": {"total_with_visits": 5}}), "llm_only"))
        self.assertEqual(reporting.case_cost(diagnosed(self.complete), "llm_only"), 325)
        self.assertEqual(cost_summary(self.failed)["pricing_status"], "unknown")

    def test_invalid_and_missing_legacy_details_do_not_become_complete(self):
        for bad in (-1, float("inf"), True):
            analysis = legacy_analysis([price("CBC", 25)])
            analysis["cost_details"][0]["cost"] = bad
            self.assertIsNone(cost_summary(analysis)["totals"]["total_with_visits"])
        incomplete = {**self.complete, "total_unique_tests": 2}
        self.assertEqual(cost_summary(incomplete)["unpriced_test_count"], 1)

    def test_pricing_failure_does_not_remove_correct_diagnosis_from_accuracy(self):
        summary = reporting._summarize_cases([diagnosed(self.partial), diagnosed(self.complete, "two")], ["llm_only"], True)
        self.assertEqual(summary["accuracy"], 1)
        self.assertEqual(summary["valid_cases"], 2)
        self.assertEqual(summary["errored"], 0)
        stats = summary["costs"]["llm_only"]
        self.assertIsNone(stats["mean"])
        self.assertIsNone(stats["total"])
        self.assertEqual(stats["complete_case_total"], 325)
        self.assertEqual(stats["partial_cases"], 1)
        self.assertNotIn("ci", stats)
        self.assertFalse(reporting.case_is_errored(diagnosed(self.partial)))

    def test_missing_or_invalid_expected_coverage_never_becomes_complete(self):
        for count in (None, -1, True, "1", 1.0):
            with self.subTest(count=count):
                analysis = {**self.complete, "total_unique_tests": count}
                summary = cost_summary(analysis)
                self.assertEqual(summary["pricing_status"], "partial")
                self.assertIsNone(summary["unpriced_test_count"])
                self.assertIsNone(summary["totals"]["total_with_visits"])
                self.assertEqual(summary["totals"]["known_tests_subtotal"], 25)
                self.assertIsNone(reporting.case_cost(diagnosed(analysis), "llm_only"))
        missing_count = {key: value for key, value in self.complete.items()
                         if key != "total_unique_tests"}
        self.assertIsNone(cost_summary(missing_count)["totals"]["total_with_visits"])

    def test_html_and_console_show_unknown_totals_and_explicit_subtotals(self):
        config = HarnessConfig(sdbench_config=sd_config(), dataset="synthetic")
        result = diagnosed(self.partial)
        result["cost_analysis_actual_workup_llm_only"] = self.failed
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            path = reporting.generate_html_overview([result], directory, config)
            reporting.generate_cost_analysis([result], config)
            html = Path(path).read_text()
        self.assertIn("Aggregate total cost: Unknown", output.getvalue())
        self.assertIn("known subtotal with visits $325.00; 1 unpriced tests", html)
        self.assertIn("<strong>Total Cost:</strong> Unknown", html)
        self.assertIn("<strong>Known test subtotal:</strong> $25.00", html)
        self.assertIn("<strong>⚠️ MRI</strong>: Unknown", html)
        self.assertNotIn("<strong>⚠️ MRI</strong>: $0", html)

    def test_incomplete_diagnosis_is_excluded_consistently_without_hiding_price_failure(self):
        config = HarnessConfig(sdbench_config=sd_config(), dataset="synthetic")
        failed = diagnosed(self.complete, "failed")
        failed.update(result_status="failed", model_diagnosis="", error_message="Provider timeout")
        rows = [diagnosed(self.partial), failed]
        with tempfile.TemporaryDirectory() as directory:
            html = Path(reporting.generate_html_overview(rows, directory, config)).read_text()
            text = Path(reporting.generate_summary_file(rows, directory, config)).read_text()
        self.assertIn("1/1 completed cases; 1 incomplete", text)
        self.assertIn("1 incomplete diagnoses", html)
        self.assertIn("Provider timeout", html)
        self.assertIn('class="case-row incomplete"', html)
        self.assertIn("⚠ Incomplete", html)
        self.assertEqual(reporting._summarize_cases(rows, ["llm_only"], False)["accuracy"], 1)

    def test_all_report_surfaces_use_completed_diagnoses_for_aggregate_costs(self):
        config = HarnessConfig(sdbench_config=sd_config(), dataset="synthetic")
        failed = diagnosed(legacy_analysis([price("Failed-case test", 9000)]), "failed")
        failed.update(result_status="failed", model_diagnosis="", error_message="Provider timeout")
        for completed in ([diagnosed(self.complete)],
                          [diagnosed(self.complete), diagnosed(self.partial, "partial"), diagnosed(case_id="missing")]):
            with self.subTest(completed=len(completed)):
                rows = completed + [failed]
                with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
                    html = Path(reporting.generate_html_overview(rows, directory, config)).read_text()
                    reporting.generate_cost_analysis(rows, config)
                summary = reporting._summarize_cases(rows, ["llm_only"], False)
                stats = summary["costs"]["llm_only"]
                self.assertEqual(summary["valid_cases"], len(completed))
                self.assertEqual(stats["cases"], len(completed))
                self.assertEqual(stats["n"], 1)
                self.assertEqual(stats["complete_case_total"], 325)
                self.assertIn(f"{len(completed)} completed diagnoses; 1 incomplete diagnoses excluded", output.getvalue())
                self.assertIn(f"aggregate clinical costs use {len(completed)} completed diagnoses", html)
                headline = html.split("<h2>Case Results</h2>")[0]
                self.assertIn(f"Complete cases: 1/{len(completed)}", headline)
                self.assertIn("Complete-case subtotal: $325.00", headline)
                self.assertNotIn("$9,300.00", headline)
                self.assertNotIn("$9,300.00", output.getvalue())
                if len(completed) == 1:
                    self.assertEqual(stats["total"], 325)
                    self.assertIn("Aggregate total cost: $325.00", output.getvalue())
                else:
                    self.assertIsNone(stats["total"])
                    self.assertEqual((stats["partial_cases"], stats["unknown_cases"]), (1, 1))
                    self.assertIn("Complete cases: 1/3; partial: 1; unknown: 1", output.getvalue())
                    self.assertIn("Aggregate total cost: Unknown", output.getvalue())

    def test_saved_json_report_preserves_accuracy_and_cost_coverage(self):
        config = HarnessConfig(sdbench_config=sd_config(), dataset="synthetic")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "json"
            folder.mkdir()
            (folder / "case_k1_fixture.json").write_text(json.dumps(diagnosed(self.partial)))
            with patch.object(HarnessConfig, "restore_run_config", return_value=config):
                report = reporting.build_accuracy_cost_report([directory], compute_ci=True)
        summary = report["groups"][0]["case_sets"]["train"]
        self.assertEqual(summary["accuracy"], 1)
        self.assertIsNone(summary["costs"]["llm_only"]["total"])
        self.assertIn("complete 0/1; partial 1; unknown 0", reporting.format_accuracy_cost_report(report))
        json.dumps(report, allow_nan=False)

    def test_costing_cli_handles_partial_without_formatting_none_as_number(self):
        with (patch.object(costing_cli.dotenv, "load_dotenv"),
              patch.object(SDBenchConfig, "from_command_line", return_value=sd_config(costing_method="llm_only")),
              patch.object(costing_cli, "estimate_costs", return_value={**self.partial, "total_requests": 2, "num_rounds": 1}),
              patch("sys.argv", ["costing", "CBC", "MRI"]),
              redirect_stdout(io.StringIO()) as output,
              self.assertRaises(SystemExit) as exit_):
            costing_cli.main()
        self.assertEqual(exit_.exception.code, 0)
        self.assertIn("Pricing status: partial", output.getvalue())
        self.assertIn("Total with visits: Unknown", output.getvalue())
        self.assertIn("Known test subtotal: $25.00", output.getvalue())

    def test_recost_retries_legacy_partial_and_keeps_complete_results(self):
        config = CostingConfig.from_sdbench_config(sd_config(costing_method="llm_only", costing_parallelism=1))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case_k1_fixture.json"
            path.write_text(json.dumps(diagnosed(self.partial)))
            with patch.object(costing.llm_estimater, "cost_test", return_value={"cost": 10}) as pricer:
                counts = recost.recost_case(str(path), config, force=False, analyze_only=False)
                self.assertEqual(pricer.call_count, 2)
                self.assertEqual(counts, {"grounded": 0, "llm": 2, "failed": 0,
                                         "cases": 1, "complete_cases": 1, "partial_cases": 0,
                                         "unknown_cases": 0, "unknown_coverage_cases": 0})
                result = json.loads(path.read_text())
                self.assertEqual(result["cost_analysis_llm_only"]["pricing_status"], "complete")
                recost.recost_case(str(path), config, force=False, analyze_only=False)
                self.assertEqual(pricer.call_count, 2)


    def test_recost_analysis_reports_missing_and_unreadable_cases_in_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "json"
            folder.mkdir()
            (folder / "case_k1_complete.json").write_text(json.dumps(diagnosed(self.complete)))
            (folder / "case_k1_missing.json").write_text(json.dumps(diagnosed()))
            (folder / "case_k1_broken.json").write_text("{")
            with (patch.object(recost.dotenv, "load_dotenv"),
                  patch.object(SDBenchConfig, "from_command_line", return_value=sd_config(costing_method="llm_only")),
                  patch("sys.argv", ["recost", directory, "--costing-method", "llm_only", "--analyze-only"]),
                  patch.object(recost, "compute_agent_costs") as compute,
                  redirect_stdout(io.StringIO()) as output):
                recost.main()
        compute.assert_not_called()
        self.assertIn("Unknown (not computed)", output.getvalue())
        self.assertIn("Cases: 3; complete: 1; partial: 0; unknown: 2", output.getvalue())
        self.assertIn("Test coverage unknown for 2 cases", output.getvalue())


if __name__ == "__main__":
    unittest.main()
