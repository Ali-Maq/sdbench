"""Fictional retrieval fixtures and selector I/O contracts, without API calls.

The scripted backend deliberately returns the independently authored expected
IDs. These tests prove data exposure and exact rendering, not an LLM's ability
to choose those IDs. The same fixture is suitable for a separately recorded
live selector evaluation, where expected IDs must never enter the prompt.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from openai.types.chat import ChatCompletion

from sdbench.config import SDBenchConfig
from sdbench.isolation.recorded import (
    RecordedTestRunner,
    SELECTOR_PROMPT_VERSION,
    gatekeeper_case,
    validate_recorded_case,
    validate_recorded_observation,
)


FIXTURE = Path(__file__).parent / "fixtures" / "recorded_retrieval_cases.json"
REFERENCE_MARKER = "FICTIONAL_RETRIEVAL_JUDGE_ONLY_REFERENCE"


def fictional_case(catalog):
    """Wrap one authored catalog in the normal recorded episode schema."""
    return {
        "case_id": f"fictional-{catalog['id']}",
        "dataset": "synthetic_retrieval",
        "case_summary": "An adult presents for investigation of fatigue.",
        "full_text": REFERENCE_MARKER,
        "final_diagnosis": "Fictional diagnosis for an interface test only",
        "recorded_episode": {
            "schema_version": 1,
            "mode": "retrospective_request",
            "opening_source_ids": ["fictional-opening"],
            "reference_source_ids": ["fictional-reference"],
            "evidence_cutoff_day": 3,
            "reference_day": 4,
            "evidence": deepcopy(catalog["evidence"]),
        },
    }


def run_scripted(catalog, case=None):
    """Exercise the production runner; replace only its provider response."""
    case = case or fictional_case(catalog)
    calls = []

    def backend(*, messages):
        calls.append(deepcopy(messages))
        selections = [
            {"request_id": index, "evidence_ids": request["expected_ids"]}
            for index, request in enumerate(catalog["requests"], 1)
        ]
        return ChatCompletion.model_validate({
            "id": "fictional-retrieval-test", "object": "chat.completion", "created": 0,
            "model": "scripted-selector", "choices": [{
                "index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps({"selections": selections}),
                },
            }], "usage": None,
        })

    config = SDBenchConfig(
        max_turns=1, compute_costs=False,
        gatekeeper_prompt_version=SELECTOR_PROMPT_VERSION,
        gatekeeper_model="scripted-selector",
        judge_prompt_version="v3_reasoning", judge_model="unused-judge",
    )
    runner = RecordedTestRunner(config, gatekeeper_case(case), {}, llm=backend)
    response = runner([request["request"] for request in catalog["requests"]])
    return response.test_results, calls


class RecordedRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())

    def test_authored_expectations_use_existing_ids_of_the_requested_kind(self):
        self.assertEqual(self.fixture["schema_version"], 1)
        for catalog in self.fixture["cases"]:
            with self.subTest(catalog=catalog["id"]):
                validate_recorded_case(fictional_case(catalog))
                evidence = {item["id"]: item for item in catalog["evidence"]}
                for request in catalog["requests"]:
                    expected = request["expected_ids"]
                    self.assertEqual(len(expected), len(set(expected)))
                    self.assertTrue(set(expected).issubset(evidence))
                    kind = "question" if request["request"].startswith("Question for the patient:") else "test"
                    self.assertTrue(all(evidence[key]["kind"] == kind for key in expected))

    def test_selector_receives_actual_excerpts_and_dates_but_not_reference_or_answers(self):
        for catalog in self.fixture["cases"]:
            with self.subTest(catalog=catalog["id"]):
                _, calls = run_scripted(catalog)
                self.assertEqual(len(calls), 1)
                messages = calls[0]
                self.assertEqual([message["role"] for message in messages], ["system", "user"])
                payload = json.loads(messages[1]["content"])
                actual = {item["id"]: item for item in payload["catalog"]}
                for item in catalog["evidence"]:
                    visible = actual[item["id"]]
                    self.assertEqual(visible["text"], item["text"])
                    self.assertEqual(visible["event_day"], item["event_day"])
                    self.assertEqual(visible["recorded_day"], item["recorded_day"])
                    self.assertNotIn("source_id", visible)
                    self.assertNotIn("source_sha256", visible)
                serialized = json.dumps(messages)
                self.assertNotIn(REFERENCE_MARKER, serialized)
                for field in ("expected_ids", "missing_components", "reference_source_ids", "final_diagnosis"):
                    self.assertNotIn(field, serialized)

    def test_scripted_selections_preserve_complete_panels_partial_answers_and_missingness(self):
        for catalog in self.fixture["cases"]:
            case = fictional_case(catalog)
            before = deepcopy(case)
            findings = {item["id"]: item for item in catalog["evidence"]}
            results, _ = run_scripted(catalog, case)
            self.assertEqual(case, before)
            for request, result in zip(catalog["requests"], results, strict=True):
                with self.subTest(catalog=catalog["id"], request=request["request"]):
                    self.assertEqual(result["test_name"], request["request"])
                    self.assertEqual(result["evidence_ids"], request["expected_ids"])
                    validate_recorded_observation(case, result)
                    if not request["expected_ids"]:
                        self.assertEqual(result["results"], "not documented")
                        self.assertEqual(result["source_refs"], [])
                        continue
                    excerpts = result["results"].split("\n\n")
                    self.assertEqual(len(excerpts), len(request["expected_ids"]))
                    for excerpt, identifier, source in zip(
                        excerpts, request["expected_ids"], result["source_refs"], strict=True
                    ):
                        self.assertEqual(excerpt.split("\n", 1)[1], findings[identifier]["text"])
                        self.assertEqual(source["evidence_id"], identifier)
                        self.assertEqual(source["source_sha256"], findings[identifier]["source_sha256"])

    def test_catalog_order_does_not_change_scripted_id_rendering(self):
        for catalog in self.fixture["cases"]:
            with self.subTest(catalog=catalog["id"]):
                forward, _ = run_scripted(catalog)
                case = fictional_case(catalog)
                case["recorded_episode"]["evidence"].reverse()
                reverse, _ = run_scripted(catalog, case)
                self.assertEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()
