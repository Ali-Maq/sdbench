"""Verify a finalized service trace and grade it with the released SDBench judge.

The evidence path must be a verifier-owned, read-only mount of the service's
events, not an agent-supplied transcript. Hash checks bind those events to the
trial inputs; they do not establish provenance for an arbitrary supplied file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any, Callable


class EvidenceError(ValueError):
    """The service evidence cannot be used to grade this trial."""


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"Missing or invalid {name}")
    return value


def _nonnegative_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise EvidenceError(f"Missing or invalid {name}")
    return value


def read_evidence(path: Path) -> list[dict[str, Any]]:
    events = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            event = json.loads(line)
        except ValueError:
            raise EvidenceError(f"Invalid JSON at evidence line {number}") from None
        if not isinstance(event, dict):
            raise EvidenceError(f"Evidence line {number} is not an object")
        events.append(event)
    return events


def validate_evidence(
    config: dict[str, Any], case: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Check the complete trace before any private judge-model call is made."""
    from sdbench.gatekeeper import BatchTestRunner
    from sdbench.isolation.common import canonical_hash, sdbench_config

    trial_id = _nonempty_string(config.get("trial_id"), "trial_id")
    protocol_hash = _nonempty_string(config.get("protocol_hash"), "protocol_hash")
    case_hash = _nonempty_string(config.get("case_hash"), "case_hash")
    if canonical_hash(case) != case_hash:
        raise EvidenceError("Case hash does not match the trial configuration")
    if str(config.get("case_id")) != str(case.get("case_id")) or case.get("case_id") is None:
        raise EvidenceError("Case identity does not match the trial configuration")
    if "dataset" in case and case["dataset"] != config.get("dataset"):
        raise EvidenceError("Dataset identity does not match the trial configuration")
    config_hash = canonical_hash(config)
    if not events:
        raise EvidenceError("No service evidence was recorded")
    expected_identity = {
        "trial_id": trial_id,
        "case_hash": case_hash,
        "protocol_hash": protocol_hash,
        "config_hash": config_hash,
    }
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise EvidenceError("Evidence events must be objects")
        if type(event.get("sequence")) is not int or event["sequence"] != sequence:
            raise EvidenceError("Evidence sequence is missing, repeated, or out of order")
        for key, expected in expected_identity.items():
            if event.get(key) != expected:
                raise EvidenceError(f"Evidence {key} does not match this trial")
    if events[0].get("event") != "session":
        raise EvidenceError("Evidence must begin with a session")
    if events[-1].get("event") != "terminal":
        raise EvidenceError("Service evidence has not been finalized")
    if events[-1].get("status") != "completed":
        raise EvidenceError("Service did not complete the trial")
    if len(events) < 3 or events[-2].get("event") != "diagnosis":
        raise EvidenceError("A completed trial requires a final diagnosis submission")
    max_turns = _nonnegative_integer(config.get("max_turns"), "configured max_turns")
    session = events[0]
    if (
        _nonnegative_integer(session.get("max_turns"), "session max_turns") != max_turns
        or _nonnegative_integer(session.get("turns_left"), "initial turns_left") != max_turns
    ):
        raise EvidenceError("Session turn budget differs from the trial configuration")
    if session.get("case_summary") != case.get("case_summary"):
        raise EvidenceError("Session presentation differs from the configured case")
    diagnosis = _nonempty_string(events[-2].get("diagnosis"), "submitted diagnosis")
    if events[-1].get("diagnosis") != diagnosis:
        raise EvidenceError("Terminal diagnosis differs from the final submission")

    turns_left = max_turns
    test_logs: list[dict[str, Any]] = []
    # Reuse SDBench's own request filtering; this parser makes no model calls.
    # Its turn-type rejection legitimately produces an empty accepted batch.
    request_parser = BatchTestRunner(sdbench_config(config), case, {})
    for event in events[1:-2]:
        if event.get("event") != "tests":
            raise EvidenceError("Unexpected event between session and diagnosis")
        names = event.get("test_names")
        requested_names = event.get("requested_test_names")
        results = event.get("test_results")
        limits = event.get("limit_messages")
        if not isinstance(requested_names, list) or not requested_names or not all(
            isinstance(name, str) and name.strip() for name in requested_names
        ):
            raise EvidenceError("Invalid original test request in evidence")
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name.strip() for name in names
        ):
            raise EvidenceError("Invalid test request in evidence")
        if not isinstance(results, list) or len(results) != len(names):
            raise EvidenceError("Invalid test result batch in evidence")
        if not isinstance(limits, list) or not all(isinstance(item, str) for item in limits):
            raise EvidenceError("Invalid test limit messages in evidence")
        if not names and not any(item.strip() for item in limits):
            raise EvidenceError("An empty accepted batch requires a limit explanation")
        expected_names, expected_limits = request_parser._parse_and_limit_requests(requested_names)
        if names != expected_names or limits != expected_limits:
            raise EvidenceError("Accepted test requests do not match SDBench's configured limits")
        remaining = _nonnegative_integer(event.get("turns_left"), "remaining turns_left")
        if remaining != turns_left - 1:
            raise EvidenceError("Each test event must consume exactly one turn")
        turns_left = remaining
        for requested_name, result in zip(names, results):
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("test_name"), str)
                or not isinstance(result.get("results"), str)
                or type(result.get("is_synthetic")) is not bool
            ):
                raise EvidenceError("Malformed test observation in evidence")
            name = result["test_name"]
            if name != requested_name:
                raise EvidenceError("Test result names must match requests in order, including duplicates")
            test_logs.append({
                "test_name": name,
                "result": result["results"],
                "is_synthetic": result["is_synthetic"],
            })
    return {
        "diagnosis": diagnosis,
        "test_logs": test_logs,
        "turns_left": turns_left,
        "turns_used": max_turns - turns_left,
        "test_batches": len(events) - 3,
        "evidence_hash": canonical_hash(events),
        "config_hash": config_hash,
    }


