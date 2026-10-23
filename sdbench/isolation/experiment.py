"""Immutable, paired gatekeeper experiments and case-level outcome analysis.

These helpers perform no model calls and never put clinical text in trial manifests.
The experimental unit is a case: repeated episodes do not become independent patients.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import json
import math
import random
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from sdbench.isolation.accounting import RateCard


GATEKEEPER_MODELS = ("o4-mini", "gpt-5")
# Pinned to the packaged Science configuration; public get_sdbench_config also
# executes ambient local overrides. A parity test detects upstream default drift
# without allowing those overrides to silently alter an immutable experiment.
# Synthetic fixtures deliberately use the NEJM versions only for smoke tests.
SCIENCE_PROMPTS = {
    "nejm": ("v6_fixed", "v3_reasoning"),
    "mimic": ("v13_mimic", "v4_reasoning_with_tests"),
    "synthetic": ("v6_fixed", "v3_reasoning"),
}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"Unsupported or non-finite protocol value: {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def canonical_hash(value: Any) -> str:
    """Hash canonical JSON; callers can hash source bytes separately if appropriate."""
    encoded = json.dumps(_plain(value), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def hash_case_data(case: Mapping[str, Any]) -> str:
    """Commit to the complete immutable case without exporting its contents."""
    return canonical_hash(case)


@dataclass(frozen=True)
class ExperimentConfig:
    dataset: str
    case_set: str
    case_ids: tuple[str, ...]
    diagnosis_model: str
    diagnostic_agent: str = "baseline"
    gk_models: tuple[str, str] = GATEKEEPER_MODELS
    judge_model: str = "gpt-5"
    gk_prompt: str | None = None
    judge_prompt: str | None = None
    repetitions: int = 1
    max_turns: int = 70
    max_model_calls: int = 150
    max_questions_per_turn: int | None = None
    max_tests_per_turn: int | None = None
    api_mode: str = "mock"
    seed: int = 0
    diagnosis_model_args: Mapping[str, Any] = field(default_factory=dict)
    gatekeeper_model_args: Mapping[str, Any] = field(default_factory=dict)
    judge_model_args: Mapping[str, Any] = field(default_factory=dict)
    rate_cards: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.diagnostic_agent not in {"baseline", "experimental_panel"}:
            raise ValueError("diagnostic_agent must be baseline or experimental_panel")
        if self.dataset not in SCIENCE_PROMPTS:
            raise ValueError("dataset must be nejm, mimic, or synthetic")
        if not self.case_set.strip() or not self.diagnosis_model.strip() or not self.judge_model.strip():
            raise ValueError("case_set and model names must be non-empty")
        case_ids = tuple(str(case_id) for case_id in self.case_ids)
        if not case_ids or any(not case_id.strip() for case_id in case_ids):
            raise ValueError("case_ids must contain non-empty identifiers")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("Duplicate case identifiers")
        object.__setattr__(self, "case_ids", tuple(sorted(case_ids)))
        if tuple(self.gk_models) != GATEKEEPER_MODELS:
            raise ValueError("gk_models must be ('o4-mini', 'gpt-5') in that order")
        object.__setattr__(self, "gk_models", GATEKEEPER_MODELS)
        for name in ("repetitions", "max_turns", "max_model_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_questions_per_turn", "max_tests_per_turn"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{name} must be None or a positive integer")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        if self.api_mode not in ("mock", "live"):
            raise ValueError("api_mode must be mock or live")
        for name, required in zip(("gk_prompt", "judge_prompt"), SCIENCE_PROMPTS[self.dataset]):
            supplied = getattr(self, name)
            if supplied is not None and supplied != required:
                raise ValueError(f"{name} must use the fixed {self.dataset} Science version {required}")
            object.__setattr__(self, name, required)
        for name in ("diagnosis_model_args", "gatekeeper_model_args", "judge_model_args"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} must be a mapping")
            object.__setattr__(self, name, _freeze(_plain(value)))
        if not isinstance(self.rate_cards, Mapping):
            raise ValueError("rate_cards must map model identifiers to dated rate cards")
        for model, rates in self.rate_cards.items():
            if not isinstance(model, str) or not model.strip() or not isinstance(rates, Mapping):
                raise ValueError("rate_cards must map model identifiers to dated rate cards")
            try:
                RateCard(**dict(rates))
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid rate card for model {model}") from error
        object.__setattr__(self, "rate_cards", _freeze(_plain(self.rate_cards)))

    def as_dict(self) -> dict[str, Any]:
        return {name: _plain(getattr(self, name)) for name in self.__dataclass_fields__}


def build_trial_configs(
    config: ExperimentConfig, *, code_hash: str,
    prompt_hashes: Mapping[str, str], case_hashes: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Create a reproducible interleaved schedule, with the same protocol hash in both arms.

    ``prompt_hashes`` identifies the pinned prompt files and their content hashes.
    ``case_hashes`` must correspond exactly to the selected case identifiers. Hashes,
    not clinical records, are included in the returned JSON-serializable manifests.
    """
    if not isinstance(code_hash, str) or not code_hash.strip():
        raise ValueError("code_hash is required")
    if not prompt_hashes or any(not isinstance(key, str) or not key for key in prompt_hashes):
        raise ValueError("prompt_hashes must identify the pinned prompt files")
    hashes = {str(key): value for key, value in case_hashes.items()}
    if len(hashes) != len(case_hashes) or set(hashes) != set(config.case_ids):
        raise ValueError("case_hashes must match selected cases exactly")
    for group in (prompt_hashes, hashes):
        if any(not isinstance(value, str) or len(value) != 64 or
               any(char not in "0123456789abcdef" for char in value) for value in group.values()):
            raise ValueError("Prompt and case hashes must be lowercase SHA-256 digests")
    protocol = {
        "schema_version": 1, "config": config.as_dict(), "code_hash": code_hash,
        "prompt_hashes": dict(prompt_hashes), "case_hashes": hashes,
    }
    protocol_hash = canonical_hash(protocol)
    settings = config.as_dict()
    settings.pop("case_ids")
    trials = []
    for case_id in config.case_ids:
        for repetition in range(config.repetitions):
            pair_id = canonical_hash({"protocol_hash": protocol_hash, "case_id": case_id,
                                      "repetition": repetition})
            for model in config.gk_models:
                trials.append({
                    **settings, "schema_version": 1, "protocol_hash": protocol_hash,
                    "code_hash": code_hash, "prompt_hashes": dict(prompt_hashes),
                    "case_id": case_id, "case_hash": hashes[case_id], "repetition": repetition,
                    "pair_id": pair_id, "gatekeeper_model": model,
                    "trial_id": canonical_hash({"pair_id": pair_id, "gatekeeper_model": model}),
                })
    random.Random(config.seed).shuffle(trials)
    for index, trial in enumerate(trials):
        trial["execution_index"] = index
    return trials


