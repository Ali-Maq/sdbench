import math
import re
import time
from typing import TYPE_CHECKING

from sdbench.config import CostingConfig
from sdbench.models import (
    ChatCompletionMessageParam,
    ModelCreateChatCompletion,
    as_assistant_message,
    create_chat_completion,
)

if TYPE_CHECKING:
    from sdbench.costing.cost_types import CostMethodEstimate


def cost_tests_batch(
    test_names: list[str],
    config: CostingConfig | None,
    llm: ModelCreateChatCompletion | None = None,
) -> dict[str, "CostMethodEstimate"]:
    """Use LLM to estimate costs for multiple tests in a single batch call.

    Args:
        test_names: List of test names to price (should all be uncached)
        config: CostingConfig containing costing parameters

    Returns:
        Dict mapping test_name -> CostMethodEstimate
        The test_name keys are normalized (case-insensitive, stripped).
    """
    if not test_names:
        return {}

    # The caller looks up prices by this identity. Ask once per identity so a
    # compliant response cannot contain duplicate normalized names.
    unique_names: dict[str, str] = {}
    for test_name in test_names:
        unique_names.setdefault(test_name.lower().strip(), test_name.strip())
    test_names = list(unique_names.values())

    start_time = time.time()

    system_prompt = """You are a medical billing expert. Given a list of medical tests, provide cost estimates in USD for typical hospital/lab pricing in the United States.

For each test, respond in this EXACT format:
Test: [test name]
Cost: $[estimate]
Explanation: [brief explanation]

IMPORTANT: Copy each test name exactly as provided. Respond in the SAME ORDER as the numbered list. Do not reorder, duplicate, or skip any tests.

Be consistent with the format and use realistic pricing. Provide a single cost estimate (not a range)."""

    numbered_tests = "\n".join(f"{i + 1}. {test}" for i, test in enumerate(test_names))
    user_prompt = f"""Please estimate the costs for the following {len(test_names)} medical tests:

{numbered_tests}

Please provide cost estimates in the specified format for each test, in the same order."""

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        if llm is not None:
            response = llm(messages=messages)
        else:
            assert config is not None
            response = create_chat_completion(
                model=config.costing_model,
                messages=messages,
                debug=config.debug,
                **config.costing_model_args,
            )
        messages.append(as_assistant_message(response.choices[0].message))
        cost_analysis_text = response.choices[0].message.content or ""
        cost_analysis_text = cost_analysis_text.strip()
        if not cost_analysis_text:
            print(f"Warning: Empty response from LLM for batch cost estimation: {response}")

        elapsed_time = time.time() - start_time

        # Parse the response to extract costs for each test
        results: dict[str, CostMethodEstimate] = {}

        # Match by name: a reordered or omitted block must not price another test.
        test_blocks = re.split(r"(?m)(?=^Test:)", cost_analysis_text)
        parsed_costs: dict[str, tuple[float, str] | None] = {}

        for block in test_blocks:
            lines = block.strip().split("\n")
            if len(lines) >= 2:
                test_line = next((line for line in lines if line.startswith("Test:")), None) or ""
                cost_lines = [line for line in lines if line.startswith("Cost:")]

                parsed_test_name = test_line.removeprefix("Test:").strip()
                key = parsed_test_name.lower()
                if not key:
                    continue
                if key in parsed_costs:
                    parsed_costs[key] = None  # Repeated names are ambiguous.
                    continue
                cost_match = re.fullmatch(
                    r"Cost:\s*\$(\d+(?:,\d{3})*(?:\.\d+)?)\s*",
                    cost_lines[0] if len(cost_lines) == 1 else "",
                )
                cost = float(cost_match.group(1).replace(",", "")) if cost_match else None
                parsed_costs[key] = (
                    (cost, parsed_test_name) if cost is not None and math.isfinite(cost) else None
                )

        for test_name in test_names:
            normalised_test_name = test_name.lower().strip()
            parsed_cost = parsed_costs.get(normalised_test_name)

            if parsed_cost is not None:
                cost_value, parsed_test_name = parsed_cost
                results[normalised_test_name] = {
                    "messages": messages,
                    "cost": cost_value,
                    "batch_test_name": parsed_test_name,
                    "time": elapsed_time,  # Total time for the batch call
                }
            else:
                if config is not None and config.debug:
                    print(
                        f"Could not parse an unambiguous cost for test '{test_name}' from LLM response."
                    )
                results[normalised_test_name] = {
                    "messages": messages,
                    "error": f"Missing, invalid, or duplicate cost for '{test_name}' in batch response",
                    "time": elapsed_time,
                }

        return results

    except Exception as e:
        elapsed_time = time.time() - start_time
        print(f"Warning: Batch LLM cost estimation failed: {e}")

        # Return error for all tests
        return {
            test_name.lower().strip(): {
                "messages": messages,
                "error": str(e),
                "time": elapsed_time,
            }
            for test_name in test_names
        }
