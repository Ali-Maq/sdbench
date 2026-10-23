"""Shared serialization and configuration; no implicit case loading."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from sdbench.config import SDBenchConfig


def plain_json(value: Any) -> Any:
    """Normalize immutable mappings and sequences without accepting invalid JSON values."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain_json(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"Unsupported or non-finite protocol value: {type(value).__name__}")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(plain_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_jsonl(path: str | Path, value: dict[str, Any]) -> None:
    """Write one private record. Callers serialize access to a given file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def sdbench_config(config: dict[str, Any]) -> SDBenchConfig:
    if config.get("diagnostic_agent", "baseline") != "baseline":
        raise ValueError("Only the released baseline diagnostic_agent is supported")
    return SDBenchConfig(
        max_turns=config["max_turns"],
        max_questions_per_turn=config.get("max_questions_per_turn"),
        max_tests_per_turn=config.get("max_tests_per_turn"),
        gatekeeper_model=config["gatekeeper_model"],
        gatekeeper_prompt_version=config["gk_prompt"],
        gatekeeper_model_args=config.get("gatekeeper_model_args", {}),
        judge_model=config["judge_model"],
        judge_prompt_version=config["judge_prompt"],
        judge_model_args=config.get("judge_model_args", {}),
        agent_model_args=config.get("diagnosis_model_args", {}),
        compute_costs=False,
        debug=False,
    )
