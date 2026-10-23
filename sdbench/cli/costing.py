import argparse
import json
import sys

import dotenv

from sdbench.config import CostingConfig, SDBenchConfig
from sdbench.costing.costing import estimate_costs
from sdbench.costing.cost_types import cost_summary


def main():
    """Exercise the cost estimation functionality."""
    dotenv.load_dotenv(override=True)

    parser = argparse.ArgumentParser(
        description="Exercise SDBench cost estimation with estimate_costs()"
    )
    parser.add_argument(
        "tests", nargs="+", help="Test names to estimate costs for (space-separated)"
    )
    parser.add_argument(
        "--num-rounds",
        type=int,
        default=1,
        help="Number of rounds/visits to expert clinician (default: 1)",
    )
    parser.add_argument(
        "--dataset", default="nejm", help="Dataset name for config loading (default: nejm)"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug mode to show detailed messages"
    )
    parser.add_argument(
        "--output-format",
        choices=["summary", "detailed", "json"],
        default="summary",
        help="Output format: 'summary' (default), 'detailed' (with per-test breakdown), 'json' (raw JSON output)",
    )

    args, remaining_argv = parser.parse_known_args()

    sdbench_config = SDBenchConfig.from_command_line(args.dataset, remaining_argv)
    sdbench_config.debug = args.debug
    config = CostingConfig.from_sdbench_config(sdbench_config)

    print("=== SDBench Cost Estimator ===")
    print(f"Tests to estimate: {len(args.tests)}")
    print(f"Clinician rounds: {args.num_rounds}")
    print(f"Clinician cost per visit: ${config.clinician_cost}")
    print(f"Costing model: {config.costing_model}")
    if config.costing_model_args:
        print(f"Costing model args: {json.dumps(config.costing_model_args, indent=2)}")
    print()

    if args.debug:
        print("Test names:")
        for i, test in enumerate(args.tests, 1):
            print(f"  {i}. {test}")
        print()

    # Run cost estimation
    try:
        print("Estimating costs...")
        result = estimate_costs(
            requested_tests=args.tests,
            num_rounds=args.num_rounds,
            config=config,
        )

        print()
        print("=" * 60)
        print("COST ESTIMATION RESULTS")
        print("=" * 60)
        print()

        # JSON output format
        if args.output_format == "json":
            print(json.dumps(result, indent=2))
            return

        # Summary output
        print(f"Method: {result['method']}")
        print(f"Total unique tests: {result['total_unique_tests']}")
        print(f"Total test requests: {result['total_requests']}")
        print(f"Clinician rounds: {result['num_rounds']}")
        print()

        # Cost totals
        summary = cost_summary(result)
        totals = summary["totals"]
        print(f"Pricing status: {summary['pricing_status']}")
        print(f"Priced unique tests: {summary['priced_test_count']}; "
              f"unpriced: {summary['unpriced_test_count']}")
        for label, field in (
            ("Tests only", "tests_only"), ("Clinician visits", "clinician_visits"),
            ("Total with visits", "total_with_visits"),
            ("Known test subtotal", "known_tests_subtotal"),
            ("Known subtotal with visits", "known_total_with_visits"),
        ):
            value = totals[field]
            display = f"${value:,.2f}" if value is not None else "Unknown"
            print(f"{label}: {display}")
        if totals["average_per_test"] is not None:
            print(f"Average per unique test: ${totals['average_per_test']:,.2f}")
        print()

        # Detailed output format
        if args.output_format == "detailed" and result["cost_details"]:
            print("=" * 60)
            print("PER-TEST COST DETAILS")
            print("=" * 60)
            print()

            for i, detail in enumerate(result["cost_details"], 1):
                print(f"Test {i}: {detail['test_name']}")
                print(f"  Method: {detail['method']}")
                if detail["cost"] is not None:
                    print(f"  Cost: ${detail['cost']:.2f}")
                else:
                    print("  Cost: N/A (estimation failed)")

                cpt_est = detail.get("cpt_estimate")
                llm_est = detail.get("llm")

                if cpt_est and "error" not in cpt_est:
                    cpt_codes = cpt_est.get("cpt_codes")
                    if cpt_codes:
                        print(f"  CPT codes: {', '.join(cpt_codes)}")

                    # Show CPT selection details if available
                    cpt_selection = cpt_est.get("cpt_selection")
                    if cpt_selection:
                        selected_results = cpt_selection.get("selected_results")
                        if selected_results:
                            print("  CPT Details:")
                            for cpt_result in selected_results:
                                print(
                                    f"    • {cpt_result['cpt_code']}: {cpt_result['procedure_name']}"
                                )
                                print(
                                    f"      Price: ${cpt_result['gross_charge']:.2f} ({cpt_result['setting']})"
                                )

                if llm_est and "error" not in llm_est:
                    llm_cost = llm_est.get("cost")
                    if llm_cost is not None:
                        print(f"  LLM estimate used: ${llm_cost:.2f}")

                # Show errors if any
                if detail["method"] == "failure":
                    if cpt_est and "error" in cpt_est:
                        print(f"  CPT error: {cpt_est['error']}")
                    if llm_est and "error" in llm_est:
                        print(f"  LLM error: {llm_est['error']}")

                print()

        # Summary analysis
        if result.get("raw_analysis"):
            print("=" * 60)
            print("ANALYSIS SUMMARY")
            print("=" * 60)
            print(result["raw_analysis"])
            print()

        sys.exit(0)

    except Exception as e:
        print(f"Error running cost estimation: {e}")
        if args.debug:
            import traceback

            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
