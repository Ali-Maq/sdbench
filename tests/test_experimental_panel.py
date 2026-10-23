"""Exercise the real LangGraph with deterministic model and observation fixtures."""

from __future__ import annotations

import copy
import importlib.util
from types import SimpleNamespace
import unittest

from sdbench.agents import experimental_panel
from sdbench.config import AgentConfig
from sdbench.gatekeeper import TestRunnerResponse


def config(**extra):
    return AgentConfig(debug=False, clinician_cost=0, max_turns=3,
                       max_questions_per_turn=None, max_tests_per_turn=None,
                       agent_extra_args=extra)


class RecordedRunner:
    def __init__(self, turns=3):
        self.max_turns = turns
        self.turns_left = turns
        self.requests = []

    def __call__(self, requests):
        if self.turns_left <= 0:
            raise AssertionError("An exhausted runner must not be called")
        self.requests.append(requests)
        self.turns_left -= 1
        return TestRunnerResponse(
            test_names=requests,
            test_results=[{"test_name": request, "results": "Recorded fixture observation"}
                          for request in requests],
            limit_messages=[],
        )


class RecordedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.messages = []

    def __call__(self, *, messages):
        self.messages.append(copy.deepcopy(messages))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(self.responses)))])


class PanelActionTests(unittest.TestCase):
    def test_preserves_upstream_request_labels_and_order(self):
        self.assertEqual(experimental_panel._action("<question>Symptom duration?</question>"),
                         (None, ["Question for the patient: Symptom duration?"], None))
        self.assertEqual(experimental_panel._action("<test>A</test><test>B</test>"),
                         (None, ["Test to order: A", "Test to order: B"], None))

    def test_rejects_ambiguous_or_overlarge_actions(self):
        for content in ["<test>A</test><question>B</question>",
                        "<test>A</test><diagnosis>B</diagnosis>",
                        "<diagnosis>A</diagnosis><diagnosis>B</diagnosis>",
                        "<test>A</test>" * 4, "No action", "<diagnosis></diagnosis>"]:
            with self.subTest(content=content):
                diagnosis, requests, error = experimental_panel._action(content)
                self.assertIsNone(diagnosis)
                self.assertEqual(requests, [])
                self.assertIsNotNone(error)


@unittest.skipUnless(importlib.util.find_spec("langgraph"), "optional LangGraph dependency is not installed")
class PanelGraphTests(unittest.TestCase):
    def test_graph_collects_evidence_then_returns_diagnosis(self):
        runner = RecordedRunner()
        model = RecordedModel("Panel summary. <question>Symptom duration?</question>",
                              "Panel summary. <test>Observation</test>",
                              "Panel summary. <diagnosis>Fixture condition</diagnosis>")
        collector = {}
        answer = experimental_panel.run("Brief presentation", runner, model, collector, config())
        self.assertEqual(answer, "Fixture condition")
        self.assertEqual(runner.requests, [["Question for the patient: Symptom duration?"],
                                          ["Test to order: Observation"]])
        self.assertEqual(len(model.messages), 3)
        self.assertEqual(model.messages[0][1]["content"], "Patient presentation:\n\nBrief presentation")
        self.assertNotIn("Recorded fixture observation", str(model.messages[0]))
        self.assertIn("Recorded fixture observation", str(model.messages[1]))
        self.assertEqual(collector["agent_stop_reason"], "diagnosis_submitted")
        self.assertEqual(collector["agent_metadata"]["implementation"], "paper-inspired; not original MAI-DxO")
        self.assertEqual([event["model_call_index"] for event in collector["graph_events"]
                          if event["event"] == "model_response"], [1, 2, 3])

    def test_final_call_after_observation_turn_limit(self):
        runner = RecordedRunner(turns=1)
        model = RecordedModel("<test>Observation</test>", "<diagnosis>Fixture condition</diagnosis>")
        collector = {}
        answer = experimental_panel.run("Brief", runner, model, collector, config())
        self.assertEqual(answer, "Fixture condition")
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(collector["agent_stop_reason"], "test_turn_limit")
        self.assertEqual(collector["graph_events"][-1]["node"], "final_answer")

    def test_invalid_responses_cannot_loop_forever_without_spending_test_turns(self):
        runner = RecordedRunner()
        model = RecordedModel("No action", "Still no action", "<diagnosis>Fixture condition</diagnosis>")
        collector = {}
        answer = experimental_panel.run("Brief", runner, model, collector, config(max_panel_calls="2"))
        self.assertEqual(answer, "Fixture condition")
        self.assertEqual(len(model.messages), 3)
        self.assertEqual(runner.requests, [])
        self.assertEqual(collector["agent_stop_reason"], "panel_call_limit")

    def test_invalid_final_answer_is_not_treated_as_a_diagnosis(self):
        runner = RecordedRunner(turns=0)
        model = RecordedModel("<test>Observation</test>")
        collector = {}
        answer = experimental_panel.run("Brief", runner, model, collector, config())
        self.assertEqual(answer, "Unable to provide diagnosis")
        self.assertEqual(runner.requests, [])
        self.assertEqual(len(model.messages), 1)
        self.assertEqual(collector["agent_stop_reason"], "invalid_final_answer")

    def test_mixed_actions_are_repaired_without_calling_gatekeeper(self):
        runner = RecordedRunner()
        model = RecordedModel("<test>A</test><diagnosis>B</diagnosis>", "<diagnosis>C</diagnosis>")
        collector = {}
        answer = experimental_panel.run("Brief", runner, model, collector, config())
        self.assertEqual(answer, "C")
        self.assertEqual(runner.requests, [])
        self.assertIn("exactly one action", model.messages[1][-1]["content"])

    def test_rejects_invalid_call_limit_before_model_or_tool_calls(self):
        for value in ("0", "-1", "nan", "1.5", 1.5, True):
            with self.subTest(value=value):
                model = RecordedModel()
                with self.assertRaises(ValueError):
                    experimental_panel.run("Brief", RecordedRunner(), model, {}, config(max_panel_calls=value))
                self.assertEqual(model.messages, [])


if __name__ == "__main__":
    unittest.main()