def strict_score(text: str, *, supports_reasoning: bool) -> int:
    """Accept the released prompt's specified format, never its digit fallback."""
    if not isinstance(text, str):
        raise ValueError("Judge output is not text")
    text = text.strip()
    if not supports_reasoning:
        if re.fullmatch(r"[1-5]", text) is None:
            raise ValueError("Judge must return exactly one score digit from 1 to 5")
        return int(text)
    if len(re.findall(r"\*\*SCORE\*\*:", text, re.IGNORECASE)) != 1:
        raise ValueError("Judge must return exactly one SCORE field")
    match = re.fullmatch(
        r"\*\*REASONING\*\*:\s*(?P<reason>.+?)\s*\*\*SCORE\*\*:\s*(?P<score>[1-5])",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if match is None or not match.group("reason").strip():
        raise ValueError("Judge reasoning output does not match the required format")
    return int(match.group("score"))


def grade_trial(
    config: dict[str, Any],
    case: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    llm: Callable[..., Any],
) -> dict[str, Any]:
    """Use the existing prompt and grader, then independently validate its score."""
    from sdbench import judge, prompts
    from sdbench.isolation.common import sdbench_config

    result = _result_identity(config)
    try:
        validated = validate_evidence(config, case, events)
        result.update(validated)
        settings = sdbench_config(config)
        prompt = prompts.load_template("check_diagnosis", settings.judge_prompt_version)
        prompt_config = judge.parse_prompt_config(prompt)
        # Preserve the released prompts and evaluation semantics. Its permissive
        # parser is followed by a strict check before a positive reward exists.
        _, details = judge.check_diagnosis(
            config=settings,
            diagnosis=validated["diagnosis"],
            case=case,
            test_logs=validated["test_logs"],
            llm=llm,
        )
        result["judge"] = details
        score = strict_score(
            details["check_diagnosis_output"], supports_reasoning=prompt_config.supports_reasoning
        )
        if score != details["diagnosis_score"]:
            raise ValueError("Strict judge score disagrees with the released parser")
        result.update(status="completed", diagnosis_score=score, correct=score >= 4)
    except Exception as exc:
        result.update(status="failed", correct=False, diagnosis_score=None, error_type=type(exc).__name__)
        if isinstance(exc, EvidenceError):
            result["error"] = str(exc)
        else:
            # Provider exceptions may contain credentials or private request
            # bodies. Preserve their type, not their text.
            result["error"] = "Judge could not produce a valid score"
    return result


def _result_identity(config: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: config.get(key)
        for key in (
            "trial_id", "pair_id", "case_id", "repetition", "gatekeeper_model", "dataset",
            "protocol_hash", "case_hash", "diagnosis_model", "judge_model", "api_mode",
        )
    }
    result["diagnostic_agent"] = config.get("diagnostic_agent", "baseline")
    mocked = config.get("api_mode") == "mock"
    result.update(
        status="failed",
        correct=False,
        diagnosis_score=None,
        mocked=mocked,
        diagnostic_accuracy_interpretation=(
            "Mocked plumbing check; not a measurement of diagnostic accuracy"
            if mocked else "Released SDBench judge assessment of this trial"
        ),
    )
    return result


def write_results(output_dir: Path, result: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    reward = float(result.get("status") == "completed" and result.get("correct") is True)
    (output_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (output_dir / "reward.json").write_text(json.dumps({"reward": reward}) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    config: dict[str, Any] = {}
    result: dict[str, Any]
    try:
        config = json.loads(args.config.read_text())
        case = json.loads(args.case.read_text())
        if not isinstance(config, dict) or not isinstance(case, dict):
            raise EvidenceError("Trial configuration and case must be JSON objects")
        events = read_evidence(args.evidence)
        # Even backend construction occurs only after validation, so missing or
        # unfinished service evidence cannot initiate a paid judge request.
        validate_evidence(config, case, events)
        from sdbench.isolation.providers import build_backend

        args.output_dir.mkdir(parents=True, exist_ok=True)
        backend = build_backend(config, "judge", args.output_dir / "judge_api_calls.jsonl")
        result = grade_trial(config, case, events, llm=backend)
    except Exception as exc:
        result = _result_identity(config if isinstance(config, dict) else {})
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc) if isinstance(exc, EvidenceError) else "Verifier input or setup failed"
    write_results(args.output_dir, result)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
