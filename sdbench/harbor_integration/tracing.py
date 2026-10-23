"""Export allowlisted API metadata from a finished Harbor trial to Langfuse.

This host-only, post-hoc exporter never reads raw calls, patient cases or agent
transcripts. Dry run is the default. Observation times describe the export;
recorded API latency is metadata, not a reconstructed execution waterfall.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sdbench.isolation.common import write_json


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


def _configured_client() -> Any:
    endpoint = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    if not endpoint:
        raise ValueError("Set LANGFUSE_BASE_URL or LANGFUSE_HOST explicitly before --send")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Langfuse endpoint must be an explicit HTTP(S) URL without credentials or query")
    if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
        raise ValueError("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY before --send")
    try:
        from langfuse import Langfuse
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as error:
        raise RuntimeError("Install the optional tracing extra to send metadata") from error
    return Langfuse(base_url=endpoint, debug=False, tracer_provider=TracerProvider())


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
    parser.add_argument("--send", action="store_true", help="Explicitly submit metadata to the configured Langfuse endpoint")
    args = parser.parse_args(argv)
    payload = build_metadata_export(args.trial_dir)
    write_json(args.output, payload)
    receipt = send_metadata_export(payload) if args.send else {
        "trace_id": payload["trace_id"], "api_attempts": len(payload["generations"]),
        "transport_status": "dry-run; no network calls",
    }
    if args.send:
        write_json(args.output.with_suffix(".receipt.json"), receipt)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
