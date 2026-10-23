"""Harbor's optional native trajectory integration, without Docker or API calls."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def report():
    return {
        "status": "completed", "diagnosis": "Condition A", "submission": {"accepted": True},
        "agent_messages": [
            {"role": "system", "content": "Public baseline instruction"},
            {"role": "user", "content": "Patient presentation: fatigue"},
            {"role": "assistant", "content": "Visible explanation. <test>CBC</test>"},
            {"role": "user", "content": "Results:\n- Test to order: CBC: anemia\n"},
            {"role": "user", "content": "No more tests available."},
            {"role": "assistant", "content": "<diagnosis>Condition A</diagnosis>"},
        ],
        "model_calls": [
            {"model": "gpt-5.4", "usage": {
                "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 50},
                "completion_tokens_details": {"reasoning_tokens": 10},
            }},
            {"model": "gpt-5.4", "usage": {
                "prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180,
                "prompt_tokens_details": {"cached_tokens": 75},
                "completion_tokens_details": {"reasoning_tokens": 15},
            }},
        ],
        "private_extra": "SHOULD_NOT_BE_PROPAGATED",
    }


@unittest.skipUnless(importlib.util.find_spec("harbor"), "Optional Harbor is not installed")
class NativeTrajectoryTests(unittest.TestCase):
    def convert(self, value):
        from sdbench.harbor_integration.trajectory import public_diagnosis_trajectory
        return public_diagnosis_trajectory(value, agent_name="baseline", session_id="test-session")

    def test_full_visible_messages_and_confirmed_actions_validate_as_atif(self):
        from harbor.models.trajectories import Trajectory

        value = report()
        trace = self.convert(value)
        parsed = Trajectory.model_validate(trace.to_json_dict())
        self.assertEqual([step.step_id for step in parsed.steps], list(range(1, 7)))
        request = parsed.steps[2]
        self.assertEqual(request.message, value["agent_messages"][2]["content"])
        self.assertEqual(request.tool_calls[0].arguments, {"test_names": ["Test to order: CBC"]})
        self.assertEqual(request.observation.results[0].content, value["agent_messages"][3]["content"])
        self.assertEqual(parsed.steps[-1].llm_call_count, 0)
        self.assertEqual(parsed.steps[-1].tool_calls[0].function_name, "submit_diagnosis")
        self.assertEqual(parsed.steps[-1].tool_calls[0].arguments, {"diagnosis": "Condition A"})
        self.assertEqual(parsed.final_metrics.total_prompt_tokens, 250)
        self.assertEqual(parsed.final_metrics.total_completion_tokens, 50)
        self.assertEqual(parsed.final_metrics.total_cached_tokens, 125)
        self.assertIsNone(parsed.final_metrics.total_cost_usd)
        serialized = json.dumps(parsed.to_json_dict())
        self.assertNotIn("SHOULD_NOT_BE_PROPAGATED", serialized)
        self.assertNotIn('"reasoning_content"', serialized)

    def test_unexecuted_tags_do_not_become_tool_calls(self):
        value = report()
        value["agent_messages"][-1]["content"] = "<test>Ignored</test><diagnosis>Condition A</diagnosis>"
        trace = self.convert(value)
        self.assertIsNone(trace.steps[-2].tool_calls)

    def test_missing_or_misaligned_usage_stays_unknown(self):
        value = report()
        value["model_calls"] = value["model_calls"][:1]
        trace = self.convert(value)
        self.assertFalse(trace.extra["model_calls_aligned"])
        self.assertIsNone(trace.final_metrics.total_prompt_tokens)
        self.assertTrue(all(step.metrics is None for step in trace.steps))

    def test_failed_client_has_truthful_placeholder_without_private_error(self):
        trace = self.convert({"status": "failed", "error_type": "PRIVATE_ERROR"})
        self.assertEqual(len(trace.steps), 1)
        self.assertEqual(trace.extra["status"], "failed")
        self.assertNotIn("PRIVATE_ERROR", json.dumps(trace.to_json_dict()))
        self.assertIsNone(trace.final_metrics.total_prompt_tokens)

    def test_native_post_run_hook_writes_expected_viewer_path_and_context(self):
        from harbor.models.agent.context import AgentContext
        from harbor.models.trajectories import Trajectory
        from sdbench.harbor_integration.agent import SDBenchAgent

        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            (logs / "diagnosis.json").write_text(json.dumps(report()))
            (logs / "private.json").write_text('this private file is never parsed')
            agent = SDBenchAgent(logs_dir=logs, model_name="gpt-5.4")
            context = AgentContext()
            agent.populate_context_post_run(context)
            trace = Trajectory.model_validate_json((logs / "trajectory.json").read_text())
            self.assertEqual(context.n_input_tokens, 250)
            self.assertEqual(context.n_output_tokens, 50)
            self.assertEqual(context.n_cache_tokens, 125)
            self.assertIsNone(context.cost_usd)
            self.assertEqual(trace.agent.model_name, "gpt-5.4")
            self.assertTrue(agent.capabilities.atif)

            value = report()
            value.pop("model_calls")
            (logs / "diagnosis.json").write_text(json.dumps(value))
            context = AgentContext()
            agent.populate_context_post_run(context)
            self.assertIsNone(context.n_input_tokens)
            self.assertEqual(context.model_usage, {})  # Avoid Harbor converting unknown counts to zero.


if __name__ == "__main__":
    unittest.main()
