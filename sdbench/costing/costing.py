import atexit
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import dataclasses
import threading
import traceback
from typing import Any

from sdbench.config import CostingConfig
from sdbench.costing import cache as cost_cache, llm_batch_estimator, llm_estimater
from sdbench.costing.cost_types import CostEstimate, CostsEstimate, cost_summary, valid_cost


# Track executors for cleanup
_executor_cache: dict[tuple[bool, int], ThreadPoolExecutor | ProcessPoolExecutor] = {}


def _shutdown_all_executors():
    """Shutdown all cached executors on program exit."""
    for executor in _executor_cache.values():
        executor.shutdown(wait=True)
    _executor_cache.clear()


# Register cleanup handler
atexit.register(_shutdown_all_executors)


def _get_executor(
    use_multiprocess: bool, num_workers: int
) -> ThreadPoolExecutor | ProcessPoolExecutor:
    """Get or create a cached executor based on configuration.

    Executors are cached and reused based on (use_multiprocess, num_workers) tuple.
    All executors are automatically shutdown when the program exits.

    Args:
        use_multiprocess: If True, use ProcessPoolExecutor; if False, use ThreadPoolExecutor
        num_workers: Maximum number of workers for the executor

    Returns:
        Cached or new executor instance
    """
    cache_key = (use_multiprocess, num_workers)

    if cache_key not in _executor_cache:
        executor_class = ProcessPoolExecutor if use_multiprocess else ThreadPoolExecutor
        _executor_cache[cache_key] = executor_class(max_workers=num_workers)

    return _executor_cache[cache_key]


_cpt_matchers: dict[str, Any] = {}
_cpt_matchers_lock = threading.Lock()


def get_cpt_fuzzy_matcher(
    costing_config: CostingConfig,
) -> "Any | None":
    """Reuse a CPT matcher only while its effective pricing inputs are unchanged.

    Initialization is expensive (it embeds the whole charge master and builds a FAISS
    index on first use). Callers that want the build to happen up front -- e.g. before a worker pool starts -- can call
    this once to warm the cache. Returns None if initialization fails.
    """
    try:
        identity = cost_cache.pricing_identity(costing_config)
    except Exception as error:
        print(f"Warning: Could not identify CPT pricing inputs: {error}")
        return None
    with _cpt_matchers_lock:
        if identity not in _cpt_matchers:
            matcher = _build_cpt_fuzzy_matcher(costing_config)
            if matcher is None:
                return None  # A transient build failure must not poison later requests.
            _cpt_matchers[identity] = matcher
        return _cpt_matchers[identity]


def _build_cpt_fuzzy_matcher(config: CostingConfig) -> "Any | None":
    """Initialize the optional grounded pricer, reporting configuration failures."""
    try:
        from sdbench.costing import cpt_matcher  # pyright: ignore[reportAttributeAccessIssue]

        return cpt_matcher.CPTFuzzyMatcher(config)
    except Exception as e:
        print(f"Warning: Could not initialize CPT matcher: {e}")
        traceback.print_exc()
        return None


def _validated_method_estimate(estimate: dict[str, Any]) -> dict[str, Any]:
    """Treat invalid provider prices as failures before caching or aggregation."""
    if "error" not in estimate and not valid_cost(estimate.get("cost")):
        estimate = {**estimate, "error": "Price must be a finite, nonnegative number"}
    if "error" in estimate:
        estimate = {key: value for key, value in estimate.items() if key != "cost"}
    return estimate


def _validated_cost_estimate(estimate: CostEstimate) -> CostEstimate:
    if estimate.get("method") == "failure" or not valid_cost(estimate.get("cost")):
        return {
            **estimate, "cost": None, "method": "failure",
            "explanation": estimate.get("explanation", "") + " (price unavailable or invalid)",
            "cpt_estimate": _validated_method_estimate(estimate["cpt_estimate"]) if estimate.get("cpt_estimate") else None,
            "llm": _validated_method_estimate(estimate["llm"]) if estimate.get("llm") else None,
        }
    return estimate


