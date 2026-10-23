"""Host-only export of completed isolated trials using SDBench's HTML renderer.

Run after Harbor has stopped the diagnosis environment. Derived files live in
the trial's artifacts/clinical-report directory and are never uploaded to main.
Original evidence and raw call ledgers are read without modification.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any

from sdbench.config import HarnessConfig
from sdbench.isolation.accounting import normalize_usage
from sdbench.isolation.common import canonical_hash
from sdbench.reporting import generate_html_overview

# Harbor 0.24's text-file endpoint allows at most 1 MiB per file.
AUDIT_PART_BYTES = 900_000


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not all(
        isinstance(item, dict) and item.get("role") in {"system", "user", "assistant", "tool", "developer"}
        and isinstance(item.get("content"), str) for item in value
    ):
        raise ValueError("Expected saved text messages with known roles")
    return [{"role": item["role"], "content": item["content"]} for item in value]


def _call_conversation(call: dict[str, Any]) -> list[dict[str, str]]:
    """Preserve visible provider text, with clearly separate audit annotations."""
    conversation = [{"role": "audit", "content": (
        f"Recorded {call['role']} attempt {call.get('call_id', '(no call id)')}; "
        f"status={call.get('status', 'unknown')}; "
        f"provider_status={call.get('provider_status', 'unknown')}. "
        "Audit annotations are exporter metadata, not model messages."
    )}, *_messages(call.get("messages"))]
    response = call.get("response") or {}
    visible_outputs = 0
    if response.get("object") == "response":
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue  # Never render hidden/encrypted reasoning as dialogue.
            texts = [part.get("text", part.get("refusal", "")) for part in item.get("content", [])
                     if part.get("type") in {"output_text", "refusal"}]
            if texts:
                conversation.extend([
                    {"role": "audit", "content": f"Assistant output phase: {item.get('phase', 'unspecified')}"},
                    {"role": "assistant", "content": "\n".join(texts)},
                ])
                visible_outputs += 1
    elif response.get("choices"):
        message = response["choices"][0].get("message", {})
        if isinstance(message.get("content"), str):
            conversation.append({"role": "assistant", "content": message["content"]})
            visible_outputs += 1
    if not visible_outputs:
        conversation.append({"role": "audit", "content": "No visible assistant text was recorded for this attempt."})
    return conversation


def _split_call(data: bytes) -> list[bytes]:
    """Losslessly split UTF-8 JSON at character boundaries for Harbor's viewer."""
    parts = []
    start = 0
    while start < len(data):
        end = min(start + AUDIT_PART_BYTES, len(data))
        while end < len(data) and data[end] & 0xC0 == 0x80:
            end -= 1
        parts.append(data[start:end])
        start = end
    return parts


