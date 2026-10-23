"""Role-bound completions with raw usage accounting and explicit mock/live modes.

Live mode supports direct OpenAI Chat Completions or explicitly configured Azure
Responses. There is no implicit routing, SDK retry, model fallback, or credential
loading from an agent workspace.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from openai import OpenAI
from openai.types.chat import ChatCompletion

from sdbench.isolation.accounting import RateCard, normalize_usage, usage_entry
from sdbench.isolation.common import append_jsonl

ROLES = {"diagnosis", "gatekeeper", "judge"}
ALLOWED_ARGS = {
    "reasoning_effort", "max_completion_tokens", "temperature", "top_p", "seed",
    "frequency_penalty", "presence_penalty",
}
RESPONSES_ALLOWED_ARGS = {"reasoning_effort", "max_completion_tokens", "temperature", "top_p"}
RESPONSES_ADAPTER_POLICY = {
    "version": "sdbench-final-text-v1",
    "output": "final_answer text only when message phases are present; otherwise legacy unphased text",
    "mixed_phased_and_unphased_messages": "reject",
    "discarded_from_chat_completion": ["commentary", "reasoning", "non-text output"],
    "assistant_history_phase": "final_answer",
    "raw_response": "preserved in full in protected raw_calls.jsonl",
}


def validate_azure_base_url(value: str) -> str:
    """Require an explicit Azure OpenAI v1 endpoint, with no embedded credentials."""
    if not isinstance(value, str):
        raise ValueError("Azure requires an explicit provider_base_url")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"/openai/v1", "/openai/v1/"}
            and re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:cognitiveservices|openai)\.azure\.com",
                parsed.hostname or "",
            ) is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Azure endpoint must be HTTPS on an allowed Azure resource host at /openai/v1/")
    return f"https://{parsed.hostname}/openai/v1/"


class AzureTokenFile:
    """Callable SDK credential provider that rereads a host-refreshed secret file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __call__(self) -> str:
        try:
            with self.path.open("rb") as stream:
                content = stream.read(64 * 1024 + 1)
            if len(content) > 64 * 1024:
                raise ValueError("Credential file too large")
            payload = json.loads(content)
            token = payload["accessToken"]
            expires_on = payload["expires_on"]
            if not isinstance(token, str) or not token or any(char.isspace() for char in token):
                raise ValueError("Invalid access token")
            if isinstance(expires_on, bool):
                raise ValueError("Invalid token expiry")
            expiry = float(expires_on)
            if not math.isfinite(expiry) or expiry <= time.time() + 60:
                raise ValueError("Access token is expired or too close to expiry")
        except (OSError, ValueError, TypeError, KeyError):
            # Never copy credential contents or a JSON decoder's source excerpt.
            raise RuntimeError("Azure token file is unavailable, malformed, or expires within 60 seconds") from None
        return token


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SDBench stores completed text turns, not intermediate Responses items."""
    result = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in {"system", "developer", "user", "assistant"}
            or not isinstance(message["content"], str)
        ):
            raise ValueError("Responses adapter requires plain role/content text history")
        item = dict(message)
        if item["role"] == "assistant":
            item["phase"] = "final_answer"
        result.append(item)
    return result


def _response_final_text(response: Any) -> str:
    """Exclude commentary and reasoning from actionable text; keep raw elsewhere."""
    messages = [item for item in response.output if item.type == "message"]
    phases = [getattr(item, "phase", None) for item in messages]
    if any(phase not in {None, "commentary", "final_answer"} for phase in phases):
        raise ValueError("Responses API returned an unknown message phase")
    if any(phase is not None for phase in phases):
        if any(phase is None for phase in phases):
            raise ValueError("Responses API mixed phased and unphased messages")
        messages = [item for item in messages if item.phase == "final_answer"]
    parts = []
    for message in messages:
        if message.role != "assistant" or message.status != "completed":
            raise ValueError("Responses API returned an unfinished final message")
        parts.extend(part.text for part in message.content if part.type == "output_text")
    if not all(isinstance(part, str) for part in parts):
        raise ValueError("Responses API returned invalid final text")
    text = "".join(parts)
    if not text.strip():
        raise ValueError("Responses API returned no usable final text")
    return text


def _response_as_chat_completion(response: Any) -> ChatCompletion:
    """Text-only adapter: final answers in ChatCompletion, full raw output in logs."""
    if response.status != "completed" or response.error is not None:
        raise ValueError("Responses API did not return a completed response")
    final_text = _response_final_text(response)
    usage = None
    if response.usage is not None:
        normalized = normalize_usage(response.usage)
        if all(normalized[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")):
            usage = {
                "prompt_tokens": normalized["input_tokens"],
                "completion_tokens": normalized["output_tokens"],
                "total_tokens": normalized["total_tokens"],
                "prompt_tokens_details": (
                    {"cached_tokens": normalized["cached_input_tokens"]}
                    if normalized["cached_input_tokens"] is not None else None
                ),
                "completion_tokens_details": (
                    {"reasoning_tokens": normalized["reasoning_tokens"]}
                    if normalized["reasoning_tokens"] is not None else None
                ),
            }
    return ChatCompletion.model_validate({
        "id": response.id,
        "object": "chat.completion",
        "created": response.created_at,
        "model": response.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": final_text}, "finish_reason": "stop"}],
        "usage": usage,
    })


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
        self.provider = config.get("provider", "openai")
        self.transport = config.get("api_transport", "chat_completions")
        if (self.provider, self.transport) not in {("openai", "chat_completions"), ("azure", "responses")}:
            raise ValueError("Supported provider/transport pairs are openai/chat_completions and azure/responses")
        self.base_url = (
            validate_azure_base_url(config.get("provider_base_url"))
            if self.provider == "azure" else "https://api.openai.com/v1"
        )
        if self.provider == "openai" and config.get("provider_base_url") not in {None, self.base_url, self.base_url + "/"}:
            raise ValueError("The direct OpenAI endpoint cannot be overridden")
        self.args = dict(config.get(f"{role}_model_args", {}))
        allowed_args = RESPONSES_ALLOWED_ARGS if self.transport == "responses" else ALLOWED_ARGS
        if set(self.args) - allowed_args:
            raise ValueError("Unsupported model arguments; arbitrary provider routes/tools are disabled")
        self.request_args = dict(self.args)
        if self.transport == "responses":
            if "reasoning_effort" in self.request_args:
                self.request_args["reasoning"] = {"effort": self.request_args.pop("reasoning_effort")}
            if "max_completion_tokens" in self.request_args:
                self.request_args["max_output_tokens"] = self.request_args.pop("max_completion_tokens")
        self.ledger_path = Path(ledger_path)
        self.calls = 0
        self.max_calls = int(config.get("max_model_calls", 150))
        if self.max_calls < 1:
            raise ValueError("max_model_calls must be positive")
        rates = config.get("rate_cards", {}).get(self.model)
        self.rate_card = RateCard(**rates) if rates else None
        self.client = None
        if self.mode == "live":
            if type(self.args.get("max_completion_tokens")) is not int or self.args["max_completion_tokens"] < 1:
                raise ValueError("Live mode requires an explicit positive max_completion_tokens for every role")
            if self.provider == "azure":
                token_path = os.environ.get("SDBENCH_AZURE_TOKEN_FILE")
                if not token_path:
                    raise ValueError("Azure live mode requires SDBENCH_AZURE_TOKEN_FILE in the trusted worker environment")
                key = AzureTokenFile(token_path)
            else:
                key = os.environ.get("OPENAI_API_KEY")
                if not key:
                    raise ValueError("Live mode requires OPENAI_API_KEY in the trusted worker environment")
            self.client = OpenAI(
                api_key=key, base_url=self.base_url, max_retries=0,
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
        request_payload = {"model": self.model, "stream": False, **self.request_args}
        effective_input = None
        if self.transport == "responses":
            effective_input = _responses_input(messages)
            request_payload.update(input=effective_input, store=False)
        else:
            request_payload["messages"] = messages
        response = None
        raw_error_response = None
        error_request_id = None
        http_status = None
        error_type = None
        try:
            if self.mode == "mock":
                response = self._mock(messages)
            elif self.transport == "responses":
                assert self.client is not None
                response = self.client.responses.create(
                    model=self.model, input=effective_input, stream=False, store=False, **self.request_args,
                )
                return _response_as_chat_completion(response)
            else:
                assert self.client is not None
                response = self.client.chat.completions.create(
                    model=self.model, messages=messages, stream=False, **self.request_args,
                )
            return response
        except Exception as error:
            error_type = type(error).__name__
            error_request_id = getattr(error, "request_id", None)
            http_status = getattr(error, "status_code", None)
            body = getattr(error, "body", None)
            # The SDK unwraps HTTP error bodies to their `error` member. Read
            # the original JSON body to retain usage outside that member.
            error_response = getattr(error, "response", None)
            if error_response is not None:
                try:
                    body = error_response.json()
                except (ValueError, RuntimeError):
                    pass
            if isinstance(body, dict):
                raw_error_response = body
            # SDBench's retry logger must never print provider errors containing prompts.
            raise RuntimeError(f"Provider call failed ({error_type}); see protected usage ledger") from None
        finally:
            raw_response = response.model_dump() if response is not None else raw_error_response
            usage = raw_response.get("usage") if raw_response is not None else None
            provider_request_id = getattr(response, "_request_id", None) or error_request_id
            response_id = raw_response.get("id") if raw_response is not None else None
            resolved_model = raw_response.get("model") if raw_response is not None else None
            provider_status = raw_response.get("status") if raw_response is not None else None
            entry = usage_entry(
                role=self.role, model=self.model, attempt=self.calls, usage=usage,
                api_mode=self.mode, rate_card=self.rate_card, call_id=call_id,
            )
            entry.update({
                "trial_id": self.config["trial_id"], "protocol_hash": self.config["protocol_hash"],
                "status": "error" if error_type else "completed", "error_type": error_type,
                "latency_seconds": time.monotonic() - start,
                "provider": self.provider, "api_transport": self.transport,
                "provider_base_url": self.base_url,
                "provider_request_id": provider_request_id,
                "response_id": response_id,
                "resolved_model": resolved_model,
                "provider_status": provider_status,
                "http_status": http_status,
                "effective_model_args": self.request_args,
            })
            append_jsonl(self.ledger_path, entry)
            append_jsonl(self.ledger_path.with_name("raw_calls.jsonl"), {
                "call_id": call_id, "trial_id": self.config["trial_id"], "role": self.role,
                "messages": messages, "response": raw_response,
                "request": request_payload,
                "effective_input": effective_input,
                "adapter_policy": RESPONSES_ADAPTER_POLICY if self.transport == "responses" else None,
                "api_mode": self.mode, "error_type": error_type,
                "status": "error" if error_type else "completed", "provider_status": provider_status,
                "provider": self.provider, "api_transport": self.transport,
                "provider_base_url": self.base_url,
                "provider_request_id": provider_request_id, "response_id": response_id,
                "resolved_model": resolved_model, "http_status": http_status,
                "effective_model_args": self.request_args,
            })


def build_backend(config: dict[str, Any], role: str, ledger_path: str | Path) -> MeteredBackend:
    return MeteredBackend(config, role, ledger_path)
