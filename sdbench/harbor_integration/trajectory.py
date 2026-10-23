"""Convert the public baseline transcript to Harbor's native ATIF format.

This is a display record, not authoritative evidence for grading. No private
case, gatekeeper prompt, judge record, or provider ledger is read here.
"""

from __future__ import annotations

from typing import Any

from harbor.models.trajectories import (
    Agent, FinalMetrics, Metrics, Observation, ObservationResult, Step, ToolCall, Trajectory,
)

from sdbench.agents.baseline import extract_diagnosis_from_response, extract_test_list_from_response
from sdbench.isolation.accounting import normalize_usage


def public_diagnosis_trajectory(
    report: dict[str, Any], *, agent_name: str, model_name: str | None = None,
    session_id: str | None = None, trajectory_id: str | None = None,
) -> Trajectory:
    """Preserve full visible messages and mark confirmed baseline actions.

    Only a test request followed by the baseline's Results message is shown as
    an executed tool call. The final submission is a deterministic client step.
    Token metrics cover received diagnosis completions only; all-role/retry
    accounting remains in the trusted service ledger. Unknown cost stays None.
    """
    messages = report.get("agent_messages", [])
    if not isinstance(messages, list) or not all(
        isinstance(message, dict)
        and message.get("role") in {"system", "user", "assistant"}
        and isinstance(message.get("content"), str)
        for message in messages
    ):
        raise ValueError("Invalid public baseline messages")
    calls = report.get("model_calls", [])
    assistant_count = sum(message["role"] == "assistant" for message in messages)
    calls_aligned = (
        isinstance(calls, list) and len(calls) == assistant_count
        and all(isinstance(call, dict) for call in calls)
    )
    steps: list[Step] = []
    normalized_calls: list[dict[str, Any]] = []
    assistant_index = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        source = "agent" if message["role"] == "assistant" else message["role"]
        kwargs: dict[str, Any] = {}
        content = message["content"]
        if source == "agent":
            kwargs["llm_call_count"] = 1
            call = calls[assistant_index] if calls_aligned else {}
            observed_model = call.get("model")
            kwargs["model_name"] = observed_model if isinstance(observed_model, str) else model_name
            assistant_index += 1
            usage = normalize_usage(call.get("usage"))
            normalized_calls.append(usage)
            if usage["usage_available"]:
                kwargs["metrics"] = Metrics(
                    prompt_tokens=usage["input_tokens"],
                    completion_tokens=usage["output_tokens"],
                    cached_tokens=usage["cached_input_tokens"],
                    extra={"reasoning_tokens": usage["reasoning_tokens"],
                           "usage_warnings": usage["warnings"]},
                )
            next_message = messages[index + 1] if index + 1 < len(messages) else {}
            tests = extract_test_list_from_response(content)
            if (
                tests and not extract_diagnosis_from_response(content)
                and next_message.get("role") == "user"
                and next_message.get("content", "").startswith("Results:\n")
            ):
                call_id = f"tests-{len(steps) + 1}"
                kwargs["tool_calls"] = [ToolCall(
                    tool_call_id=call_id, function_name="order_tests", arguments={"test_names": tests},
                )]
                kwargs["observation"] = Observation(results=[ObservationResult(
                    source_call_id=call_id, content=next_message["content"],
                )])
                index += 1
        steps.append(Step(step_id=len(steps) + 1, source=source, message=content, **kwargs))
        index += 1

    submission = report.get("submission", {})
    if (
        report.get("status") == "completed" and isinstance(submission, dict)
        and submission.get("accepted") is True and isinstance(report.get("diagnosis"), str)
    ):
        steps.append(Step(
            step_id=len(steps) + 1, source="agent", llm_call_count=0,
            message="The diagnosis client submitted its final diagnosis.",
            tool_calls=[ToolCall(tool_call_id="final-submission", function_name="submit_diagnosis",
                                 arguments={"diagnosis": report["diagnosis"]})],
            observation=Observation(results=[ObservationResult(
                source_call_id="final-submission", content='{"accepted": true}',
            )]),
        ))
    if not steps:
        # Failure logs intentionally contain no provider exception text.
        steps.append(Step(step_id=1, source="system", message="No completed diagnosis transcript was recorded."))

    def total(field: str) -> int | None:
        values = [call[field] for call in normalized_calls]
        return sum(values) if values and all(value is not None for value in values) else None

    observed_models = {step.model_name for step in steps if step.model_name}
    return Trajectory(
        session_id=session_id, trajectory_id=trajectory_id,
        agent=Agent(name=agent_name, version="public-transcript-v1",
                    model_name=next(iter(observed_models)) if len(observed_models) == 1 else model_name),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=total("input_tokens"),
            total_completion_tokens=total("output_tokens"),
            total_cached_tokens=total("cached_input_tokens"), total_steps=len(steps),
        ),
        notes=("Public diagnosis transcript only. Baseline tag actions are reconstructed from visible "
               "messages and their observations; final submission is a deterministic client action. "
               "This transcript is not authoritative grading evidence. Token counts cover received "
               "diagnosis completions only, excluding gatekeeper, judge, and failed attempts. "
               "No dollar cost or hidden reasoning is supplied."),
        extra={"authoritative": False, "status": report.get("status"),
               "model_calls_aligned": calls_aligned, "usage_scope": "received-diagnosis-completions"},
    )
