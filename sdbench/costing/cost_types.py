import math
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    from sdbench.costing import cpt_matcher  # pyright: ignore[reportAttributeAccessIssue]
    from sdbench.models import ChatCompletionMessageParam


class CostMethodEstimate(TypedDict, total=False):
    """Either 'cost' or 'error' will be present."""

    cost: float
    error: str
    time: float  # Time taken in seconds

    # llm_estimator or llm_batch_estimator values
    messages: list["ChatCompletionMessageParam"]
    batch_test_name: str  # Only for llm_batch_estimator

    # cpt_matcher values
    cpt_codes: list[str]
    cpt_selection: "cpt_matcher.CPTSelectionResult"


class CostEstimate(TypedDict):
    """Full cost estimate for a single test."""

    test_name: str
    cost: float | None  # Set, unless method is "failure"
    method: str  # One of "cpt_lookup", "llm_fallback", "llm_only", "batch_llm_only", "failure"
    cpt_estimate: CostMethodEstimate | None
    llm: CostMethodEstimate | None
    explanation: str


class CostsEstimate(TypedDict):
    """Aggregate cost estimates for all tests in a run."""

    config: dict[str, Any]
    raw_analysis: str
    # One price per case-insensitive, outer-whitespace-normalized test name.
    # Repeated requests remain in total_requests; these are not repeat-procedure bills.
    total_unique_tests: int
    total_requests: int
    num_rounds: int
    cost_details: list[CostEstimate]
    clinician_cost_per_visit: int
    method: str  # One of "unified_cpt_llm", "llm_only", "batch_llm_only", "no_tests"
    pricing_status: Literal["complete", "partial", "unknown"]
    priced_test_count: int
    unpriced_test_count: int | None
    totals: "CostsEstimateTotals"


class CostsEstimateTotals(TypedDict):
    """Totals section of a CostsEstimate."""

    tests_only: float | None
    clinician_visits: float | None
    total_with_visits: float | None
    average_per_test: float | None
    known_tests_subtotal: float | None
    known_total_with_visits: float | None


def valid_cost(value: Any) -> bool:
    """A price is a finite, nonnegative number; booleans are not prices."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def cost_summary(analysis: dict[str, Any] | None) -> dict[str, Any]:
    """Derive safe totals from per-test evidence, including legacy result files.

    Stored totals are not proof of pricing coverage: older results silently summed
    successful prices. Missing or failed details therefore never become a complete
    total. An explicit zero-test analysis with known visit costs is complete.
    """
    analysis = analysis if isinstance(analysis, dict) else {}
    details = analysis.get("cost_details")
    details = details if isinstance(details, list) else None
    expected = analysis.get("total_unique_tests")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        # Returned rows cannot establish how many prices were supposed to exist.
        expected = None
    priced = [
        detail["cost"] for detail in (details or [])
        if isinstance(detail, dict)
        and detail.get("method") != "failure"
        and valid_cost(detail.get("cost"))
    ]
    priced_count = len(priced)
    unpriced_count = max(expected - priced_count, 0) if expected is not None else None
    tests_complete = (
        details is not None and expected == len(details) and unpriced_count == 0
    )
    stored_totals = analysis.get("totals")
    visits = stored_totals.get("clinician_visits") if isinstance(stored_totals, dict) else None
    visits = float(visits) if valid_cost(visits) else None
    subtotal = sum(priced)
    # Even finite per-test values can overflow when summed.
    if not valid_cost(subtotal):
        subtotal = None
    known_total = subtotal + visits if subtotal is not None and visits is not None else None
    if not valid_cost(known_total):
        known_total = None
    complete = tests_complete and known_total is not None
    status = "complete" if complete else "partial" if priced_count else "unknown"
    return {
        "pricing_status": status,
        "priced_test_count": priced_count,
        "unpriced_test_count": unpriced_count,
        "totals": {
            "tests_only": subtotal if complete else None,
            "clinician_visits": visits,
            "total_with_visits": known_total if complete else None,
            "average_per_test": (subtotal / expected if expected else 0) if complete else None,
            "known_tests_subtotal": subtotal,
            "known_total_with_visits": known_total,
        },
    }