def estimate_unified_test_costs(test_name: str, config: CostingConfig) -> CostEstimate:
    """Estimate cost for a single test with a unified GPT+LLM fallback method, using global cache.
    Cache entries identify the effective pricing inputs and normalized test name.

    Args:
        test_name: Name of the test to estimate cost for
        config: CostingConfig containing costing parameters
    """
    cached_result = cost_cache.get_cached_cost(config, test_name)
    if cached_result is not None:
        return _validated_cost_estimate(cached_result)

    try:
        cpt_fuzzy_matcher = get_cpt_fuzzy_matcher(config)
        if not cpt_fuzzy_matcher:
            raise RuntimeError(
                "CPT matcher failed to initialize. This indicates a configuration problem "
                "(e.g., missing OPENAI_API_KEY, wrong OPENAI_BASE_URL, or missing cost data file). "
                "Check your environment variables and configuration."
            )
        cpt_estimate = _validated_method_estimate(cpt_fuzzy_matcher.cost_test(test_name))
        if "error" not in cpt_estimate:
            result: CostEstimate = {
                "test_name": test_name,
                "cost": cpt_estimate.get("cost"),
                "method": "cpt_lookup",
                "cpt_estimate": cpt_estimate,
                "llm": None,
                "explanation": f"CPT lookup successful for '{test_name}': ${cpt_estimate.get('cost'):.2f} for CPTs {cpt_estimate.get('cpt_codes')}",
            }
            # Store in cache
            cost_cache.set_cached_cost(config, test_name, result)
            return result

        llm_cost = _validated_method_estimate(llm_estimater.cost_test(test_name, config))
        if "error" not in llm_cost:
            result = {
                "test_name": test_name,
                "cost": llm_cost.get("cost"),
                "method": "llm_fallback",
                "cpt_estimate": cpt_estimate,
                "llm": llm_cost,
                "explanation": f"LLM fallback successful for '{test_name}': ${llm_cost.get('cost'):.2f}",
            }
            # Store in cache
            cost_cache.set_cached_cost(config, test_name, result)
            return result

        result = {
            "test_name": test_name,
            "cost": None,
            "method": "failure",
            "cpt_estimate": cpt_estimate,
            "llm": llm_cost,
            "explanation": f"Failed to price '{test_name}' with both CPT lookup ({cpt_estimate['error']}) and LLM fallback ({llm_cost['error']})",
        }
        return result
    except Exception as e:
        print(f"Error processing test '{test_name}': {e}")
        result = {
            "test_name": test_name,
            "cost": None,
            "method": "failure",
            "cpt_estimate": None,
            "llm": None,
            "explanation": f"Error pricing '{test_name}': {e}",
        }
        # Don't cache exceptions - might be transient
        return result


def estimate_llm_only_test_costs(test_name: str, config: CostingConfig) -> CostEstimate:
    """Estimate cost for a single test using LLM pricing and optional caching.

    Args:
        test_name: Name of the test to estimate cost for
        config: CostingConfig containing costing parameters
    """
    cached_result = cost_cache.get_cached_cost(config, test_name)
    if cached_result is not None:
        return _validated_cost_estimate(cached_result)

    try:
        llm_cost = _validated_method_estimate(llm_estimater.cost_test(test_name, config))
        if "error" not in llm_cost:
            result: CostEstimate = {
                "test_name": test_name,
                "cost": llm_cost.get("cost"),
                "method": "llm_only",
                "cpt_estimate": None,
                "llm": llm_cost,
                "explanation": f"LLM successful for '{test_name}': ${llm_cost.get('cost'):.2f}",
            }
            # Store in cache
            cost_cache.set_cached_cost(config, test_name, result)
            return result

        # For LLM only costing, don't cache failures as they might be transient also
        return {
            "test_name": test_name,
            "cost": None,
            "method": "failure",
            "cpt_estimate": None,
            "llm": llm_cost,
            "explanation": f"Failed to price '{test_name}' with LLM ({llm_cost['error']})",
        }
    except Exception as e:
        # Don't cache exceptions - might be transient
        print(f"Error processing test '{test_name}': {e}")
        return {
            "test_name": test_name,
            "cost": None,
            "method": "failure",
            "cpt_estimate": None,
            "llm": None,
            "explanation": f"Error pricing '{test_name}': {e}",
        }


def estimate_batch_llm_only_test_costs(
    test_names: list[str], config: CostingConfig
) -> list[CostEstimate]:
    """Estimate costs for multiple tests using batch LLM estimation with caching.

    Checks cache first for each test, then batches uncached tests into a single LLM call.

    Args:
        test_names: List of test names to estimate costs for
        config: CostingConfig containing costing parameters

    Returns:
        List of CostEstimate for all tests (from cache + batch results)
    """
    if not test_names:
        return []

    results: list[CostEstimate] = []
    uncached_tests: list[str] = []

    # First pass: check cache for each test
    for test_name in test_names:
        cached_result = cost_cache.get_cached_cost(config, test_name)
        if cached_result is not None:
            results.append(_validated_cost_estimate(cached_result))
        else:
            uncached_tests.append(test_name)

    # If there are uncached tests, batch price them
    if uncached_tests:
        batch_results = llm_batch_estimator.cost_tests_batch(uncached_tests, config)

        # Process each batch result
        for test_name in uncached_tests:
            llm_cost = batch_results.get(test_name.lower().strip())
            if llm_cost is not None:
                llm_cost = _validated_method_estimate(llm_cost)

            if llm_cost and "error" not in llm_cost:
                result: CostEstimate = {
                    "test_name": test_name,
                    "cost": llm_cost.get("cost"),
                    "method": "batch_llm_only",
                    "cpt_estimate": None,
                    "llm": llm_cost,
                    "explanation": f"Batch LLM successful for '{test_name}': ${llm_cost.get('cost'):.2f}",
                }
                # Store in cache
                cost_cache.set_cached_cost(config, test_name, result)
                results.append(result)
            else:
                # Don't cache failures as they might be transient
                error_msg = (
                    llm_cost.get("error", "Unknown error") if llm_cost else "No result returned"
                )
                result = {
                    "test_name": test_name,
                    "cost": None,
                    "method": "failure",
                    "cpt_estimate": None,
                    "llm": llm_cost,
                    "explanation": f"Failed to price '{test_name}' with batch LLM ({error_msg})",
                }
                results.append(result)

    return results


