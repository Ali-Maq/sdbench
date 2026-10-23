"""Usage ledgers that distinguish provider estimates, missing data, and mock work."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import math
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class RateCard:
    effective_date: str
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        date.fromisoformat(self.effective_date)
        for name in ("input_per_million", "output_per_million", "cached_input_per_million"):
            value = getattr(self, name)
            if value is None and name == "cached_input_per_million":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative price")


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return vars(value)
    raise ValueError("usage must be a mapping or provider usage object")


def normalize_usage(usage: Any) -> dict[str, Any]:
    """Normalize Chat Completions/Responses usage without adding subset tokens twice.

    Cached input belongs to input tokens, and reasoning belongs to output tokens.
    Missing token counts remain None. Invalid provider counts are retained as warnings.
    """
    raw = _mapping(usage)
    warnings: list[str] = []

    def count(value: Any, label: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            warnings.append(f"Invalid {label}; treated as unknown")
            return None
        return value

    input_tokens = count(raw.get("input_tokens", raw.get("prompt_tokens")), "input_tokens")
    output_tokens = count(raw.get("output_tokens", raw.get("completion_tokens")), "output_tokens")
    input_details = _mapping(raw.get("input_tokens_details", raw.get("prompt_tokens_details")))
    output_details = _mapping(raw.get("output_tokens_details", raw.get("completion_tokens_details")))
    cached = count(input_details.get("cached_tokens"), "cached_input_tokens")
    reasoning = count(output_details.get("reasoning_tokens"), "reasoning_tokens")
    if cached is not None and input_tokens is not None and cached > input_tokens:
        warnings.append("Cached tokens exceed input tokens; cached count treated as unknown")
        cached = None
    if reasoning is not None and output_tokens is not None and reasoning > output_tokens:
        warnings.append("Reasoning tokens exceed output tokens; reasoning count treated as unknown")
        reasoning = None
    total = count(raw.get("total_tokens"), "total_tokens")
    if input_tokens is not None and output_tokens is not None:
        computed = input_tokens + output_tokens
        if total is not None and total != computed:
            warnings.append("Reported total differs from input plus output; using input plus output")
        total = computed
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total,
            "cached_input_tokens": cached, "reasoning_tokens": reasoning,
            "usage_available": bool(raw), "warnings": warnings}


def _estimate(usage: Mapping[str, Any], card: RateCard | None) -> tuple[float | None, list[float] | None]:
    if card is None or usage["input_tokens"] is None or usage["output_tokens"] is None:
        return None, None
    incoming, outgoing, cached = usage["input_tokens"], usage["output_tokens"], usage["cached_input_tokens"]
    # None means no distinct cached rate has been specified, not a zero-dollar rate.
    cached_rate = card.input_per_million if card.cached_input_per_million is None else card.cached_input_per_million
    if cached is None and cached_rate != card.input_per_million:
        bounds = sorted((incoming * rate + outgoing * card.output_per_million) / 1_000_000
                        for rate in (cached_rate, card.input_per_million))
        return None, bounds
    cached = cached or 0
    cost = ((incoming - cached) * card.input_per_million + cached * cached_rate +
            outgoing * card.output_per_million) / 1_000_000
    return cost, [cost, cost]


def usage_entry(
    *, role: str, model: str, attempt: int, usage: Any, api_mode: str,
    rate_card: RateCard | None = None, call_id: str | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    """One record per actual attempt; a failed call with no usage has unknown cost.

    Rates must be supplied explicitly. No built-in prices or guessed usage are used.
    ``actual_paid_usd`` is deliberately unknown: token estimates are not invoices.
    """
    if not role.strip() or not model.strip():
        raise ValueError("role and model are required")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if api_mode not in ("mock", "live"):
        raise ValueError("api_mode must be mock or live")
    if call_id is not None and (not isinstance(call_id, str) or not call_id):
        raise ValueError("call_id must be None or a non-empty string")
    normalized = normalize_usage(usage)
    cost, bounds = _estimate(normalized, rate_card)
    return {
        "call_id": call_id, "role": role, "model": model, "attempt": attempt,
        "status": status, "api_mode": api_mode, "usage": normalized,
        "rate_card": asdict(rate_card) if rate_card else None, "currency": "USD",
        "estimated_cost_usd": cost if api_mode == "live" else None,
        "simulated_cost_usd": cost if api_mode == "mock" else None,
        "cost_bounds_usd": bounds,
        "cost_kind": "simulation" if api_mode == "mock" else "token_rate_estimate",
        "actual_paid_usd": None,
    }


def summarize_usage(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum known usage, exposing incompleteness instead of silently substituting zero."""
    ledger = list(entries)
    ids = [entry["call_id"] for entry in ledger if entry.get("call_id") is not None]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate call_id would double-count usage")

    def summarize(subset: list[Mapping[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {"calls": len(subset), "actual_paid_usd": None}
        for metric in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"):
            values = [entry["usage"].get(metric) for entry in subset]
            known = [value for value in values if value is not None]
            result[metric] = sum(known) if len(known) == len(values) else None
            result[f"known_{metric}"] = sum(known)
            result[f"unknown_{metric}_calls"] = len(values) - len(known)
        for mode, metric in (("live", "estimated_cost_usd"), ("mock", "simulated_cost_usd")):
            relevant = [entry for entry in subset if entry["api_mode"] == mode]
            known = [entry[metric] for entry in relevant if entry.get(metric) is not None]
            result[f"{mode}_calls"] = len(relevant)
            result[metric] = sum(known) if len(known) == len(relevant) else None
            result[f"known_{metric}"] = sum(known)
            result[f"unknown_{mode}_cost_calls"] = len(relevant) - len(known)
        return result

    return {**summarize(ledger), "by_role": {
        role: summarize([entry for entry in ledger if entry["role"] == role])
        for role in sorted({entry["role"] for entry in ledger})
    }}
