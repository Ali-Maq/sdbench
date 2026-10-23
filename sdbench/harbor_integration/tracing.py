"""Export completed Harbor trials to Langfuse, metadata-only by default.

Full clinical content requires an explicit mode and case path, and can only be
sent to a literal loopback endpoint. This is host-side audit export, never an
input to an agent. Full exports use recorded provider intervals where available;
metadata-only observations describe export time.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sdbench.isolation.common import canonical_hash, write_json


_SOURCES = {
    "artifacts/gatekeeper-evidence/usage.jsonl": {"diagnosis", "gatekeeper"},
    "verifier/judge_api_calls.jsonl": {"judge"},
}
_TOKEN_NAMES = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
_STATUSES = {"completed", "error", "failed", "incomplete", "cancelled"}


def _hash(label: str, value: str) -> str:
    return hashlib.sha256(f"sdbench-metadata-v1:{label}:{value}".encode()).hexdigest()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("Missing or invalid ledger identifier")
    return value


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("Token counts must be non-negative integers or null")
    return value


def _trusted_file(root: Path, relative: str) -> Path:
    path = root / relative
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise ValueError("Tracing sources must not use symlinks")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError("Required trusted usage ledger is unavailable")
    return path


def build_metadata_export(trial_dir: Path) -> dict[str, Any]:
    """Build a reviewable payload using only two trusted usage-ledger paths.

    Unrecognized fields, identifiers and free text never enter the payload.
    Model names are hashed and are not passed as Langfuse model identifiers,
    preventing automatic catalogue prices from masquerading as API charges.
    """
    if trial_dir.is_symlink():
        raise ValueError("Trial directory must not be a symlink")
    root = trial_dir.resolve()
    entries: dict[str, dict[str, Any]] = {}
    trial_ids: set[str] = set()
    for relative, roles in _SOURCES.items():
        for line in _trusted_file(root, relative).read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("role") not in roles:
                raise ValueError("Ledger role does not match its trusted source")
            trial_ids.add(_identifier(entry.get("trial_id")))
            call_id = _identifier(entry.get("call_id"))
            if call_id in entries and entries[call_id] != entry:
                raise ValueError("Conflicting records for one API attempt")
            entries[call_id] = entry
    if len(trial_ids) != 1 or not entries:
        raise ValueError("Usage ledgers must describe exactly one nonempty trial")
    trial_id = next(iter(trial_ids))
    generations = []
    for entry in entries.values():
        role, mode, status = entry["role"], entry.get("api_mode"), entry.get("status")
        if mode not in {"mock", "live"} or status not in _STATUSES:
            raise ValueError("Unknown API mode or status")
        attempt = entry.get("attempt")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("Attempt number must be a positive integer")
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("Normalized usage object is required")
        tokens = {name: _count(usage.get(name)) for name in _TOKEN_NAMES}
        if tokens["cached_input_tokens"] is not None and tokens["input_tokens"] is not None:
            if tokens["cached_input_tokens"] > tokens["input_tokens"]:
                raise ValueError("Cached tokens exceed input tokens")
        if tokens["reasoning_tokens"] is not None and tokens["output_tokens"] is not None:
            if tokens["reasoning_tokens"] > tokens["output_tokens"]:
                raise ValueError("Reasoning tokens exceed output tokens")
        if all(tokens[name] is not None for name in ("input_tokens", "output_tokens", "total_tokens")):
            if tokens["total_tokens"] != tokens["input_tokens"] + tokens["output_tokens"]:
                raise ValueError("Normalized token total is inconsistent")
        metadata = {
            "role": role, "api_mode": mode, "status": status, "attempt": attempt,
            "call_ref": _hash("call", entry["call_id"]),
            "requested_model_ref": _hash("model", _identifier(entry.get("model"))),
            "tokens": tokens,
            "actual_paid_usd": None,
            "timing_scope": "posthoc-export; API latency is separate metadata",
        }
        resolved = entry.get("resolved_model")
        if resolved is not None:
            metadata["resolved_model_ref"] = _hash("model", _identifier(resolved))
        latency = entry.get("latency_seconds")
        if latency is not None:
            if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency < 0:
                raise ValueError("Recorded latency must be a finite non-negative number")
            metadata["recorded_api_latency_seconds"] = latency
        generations.append({
            "name": f"sdbench.{role}.api-attempt",
            "metadata": metadata,
            # Subset counts stay in metadata rather than being counted twice.
            "usage_details": {target: tokens[source] for source, target in (
                ("input_tokens", "input"), ("output_tokens", "output"), ("total_tokens", "total")
            ) if tokens[source] is not None},
        })
    return {
        "schema_version": 1,
        "export_kind": "host-only-posthoc-api-metadata",
        "trace_id": _hash("trial", trial_id)[:32],
        "name": "sdbench.harbor.metadata-export",
        "metadata": {
            "content_scope": "allowlisted-api-metadata-only",
            "api_attempts": len(generations),
            "actual_paid_usd": None,
            "timing_scope": "export time, not original execution timestamps",
            "identity_scope": "hashed technical references; not a de-identification certification",
        },
        "generations": generations,
    }


def _visible_output(call: dict[str, Any]) -> list[dict[str, str]]:
    """Export provider-visible text only, never encrypted or hidden reasoning."""
    response = call.get("response") or {}
    if response.get("object") == "response":
        return [{"phase": item.get("phase") or "unspecified", "text": "\n".join(
            part.get("text", part.get("refusal", "")) for part in item.get("content", [])
            if part.get("type") in {"output_text", "refusal"}
        )} for item in response.get("output", []) if item.get("type") == "message"]
    return [{"phase": "unspecified", "text": choice["message"]["content"]}
            for choice in response.get("choices", [])
            if isinstance(choice.get("message", {}).get("content"), str)]


def build_full_export(trial_dir: Path, case_path: Path) -> dict[str, Any]:
    """Validate frozen sources and build an auditor-only recorded-episode tree.

    The saved judge request is displayed verbatim; this function neither grades
    again nor repairs missing context. It never adds the reference to a diagnostic
    or gatekeeper input. Raw model inputs are shown exactly as recorded.
    """
    from sdbench.harbor_integration.reporting import _messages, _recorded_evidence
    from sdbench.isolation.accounting import normalize_usage

    metadata_payload = build_metadata_export(trial_dir)
    root = trial_dir.resolve()
    sources: dict[str, str] = {}

    def read(relative: str) -> Any:
        data = _trusted_file(root, relative).read_bytes()
        sources[relative] = hashlib.sha256(data).hexdigest()
        if relative.endswith(".jsonl"):
            return [json.loads(line) for line in data.splitlines() if line.strip()]
        return json.loads(data)

    harbor, public, verified = (read(name) for name in (
        "result.json", "agent/diagnosis.json", "verifier/result.json"
    ))
    if (not harbor.get("finished_at") or harbor.get("exception_info")
            or public.get("status") != "completed" or verified.get("status") != "completed"):
        raise ValueError("Full tracing requires a successfully finished and verified trial")
    if case_path.is_symlink():
        raise ValueError("Private reference case must not be a symlink")
    case_bytes = case_path.read_bytes()
    case = json.loads(case_bytes)
    sources["private_case"] = hashlib.sha256(case_bytes).hexdigest()
    if canonical_hash(case) != verified.get("case_hash") or str(case.get("case_id")) != str(verified.get("case_id")):
        raise ValueError("Private reference case does not match the verified trial")
    events = read("artifacts/gatekeeper-evidence/events.jsonl")
    if canonical_hash(events) != verified.get("evidence_hash"):
        raise ValueError("Service events do not match the verified evidence hash")
    if public.get("diagnosis") != verified.get("diagnosis"):
        raise ValueError("Saved diagnosis differs from the verified submission")
    if _recorded_evidence(case, verified, events) is None:
        raise ValueError("Full content tracing currently requires a recorded-only episode")
    score = verified.get("diagnosis_score")
    if type(score) is not int or not 1 <= score <= 5 or verified.get("correct") is not (score >= 4):
        raise ValueError("Verified score and correctness must agree")
    trial_id = verified["trial_id"]
    if metadata_payload["trace_id"] != _hash("trial", trial_id)[:32]:
        raise ValueError("Usage ledgers differ from the verified trial")
    roles = {role: {"name": f"sdbench.{role}", "as_type": "agent" if role != "judge" else "evaluator",
                    "metadata": {"role": role}, "children": []}
             for role in ("diagnosis", "gatekeeper", "judge")}
    roles["diagnosis"].update(input={"opening": case["case_summary"]}, output={"diagnosis": verified["diagnosis"]})
    roles["judge"].update(
        input={"reference_diagnosis": case["final_diagnosis"], "reference_context": case["full_text"],
               "candidate_diagnosis": verified["diagnosis"]},
        output={"score": score, "correct": verified["correct"],
                "judge_output": verified["judge"]["check_diagnosis_output"]},
    )
    generations = {item["metadata"]["call_ref"]: item for item in metadata_payload["generations"]}
    ledger = {entry["call_id"]: entry for path in _SOURCES for entry in read(path)}
    raw_calls: dict[str, dict[str, Any]] = {}
    for relative, allowed_roles in (
        ("artifacts/gatekeeper-evidence/raw_calls.jsonl", {"diagnosis", "gatekeeper"}),
        ("verifier/raw_calls.jsonl", {"judge"}),
    ):
        for call in read(relative):
            call_id = _identifier(call.get("call_id"))
            if call_id in raw_calls:
                if raw_calls[call_id] != call:
                    raise ValueError("Conflicting raw records for one API attempt")
                continue
            if call.get("trial_id") != trial_id or call.get("role") not in allowed_roles or call_id not in ledger:
                raise ValueError("Raw call identity does not match its trusted usage ledger")
            entry = ledger[call_id]
            if (entry.get("protocol_hash") != verified.get("protocol_hash")
                    or entry.get("api_mode") != verified.get("api_mode")
                    or entry.get("model") != verified.get(f"{call['role']}_model")):
                raise ValueError("Usage protocol or model differs from the verified trial")
            if (call.get("request") or {}).get("model") != entry["model"]:
                raise ValueError("Raw request model differs from usage ledger")
            if any(call.get(key) != entry.get(key) for key in ("role", "status", "api_mode")):
                raise ValueError("Raw call status or role differs from usage ledger")
            for key in ("resolved_model", "provider", "api_transport", "effective_model_args", "response_id"):
                if call.get(key) != entry.get(key):
                    raise ValueError("Raw call routing or configuration differs from usage ledger")
            normalized = normalize_usage((call.get("response") or {}).get("usage"))
            if any(normalized.get(key) != entry["usage"].get(key) for key in _TOKEN_NAMES):
                raise ValueError("Raw response token counts differ from usage ledger")
            generation = generations[_hash("call", call_id)]
            generation.update(name=f"sdbench.{call['role']}.call-{entry['attempt']}",
                              as_type="generation", input=_messages(call.get("messages")), output=_visible_output(call))
            generation["metadata"].update(
                requested_model=entry["model"], resolved_model=entry.get("resolved_model"),
                ledger_order=len(raw_calls) + 1,
                api_transport=entry.get("api_transport"),
                model_parameters=entry.get("effective_model_args", {}),
            )
            for key in ("started_at", "finished_at"):
                if call.get(key) != entry.get(key):
                    raise ValueError("Raw call timestamps differ from usage ledger")
                generation["metadata"][key] = entry.get(key)
            # Parameters stay metadata: no automatic catalogue price inference.
            roles[call["role"]]["children"].append(generation)
            raw_calls[call_id] = call
    if set(raw_calls) != set(ledger) or not all(roles[role]["children"] for role in ("diagnosis", "judge")):
        raise ValueError("Full export requires diagnosis, judge and every saved API attempt")
    batch_number = 0
    for ordinal, event in enumerate(events, 1):
        if event.get("event") != "tests":
            continue
        batch_number += 1
        observations = event.get("test_results", [])
        roles["gatekeeper"]["children"].append({
            "name": f"sdbench.tests.batch-{batch_number}", "as_type": "tool",
            "input": {"requested_test_names": event.get("requested_test_names", [])},
            "output": {"returned_findings": len(observations)},
            "metadata": {"service_event_sequence": event.get("sequence", ordinal),
                         "recorded_completion_timestamp": event.get("timestamp"),
                         "timing_scope": "completion event only; start time was not recorded"},
            "children": [{"name": f"Retrieve {index}: {item['test_name'][:120]}", "as_type": "retriever",
                          "input": {"requested_test": item["test_name"]},
                          "output": item,
                          "metadata": {"evidence_status": item.get("evidence_status"),
                                       "recorded_completion_timestamp": event.get("timestamp"),
                                       "service_event_sequence": event.get("sequence", ordinal)}}
                         for index, item in enumerate(observations, 1)],
        })
    return {
        "schema_version": 2, "export_kind": "host-only-posthoc-full-content",
        "trace_id": _hash("full-trial", trial_id)[:32], "name": "sdbench.harbor.completed-workflow",
        "input": {"opening": case["case_summary"]},
        "output": {"diagnosis": verified["diagnosis"], "judge_score": score,
                   "correct": verified["correct"], "status": verified["status"]},
        "metadata": {"content_scope": "full-clinical-auditor-view", "api_attempts": len(raw_calls),
                     "case_hash": verified["case_hash"], "evidence_hash": verified["evidence_hash"],
                     "actual_paid_usd": None, "mocked": verified.get("mocked"),
                     "source_sha256": sources,
                     "timing_scope": "recorded provider intervals when available; tools show completion instants; absent times use explicitly marked export time",
                     "data_flow": "opening -> diagnosis <-> gatekeeper tools; frozen reference + final diagnosis -> judge"},
        "roles": list(roles.values()), "score": score,
    }


def _configured_client(*, local_only: bool = False) -> Any:
    endpoint = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    if not endpoint:
        raise ValueError("Set LANGFUSE_BASE_URL or LANGFUSE_HOST explicitly before --send")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Langfuse endpoint must be an explicit HTTP(S) URL without credentials or query")
    if local_only:
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("Full clinical export requires a literal loopback address, for example http://127.0.0.1:3001")
    if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
        raise ValueError("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY before --send")
    try:
        from langfuse import Langfuse
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as error:
        raise RuntimeError("Install the optional tracing extra to send metadata") from error
    return Langfuse(base_url=endpoint, debug=False, tracer_provider=TracerProvider())


def send_full_export(payload: dict[str, Any], *, client: Any = None) -> dict[str, Any]:
    """Submit a validated full-content payload to a local, isolated collector.

    Injected clients are reserved for callers that independently control their
    transport (including in-memory tests). CLI clients always enforce loopback.
    Uses the official SDK's typed ingestion API for self-hosted Langfuse v3.
    Trace and observation IDs are deterministic. This adapter does not support
    Langfuse v4, which removes this historical ingestion API.
    """
    owns_client = client is None
    if owns_client:
        client = _configured_client(local_only=True)
    try:
        events = _full_ingestion_events(payload)
        batches: list[list[Any]] = [[]]
        size = 0
        for event in events:
            event_size = len(event.model_dump_json(by_alias=True, exclude_none=True).encode())
            if event_size > 3_000_000:
                raise ValueError("One observation exceeds the local Langfuse v3 ingestion limit")
            if size + event_size > 3_000_000:
                batches.append([])
                size = 0
            batches[-1].append(event)
            size += event_size
        for batch in batches:
            response = client.api.ingestion.batch(batch=batch)
            if response.errors or {item.id for item in response.successes} != {item.id for item in batch}:
                # Server messages may echo clinical input; report only counts
                # and numeric statuses, leaving the private preview inspectable.
                statuses = sorted({str(item.status) for item in response.errors
                                   if type(getattr(item, "status", None)) is int})
                raise RuntimeError(
                    "Langfuse did not acknowledge every ingestion event: "
                    f"{len(response.successes)}/{len(batch)} accepted; "
                    f"{len(response.errors)} rejected; HTTP statuses={statuses}. "
                    "Inspect the local server before retrying."
                )
    finally:
        if owns_client:
            client.shutdown()
    return {"trace_id": payload["trace_id"], "api_attempts": payload["metadata"]["api_attempts"],
            "transport_status": "every-event-acknowledged-by-v3-ingestion", "server_acceptance_verified": True,
            "server_materialization_verified": False,
            "content_scope": "full-clinical-auditor-view"}


def _full_ingestion_events(payload: dict[str, Any]) -> list[Any]:
    """Map saved observations to supported v3 SDK types without guessing timing."""
    from langfuse.api.ingestion.types.create_generation_body import CreateGenerationBody
    from langfuse.api.ingestion.types.ingestion_event import (
        IngestionEvent_GenerationCreate, IngestionEvent_ObservationCreate,
        IngestionEvent_ScoreCreate, IngestionEvent_TraceCreate,
    )
    from langfuse.api.ingestion.types.observation_body import ObservationBody
    from langfuse.api.ingestion.types.score_body import ScoreBody
    from langfuse.api.ingestion.types.trace_body import TraceBody

    exported_at = datetime.now(timezone.utc)
    trace_id = payload["trace_id"]
    events = []

    def add(kind: Any, body: Any) -> None:
        events.append(kind(id=_hash("ingestion", body.model_dump_json(by_alias=True, exclude_none=True)),
                           timestamp=exported_at.isoformat(), body=body))

    def parsed_time(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return result if result.utcoffset() is not None else None

    def emit(item: dict[str, Any], path: str, parent: str | None) -> tuple[datetime, datetime]:
        observation_id = _hash("full-observation", f"{trace_id}:{path}")[:16]
        nested_times = [emit(child, f"{path}/{index}", observation_id)
                        for index, child in enumerate(item.get("children", []))]
        metadata = dict(item["metadata"])
        metadata["workflow_observation_type"] = item["as_type"]
        start, end = parsed_time(metadata.get("started_at")), parsed_time(metadata.get("finished_at"))
        if start and end and start <= end:
            timing = "recorded provider UTC interval; monotonic latency remains authoritative duration"
        elif nested_times:
            start, end = min(pair[0] for pair in nested_times), max(pair[1] for pair in nested_times)
            timing = "envelope of displayed child observations; missing child times may be export time"
        elif isinstance(metadata.get("recorded_completion_timestamp"), (float, int)):
            start = end = datetime.fromtimestamp(metadata["recorded_completion_timestamp"], timezone.utc)
            timing = "recorded completion instant only; operation duration unknown"
        else:
            start = end = exported_at
            timing = "export-time placeholder; original execution timestamps unavailable or invalid"
        metadata["displayed_timing"] = timing
        args = {key: item[key] for key in ("name", "input", "output") if key in item}
        args.update(id=observation_id, trace_id=trace_id, parent_observation_id=parent,
                    start_time=start, end_time=end, metadata=metadata,
                    level="ERROR" if metadata.get("status") not in {None, "completed"} else "DEFAULT")
        if item["as_type"] == "generation":
            add(IngestionEvent_GenerationCreate, CreateGenerationBody(**args, usage_details=item["usage_details"]))
        else:
            # v3's ingestion endpoint accepts SPAN/GENERATION/EVENT only, even
            # though the v4 Python SDK exposes newer observation enum members.
            add(IngestionEvent_ObservationCreate, ObservationBody(**args, type="SPAN"))
        return start, end

    top = {"name": payload["name"], "as_type": "span", "metadata": payload["metadata"],
           "input": payload["input"], "output": payload["output"], "children": payload["roles"]}
    start, _ = emit(top, "root", None)
    add(IngestionEvent_TraceCreate, TraceBody(id=trace_id, name=payload["name"], timestamp=start,
                                            input=payload["input"], output=payload["output"],
                                            metadata=payload["metadata"], public=False))
    add(IngestionEvent_ScoreCreate, ScoreBody(
        id=_hash("full-score", trace_id), trace_id=trace_id, name="sdbench.diagnosis_score",
        value=payload["score"], data_type="NUMERIC",
        comment="Saved released-judge score; exported without regrading. Range 1–5; correct threshold 4.",
    ))
    return events


def send_metadata_export(payload: dict[str, Any], *, client: Any = None) -> dict[str, Any]:
    """Submit a payload created by build_metadata_export with the official SDK.

    Flush is not a server acknowledgement. Re-exporting appends observations to
    the same trace; review the dry-run file and send it once. This function is for
    an in-memory payload from this module, never an arbitrary uploaded JSON file.
    """
    owns_client = client is None
    if owns_client:
        client = _configured_client()
    trace_id = payload["trace_id"]
    observation = client.start_observation(
        name=payload["name"], as_type="span",
        trace_context={"trace_id": trace_id, "parent_span_id": _hash("parent", trace_id)[:16]},
        metadata=payload["metadata"],
    )
    try:
        for generation in payload["generations"]:
            child = observation.start_observation(
                name=generation["name"], as_type="generation",
                metadata=generation["metadata"],
                usage_details=generation["usage_details"] or None,
                level="ERROR" if generation["metadata"]["status"] != "completed" else "DEFAULT",
            )
            child.end()
    finally:
        observation.end()
        client.flush()
        if owns_client:
            client.shutdown()
    return {
        "trace_id": trace_id, "api_attempts": len(payload["generations"]),
        "transport_status": "submitted-to-sdk-and-flushed",
        "server_acceptance_verified": False,
        "content_scope": "allowlisted-api-metadata-only",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New local JSON preview; never overwritten")
    parser.add_argument("--content", choices=("metadata", "full"), default="metadata")
    parser.add_argument("--case", type=Path, help="Frozen judge case; required only for full clinical export")
    parser.add_argument("--send", action="store_true", help="Explicitly submit the preview to the configured Langfuse endpoint")
    args = parser.parse_args(argv)
    if (args.content == "full") != (args.case is not None):
        parser.error("Use --case only with --content full; full export requires its frozen case")
    full = args.content == "full"
    payload = build_full_export(args.trial_dir, args.case) if full else build_metadata_export(args.trial_dir)
    write_json(args.output, payload)
    send = send_full_export if full else send_metadata_export
    receipt = send(payload) if args.send else {
        "trace_id": payload["trace_id"], "api_attempts": payload["metadata"]["api_attempts"],
        "transport_status": "dry-run; no network calls",
    }
    if args.send:
        write_json(args.output.with_suffix(".receipt.json"), receipt)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