def _estimate_single_test_cost(args: tuple[str, CostingConfig]) -> CostEstimate:
    """Helper function to estimate cost for a single test.
    Designed to be used with concurrent.futures executors and also for sequential processing.

    Args:
        args: Tuple of (test_name, costing_config)

    Returns:
        CostEstimate for the test
    """
    test_name, costing_config = args

    if costing_config.costing_method == "llm_only":
        return estimate_llm_only_test_costs(test_name, costing_config)
    elif costing_config.costing_method == "unified_cpt_llm":
        return estimate_unified_test_costs(test_name, costing_config)
    else:
        raise ValueError(f"Unknown costing method: {costing_config.costing_method}")


def estimate_costs(
    requested_tests: list[str], num_rounds: int, config: CostingConfig
) -> CostsEstimate:
    """Unified cost analysis that tries CPT matching first, falls back to LLM estimation.
    No dependency on previous cost analysis.

    Args:
        requested_tests: List of test names requested for this case. All prefixes have been removed
            and this is just the raw test name to cost.
        num_rounds: Number of rounds/visits to expert clinician
        config: SDBench config

    Returns:
        CostsEstimate containing costs estimates

    """
    if not isinstance(num_rounds, int) or isinstance(num_rounds, bool) or num_rounds < 0:
        raise ValueError("num_rounds must be a nonnegative integer")
    if not valid_cost(config.clinician_cost):
        raise ValueError("clinician_cost must be a finite, nonnegative number")
    if any(not isinstance(name, str) or not name.strip() for name in requested_tests):
        raise ValueError("Requested test names must be nonempty strings")
    # The benchmark prices unique test names, not every repeated procedure request.
    unique_names = {}
    for name in requested_tests:
        unique_names.setdefault(name.lower().strip(), name.strip())
    unique_tests = list(unique_names.values())

    cost_data: list[CostEstimate] = []

    # Use batch processing for batch_llm_only method
    if not unique_tests:
        pass
    elif config.costing_method == "batch_llm_only":
        cost_data = estimate_batch_llm_only_test_costs(unique_tests, config)
    # Parallelize cost estimation if costing_parallelism > 1 and we have multiple tests
    elif config.costing_parallelism > 1 and len(unique_tests) > 1:
        num_workers = min(config.costing_parallelism, len(unique_tests))
        args_list = [(test_name, config) for test_name in unique_tests]

        # Get cached executor based on configuration
        executor = _get_executor(
            use_multiprocess=config.costing_multiprocess, num_workers=num_workers
        )
        cost_data = list(executor.map(_estimate_single_test_cost, args_list))
    else:
        # Sequential processing (when costing_parallelism <= 1 or only one test)
        for test_name in unique_tests:
            cost_estimate = _estimate_single_test_cost((test_name, config))
            cost_data.append(cost_estimate)

    cost_data = [_validated_cost_estimate(item) for item in cost_data]
    summary = cost_summary({
        "total_unique_tests": len(unique_tests),
        "cost_details": cost_data,
        "totals": {"clinician_visits": config.clinician_cost * num_rounds},
    })
    totals = summary["totals"]
    analysis_summary = (
        f"{config.costing_method} cost analysis ({summary['pricing_status']}):\n"
        f"- Priced {summary['priced_test_count']} of {len(unique_tests)} unique tests\n"
        f"- Unpriced tests: {summary['unpriced_test_count']}\n"
        f"- Known test subtotal: {totals['known_tests_subtotal']}\n"
        f"- Clinician costs: {totals['clinician_visits']}\n"
        f"- Complete total with visits: {totals['total_with_visits']}"
    )
    return {
        "config": dataclasses.asdict(config),
        "raw_analysis": analysis_summary,
        "total_unique_tests": len(unique_tests),
        "total_requests": len(requested_tests),
        "num_rounds": num_rounds,
        "cost_details": cost_data,
        "clinician_cost_per_visit": config.clinician_cost,
        "method": config.costing_method if unique_tests else "no_tests",
        **summary,
    }
