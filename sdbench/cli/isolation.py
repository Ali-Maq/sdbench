"""Prepare and summarize paired Harbor experiments; preparation makes no API calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from sdbench import case_data
from sdbench.isolation.accounting import summarize_usage
from sdbench.isolation.common import canonical_hash, write_json
from sdbench.isolation.experiment import ExperimentConfig, SCIENCE_PROMPTS, analyze_trials, build_trial_configs


def source_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.suffix in {".py", ".yaml", ".txt"} and "_bundled_data" not in path.parts
    }
    return canonical_hash(hashes)


def prepare_recorded(args: argparse.Namespace) -> None:
    """Freeze one local recorded-evidence episode; never call a model here."""
    from sdbench.harbor_integration.tasks import build_task
    from sdbench.isolation.recorded import (
        SELECTOR_PROMPT_VERSION, gatekeeper_case, validate_recorded_case,
    )

    os.environ["SDBENCH_LOCAL_DIR"] = "disable"
    case = json.loads(args.case.read_text())
    validate_recorded_case(case)
    if min(args.max_turns, args.max_model_calls, args.max_completion_tokens) < 1:
        raise ValueError("Turn, call, and token limits must be positive")
    models = {role: getattr(args, f"{role}_model") for role in ("diagnosis", "gatekeeper", "judge")}
    if any(not model.strip() for model in models.values()):
        raise ValueError("Every role needs an explicit nonempty model")
    route = {"provider": args.provider, "api_transport": "chat_completions"}
    if args.provider == "azure":
        from sdbench.isolation.providers import validate_azure_base_url
        route.update(provider_base_url=validate_azure_base_url(args.base_url), api_transport="responses")
    elif args.base_url:
        raise ValueError("--base-url is only used with --provider azure")
    package = Path(__file__).resolve().parents[1]
    prompt = package / "prompts/check_diagnosis/v3_reasoning/prompt.yaml"
    code_hash = source_hash()
    model_args = {"max_completion_tokens": args.max_completion_tokens}
    if args.reasoning_effort:
        model_args["reasoning_effort"] = args.reasoning_effort
    protocol = {
        "schema_version": 1, "experiment_kind": "recorded_episode",
        "dataset": case["dataset"], "case_id": case["case_id"],
        "case_hash": canonical_hash(case), "gatekeeper_case_hash": canonical_hash(gatekeeper_case(case)),
        "code_hash": code_hash, "gatekeeper_mode": "recorded_only",
        "gk_prompt": SELECTOR_PROMPT_VERSION, "judge_prompt": "v3_reasoning",
        "prompt_hashes": {str(prompt.relative_to(package)): hashlib.sha256(prompt.read_bytes()).hexdigest()},
        "diagnostic_agent": "baseline", "api_mode": args.api_mode,
        "max_turns": args.max_turns, "max_model_calls": args.max_model_calls,
        "max_questions_per_turn": None, "max_tests_per_turn": None,
        "rate_cards": {}, "repetition": 0, **route,
    }
    for role, model in models.items():
        protocol[f"{role}_model"] = model
        protocol[f"{role}_model_args"] = dict(model_args)
    protocol_hash = canonical_hash(protocol)
    trial = {**protocol, "protocol_hash": protocol_hash, "pair_id": protocol_hash,
             "trial_id": canonical_hash({"protocol_hash": protocol_hash, "case_id": case["case_id"]}),
             "execution_index": 0}
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Neutral directory names also keep diagnosis container paths free of cohort hints.
    task = build_task(case, trial, destination / "tasks" / "episode-0001")
    if source_hash() != code_hash:
        raise ValueError("Source changed during preparation; prepare again in a new directory")
    write_json(destination / "manifest.json", {
        "schema_version": 1, "experiment_kind": "recorded_episode", "trials": [trial],
        "task_paths": [str(task)], "api_mode": args.api_mode,
        "isolation": "docker-containers-shared-kernel",
        "interpretation": "One retrospective recorded-evidence episode; not MAI-DxO, an as-of replay, a paired ablation or a population estimate. Mock calls test execution only. Clinical costs are not calculated; API invoice cost is unknown.",
    })
    write_json(destination / "job.json", {
        "job_name": destination.name, "jobs_dir": str(args.jobs.resolve()),
        "n_attempts": 1, "n_concurrent_trials": 1,
        "environment": {"type": "docker"},
        "agents": [{"import_path": "sdbench.harbor_integration.agent:SDBenchAgent", "model_name": models["diagnosis"]}],
        "datasets": [{"path": str(destination / "tasks")}],
    })
    print(json.dumps({"job": str(destination / "job.json"), "trials": 1,
                      "experiment_kind": "recorded_episode", "api_mode": args.api_mode}, indent=2))


def prepare_smoke(args: argparse.Namespace) -> None:
    """One synthetic live encounter, explicitly separate from the paired experiment."""
    from sdbench.harbor_integration.tasks import build_task

    os.environ["SDBENCH_LOCAL_DIR"] = "disable"
    if min(args.max_turns, args.max_model_calls, args.max_completion_tokens) < 1:
        raise ValueError("Turn, call, and token limits must be positive")
    if not args.model.strip():
        raise ValueError("model must be nonempty")
    from sdbench.isolation.providers import validate_azure_base_url
    endpoint = validate_azure_base_url(args.base_url)
    package = Path(__file__).resolve().parents[1]
    shipped_data = package / "_bundled_data"
    if not shipped_data.is_dir():
        shipped_data = package.parent / "data"
    if not (shipped_data / "processed_case_data/synthetic").is_dir():
        raise ValueError("The packaged or source-checkout synthetic dataset is missing")
    os.environ["SDBENCH_DATA_PATH"] = str(shipped_data)
    case_data._get_case_sets.cache_clear()
    case_data._get_case_lookup.cache_clear()
    cases = case_data.get_case_set("synthetic", "all")
    case = next((case for case in cases if case["case_id"] == args.case_id), None)
    if case is None:
        raise ValueError("case-id must identify a bundled synthetic case")
    model_args = {"max_completion_tokens": args.max_completion_tokens,
                  "reasoning_effort": args.reasoning_effort}
    gk_prompt, judge_prompt = SCIENCE_PROMPTS["synthetic"]
    paths = [package / "prompts/gatekeeper_synthetic_tests" / gk_prompt / "prompt.yaml",
             package / "prompts/check_diagnosis" / judge_prompt / "prompt.yaml"]
    code_hash = source_hash()
    protocol = {
        "schema_version": 1, "experiment_kind": "single_model_smoke",
        "dataset": "synthetic", "case_set": "all", "case_id": case["case_id"],
        "case_hash": canonical_hash(case), "code_hash": code_hash,
        "prompt_hashes": {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        "gk_prompt": gk_prompt, "judge_prompt": judge_prompt,
        "diagnostic_agent": "baseline",
        "diagnosis_model": args.model, "gatekeeper_model": args.model, "judge_model": args.model,
        "diagnosis_model_args": model_args, "gatekeeper_model_args": model_args, "judge_model_args": model_args,
        "provider": "azure", "provider_base_url": endpoint, "api_transport": "responses",
        "api_mode": "live", "max_turns": args.max_turns, "max_model_calls": args.max_model_calls,
        "max_questions_per_turn": None, "max_tests_per_turn": None,
        "rate_cards": {}, "repetition": 0,
    }
    protocol_hash = canonical_hash(protocol)
    trial = {**protocol, "protocol_hash": protocol_hash, "pair_id": protocol_hash,
             "trial_id": canonical_hash({"protocol_hash": protocol_hash, "case_id": case["case_id"]}),
             "execution_index": 0}
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    task = build_task(case, trial, destination / "tasks" / f"synthetic-{case['case_id']}")
    if source_hash() != code_hash:
        raise ValueError("Source changed during preparation; prepare again in a new directory")
    write_json(destination / "manifest.json", {
        "schema_version": 1, "experiment_kind": "single_model_smoke", "trials": [trial],
        "task_paths": [str(task)], "api_mode": "live",
        "isolation": "docker-containers-shared-kernel",
        "interpretation": "One synthetic smoke test; not an o4-mini/GPT-5 ablation or clinical validation. No clinical-cost calculation; API invoice cost is unknown without Azure billing rates.",
    })
    write_json(destination / "job.json", {
        "job_name": destination.name, "jobs_dir": str(args.jobs.resolve()),
        "n_attempts": 1, "n_concurrent_trials": 1,
        "environment": {"type": "docker"},
        "agents": [{"import_path": "sdbench.harbor_integration.agent:SDBenchAgent", "model_name": args.model}],
        "datasets": [{"path": str(destination / "tasks")}],
    })
    print(json.dumps({"job": str(destination / "job.json"), "trials": 1,
                      "experiment_kind": "single_model_smoke", "model": args.model}, indent=2))


def prepare(args: argparse.Namespace) -> None:
    from sdbench.harbor_integration.tasks import build_task

    os.environ["SDBENCH_LOCAL_DIR"] = "disable"
    if args.concurrency < 1 or args.max_completion_tokens < 1:
        raise ValueError("concurrency and max_completion_tokens must be positive")
    rate_cards: dict[str, Any] = {}
    if args.rate_cards:
        from sdbench.isolation.accounting import RateCard
        rate_cards = json.loads(args.rate_cards.read_text())
        if not isinstance(rate_cards, dict):
            raise ValueError("Rate cards must be a JSON object keyed by exact model identifier")
        for rates in rate_cards.values():
            RateCard(**rates)
    cases = case_data.get_case_set(args.dataset, args.case_set)
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case["case_id"] in selected]
        if {case["case_id"] for case in cases} != selected:
            raise ValueError("Requested case IDs must belong to the selected dataset/split")
    model_args = {"max_completion_tokens": args.max_completion_tokens}
    if args.reasoning_effort:
        model_args["reasoning_effort"] = args.reasoning_effort
    config = ExperimentConfig(
        dataset=args.dataset, case_set=args.case_set,
        case_ids=tuple(case["case_id"] for case in cases),
        diagnosis_model=args.diagnosis_model, repetitions=args.repetitions,
        max_turns=args.max_turns, max_model_calls=args.max_model_calls,
        api_mode=args.api_mode, seed=args.seed,
        diagnosis_model_args=model_args, gatekeeper_model_args=model_args,
        judge_model_args=model_args, rate_cards=rate_cards,
    )
    package = Path(__file__).resolve().parents[1]
    prompt_paths = [
        package / "prompts" / "gatekeeper_synthetic_tests" / config.gk_prompt / "prompt.yaml",
        package / "prompts" / "check_diagnosis" / config.judge_prompt / "prompt.yaml",
    ]
    code_hash = source_hash()
    trials = build_trial_configs(
        config, code_hash=code_hash,
        prompt_hashes={path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in prompt_paths},
        case_hashes={case["case_id"]: canonical_hash(case) for case in cases},
    )
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    lookup = {case["case_id"]: case for case in cases}
    paths = []
    for trial in trials:
        path = destination / "tasks" / f"{trial['execution_index']:04d}-{trial['trial_id'][:16]}"
        build_task(lookup[trial["case_id"]], trial, path)
        paths.append(str(path))
    if source_hash() != code_hash:
        raise ValueError("Source changed while preparing tasks; discard this incomplete preparation and retry")
    manifest = {
        "schema_version": 1, "config": config.as_dict(), "trials": trials,
        "task_paths": paths, "api_mode": args.api_mode,
        "isolation": "docker-containers-shared-kernel",
        "interpretation": "Mock execution checks plumbing only; it is not clinical performance." if args.api_mode == "mock" else "Controlled evaluation; API charges are incurred when run.",
    }
    write_json(destination / "manifest.json", manifest)
    write_json(destination / "job.json", {
        "job_name": destination.name,
        "jobs_dir": str(destination / "jobs"),
        "n_attempts": 1,
        "n_concurrent_trials": args.concurrency,
        "environment": {"type": "docker"},
        "agents": [{"import_path": "sdbench.harbor_integration.agent:SDBenchAgent"}],
        "datasets": [{"path": str(destination / "tasks")}],
    })
    print(json.dumps({"manifest": str(destination / "manifest.json"), "job": str(destination / "job.json"), "trials": len(trials), "api_mode": args.api_mode}, indent=2))


def summarize(args: argparse.Namespace) -> None:
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("experiment_kind") == "single_model_smoke":
        raise ValueError("This is a single-model smoke run, not a paired experiment; inspect it in harbor view and use its protected per-role usage ledgers")
    if manifest.get("experiment_kind") == "recorded_episode":
        raise ValueError("This is a single recorded episode, not a paired experiment; use the completed trial report and per-role usage ledgers")
    records = []
    usage = []
    expected_ids = {trial["trial_id"] for trial in manifest["trials"]}
    for path in sorted(args.jobs.rglob("result.json")):
        # Harbor also writes result.json at job and trial level; only our verifier
        # file has an authoritative clinical trial_id/status record.
        relative_parts = path.relative_to(args.jobs).parts
        if path.parent.name != "verifier" or "artifacts" in relative_parts[:-2] or "agent" in relative_parts[:-2]:
            continue
        record = json.loads(path.read_text())
        if record.get("trial_id") in expected_ids:
            records.append(record)
    seen_calls: dict[str, dict[str, Any]] = {}
    for pattern in ("usage.jsonl", "judge_api_calls.jsonl"):
        for path in sorted(args.jobs.rglob(pattern)):
            parts = path.relative_to(args.jobs).parts
            trusted_gatekeeper = (
                parts[-3:] == ("artifacts", "gatekeeper-evidence", "usage.jsonl")
                and "artifacts" not in parts[:-3] and "agent" not in parts[:-3]
            )
            trusted_judge = (
                parts[-2:] == ("verifier", "judge_api_calls.jsonl")
                and "artifacts" not in parts[:-2] and "agent" not in parts[:-2]
            )
            if not (trusted_gatekeeper or trusted_judge):
                continue
            # Sidecar evidence may be copied more than once by Harbor. A call ID
            # identifies the same immutable record, not a second billed request.
            for line in path.read_text().splitlines():
                entry = json.loads(line)
                if entry.get("trial_id") not in expected_ids:
                    continue
                call_id = entry.get("call_id")
                if not call_id:
                    raise ValueError("Usage entry has no call_id")
                if call_id in seen_calls and seen_calls[call_id] != entry:
                    raise ValueError("Conflicting copies of an API usage record")
                if call_id not in seen_calls:
                    seen_calls[call_id] = entry
                    usage.append(entry)
    report = analyze_trials(records, expected_trials=manifest["trials"])
    report.update({"api_mode": manifest["api_mode"], "usage": summarize_usage(usage),
                   "isolation": manifest["isolation"], "interpretation": manifest["interpretation"]})
    trials_with_usage = {entry["trial_id"] for entry in usage}
    completed_ids = {record["trial_id"] for record in records if record.get("status") == "completed"}
    record_lookup = {record["trial_id"]: record for record in records}
    missing_roles = {}
    for trial_id in completed_ids:
        roles = {entry["role"] for entry in usage if entry["trial_id"] == trial_id}
        required_roles = {"diagnosis", "judge"}
        if record_lookup[trial_id].get("test_logs"):
            required_roles.add("gatekeeper")
        if not required_roles <= roles:
            missing_roles[trial_id] = sorted(required_roles - roles)
    report["usage_coverage"] = {
        "expected_trials": len(expected_ids), "trials_with_usage": len(trials_with_usage),
        "trials_without_usage": sorted(expected_ids - trials_with_usage),
        "missing_roles_on_completed_trials": missing_roles,
    }
    if manifest["api_mode"] == "live" and (completed_ids != expected_ids or missing_roles or trials_with_usage != expected_ids):
        report["usage"]["estimated_cost_usd"] = None
        report["usage"]["incomplete_evidence"] = True
    trial_lookup = {trial["trial_id"]: trial for trial in manifest["trials"]}
    report["usage_by_gatekeeper"] = {
        model: summarize_usage([entry for entry in usage if trial_lookup[entry["trial_id"]]["gatekeeper_model"] == model])
        for model in ("o4-mini", "gpt-5")
    }
    if report["usage"].get("incomplete_evidence"):
        for arm_usage in report["usage_by_gatekeeper"].values():
            arm_usage["estimated_cost_usd"] = None
            arm_usage["incomplete_evidence"] = True
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recorded = commands.add_parser("prepare-recorded", help="Prepare a private recorded-only episode; no API calls")
    recorded.add_argument("--case", type=Path, required=True)
    for role in ("diagnosis", "gatekeeper", "judge"):
        recorded.add_argument(f"--{role}-model", required=True)
    recorded.add_argument("--api-mode", choices=["mock", "live"], default="mock")
    recorded.add_argument("--provider", choices=["openai", "azure"], default="openai")
    recorded.add_argument("--base-url")
    recorded.add_argument("--max-turns", type=int, default=10)
    recorded.add_argument("--max-model-calls", type=int, default=24)
    recorded.add_argument("--max-completion-tokens", type=int, default=4096)
    recorded.add_argument("--reasoning-effort", choices=["low", "medium", "high"])
    recorded.add_argument("--jobs", type=Path, required=True)
    recorded.add_argument("--output", type=Path, required=True)
    recorded.set_defaults(func=prepare_recorded)
    smoke = commands.add_parser("prepare-smoke", help="Prepare one live Azure synthetic encounter for the native Harbor viewer")
    smoke.add_argument("--model", required=True)
    smoke.add_argument("--base-url", required=True)
    smoke.add_argument("--case-id", default="0")
    smoke.add_argument("--max-turns", type=int, default=5)
    smoke.add_argument("--max-model-calls", type=int, default=8)
    smoke.add_argument("--max-completion-tokens", type=int, default=4096)
    smoke.add_argument("--reasoning-effort", choices=["low", "medium", "high"], default="medium")
    smoke.add_argument("--jobs", type=Path, required=True)
    smoke.add_argument("--output", type=Path, required=True)
    smoke.set_defaults(func=prepare_smoke)
    plan = commands.add_parser("prepare", help="Build private tasks and a frozen paired manifest; no API calls")
    plan.add_argument("--dataset", choices=["synthetic", "nejm", "mimic"], default="synthetic")
    plan.add_argument("--case-set", default="all")
    plan.add_argument("--case-id", action="append")
    plan.add_argument("--diagnosis-model", default="gpt-5")
    plan.add_argument("--repetitions", type=int, default=1)
    plan.add_argument("--max-turns", type=int, default=70)
    plan.add_argument("--max-model-calls", type=int, default=150)
    plan.add_argument("--max-completion-tokens", type=int, default=4096)
    plan.add_argument("--reasoning-effort", choices=["low", "medium", "high"])
    plan.add_argument("--api-mode", choices=["mock", "live"], default="mock")
    plan.add_argument("--seed", type=int, default=0)
    plan.add_argument("--concurrency", type=int, default=2)
    plan.add_argument("--rate-cards", type=Path)
    plan.add_argument("--output", type=Path, required=True)
    plan.set_defaults(func=prepare)
    report = commands.add_parser("summarize", help="Analyze outcomes and usage against the expected paired schedule")
    report.add_argument("--manifest", type=Path, required=True)
    report.add_argument("--jobs", type=Path, required=True)
    report.add_argument("--output", type=Path)
    report.set_defaults(func=summarize)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, FileExistsError, FileNotFoundError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
