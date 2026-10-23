"""Boundary tests for the public baseline worker and fail-closed verifier."""

from __future__ import annotations

from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sdbench.isolation import diagnosis, judge


def completion(text: str) -> dict:
    return {
        "id": "worker-test",
        "object": "chat.completion",
        "created": 0,
        "model": "fixed-diagnosis-model",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": text},
        }],
    }


@contextmanager
def public_service(outputs: list[str], *, max_turns: int = 1, max_model_calls: int = 8):
    state = {"requests": [], "model_calls": 0, "turns_left": max_turns}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, data):
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def handle_request(self):
            payload = None
            if self.command == "POST":
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, payload, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Bearer diagnosis-role-token":
                return self.send_json(403, {"error": "private error text must not be copied"})
            if self.path == "/session":
                return self.send_json(200, {
                    "case_summary": "Public presentation only",
                    "max_turns": max_turns,
                    "turns_left": state["turns_left"],
                })
            if self.path == "/model":
                state["model_calls"] += 1
                if state["model_calls"] > max_model_calls:
                    return self.send_json(429, {"error": "private provider detail"})
                text = outputs[min(state["model_calls"] - 1, len(outputs) - 1)]
                return self.send_json(200, completion(text))
            if self.path == "/tests":
                state["turns_left"] -= 1
                return self.send_json(200, {
                    "test_results": [
                        {"test_name": name, "results": "Requested public observation"}
                        for name in payload["test_names"]
                    ],
                    "limit_messages": [],
                    "turns_left": state["turns_left"],
                })
            if self.path == "/diagnosis":
                return self.send_json(200, {"accepted": True})
            return self.send_json(404, {})

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def fixtures():
    from sdbench.isolation.common import canonical_hash

    case = {
        "case_id": "synthetic-worker-case",
        "dataset": "synthetic",
        "case_title": "Private title",
        "case_summary": "Public presentation only",
        "full_text": "Private full case text for the verifier",
        "final_diagnosis": "Condition A",
    }
    config = {
        "trial_id": "trial-1",
        "pair_id": "pair-1",
        "case_id": case["case_id"],
        "dataset": "synthetic",
        "repetition": 0,
        "protocol_hash": "a" * 64,
        "case_hash": canonical_hash(case),
        "diagnosis_model": "fixed-diagnosis-model",
        "gatekeeper_model": "o4-mini",
        "judge_model": "gpt-5",
        "gk_prompt": "v6_fixed",
        "judge_prompt": "v3_reasoning",
        "max_turns": 1,
        "max_model_calls": 4,
        "max_questions_per_turn": None,
        "max_tests_per_turn": None,
        "diagnosis_model_args": {},
        "gatekeeper_model_args": {},
        "judge_model_args": {},
        "api_mode": "mock",
    }
    identity = {
        "trial_id": config["trial_id"],
        "case_hash": config["case_hash"],
        "protocol_hash": config["protocol_hash"],
        "config_hash": canonical_hash(config),
    }
    events = [
        {"event": "session", "case_summary": case["case_summary"], "max_turns": 1, "turns_left": 1},
        {"event": "tests", "requested_test_names": ["Test to order: Observation"],
         "test_names": ["Test to order: Observation"], "test_results": [
            {"test_name": "Test to order: Observation", "results": "An observation", "is_synthetic": False}
        ], "limit_messages": [], "turns_left": 0},
        {"event": "diagnosis", "diagnosis": "Condition A"},
        {"event": "terminal", "status": "completed", "diagnosis": "Condition A"},
    ]
    for sequence, event in enumerate(events, start=1):
        event.update(identity, sequence=sequence)
    return config, case, events


