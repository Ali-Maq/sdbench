"""Compare scripted baseline encounters before and after the HTTP boundary.

These tests exercise the real baseline, BatchTestRunner, service, and HTTP client.
Only model completions are scripted. They do not test clinical correctness, live
provider equivalence, Docker isolation, or reproduction of published scores.
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from openai.types.chat import ChatCompletion

from sdbench.agents import baseline
from sdbench.config import AgentConfig
from sdbench.gatekeeper import BatchTestRunner
from sdbench.isolation import diagnosis, service
from sdbench.isolation.common import canonical_hash, sdbench_config


class ScriptedModel:
    """Record each input before returning the next fixed completion."""

    def __init__(self, outputs: list[str]):
        self.outputs = outputs
        self.inputs: list[list[dict]] = []

    def __call__(self, *, messages):
        self.inputs.append(copy.deepcopy(list(messages)))
        index = len(self.inputs) - 1
        if index >= len(self.outputs):
            raise AssertionError("Unexpected additional model call")
        return ChatCompletion.model_validate({
            "id": f"scripted-{index}", "object": "chat.completion",
            "created": 0, "model": "scripted-transport-test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": self.outputs[index],
            }}],
        })


class RecordingTestRunner(BatchTestRunner):
    """Record the baseline's requests before the existing filtering runs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_batches: list[list[str]] = []

    def __call__(self, test_names):
        self.request_batches.append(list(test_names))
        return super().__call__(test_names)


QUESTION_RESULT = (
    '<patient_response id="1"><response>Symptoms began two weeks ago.</response>'
    '<is_synthetic>true</is_synthetic></patient_response>'
)
TEST_RESULT = (
    '<test_result id="1"><result>Requested observation: 10 units.</result>'
    '<is_synthetic>false</is_synthetic></test_result>'
)
FINAL_ANSWER = "<diagnosis>Candidate condition</diagnosis>"


