"""Host-only export of completed isolated trials using SDBench's HTML renderer.

Run after Harbor has stopped the diagnosis environment. Derived files live in
the trial's artifacts/clinical-report directory and are never uploaded to main.
Original evidence and raw call ledgers are read without modification.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
import os
from pathlib import Path
from typing import Any

from sdbench.config import HarnessConfig
from sdbench.isolation.accounting import normalize_usage
from sdbench.isolation.common import canonical_hash, write_json
from sdbench.reporting import generate_html_overview
from sdbench.results import is_completed_result

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


def _write_private(path: Path, data: bytes) -> None:
    """Create an auditor artifact without making its contents world-readable."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _recorded_evidence(
    case: dict[str, Any], verified: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Derive an auditor view from the case bound to the completed verifier."""
    if "recorded_episode" not in case:
        return None
    from sdbench.isolation.recorded import validate_recorded_case, validate_recorded_observation

    validate_recorded_case(case)
    if (verified.get("gatekeeper_mode") != "recorded_only"
            or verified.get("recorded_evidence_integrity") != "validated"):
        raise ValueError("Recorded-only report requires validated recorded evidence")
    observations = []
    for log in verified.get("test_logs", []):
        observation = {name: log[name] for name in (
            "test_name", "is_synthetic", "evidence_status", "evidence_ids", "source_refs"
        ) if name in log}
        observation["results"] = log.get("result")
        validate_recorded_observation(case, observation)
        observations.append(observation)
    saved_observations = [result for event in events if event.get("event") == "tests"
                          for result in event.get("test_results", [])]
    if observations != saved_observations:
        raise ValueError("Recorded report observations differ from the hashed service evidence")
    episode = case["recorded_episode"]
    return {
        "kind": "derived-recorded-evidence-report", "authoritative": False,
        "trial_id": verified["trial_id"], "case_hash": verified["case_hash"],
        "evidence_hash": verified["evidence_hash"],
        "gatekeeper_mode": "recorded_only", "recorded_evidence_integrity": "validated",
        "api_mode": verified["api_mode"], "mocked": verified["mocked"],
        "opening": {"text": case["case_summary"], "source_ids": episode["opening_source_ids"]},
        "policy": {"mode": episode["mode"], "evidence_cutoff_day": episode["evidence_cutoff_day"]},
        "allowed_evidence": episode["evidence"],
        "reference": {"diagnosis": case["final_diagnosis"], "expert_record": case["full_text"],
                      "source_ids": episode["reference_source_ids"], "day": episode["reference_day"]},
        "observations": observations,
    }


def _evidence_html(evidence: dict[str, Any]) -> str:
    """Render a small escaped source appendix, not a second run dashboard."""
    def escape(value: Any) -> str:
        return html.escape(str(value))

    def quoted(value: str) -> str:
        return f"<pre>{escape(value)}</pre>"

    rows = []
    for item in evidence["allowed_evidence"]:
        rows.append(
            f'<tr><td>{escape(item["id"])}<br>{escape(item["kind"])}</td>'
            f'<td>{escape(item["label"])}{quoted(item["text"])}</td>'
            f'<td>{escape(item["source_id"])}<br><code>{escape(item["source_sha256"])}</code></td>'
            f'<td>{item["event_day"]:+d}</td><td>{item["recorded_day"]:+d}</td></tr>'
        )
    responses = []
    for observation in evidence["observations"]:
        ids = ", ".join(observation["evidence_ids"]) or "none"
        responses.append(
            f'<li><strong>{escape(observation["test_name"])}</strong>'
            f'<p>Status: {escape(observation["evidence_status"])}; selected evidence IDs: {escape(ids)}.</p>'
            f'{quoted(observation["results"])}</li>'
        )
    opening = evidence["opening"]
    reference = evidence["reference"]
    mode = "MOCKED model calls: scores are scripted execution checks, not clinical performance." if evidence["mocked"] else "LIVE model calls: one encounter is not a population accuracy estimate."
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Recorded-only source appendix</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid #bbb;padding:.6rem;text-align:left;vertical-align:top}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}code{{overflow-wrap:anywhere;font-size:.8rem}}</style></head>
<body><p><a href="overview.html">Saved run report</a> · <a href="evidence.json">Source appendix JSON</a></p>
<h1>Recorded-only source appendix</h1>
<p>Private host-only auditor view. The full evidence catalog and judge reference are not the agent's opening.</p>
<p>{escape(mode)}</p>
<h2>Opening seen by the diagnostic agent</h2>{quoted(opening["text"])}
<p>Opening source IDs: {escape(', '.join(opening['source_ids']))}.</p>
<h2>Allowed evidence</h2>
<p>Retrospective request-based retrieval through event day {evidence['policy']['evidence_cutoff_day']:+d}.
Event dates and source-record dates are preserved separately. Neither is a verified result-release timestamp;
this is not a clock-accurate replay. Missing recorded findings return “not documented”.</p>
<table><thead><tr><th>Evidence ID / type</th><th>Finding</th><th>Source ID / declared SHA-256</th><th>Event day</th><th>Source-record day</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p>The verifier checks selected evidence IDs, source pointers and exact rendered text against the frozen episode.
The declared source digests identify the source files used during preparation; this export does not reopen those files,
verify excerpt selection, or prove that the chosen evidence is clinically relevant to a request.</p>
<h2>Judge-only expert reference</h2>
<p><strong>{escape(reference['diagnosis'])}</strong> · reference day {reference['day']:+d}.</p>
<p>Reference source IDs: {escape(', '.join(reference['source_ids']))}.</p>{quoted(reference['expert_record'])}
<h2>Actual evidence returned during the run</h2><ol>{''.join(responses)}</ol>
<p>This appendix is derived from the private case and completed verifier record. Original service events and raw model calls remain authoritative.</p>
</body></html>"""


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
    recorded = _recorded_evidence(case, verified, events)

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
    model_identities = {}
    for role in ("diagnosis", "gatekeeper", "judge"):
        reported = [(call.get("response") or {}).get("model") for call in calls if call["role"] == role]
        known = [model for model in reported if isinstance(model, str) and model.strip()]
        distinct = sorted(set(known))
        model_identities[role] = {
            "configured_model": verified[f"{role}_model"],
            "provider_returned_models": distinct,
            "attempts_without_model": len(reported) - len(known),
            "multiple_provider_models": len(distinct) > 1,
        }

    def token_total(role: str | None = None) -> int | None:
        values = [normalize_usage((call.get("response") or {}).get("usage"))["total_tokens"]
                  for call in calls if role is None or call["role"] == role]
        return sum(values) if values and all(value is not None for value in values) else None

    test_logs = [{**log, "messages": [{"role": "assistant", "content": log["result"]}],
                  "reason": (
                      "Requested by the diagnostic agent; verified evidence IDs: "
                      + (", ".join(log["evidence_ids"]) or "none (not documented)")
                      if recorded else "Requested by the diagnostic agent; saved service observation."
                  ),
                  "timestamp": "Not stored in the per-test verifier record"}
                 for log in verified.get("test_logs", [])]
    mapped = {
        "result_status": "success",
        "case_id": verified["case_id"], "dataset": verified["dataset"],
        "case_title": case.get("case_title", f"Case {verified['case_id']}"),
        "model_diagnosis": verified["diagnosis"], "actual_diagnosis": case["final_diagnosis"],
        "correct": verified["correct"], "diagnosis_score": verified["diagnosis_score"],
        "diagnostic_agent": verified.get("diagnostic_agent", "baseline"),
        "agent_messages": _messages(public["agent_messages"]),
        "check_diagnosis_messages": _messages(verified["judge"]["check_diagnosis_messages"]),
        "check_diagnosis_output": verified["judge"].get("check_diagnosis_output"),
        "gatekeeper_messages": [_call_conversation(call) for call in calls if call["role"] == "gatekeeper"],
        "test_logs": test_logs, "requested_tests": [log["test_name"] for log in test_logs],
        "api_mode": verified["api_mode"], "mocked": verified["mocked"],
        "api_actual_paid_usd": None, "clinical_workup_cost_usd": None,
        "export_metadata": {"authoritative": False, "source": "completed-isolated-trial",
                            "trial_id": verified["trial_id"], "api_attempts_by_role": dict(counts),
                            "model_identities": model_identities,
                            "all_role_tokens": token_total(), "diagnosis_tokens": token_total("diagnosis")},
    }
    if not is_completed_result(mapped):
        raise ValueError("Completed report requires consistent score flags and saved judge output")
    if recorded:
        mapped["export_metadata"]["recorded_evidence_integrity"] = "validated"
        mapped["export_metadata"]["source_appendix"] = "evidence.json"
    synthetic = not recorded and str(verified["dataset"]).lower() == "synthetic"
    mode = "MOCKED model calls" if verified["mocked"] else "LIVE model calls"
    label = (
        f"Recorded-only retrospective episode; dataset label: {verified['dataset']}; origin is not inferred from that label"
        if recorded else "SYNTHETIC development/demo case" if synthetic else f"{verified['dataset']} benchmark case"
    )
    reference_note = (
        "The patient and reference diagnosis belong to an authored fictional case. " if synthetic else ""
    )
    provenance_note = (
        "Recorded-only observations were validated against selected evidence IDs, source pointers and exact text in the frozen episode. "
        "See the source appendix for the opening, allowed evidence, reference and selected IDs; gatekeeper flags alone are not provenance. "
        if recorded else "Observation flags reproduce gatekeeper metadata; they do not verify that a result appears in the source record. "
    )
    mock_note = "Mock scores are scripted execution checks and do not measure diagnostic accuracy. " if verified["mocked"] else ""
    model_notice = "; ".join(
        f"{role}=[{', '.join(identity['provider_returned_models']) or 'unknown'}] "
        f"(attempts without model={identity['attempts_without_model']}; "
        f"multiple identities={'yes' if identity['multiple_provider_models'] else 'no'})"
        for role, identity in model_identities.items()
    )
    notice = (
        f"{label}; {mode}. One saved encounter, not a population accuracy estimate. "
        f"{reference_note}{mock_note}{provenance_note}"
        f"Configured models: diagnosis={verified['diagnosis_model']}, gatekeeper={verified['gatekeeper_model']}, "
        f"judge={verified['judge_model']}. Provider-returned model identities: {model_notice}. "
        "Missing model identities remain unknown; a configured alias is not a resolved model version. "
        "Recorded API attempts (including failed/incomplete attempts): "
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
            _write_private(audit_dir / name, data)
            names.append({"path": f"audit-calls/{name}", "sha256": _sha256(data), "bytes": len(data)})
        audit_manifest.append({"source": source, "line": line_number, "call_id": call.get("call_id"),
                               "role": call["role"], "status": call.get("status"),
                               "sha256": _sha256(line), "parts": names})
    write_json(destination / "result.json", mapped)
    config = HarnessConfig(dataset=verified["dataset"], agent="isolated " + verified.get("diagnostic_agent", "baseline"),
                           model=verified["diagnosis_model"], case_set="saved encounter",
                           azure_storage_enabled=False)
    html_path = Path(generate_html_overview([mapped], str(destination), config, report_notice=notice))
    html_path.chmod(0o600)
    appendix_manifest = {}
    if recorded:
        write_json(destination / "evidence.json", recorded)
        _write_private(destination / "evidence.html", _evidence_html(recorded).encode("utf-8"))
        navigation = ('<p><a href="evidence.html">Recorded-only source appendix: opening, allowed evidence, '
                      'judge reference and returned findings</a></p>')
        html_path.write_text(html_path.read_text().replace("<body>", "<body>\n" + navigation, 1))
        for name in ("evidence.json", "evidence.html"):
            data = (destination / name).read_bytes()
            appendix_manifest[name] = {"sha256": _sha256(data), "bytes": len(data)}
    manifest = {"kind": "derived-host-only-clinical-report", "authoritative": False,
                "sources": source_manifest, "calls": audit_manifest,
                "html_sha256": _sha256(html_path.read_bytes()),
                "renderer": "sdbench.reporting.generate_html_overview",
                "model_identities": model_identities,
                "clinical_workup_cost_usd": None, "api_actual_paid_usd": None}
    if appendix_manifest:
        manifest["source_appendix"] = appendix_manifest
    write_json(destination / "manifest.json", manifest)
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
