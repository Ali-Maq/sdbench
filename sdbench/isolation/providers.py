"""Role-bound completions with raw usage accounting and explicit mock/live modes.

Live mode deliberately supports direct OpenAI only. There is no implicit provider
routing, SDK retry, model fallback, or credential loading from an agent workspace.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from openai import OpenAI
from openai.types.chat import ChatCompletion

from sdbench.isolation.accounting import RateCard, usage_entry
from sdbench.isolation.common import append_jsonl

ROLES = {"diagnosis", "gatekeeper", "judge"}
ALLOWED_ARGS = {
    "reasoning_effort", "max_completion_tokens", "temperature", "top_p", "seed",
    "frequency_penalty", "presence_penalty",
}


class MeteredBackend:
    def __init__(self, config: dict[str, Any], role: str, ledger_path: str | Path):
        if role not in ROLES:
            raise ValueError("Unknown model role")
        self.config = config
        self.role = role
        self.model = config[f"{role}_model"]
        self.mode = config["api_mode"]
        if self.mode not in {"mock", "live"}:
            raise ValueError("api_mode must be mock or live")
        self.args = dict(config.get(f"{role}_model_args", {}))
        if set(self.args) - ALLOWED_ARGS:
            raise ValueError("Unsupported model arguments; arbitrary provider routes/tools are disabled")
        self.ledger_path = Path(ledger_path)
        self.calls = 0
        self.max_calls = int(config.get("max_model_calls", 150))
        if self.max_calls < 1:
            raise ValueError("max_model_calls must be positive")
        rates = config.get("rate_cards", {}).get(self.model)
        self.rate_card = RateCard(**rates) if rates else None
        self.client = None
        if self.mode == "live":
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise ValueError("Live mode requires OPENAI_API_KEY in the trusted worker environment")
            if not isinstance(self.args.get("max_completion_tokens"), int) or self.args["max_completion_tokens"] < 1:
                raise ValueError("Live mode requires an explicit positive max_completion_tokens for every role")
            self.client = OpenAI(
                api_key=key, base_url="https://api.openai.com/v1", max_retries=0,
                timeout=120.0,
            )

    def _mock(self, messages: list[dict[str, Any]]) -> ChatCompletion:
        if self.role == "diagnosis":
            content = (
                "<test>CBC</test>" if self.calls == 1
                else "<diagnosis>Mock diagnosis for transport testing only</diagnosis>"
            )
        elif self.role == "gatekeeper":
            # No reference answer is extracted, even though the private prompt has it.
            user_text = str(next((m["content"] for m in messages if m["role"] == "user"), ""))
            ids = list(dict.fromkeys(re.findall(r"ID (\d+):", user_text)))
            content = "\n".join(
                f'<test_result id="{index}"><result>Mock observation; not clinical evidence.</result>'
                "<is_synthetic>true</is_synthetic></test_result>" for index in ids
            )
        else:
            content = "**REASONING**: Mock transport check; no clinical assessment was performed.\n**SCORE**: 3"
        return ChatCompletion.model_validate({
            "id": "mock-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": self.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": None,
        })

    def __call__(self, *, messages: Iterable[dict[str, Any]], **kwargs: Any) -> ChatCompletion:
        if kwargs:
            raise ValueError("Callers may supply messages only; model and settings are role-bound")
        if self.calls >= self.max_calls:
            raise RuntimeError("Per-role model call limit exhausted")
        self.calls += 1
        messages = list(messages)
        call_id = uuid.uuid4().hex
        start = time.monotonic()
        response = None
        error_type = None
        try:
            if self.mode == "mock":
                response = self._mock(messages)
            else:
                assert self.client is not None
                response = self.client.chat.completions.create(
                    model=self.model, messages=messages, stream=False, **self.args,
                )
            return response
        except Exception as error:
            error_type = type(error).__name__
            # SDBench's retry logger must never print provider errors containing prompts.
            raise RuntimeError(f"Provider call failed ({error_type}); see protected usage ledger") from None
        finally:
            usage = response.usage.model_dump() if response is not None and response.usage is not None else None
            entry = usage_entry(
                role=self.role, model=self.model, attempt=self.calls, usage=usage,
                api_mode=self.mode, rate_card=self.rate_card, call_id=call_id,
            )
            entry.update({
                "trial_id": self.config["trial_id"], "protocol_hash": self.config["protocol_hash"],
                "status": "error" if error_type else "completed", "error_type": error_type,
                "latency_seconds": time.monotonic() - start,
                "provider_request_id": getattr(response, "_request_id", None),
                "response_id": response.id if response is not None else None,
                "resolved_model": response.model if response is not None else None,
                "effective_model_args": self.args,
            })
            append_jsonl(self.ledger_path, entry)
            append_jsonl(self.ledger_path.with_name("raw_calls.jsonl"), {
                "call_id": call_id, "trial_id": self.config["trial_id"], "role": self.role,
                "messages": messages, "response": response.model_dump() if response else None,
                "api_mode": self.mode, "error_type": error_type,
            })


def build_backend(config: dict[str, Any], role: str, ledger_path: str | Path) -> MeteredBackend:
    return MeteredBackend(config, role, ledger_path)