@patch.dict("os.environ", {"SDBENCH_LOCAL_DIR": "disable"})
class BaselineTransportEquivalenceTests(unittest.TestCase):
    def compare_encounter(self, diagnostic_outputs, gatekeeper_outputs, *, max_turns):
        case = {
            "dataset": "synthetic", "case_id": "transport-fixture",
            "case_title": "PRIVATE_TITLE_SENTINEL",
            "case_summary": "An adult presents with a new symptom.",
            "full_text": "PRIVATE_SOURCE_SENTINEL. Reference: PRIVATE_REFERENCE_SENTINEL.",
            "final_diagnosis": "PRIVATE_REFERENCE_SENTINEL",
        }
        config = {
            "trial_id": "transport-trial", "case_id": case["case_id"],
            "case_hash": canonical_hash(case), "protocol_hash": "scripted-test",
            "diagnosis_token": "test-only-token-" + "a" * 32,
            "diagnostic_agent": "baseline", "api_mode": "mock",
            "diagnosis_model": "scripted", "gatekeeper_model": "scripted",
            "judge_model": "unused", "gk_prompt": "v6_fixed",
            "judge_prompt": "v3_reasoning", "max_turns": max_turns,
            "max_model_calls": 10, "max_questions_per_turn": 1,
            "max_tests_per_turn": 1,
        }
        settings = sdbench_config(config)
        local_diagnosis = ScriptedModel(diagnostic_outputs)
        local_gatekeeper = ScriptedModel(gatekeeper_outputs)
        local_collector = {}
        local_runner = RecordingTestRunner(settings, case, local_collector, llm=local_gatekeeper)
        local_answer = baseline.run(
            case["case_summary"], local_runner, local_diagnosis, local_collector,
            AgentConfig.from_sdbench_config(settings),
        )

        remote_models = {
            "diagnosis": ScriptedModel(diagnostic_outputs),
            "gatekeeper": ScriptedModel(gatekeeper_outputs),
        }
        wire_responses = []

        class RecordingRemoteRunner(diagnosis.RemoteTestRunner):
            def _request(self, *args, **kwargs):
                response = super()._request(*args, **kwargs)
                wire_responses.append(copy.deepcopy(response))
                return response

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(service, "build_backend", side_effect=lambda config, role, path: remote_models[role]):
                trial_service = service.TrialService(config, case, directory)
            server = service.make_server(trial_service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(diagnosis, "RemoteTestRunner", RecordingRemoteRunner):
                    remote_result = diagnosis.run_diagnosis(
                        f"http://127.0.0.1:{server.server_port}", config["diagnosis_token"], timeout=3,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
            events = [json.loads(line) for line in (Path(directory) / "events.jsonl").read_text().splitlines()]

        # Compare all model inputs, not just the final answer. This includes
        # previous observations, budget notices, and malformed-response repairs.
        self.assertEqual(remote_models["diagnosis"].inputs, local_diagnosis.inputs)
        self.assertEqual(remote_models["gatekeeper"].inputs, local_gatekeeper.inputs)
        self.assertEqual(remote_result["agent_messages"], local_collector["agent_messages"])
        self.assertEqual(remote_result["diagnosis"], local_answer)
        self.assertEqual(remote_result["turns_left"], local_runner.turns_left)
        self.assertEqual(remote_result["status"], "completed")
        self.assertTrue(trial_service.closed)
        self.assertEqual(events[-1]["diagnosis"], local_answer)
        self.assertEqual(events[-1]["status"], "completed")
        self.assertEqual(
            [event["requested_test_names"] for event in events if event["event"] == "tests"],
            local_runner.request_batches,
        )
        self.assertEqual(trial_service.runner.requested_tests, local_runner.requested_tests)
        for role, outputs in (("diagnosis", diagnostic_outputs), ("gatekeeper", gatekeeper_outputs)):
            self.assertEqual(len(remote_models[role].inputs), len(outputs))

        # The source must reach the gatekeeper, while neither it nor private
        # provenance may reach the diagnostic client or its model inputs.
        self.assertIn(case["full_text"], json.dumps(remote_models["gatekeeper"].inputs))
        self.assertTrue(trial_service.collector["test_logs"][0]["is_synthetic"])
        public_data = json.dumps([wire_responses, remote_result, remote_models["diagnosis"].inputs])
        for private_value in (
            case["case_title"], "PRIVATE_SOURCE_SENTINEL", case["final_diagnosis"],
            config["diagnosis_token"], "is_synthetic",
        ):
            self.assertNotIn(private_value, public_data)
        return remote_result, local_runner

    def test_questions_tests_and_limits_match_without_forcing_final_answer(self):
        result, runner = self.compare_encounter([
            "<question>When did symptoms begin?</question><question>Any other symptoms?</question>",
            "<test>Observation A</test><test>Observation B</test>",
            FINAL_ANSWER,
        ], [QUESTION_RESULT, TEST_RESULT], max_turns=4)
        self.assertEqual(runner.requested_tests, [
            "Question for the patient: When did symptoms begin?", "Test to order: Observation A",
        ])
        self.assertEqual(result["diagnosis"], "Candidate condition")
        self.assertEqual(result["turns_left"], 2)
        dialogue = json.dumps(result["agent_messages"])
        self.assertIn("Question limit applied", dialogue)
        self.assertIn("Test limit applied", dialogue)
        self.assertNotIn("No more tests available", dialogue)

    def test_turn_limit_forces_the_same_final_diagnosis_request(self):
        result, _ = self.compare_encounter([
            "<question>When did symptoms begin?</question>",
            "<test>Observation A</test>",
            FINAL_ANSWER,
        ], [QUESTION_RESULT, TEST_RESULT], max_turns=2)
        self.assertEqual(result["turns_left"], 0)
        self.assertEqual(result["agent_messages"][-2], {
            "role": "user",
            "content": "No more tests available. Please provide your diagnosis using <diagnosis> tags.",
        })

    def test_diagnostic_repair_and_gatekeeper_format_retry_match(self):
        result, runner = self.compare_encounter([
            "I have not supplied an actionable tag.",
            "<question>When did symptoms begin?</question>",
            FINAL_ANSWER,
        ], ["This response is missing the required XML.", QUESTION_RESULT], max_turns=1)
        self.assertEqual(len(runner.request_batches), 1)
        self.assertEqual(result["turns_left"], 0)
        self.assertIn({
            "role": "user",
            "content": "Please order tests using <test> tags, questions using <question> tags, or provide diagnosis using <diagnosis> tags.",
        }, result["agent_messages"])
        self.assertNotIn("missing the required XML", json.dumps(result["agent_messages"]))


if __name__ == "__main__":
    unittest.main()
