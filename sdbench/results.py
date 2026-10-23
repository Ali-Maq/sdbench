"""Completion checks shared by stock-harness resume and diagnostic reporting."""

from collections.abc import Mapping
from typing import Any


def is_completed_result(result: Mapping[str, Any]) -> bool:
    """Require consistent score/correct flags and nonempty diagnosis/judge output.

    New records carry ``result_status``; older records are accepted only when the
    same judge evidence is present. A diagnosis is ordinary clinical text, never
    an error channel. Pricing failures are separate from diagnostic completion.
    """
    if "result_status" in result and result["result_status"] != "success":
        return False

    score = result.get("diagnosis_score")
    correct = result.get("correct")
    if type(score) is not int or not 1 <= score <= 5:
        return False
    if type(correct) is not bool or correct != (score >= 4):
        return False

    return all(
        isinstance(result.get(field), str) and bool(result[field].strip())
        for field in ("model_diagnosis", "check_diagnosis_output")
    )