def export_trial_report(trial_dir: Path, case_path: Path) -> Path:
    """Render one finished encounter, verifying case/evidence identity first.

    This export is an auditor view: it includes the private reference and role
    prompts. It is not an input to an agent or verifier and makes no API calls.
    """
    trial_dir = trial_dir.resolve()
    source_manifest: dict[str, dict[str, Any]] = {}

    def read(relative: str) -> bytes:
        path = trial_dir / relative
        if not path.resolve().is_relative_to(trial_dir):
            raise ValueError("Report source must remain inside its trial directory")
        data = path.read_bytes()
        source_manifest[relative] = {"sha256": _sha256(data), "bytes": len(data)}
        return data

    harbor_result = json.loads(read("result.json"))
    if not harbor_result.get("finished_at") or harbor_result.get("exception_info"):
        raise ValueError("Only successfully finished Harbor trials can be exported")
    verified = json.loads(read("verifier/result.json"))
    public = json.loads(read("agent/diagnosis.json"))
    if verified.get("status") != "completed" or public.get("status") != "completed":
        raise ValueError("A completed verifier and diagnosis transcript are required")
    case_bytes = case_path.read_bytes()
    case = json.loads(case_bytes)
    source_manifest["private_case"] = {"sha256": _sha256(case_bytes), "bytes": len(case_bytes)}
    if canonical_hash(case) != verified.get("case_hash") or str(case.get("case_id")) != str(verified.get("case_id")):
        raise ValueError("Private reference case does not match the verified trial")
    events = [json.loads(line) for line in read("artifacts/gatekeeper-evidence/events.jsonl").splitlines()]
    if canonical_hash(events) != verified.get("evidence_hash"):
        raise ValueError("Service evidence does not match the verified evidence hash")
    if public.get("diagnosis") != verified.get("diagnosis"):
        raise ValueError("Public diagnosis differs from the verified submission")

    raw_calls: list[tuple[str, int, bytes, dict[str, Any]]] = []
    for source in ("artifacts/gatekeeper-evidence/raw_calls.jsonl", "verifier/raw_calls.jsonl"):
        for line_number, line in enumerate(read(source).splitlines(keepends=True), 1):
            call = json.loads(line)
            if call.get("trial_id") != verified["trial_id"] or call.get("role") not in {"diagnosis", "gatekeeper", "judge"}:
                raise ValueError("Raw call identity does not match the verified trial")
            raw_calls.append((source, line_number, line, call))
    calls = [item[3] for item in raw_calls]
    counts = Counter(call["role"] for call in calls)
    if not counts["diagnosis"] or not counts["judge"]:
        raise ValueError("Diagnosis and judge call records are required")

    def token_total(role: str | None = None) -> int | None:
        values = [normalize_usage((call.get("response") or {}).get("usage"))["total_tokens"]
                  for call in calls if role is None or call["role"] == role]
        return sum(values) if values and all(value is not None for value in values) else None

    test_logs = [{**log, "messages": [{"role": "assistant", "content": log["result"]}],
                  "reason": "Requested by the diagnostic agent; saved service observation.",
                  "timestamp": "Not stored in the per-test verifier record"}
                 for log in verified.get("test_logs", [])]
    mapped = {
        "case_id": verified["case_id"], "dataset": verified["dataset"],
        "case_title": case.get("case_title", f"Case {verified['case_id']}"),
        "model_diagnosis": verified["diagnosis"], "actual_diagnosis": case["final_diagnosis"],
        "correct": verified["correct"], "diagnosis_score": verified["diagnosis_score"],
        "diagnostic_agent": verified.get("diagnostic_agent", "baseline"),
        "agent_messages": _messages(public["agent_messages"]),
        "check_diagnosis_messages": _messages(verified["judge"]["check_diagnosis_messages"]),
        "gatekeeper_messages": [_call_conversation(call) for call in calls if call["role"] == "gatekeeper"],
        "test_logs": test_logs, "requested_tests": [log["test_name"] for log in test_logs],
        "api_mode": verified["api_mode"], "mocked": verified["mocked"],
        "api_actual_paid_usd": None, "clinical_workup_cost_usd": None,
        "export_metadata": {"authoritative": False, "source": "completed-isolated-trial",
                            "trial_id": verified["trial_id"], "api_attempts_by_role": dict(counts),
                            "all_role_tokens": token_total(), "diagnosis_tokens": token_total("diagnosis")},
    }
    synthetic = str(verified["dataset"]).lower() == "synthetic"
    mode = "MOCKED model calls" if verified["mocked"] else "LIVE model calls"
    label = "SYNTHETIC development/demo case" if synthetic else f"{verified['dataset']} benchmark case"
    reference_note = (
        "The patient and reference diagnosis belong to an authored fictional case. " if synthetic else ""
    )
    notice = (
        f"{label}; {mode}. One saved encounter, not a population accuracy estimate. "
        f"{reference_note}Observation flags reproduce gatekeeper metadata; they do not verify that a result appears in the source record. "
        f"Models: diagnosis={verified['diagnosis_model']}, gatekeeper={verified['gatekeeper_model']}, "
        f"judge={verified['judge_model']}. Recorded API attempts (including failed/incomplete attempts): "
        f"diagnosis={counts['diagnosis']}, gatekeeper={counts['gatekeeper']}, judge={counts['judge']}. "
        f"Provider-reported tokens: all roles/attempts={token_total() if token_total() is not None else 'unknown'}, "
        f"diagnosis only={token_total('diagnosis') if token_total('diagnosis') is not None else 'unknown'}. "
        "Clinical workup costs: not computed. API dollars: unknown; no invoice or price estimate applied. "
        "Gatekeeper Dialogue includes every recorded attempt; Agent Dialogue is the diagnostic policy's visible transcript. "
        "This host-only audit report contains private reference material and never enters the diagnosis container."
    )

    destination = trial_dir / "artifacts" / "clinical-report"
    if destination.exists() or not destination.parent.resolve().is_relative_to(trial_dir):
        raise ValueError("Report destination already exists or escapes the trial")
    destination.mkdir(mode=0o700)
    audit_dir = destination / "audit-calls"
    audit_dir.mkdir(mode=0o700)
    audit_manifest = []
    for ordinal, (source, line_number, line, call) in enumerate(raw_calls, 1):
        parts = _split_call(line)
        names = []
        for part_number, data in enumerate(parts, 1):
            suffix = ".json" if len(parts) == 1 else f".part-{part_number:03d}.txt"
            name = f"{ordinal:03d}-{call['role']}" + suffix
            (audit_dir / name).write_bytes(data)
            names.append({"path": f"audit-calls/{name}", "sha256": _sha256(data), "bytes": len(data)})
        audit_manifest.append({"source": source, "line": line_number, "call_id": call.get("call_id"),
                               "role": call["role"], "status": call.get("status"),
                               "sha256": _sha256(line), "parts": names})
    (destination / "result.json").write_text(json.dumps(mapped, ensure_ascii=False, indent=2) + "\n")
    config = HarnessConfig(dataset=verified["dataset"], agent="isolated " + verified.get("diagnostic_agent", "baseline"),
                           model=verified["diagnosis_model"], case_set="saved encounter",
                           azure_storage_enabled=False)
    html_path = Path(generate_html_overview([mapped], str(destination), config, report_notice=notice))
    manifest = {"kind": "derived-host-only-clinical-report", "authoritative": False,
                "sources": source_manifest, "calls": audit_manifest,
                "html_sha256": _sha256(html_path.read_bytes()),
                "renderer": "sdbench.reporting.generate_html_overview",
                "clinical_workup_cost_usd": None, "api_actual_paid_usd": None}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return html_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    args = parser.parse_args(argv)
    print(export_trial_report(args.trial_dir, args.case))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