def _percentile(values: list[float], quantile: float) -> float:
    position = (len(values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def analyze_trials(
    records: Iterable[Mapping[str, Any]], *,
    expected_trials: Iterable[Mapping[str, Any]] | None = None,
    bootstrap_samples: int = 2000, seed: int = 0,
) -> dict[str, Any]:
    """Analyze paired outcomes, retaining failed/missing attempts in primary accuracy.

    Completed records require ``status='completed'`` and a boolean ``correct``.
    Supply the frozen expected schedule to detect episodes absent from the output.
    Without it, only pairs visible in the records can be assessed. Audit findings are
    reported only when an explicit boolean ``audit.leakage`` is supplied.
    """
    if isinstance(bootstrap_samples, bool) or not isinstance(bootstrap_samples, int) or bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be a positive integer")
    observations = [dict(record) for record in records]
    schedule = None if expected_trials is None else [dict(trial) for trial in expected_trials]
    identity_fields = ("trial_id", "pair_id", "dataset", "protocol_hash", "case_id", "repetition", "gatekeeper_model")

    def index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        indexed = {}
        for item in items:
            if any(key not in item for key in identity_fields):
                raise ValueError("Trial is missing identity fields")
            if any(not isinstance(item[key], str) or not item[key] for key in identity_fields if key != "repetition"):
                raise ValueError("Trial identity strings must be non-empty")
            if isinstance(item["repetition"], bool) or not isinstance(item["repetition"], int) or item["repetition"] < 0:
                raise ValueError("repetition must be a non-negative integer")
            if item["gatekeeper_model"] not in GATEKEEPER_MODELS:
                raise ValueError("Unexpected gatekeeper arm")
            if item["trial_id"] in indexed:
                raise ValueError(f"Duplicate trial_id: {item['trial_id']}")
            indexed[item["trial_id"]] = item
        return indexed

    observed = index(observations)
    expected = index(schedule) if schedule is not None else observed
    if schedule is not None:
        if set(observed) - set(expected):
            raise ValueError("Observed trial not present in the expected schedule")
        for trial_id, record in observed.items():
            for key in (*identity_fields, "case_hash"):
                if key in expected[trial_id] and record.get(key) != expected[trial_id][key]:
                    raise ValueError(f"Observed trial differs from manifest: {key}")

    groups: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    protocols: dict[str, set[str]] = defaultdict(set)
    identities: dict[tuple[str, str, int], str] = {}
    for trial in expected.values():
        dataset, pair_id, model = trial["dataset"], trial["pair_id"], trial["gatekeeper_model"]
        protocols[dataset].add(trial["protocol_hash"])
        if len(protocols[dataset]) > 1:
            raise ValueError("Cannot pool different protocols within a dataset")
        identity = (dataset, trial["case_id"], trial["repetition"])
        if identity in identities and identities[identity] != pair_id:
            raise ValueError("Duplicate case/repetition represented by different pairs")
        identities[identity] = pair_id
        pair = groups[dataset].setdefault(pair_id, {"case_id": trial["case_id"], "repetition": trial["repetition"], "arms": {}})
        if pair["case_id"] != trial["case_id"] or pair["repetition"] != trial["repetition"]:
            raise ValueError("Pair contains different cases or repetitions")
        if model in pair["arms"]:
            raise ValueError("Duplicate arm within a pair")
        pair["arms"][model] = trial
    if schedule is not None and any(set(pair["arms"]) != set(GATEKEEPER_MODELS)
                                    for pairs in groups.values() for pair in pairs.values()):
        raise ValueError("Expected schedule must contain both arms for every pair")

    rng = random.Random(seed)
    datasets = {}
    for dataset, pairs in sorted(groups.items()):
        arms = {}
        case_differences: dict[str, list[float]] = defaultdict(list)
        missing_pairs = []
        complete_pairs = 0
        for model in GATEKEEPER_MODELS:
            arms[model] = {"attempted_episodes": len(pairs), "observed_episodes": 0,
                           "completed_episodes": 0, "correct_episodes": 0, "missing_episodes": 0,
                           "audited_episodes": 0, "leakage_episodes": 0}
        for pair_id, pair in sorted(pairs.items()):
            outcomes, completion = {}, []
            missing = []
            for model in GATEKEEPER_MODELS:
                trial = pair["arms"].get(model)
                record = observed.get(trial["trial_id"]) if trial else None
                stats = arms[model]
                if record is None:
                    stats["missing_episodes"] += 1
                    missing.append(model)
                else:
                    stats["observed_episodes"] += 1
                completed = record is not None and record.get("status") == "completed" and isinstance(record.get("correct"), bool)
                correct = bool(completed and record["correct"])
                stats["completed_episodes"] += int(completed)
                stats["correct_episodes"] += int(correct)
                outcomes[model] = int(correct)
                completion.append(completed)
                audit = record.get("audit") if record else None
                if isinstance(audit, Mapping) and isinstance(audit.get("leakage"), bool):
                    stats["audited_episodes"] += 1
                    stats["leakage_episodes"] += int(audit["leakage"])
            complete_pairs += int(all(completion))
            if missing:
                missing_pairs.append({"pair_id": pair_id, "case_id": pair["case_id"], "missing_arms": missing})
            case_differences[pair["case_id"]].append(outcomes["gpt-5"] - outcomes["o4-mini"])
        for stats in arms.values():
            n, complete = stats["attempted_episodes"], stats["completed_episodes"]
            stats["failed_episodes"] = n - complete
            stats["accuracy_primary"] = stats["correct_episodes"] / n if n else None
            stats["accuracy_completed"] = stats["correct_episodes"] / complete if complete else None
            stats["leakage_rate"] = stats["leakage_episodes"] / stats["audited_episodes"] if stats["audited_episodes"] else None
        differences = [sum(values) / len(values) for _, values in sorted(case_differences.items())]
        mean_difference = sum(differences) / len(differences) if differences else None
        # At least two independent cases are required to estimate case-level uncertainty.
        ci = None
        if len(differences) >= 2:
            bootstrap = sorted(sum(rng.choice(differences) for _ in differences) / len(differences)
                               for _ in range(bootstrap_samples))
            ci = [_percentile(bootstrap, .025), _percentile(bootstrap, .975)]
        datasets[dataset] = {
            "protocol_hash": next(iter(protocols[dataset])), "case_count": len(differences),
            "pair_count": len(pairs), "arms": arms,
            "paired": {"contrast": "gpt-5 minus o4-mini", "complete_pairs": complete_pairs,
                       "missing_pairs": missing_pairs, "mean_accuracy_difference": mean_difference,
                       "ci95": ci, "bootstrap_unit": "case", "bootstrap_samples": bootstrap_samples,
                       "repetitions_per_case": {case: len(values) for case, values in sorted(case_differences.items())},
                       "uneven_repetitions": len({len(values) for values in case_differences.values()}) > 1,
                       "case_differences": {case: sum(values) / len(values) for case, values in sorted(case_differences.items())}},
        }
    return {"schema_version": 1, "expected_manifest_provided": schedule is not None,
            "failure_policy": "Missing, failed, and invalid completed results count as incorrect in primary accuracy",
            "denominator_scope": "frozen_schedule" if schedule is not None else "observed_pairs_only",
            "datasets": datasets}