class DiagnosisWorkerTests(unittest.TestCase):
    def test_released_baseline_orders_observation_then_submits_final_diagnosis(self):
        with public_service(["<test>Observation</test>", "<diagnosis>Condition A</diagnosis>"]) as (url, state):
            result = diagnosis.run_diagnosis(url, "diagnosis-role-token")
        self.assertEqual(result["diagnosis"], "Condition A")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["authoritative"])
        self.assertEqual([request[0] for request in state["requests"]],
                         ["/session", "/model", "/tests", "/model", "/diagnosis"])
        self.assertEqual(state["requests"][2][1], {"test_names": ["Test to order: Observation"]})
        model_messages = state["requests"][3][1]["messages"]
        self.assertIn("Requested public observation", json.dumps(model_messages))
        self.assertIn("No more tests available", json.dumps(model_messages))
        self.assertEqual(set(state["requests"][1][1]), {"messages"})
        self.assertNotIn("Private full case", json.dumps(result))
        self.assertNotIn("diagnosis-role-token", json.dumps(result))
        self.assertNotIn("is_synthetic", json.dumps(result))

    def test_remote_observations_have_no_synthetic_provenance(self):
        with public_service(["<diagnosis>Condition A</diagnosis>"]) as (url, _):
            runner = diagnosis.RemoteTestRunner(url, "diagnosis-role-token")
            result = runner(["Test to order: Observation"])
        self.assertEqual(result.test_results, [{
            "test_name": "Test to order: Observation", "results": "Requested public observation",
        }])

    def test_client_does_not_propagate_private_metadata_if_returned(self):
        with public_service(["<diagnosis>Condition A</diagnosis>"]) as (url, _):
            runner = diagnosis.RemoteTestRunner(url, "diagnosis-role-token")
            with patch.object(runner, "_request", return_value={
                "test_results": [{"test_name": "Observation", "results": "Text", "is_synthetic": True}],
                "limit_messages": [], "turns_left": 0,
            }):
                result = runner(["Observation"])
        self.assertEqual(result.test_results, [{"test_name": "Observation", "results": "Text"}])

    def test_model_call_cap_stops_malformed_baseline_action_loop(self):
        with public_service(["No actionable tags"], max_model_calls=2) as (url, state):
            with self.assertRaises(diagnosis.RemoteProtocolError) as raised:
                diagnosis.run_diagnosis(url, "diagnosis-role-token")
        self.assertIn("HTTP 429", str(raised.exception))
        self.assertNotIn("private provider detail", str(raised.exception))
        self.assertEqual(state["model_calls"], 3)
        self.assertNotIn("/diagnosis", [request[0] for request in state["requests"]])

    def test_credentials_are_required_and_error_bodies_are_not_exposed(self):
        with public_service(["<diagnosis>Condition A</diagnosis>"]) as (url, _):
            with self.assertRaises(diagnosis.RemoteProtocolError) as raised:
                diagnosis.run_diagnosis(url, "wrong-token")
        self.assertIn("HTTP 403", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_remote_model_settings_cannot_be_overridden(self):
        with public_service(["<diagnosis>Condition A</diagnosis>"]) as (url, _):
            runner = diagnosis.RemoteTestRunner(url, "diagnosis-role-token")
            with self.assertRaises(ValueError):
                runner.chat_completion([], model="different-model")

    def test_invalid_turn_budget_is_rejected(self):
        with patch.object(diagnosis.RemoteTestRunner, "_request", return_value={
            "case_summary": "Public presentation", "max_turns": 1, "turns_left": 2,
        }):
            with self.assertRaises(diagnosis.RemoteProtocolError):
                diagnosis.RemoteTestRunner("http://localhost:1", "token")

    def test_unacknowledged_submission_is_not_reported_as_complete(self):
        with public_service(["<diagnosis>Condition A</diagnosis>"]) as (url, _):
            runner = diagnosis.RemoteTestRunner(url, "diagnosis-role-token")
            with patch.object(runner, "_request", return_value={"accepted": False}):
                with self.assertRaises(diagnosis.RemoteProtocolError):
                    runner.submit_diagnosis("Condition A")

    def test_client_cli_writes_failure_without_token_or_private_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "token").write_text("diagnosis-role-token")
            with patch.object(diagnosis, "run_diagnosis", side_effect=RuntimeError("private bearer secret")):
                code = diagnosis.main([
                    "--url", "http://localhost:1", "--token-file", str(path / "token"),
                    "--output", str(path / "output.json"),
                ])
            result = json.loads((path / "output.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", json.dumps(result))


class JudgeWorkerTests(unittest.TestCase):
    def test_valid_evidence_uses_released_prompt_and_records_mock_label(self):
        from openai.types.chat import ChatCompletion

        config, case, events = fixtures()
        llm = Mock(return_value=ChatCompletion.model_validate(completion(
            "**REASONING**: Deliberately mocked score.\n**SCORE**: 3"
        )))
        result = judge.grade_trial(config, case, events, llm=llm)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["correct"])
        self.assertEqual(result["diagnosis_score"], 3)
        self.assertTrue(result["mocked"])
        self.assertIn("not a measurement", result["diagnostic_accuracy_interpretation"])
        messages = llm.call_args.kwargs["messages"]
        self.assertIn(case["full_text"], messages[0]["content"])
        self.assertIn("Reference Diagnosis: Condition A", messages[1]["content"])
        self.assertEqual(result["test_logs"][0]["result"], "An observation")

    def test_unfinished_or_corrupt_evidence_never_calls_judge(self):
        config, case, events = fixtures()
        variants = {
            "empty": [],
            "missing_terminal": copy.deepcopy(events[:-1]),
            "missing_submission": copy.deepcopy(events[:-2] + events[-1:]),
            "failed_terminal": copy.deepcopy(events),
            "reordered_sequence": copy.deepcopy(events),
            "wrong_trial": copy.deepcopy(events),
            "wrong_case": copy.deepcopy(events),
            "wrong_config": copy.deepcopy(events),
            "wrong_protocol": copy.deepcopy(events),
            "different_terminal_diagnosis": copy.deepcopy(events),
            "test_after_diagnosis": copy.deepcopy(events),
            "changed_presentation": copy.deepcopy(events),
            "budget_increase": copy.deepcopy(events),
            "budget_unchanged": copy.deepcopy(events),
            "missing_result": copy.deepcopy(events),
            "extra_result": copy.deepcopy(events),
            "unrequested_result": copy.deepcopy(events),
        }
        variants["failed_terminal"][-1]["status"] = "failed"
        variants["reordered_sequence"][1]["sequence"] = 1
        variants["wrong_trial"][1]["trial_id"] = "another-trial"
        variants["wrong_case"][1]["case_hash"] = "b" * 64
        variants["wrong_config"][1]["config_hash"] = "b" * 64
        variants["wrong_protocol"][1]["protocol_hash"] = "b" * 64
        variants["different_terminal_diagnosis"][-1]["diagnosis"] = "Condition B"
        variants["test_after_diagnosis"][-2]["event"] = "tests"
        variants["changed_presentation"][0]["case_summary"] = "Another case"
        variants["budget_increase"][1]["turns_left"] = 2
        variants["budget_unchanged"][1]["turns_left"] = 1
        variants["missing_result"][1]["test_results"] = []
        variants["extra_result"][1]["test_results"] *= 2
        variants["unrequested_result"][1]["test_results"][0]["test_name"] = "Unrequested observation"
        for name, trace in variants.items():
            with self.subTest(name=name):
                llm = Mock(side_effect=AssertionError("Judge must not run"))
                result = judge.grade_trial(config, case, trace, llm=llm)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["correct"])
                llm.assert_not_called()

    def test_result_order_and_duplicate_requests_must_match_exactly(self):
        config, case, events = fixtures()
        names = ["Observation A", "Observation A", "Observation B"]
        events[1]["requested_test_names"] = names
        events[1]["test_names"] = names
        events[1]["test_results"] = [
            {"test_name": name, "results": f"Observation {index}", "is_synthetic": False}
            for index, name in enumerate(names)
        ]
        self.assertEqual(len(judge.validate_evidence(config, case, events)["test_logs"]), 3)
        for invalid_names in (["Observation A", "Observation B", "Observation A"],
                              ["Observation A", "Observation B", "Observation B"]):
            with self.subTest(invalid_names=invalid_names):
                changed = copy.deepcopy(events)
                for item, name in zip(changed[1]["test_results"], invalid_names):
                    item["test_name"] = name
                with self.assertRaises(judge.EvidenceError):
                    judge.validate_evidence(config, case, changed)

    def test_sdb_bench_mixed_turn_rejection_is_valid_empty_batch(self):
        from dataclasses import asdict
        from sdbench.gatekeeper import BatchTestRunner
        from sdbench.isolation.common import sdbench_config

        config, case, events = fixtures()
        llm = Mock(side_effect=AssertionError("Rejected mixed turn must not call a model"))
        runner = BatchTestRunner(sdbench_config(config), case, {}, llm=llm)
        requested = ["Question for the patient: Symptoms?", "Test to order: Observation"]
        response = asdict(runner(requested))
        events[1].update(response, requested_test_names=requested, turns_left=runner.turns_left)
        result = judge.validate_evidence(config, case, events)
        self.assertEqual(result["test_logs"], [])
        self.assertEqual(result["turns_used"], 1)
        llm.assert_not_called()

    def test_empty_accepted_batch_requires_real_sdbench_limit_response(self):
        config, case, events = fixtures()
        for limits in ([], [""], ["fabricated rejection"]):
            with self.subTest(limits=limits):
                changed = copy.deepcopy(events)
                changed[1].update(test_names=[], test_results=[], limit_messages=limits)
                with self.assertRaises(judge.EvidenceError):
                    judge.validate_evidence(config, case, changed)

    def test_original_request_cannot_be_replaced_or_omitted(self):
        config, case, events = fixtures()
        for requested in (None, [], ["Another test"]):
            with self.subTest(requested=requested):
                changed = copy.deepcopy(events)
                changed[1]["requested_test_names"] = requested
                with self.assertRaises(judge.EvidenceError):
                    judge.validate_evidence(config, case, changed)

    def test_more_than_one_turn_cannot_disappear_in_a_test_event(self):
        from sdbench.isolation.common import canonical_hash

        config, case, events = fixtures()
        config["max_turns"] = 2
        events[0].update(max_turns=2, turns_left=2)
        for event in events:
            event["config_hash"] = canonical_hash(config)
        # The original single test event still claims zero turns remain.
        with self.assertRaises(judge.EvidenceError):
            judge.validate_evidence(config, case, events)

    def test_case_and_config_changes_are_detected(self):
        config, case, events = fixtures()
        changed_case = {**case, "final_diagnosis": "Condition B"}
        changed_config = {**config, "judge_model": "another-model"}
        for inputs in ((config, changed_case), (changed_config, case)):
            with self.subTest(inputs=inputs[0]["judge_model"]):
                with self.assertRaises(judge.EvidenceError):
                    judge.validate_evidence(*inputs, events)

    def test_no_reasoning_score_requires_one_digit_only(self):
        for score in range(1, 6):
            self.assertEqual(judge.strict_score(f" {score}\n", supports_reasoning=False), score)
        for invalid in ("Score 5", "50", "4/5", "I considered 5 diagnoses", "", "0", "6", "4.0"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    judge.strict_score(invalid, supports_reasoning=False)

    def test_reasoning_cannot_pass_via_legacy_digit_fallback(self):
        from openai.types.chat import ChatCompletion

        config, case, events = fixtures()
        outputs = [
            "I considered 5 possibilities but could not assign a score.",
            "**REASONING**: Considered 5 findings.\n**SCORE**: 50",
            "**REASONING**: Considered 5 findings.\n**SCORE**: 5 out of 5",
            "**REASONING**: Fine.\n**SCORE**: 5\n**SCORE**: 1",
            "**REASONING**:   \n**SCORE**: 5",
        ]
        for text in outputs:
            with self.subTest(text=text):
                llm = Mock(return_value=ChatCompletion.model_validate(completion(text)))
                result = judge.grade_trial(config, case, events, llm=llm)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["correct"])
                self.assertIsNone(result["diagnosis_score"])

    def test_valid_score_is_parsed_from_score_field_not_reasoning_digits(self):
        self.assertEqual(judge.strict_score(
            "**REASONING**: Of 5 considerations, 4 were relevant.\n**SCORE**: 2",
            supports_reasoning=True,
        ), 2)

    def test_zero_reward_for_failed_status_even_if_correct_flag_is_present(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            judge.write_results(output, {"status": "failed", "correct": True})
            self.assertEqual(json.loads((output / "reward.json").read_text()), {"reward": 0.0})

    def test_missing_evidence_cli_writes_failure_and_zero_reward(self):
        config, case, _ = fixtures()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.json").write_text(json.dumps(config))
            (path / "case.json").write_text(json.dumps(case))
            code = judge.main([
                "--config", str(path / "config.json"), "--case", str(path / "case.json"),
                "--evidence", str(path / "missing.jsonl"), "--output-dir", str(path / "verifier"),
            ])
            result = json.loads((path / "verifier/result.json").read_text())
            reward = json.loads((path / "verifier/reward.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(reward, {"reward": 0.0})

    def test_non_object_or_truncated_jsonl_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            for text in ("{}\n{", "[]\n", "\n"):
                path.write_text(text)
                with self.assertRaises(judge.EvidenceError):
                    judge.read_evidence(path)


if __name__ == "__main__":
    unittest.main()
