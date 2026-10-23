"""Resume regressions using fabricated records and mocked model execution only."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdbench.config import HarnessConfig, SDBenchConfig
from sdbench.harness import harness
from sdbench.results import is_completed_result


def completed_result(**overrides):
    return {
        "result_status": "success",
        "correct": True,
        "model_diagnosis": "Fictional diagnosis",
        "diagnosis_score": 5,
        "check_diagnosis_output": "5",
        **overrides,
    }


def configuration(*, repetitions=1, compute_costs=False):
    return HarnessConfig(
        dataset="fictional", model="fixture", k=repetitions, max_workers=1,
        sdbench_config=SDBenchConfig(
            max_turns=3, compute_costs=compute_costs,
            gatekeeper_prompt_version="v6_fixed", gatekeeper_model="fixture",
            judge_prompt_version="v3_reasoning", judge_model="fixture",
        ),
    )


def fictional_case(case_id):
    return {
        "case_id": case_id, "dataset": "fictional",
        "case_summary": "A fabricated diagnostic presentation.",
        "full_text": "Fabricated evidence.", "final_diagnosis": "Fictional diagnosis",
    }


class CompletionTests(unittest.TestCase):
    def test_diagnosis_text_does_not_control_completion(self):
        for diagnosis in ("Inborn error of metabolism", "Exceptional presentation of a fictional condition"):
            with self.subTest(diagnosis=diagnosis):
                self.assertTrue(is_completed_result(completed_result(model_diagnosis=diagnosis)))

    def test_legacy_success_requires_judge_evidence(self):
        legacy = completed_result()
        del legacy["result_status"]
        self.assertTrue(is_completed_result(legacy))
        for field in ("diagnosis_score", "check_diagnosis_output", "correct", "model_diagnosis"):
            incomplete = {key: value for key, value in legacy.items() if key != field}
            with self.subTest(field=field):
                self.assertFalse(is_completed_result(incomplete))
        self.assertFalse(is_completed_result({"correct": False, "model_diagnosis": "Timed out"}))

    def test_failed_or_malformed_records_are_not_complete(self):
        overrides = (
            {"result_status": "failed"}, {"result_status": "pending"}, {"result_status": None},
            {"diagnosis_score": True}, {"diagnosis_score": "5"}, {"diagnosis_score": 6},
            {"diagnosis_score": 0}, {"correct": 1}, {"correct": False},
            {"check_diagnosis_output": "  "}, {"model_diagnosis": ""},
        )
        for invalid in overrides:
            with self.subTest(invalid=invalid):
                self.assertFalse(is_completed_result(completed_result(**invalid)))

    def test_incorrect_diagnosis_is_completed_and_cost_failure_is_independent(self):
        self.assertTrue(is_completed_result(completed_result(
            correct=False, diagnosis_score=2, check_diagnosis_output="2",
            cost_analysis_batch_llm_only={"error": "Pricing unavailable"},
        )))

    def test_run_case_marks_success_after_judge_finishes(self):
        runner = Mock(requested_tests=[], requested_tests_only=[], num_test_rounds=0)
        config = configuration().sdbench
        with (
            patch.object(harness.gatekeeper, "BatchTestRunner", return_value=runner),
            patch.object(harness.agents.baseline, "run", return_value="Fictional diagnosis"),
            patch.object(harness.judge, "check_diagnosis", return_value=(True, {
                "diagnosis_score": 5, "check_diagnosis_output": "5",
            })),
        ):
            result = harness.run_case(config, fictional_case("fixture"), "baseline", lambda **_: None)
        self.assertEqual(result["result_status"], "success")
        self.assertTrue(is_completed_result(result))


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.json_dir = self.root / "json"
        self.json_dir.mkdir()

    def write_result(self, case_id, k, result=None):
        result = completed_result() if result is None else result
        path = self.json_dir / f"case_k{k}_{case_id}.json"
        path.write_text(json.dumps({"case_id": case_id, "k": k, **result}))
        return path

    def scan(self):
        with redirect_stdout(io.StringIO()):
            return harness.scan_completed_cases(str(self.root))

    def test_string_ids_and_multiple_repetitions_are_resumable(self):
        expected = {(case_id, k) for case_id in ("mm-01", "fixture_2.v1", "007") for k in (0, 1)}
        for case_id, k in expected:
            self.write_result(case_id, k)
        self.assertEqual(self.scan(), expected)

    def test_stored_identity_must_match_filename_and_repetition(self):
        overrides = (
            {"case_id": "another"}, {"k": 3}, {"k": "0"}, {"k": False},
        )
        for index, invalid in enumerate(overrides):
            self.write_result(f"fixture-{index}", 0, completed_result(**invalid))
        self.assertEqual(self.scan(), set())

    def test_corrupt_incomplete_and_failed_records_are_rerun(self):
        for index, invalid in enumerate((None, [], "text", 12, {})):
            (self.json_dir / f"case_k0_invalid-{index}.json").write_text(json.dumps(invalid))
        (self.json_dir / "case_k0_truncated.json").write_text('{"case_id":')
        (self.json_dir / "case_k0_invalid-utf8.json").write_bytes(b"\xff")
        self.write_result("old-exception", 0, {"correct": False, "model_diagnosis": "Timed out"})
        self.write_result("new-failure", 0, completed_result(result_status="failed"))
        self.write_result("success", 0)
        self.assertEqual(self.scan(), {("success", 0)})

    def test_failed_execution_saves_explicit_failure_and_reruns(self):
        with (
            patch.object(harness, "run_case", side_effect=RuntimeError("Timed out")),
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
        ):
            result = harness._process_single_case(
                0, fictional_case("fixture"), "baseline", "fixture", {},
                configuration(), str(self.json_dir),
            )
        stored = json.loads((self.json_dir / "case_k0_fixture.json").read_text())
        self.assertEqual(result["result_status"], "failed")
        self.assertEqual(stored["error_type"], "RuntimeError")
        self.assertEqual(stored["error_message"], "Timed out")
        self.assertEqual(stored["model_diagnosis"], "")
        self.assertEqual(self.scan(), set())

    def test_case_metadata_cannot_be_replaced_by_agent_fields(self):
        config = configuration()
        harness._save_result_to_json(
            completed_result(k=0, case_id="wrong", dataset="wrong"), "fixture",
            str(self.json_dir), config, fictional_case("fixture"),
        )
        stored = json.loads((self.json_dir / "case_k0_fixture.json").read_text())
        self.assertEqual(stored["case_id"], "fixture")
        self.assertEqual(stored["dataset"], "fictional")
        self.assertEqual(self.scan(), {("fixture", 0)})

    def run_with_pricing(self, case, requested_tests):
        config = configuration(compute_costs=True)
        config.sdbench.compute_actual_workup_costs = True
        runner = Mock(requested_tests=requested_tests, requested_tests_only=requested_tests,
                      num_test_rounds=0)
        with (
            patch.object(harness.models, "model_create_chat_completion_fn", return_value=Mock()),
            patch.object(harness.gatekeeper, "BatchTestRunner", return_value=runner),
            patch.object(harness.agents.baseline, "run", return_value="Fictional diagnosis"),
            patch.object(harness.judge, "check_diagnosis", return_value=(True, {
                "diagnosis_score": 5, "check_diagnosis_output": "5",
            })),
            patch("sdbench.costing.llm_batch_estimator.cost_tests_batch",
                  side_effect=AssertionError("This regression must not call a pricing model")) as pricer,
        ):
            result = harness._process_single_case(
                0, case, "baseline", "fixture", {}, config, str(self.json_dir),
            )
        pricer.assert_not_called()
        return result

    def assert_unknown_pricing(self, analysis):
        self.assertEqual(analysis["pricing_status"], "unknown")
        self.assertEqual(analysis["error_type"], "ValueError")
        self.assertTrue(analysis["error_message"])
        self.assertEqual(analysis["priced_test_count"], 0)
        self.assertIsNone(analysis["unpriced_test_count"])
        self.assertTrue(all(value is None for value in analysis["totals"].values()))

    def test_agent_pricing_failure_preserves_diagnosis_and_historical_costing(self):
        result = self.run_with_pricing(fictional_case("fixture"), [" "])
        self.assert_unknown_pricing(result["cost_analysis_batch_llm_only"])
        self.assertEqual(result["cost_analysis_actual_workup_batch_llm_only"]["pricing_status"], "complete")
        self.assertEqual(result["requested_tests_only"], [" "])
        self.assertTrue(is_completed_result(result))
        self.assertEqual(self.scan(), {("fixture", 0)})

    def test_bad_historical_inputs_preserve_diagnosis_and_agent_costing(self):
        for source_fields in ({"tests": [{"name": ""}]}, {"estimated_clinician_visits": None}):
            case = {**fictional_case("fixture"), **source_fields}
            with self.subTest(source_fields=source_fields):
                result = self.run_with_pricing(case, [])
                self.assert_unknown_pricing(result["cost_analysis_actual_workup_batch_llm_only"])
                self.assertEqual(result["cost_analysis_batch_llm_only"]["pricing_status"], "complete")
                self.assertTrue(is_completed_result(result))
                self.assertEqual(self.scan(), {("fixture", 0)})
                self.assertEqual({key: case[key] for key in source_fields}, source_fields)

    def test_both_pricing_failures_do_not_cause_completed_diagnosis_to_rerun(self):
        case = {**fictional_case("fixture"), "estimated_clinician_visits": None}
        result = self.run_with_pricing(case, [""])
        for key in ("cost_analysis_batch_llm_only", "cost_analysis_actual_workup_batch_llm_only"):
            self.assert_unknown_pricing(result[key])
        stored = json.loads((self.json_dir / "case_k0_fixture.json").read_text())
        self.assertTrue(is_completed_result(stored))
        self.assertEqual(stored["model_diagnosis"], "Fictional diagnosis")
        self.assertEqual(stored["check_diagnosis_output"], "5")
        with (
            patch.object(harness.case_data, "get_case_set", return_value=[case]),
            patch.object(harness, "run_case") as executed,
            redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped,
        ):
            harness.run(configuration(compute_costs=True), str(self.json_dir), self.scan())
        self.assertEqual(stopped.exception.code, 0)
        executed.assert_not_called()

    def test_resume_runs_only_unfinished_attempts_then_skips_completed_batch(self):
        cases = [fictional_case(case_id) for case_id in ("mm-01", "fixture_2", "3")]
        config = configuration(repetitions=2)
        self.write_result("mm-01", 0)
        self.write_result("fixture_2", 0, {"correct": False, "model_diagnosis": "Timed out"})
        with (
            patch.object(harness.case_data, "get_case_set", return_value=cases),
            patch.object(harness, "run_case", side_effect=lambda *_, **__: completed_result()) as executed,
            patch.object(harness, "tqdm"), redirect_stdout(io.StringIO()),
        ):
            harness.run(config, str(self.json_dir), self.scan())
            self.assertEqual(executed.call_count, 5)
            self.assertEqual(sum(call.args[1]["case_id"] == "mm-01" for call in executed.call_args_list), 1)
            self.assertEqual(len(self.scan()), 6)
            with self.assertRaises(SystemExit) as stopped:
                harness.run(config, str(self.json_dir), self.scan())
            self.assertEqual(stopped.exception.code, 0)
            self.assertEqual(executed.call_count, 5)

    def test_progress_average_excludes_partial_costs_and_keeps_zero(self):
        cases = [fictional_case(case_id) for case_id in ("complete", "partial")]
        costs = {
            "complete": {"total_unique_tests": 0, "cost_details": [], "totals": {"clinician_visits": 0}},
            "partial": {
                "total_unique_tests": 2,
                "cost_details": [{"cost": 10, "method": "llm_only"}, {"cost": None, "method": "failure"}],
                "totals": {"clinician_visits": 300, "total_with_visits": 310},
            },
        }
        with (
            patch.object(harness.case_data, "get_case_set", return_value=cases),
            patch.object(harness, "run_case", side_effect=lambda config, case, *_: completed_result(
                cost_analysis_batch_llm_only=costs[case["case_id"]],
            )),
            patch.object(harness, "tqdm") as progress, redirect_stdout(io.StringIO()),
        ):
            harness.run(configuration(compute_costs=True), str(self.json_dir), set())
        stats = progress.return_value.set_postfix.call_args.kwargs["ordered_dict"]
        self.assertEqual(stats["priced"], "1/2")
        self.assertEqual(stats["avg_cost"], "$0")


if __name__ == "__main__":
    unittest.main()
