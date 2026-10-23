"""Azure Responses transport tests using an in-process HTTP mock only."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from sdbench.isolation import providers
from sdbench.models import ModelCallLimitError, call_llm_with_retry


AZURE_BASE = "https://aif-shajam-ari-nonprod-eus2-01.cognitiveservices.azure.com/openai/v1/"


def response_body(*, status="completed", usage=True, text="A model answer"):
    return {
        "id": "resp-test",
        "object": "response",
        "created_at": 1234567890,
        "model": "gpt-5.4-resolved",
        "status": status,
        "error": {"code": "server_error", "message": "Protected provider detail"} if status == "failed" else None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "output": [{
            "id": "msg-test", "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
        }],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 40,
            "total_tokens": 140,
            "input_tokens_details": {"cached_tokens": 25},
            "output_tokens_details": {"reasoning_tokens": 10},
        } if usage else None,
    }


class AzureResponsesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.token_file = self.root / "token.json"
        self.write_token("first-test-token")
        self.environment = patch.dict(os.environ, {"SDBENCH_AZURE_TOKEN_FILE": str(self.token_file)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.directory.cleanup)
        self.config = {
            "trial_id": "azure-test", "protocol_hash": "protocol-test", "api_mode": "live",
            "provider": "azure", "api_transport": "responses", "provider_base_url": AZURE_BASE,
            "diagnosis_model": "gpt-5.4", "max_model_calls": 4,
            "diagnosis_model_args": {"reasoning_effort": "high", "max_completion_tokens": 800},
            "rate_cards": {"gpt-5.4": {
                "effective_date": "2026-10-22", "input_per_million": 2,
                "output_per_million": 10, "cached_input_per_million": 0.5,
            }},
        }
        self.requests = []

    def write_token(self, value, *, expires_on=None):
        self.token_file.write_text(json.dumps({
            "accessToken": value,
            "expires_on": time.time() + 3600 if expires_on is None else expires_on,
        }))

    def backend(self, body=None, *, http_status=200, config=None):
        body = response_body() if body is None else body

        def handler(request):
            self.requests.append(request)
            return httpx.Response(http_status, json=body, headers={"x-request-id": "req-test"})

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http_client.close)
        sdk_constructor = []

        def client_factory(**kwargs):
            sdk_constructor.append(kwargs)
            return OpenAI(http_client=http_client, **kwargs)

        with patch.object(providers, "OpenAI", side_effect=client_factory):
            backend = providers.build_backend(config or self.config, "diagnosis", self.root / "usage.jsonl")
        self.assertEqual(sdk_constructor[0]["max_retries"], 0)
        return backend

    def ledger(self):
        return [json.loads(line) for line in (self.root / "usage.jsonl").read_text().splitlines()]

    def raw(self):
        return [json.loads(line) for line in (self.root / "raw_calls.jsonl").read_text().splitlines()]

    def assert_attempt_timestamps(self, usage, raw):
        for key in ("started_at", "finished_at"):
            self.assertEqual(raw[key], usage[key])
            self.assertEqual(datetime.fromisoformat(usage[key]).utcoffset(), timedelta(0))
        self.assertLessEqual(usage["started_at"], usage["finished_at"])

    def test_exhausted_call_budget_does_not_retry_or_create_a_provider_attempt(self):
        backend = self.backend(config={**self.config, "max_model_calls": 1})
        messages = [{"role": "user", "content": "Fixture"}]
        call_llm_with_retry(backend, messages=messages)
        with patch("sdbench.models.time.sleep") as sleep:
            with self.assertRaises(ModelCallLimitError):
                call_llm_with_retry(backend, messages=messages)
        sleep.assert_not_called()
        self.assertEqual(backend.calls, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.ledger()), 1)

    def test_completed_response_adapts_text_usage_and_translates_arguments(self):
        backend = self.backend()
        messages = [{"role": "system", "content": "Existing prompt"}, {"role": "user", "content": "Case question"}]
        result = backend(messages=messages)
        self.assertEqual(result.object, "chat.completion")
        self.assertEqual(result.choices[0].message.content, "A model answer")
        self.assertEqual(result.usage.prompt_tokens, 100)
        self.assertEqual(result.usage.completion_tokens, 40)
        self.assertEqual(result.usage.total_tokens, 140)
        self.assertEqual(result.usage.prompt_tokens_details.cached_tokens, 25)
        self.assertEqual(result.usage.completion_tokens_details.reasoning_tokens, 10)
        request = self.requests[0]
        self.assertEqual(str(request.url), AZURE_BASE + "responses")
        payload = json.loads(request.content)
        self.assertEqual(payload["input"], messages)
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["max_output_tokens"], 800)
        self.assertFalse(payload["store"])
        self.assertFalse(payload["stream"])
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("reasoning_effort", payload)
        usage = self.ledger()[0]
        self.assertEqual(usage["provider_status"], "completed")
        self.assertEqual(usage["provider_request_id"], "req-test")
        self.assertEqual(usage["response_id"], "resp-test")
        self.assertEqual(usage["resolved_model"], "gpt-5.4-resolved")
        self.assertEqual(usage["usage"]["cached_input_tokens"], 25)
        self.assertEqual(usage["usage"]["reasoning_tokens"], 10)
        self.assertEqual(usage["usage"]["total_tokens"], 140)
        self.assertAlmostEqual(usage["estimated_cost_usd"], 0.0005625)
        self.assertIsNone(usage["actual_paid_usd"])
        raw = self.raw()[0]
        self.assert_attempt_timestamps(usage, raw)
        self.assertEqual(raw["response"]["object"], "response")
        self.assertEqual(raw["response"]["usage"]["input_tokens"], 100)
        self.assertEqual(raw["request"], payload)
        self.assertNotIn("first-test-token", json.dumps([raw, usage]))

    def test_token_is_reloaded_for_every_sdk_request(self):
        backend = self.backend()
        backend(messages=[{"role": "user", "content": "first"}])
        self.write_token("refreshed-test-token")
        backend(messages=[{"role": "user", "content": "second"}])
        self.assertEqual(self.requests[0].headers["authorization"], "Bearer first-test-token")
        self.assertEqual(self.requests[1].headers["authorization"], "Bearer refreshed-test-token")
        self.assertNotIn("refreshed-test-token", json.dumps(self.raw()))

    def test_identical_commentary_and_final_answer_execute_questions_once(self):
        from sdbench.agents.baseline import extract_test_list_from_response

        text = "\n".join(f"<question>Question {index}?</question>" for index in range(10))
        body = response_body(text=text)
        final = body["output"][0]
        final["phase"] = "final_answer"
        commentary = copy.deepcopy(final)
        commentary.update(id="msg-commentary", phase="commentary")
        body["output"] = [
            {"id": "reasoning-test", "type": "reasoning", "summary": []}, commentary, final,
        ]
        result = self.backend(body)(messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(result.choices[0].message.content, text)
        self.assertEqual(len(extract_test_list_from_response(result.choices[0].message.content)), 10)
        raw = self.raw()[0]
        self.assertEqual(len(raw["response"]["output"]), 3)
        self.assertEqual(raw["response"]["output"][1]["phase"], "commentary")
        self.assertEqual(raw["response"]["output"][2]["phase"], "final_answer")
        self.assertEqual(raw["adapter_policy"]["version"], "sdbench-final-text-v1")

    def test_completed_commentary_only_response_is_not_an_action(self):
        body = response_body(text="<test>Observation</test>")
        body["output"][0]["phase"] = "commentary"
        with self.assertRaises(RuntimeError):
            self.backend(body)(messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(self.ledger()[0]["usage"]["total_tokens"], 140)
        self.assertEqual(self.raw()[0]["response"]["output"][0]["phase"], "commentary")

    def test_final_answer_parts_concatenate_in_order_without_commentary(self):
        body = response_body(text="<diagnosis>")
        body["output"][0]["phase"] = "final_answer"
        middle = copy.deepcopy(body["output"][0])
        middle.update(id="middle", phase="commentary")
        middle["content"][0]["text"] = "discard this intermediate text"
        last = copy.deepcopy(body["output"][0])
        last["id"] = "last"
        last["content"][0]["text"] = "Answer</diagnosis>"
        body["output"].extend([middle, last])
        result = self.backend(body)(messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(result.choices[0].message.content, "<diagnosis>Answer</diagnosis>")

    def test_assistant_history_has_final_phase_and_other_roles_do_not(self):
        messages = [
            {"role": "system", "content": "Instructions"},
            {"role": "user", "content": "Presentation"},
            {"role": "assistant", "content": "<test>Observation</test>"},
            {"role": "user", "content": "Observation result"},
        ]
        original = copy.deepcopy(messages)
        self.backend()(messages=messages)
        actual = json.loads(self.requests[0].content)["input"]
        self.assertEqual(actual[2]["phase"], "final_answer")
        self.assertTrue(all("phase" not in actual[index] for index in (0, 1, 3)))
        self.assertEqual(messages, original)
        self.assertEqual(self.raw()[0]["effective_input"], actual)
        self.assertEqual(self.raw()[0]["request"], json.loads(self.requests[0].content))
        self.assertEqual(self.raw()[0]["messages"], original)

    def test_unphased_legacy_messages_still_concatenate_in_order(self):
        body = response_body(text="First ")
        last = copy.deepcopy(body["output"][0])
        last["id"] = "last"
        last["content"][0]["text"] = "second"
        body["output"].append(last)
        result = self.backend(body)(messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(result.choices[0].message.content, "First second")

    def test_mixed_phased_and_unphased_output_is_rejected(self):
        for phase in ("commentary", "final_answer"):
            with self.subTest(phase=phase):
                body = response_body(text="<test>Ambiguous action</test>")
                phased = copy.deepcopy(body["output"][0])
                phased.update(id="phased", phase=phase)
                body["output"].append(phased)
                with self.assertRaises(RuntimeError):
                    self.backend(body)(messages=[{"role": "user", "content": "hello"}])
                self.assertEqual(self.ledger()[-1]["status"], "error")

    def test_expired_or_near_expiry_token_prevents_http_request(self):
        backend = self.backend()
        for expiry in (time.time() - 1, time.time() + 30):
            with self.subTest(expiry=expiry):
                self.write_token("expired-test-token", expires_on=expiry)
                with self.assertRaises(RuntimeError) as raised:
                    backend(messages=[{"role": "user", "content": "hello"}])
                self.assertNotIn("expired-test-token", str(raised.exception))
        self.assertEqual(self.requests, [])
        self.assertTrue(all(record["usage"]["input_tokens"] is None for record in self.ledger()))

    def test_malformed_missing_and_invalid_token_files_fail_without_disclosure(self):
        reader = providers.AzureTokenFile(self.token_file)
        for content in ("not JSON: token-secret", "[]", '{"accessToken":"token-secret"}',
                        '{"accessToken":"token-secret","expires_on":true}',
                        '{"accessToken":"token-secret","expires_on":"NaN"}'):
            with self.subTest(content=content):
                self.token_file.write_text(content)
                with self.assertRaises(RuntimeError) as raised:
                    reader()
                self.assertNotIn("token-secret", str(raised.exception))
        self.token_file.unlink()
        with self.assertRaises(RuntimeError):
            reader()

    def test_incomplete_and_failed_responses_retain_usage_and_raw_status(self):
        for status in ("incomplete", "failed"):
            with self.subTest(status=status):
                backend = self.backend(response_body(status=status, text="Partial answer 5"))
                with self.assertRaises(RuntimeError):
                    backend(messages=[{"role": "user", "content": "hello"}])
                entry = self.ledger()[-1]
                raw = self.raw()[-1]
                self.assertEqual(entry["status"], "error")
                self.assertEqual(entry["provider_status"], status)
                self.assertEqual(entry["usage"]["total_tokens"], 140)
                self.assertAlmostEqual(entry["estimated_cost_usd"], 0.0005625)
                self.assertEqual(raw["response"]["status"], status)
                self.assertEqual(raw["provider_request_id"], "req-test")

    def test_http_error_does_not_lose_usage_outside_error_member(self):
        body = response_body(status="failed")
        backend = self.backend(body, http_status=400)
        with self.assertRaises(RuntimeError) as raised:
            backend(messages=[{"role": "user", "content": "hello"}])
        self.assertNotIn("Protected provider detail", str(raised.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.ledger()[0]["http_status"], 400)
        self.assertEqual(self.ledger()[0]["usage"]["input_tokens"], 100)
        self.assertEqual(self.raw()[0]["response"], body)
        self.assert_attempt_timestamps(self.ledger()[0], self.raw()[0])

    def test_mock_attempt_timestamps_do_not_determine_latency(self):
        backend = providers.build_backend(
            {**self.config, "api_mode": "mock"}, "diagnosis", self.root / "usage.jsonl",
        )
        start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        # A wall-clock correction must not make the measured duration negative.
        finish = start - timedelta(seconds=30)
        with patch.object(providers, "datetime") as clock, \
                patch.object(providers.time, "monotonic", side_effect=[10.0, 12.5]):
            clock.now.side_effect = [start, finish]
            backend(messages=[{"role": "user", "content": "Fictional transport fixture"}])
        entry, raw = self.ledger()[0], self.raw()[0]
        self.assertEqual(entry["started_at"], start.isoformat())
        self.assertEqual(entry["finished_at"], finish.isoformat())
        self.assertEqual(raw["started_at"], entry["started_at"])
        self.assertEqual(raw["finished_at"], entry["finished_at"])
        self.assertEqual(entry["latency_seconds"], 2.5)
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["api_mode"], "mock")
        self.assertEqual(self.requests, [])

    def test_missing_usage_is_unknown_instead_of_zero(self):
        result = self.backend(response_body(usage=False))(messages=[{"role": "user", "content": "hello"}])
        self.assertIsNone(result.usage)
        entry = self.ledger()[0]
        for field in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"):
            self.assertIsNone(entry["usage"][field])
        self.assertIsNone(entry["estimated_cost_usd"])

    def test_partial_usage_preserves_known_counts_only_in_ledger(self):
        body = response_body()
        body["usage"] = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 25}}
        result = self.backend(body)(messages=[{"role": "user", "content": "hello"}])
        self.assertIsNone(result.usage)
        self.assertEqual(self.ledger()[0]["usage"]["input_tokens"], 100)
        self.assertIsNone(self.ledger()[0]["usage"]["output_tokens"])
        self.assertIsNone(self.ledger()[0]["estimated_cost_usd"])

    def test_empty_completed_output_cannot_be_used_as_a_completion(self):
        backend = self.backend(response_body(text=""))
        with self.assertRaises(RuntimeError):
            backend(messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(self.ledger()[0]["usage"]["total_tokens"], 140)

    def test_unsupported_responses_arguments_are_rejected_before_client_construction(self):
        for argument in ("seed", "frequency_penalty", "presence_penalty", "tools", "base_url"):
            with self.subTest(argument=argument):
                config = copy.deepcopy(self.config)
                config["diagnosis_model_args"][argument] = 1
                with patch.object(providers, "OpenAI") as constructor:
                    with self.assertRaises(ValueError):
                        providers.build_backend(config, "diagnosis", self.root / "usage.jsonl")
                    constructor.assert_not_called()

    def test_direct_openai_chat_completion_defaults_are_unchanged(self):
        config = {key: value for key, value in self.config.items()
                  if key not in {"provider", "api_transport", "provider_base_url"}}
        body = {
            "id": "chat-test", "object": "chat.completion", "created": 1, "model": "gpt-5.4",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Answer"}, "finish_reason": "stop"}],
            "usage": None,
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "direct-test-key"}):
            result = self.backend(body, config=config)(messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(str(self.requests[0].url), "https://api.openai.com/v1/chat/completions")
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["max_completion_tokens"], 800)
        self.assertEqual(result.choices[0].message.content, "Answer")


class AzureEndpointTests(unittest.TestCase):
    def test_only_explicit_azure_v1_endpoints_are_accepted(self):
        self.assertEqual(providers.validate_azure_base_url(AZURE_BASE.rstrip("/")), AZURE_BASE)
        self.assertEqual(providers.validate_azure_base_url("https://resource.openai.azure.com/openai/v1/"),
                         "https://resource.openai.azure.com/openai/v1/")
        for value in (
            None, "https://cognitiveservices.azure.com/openai/v1/",
            "http://resource.openai.azure.com/openai/v1/",
            "https://resource.openai.azure.com.attacker.invalid/openai/v1/",
            "https://resource.openai.azure.com@attacker.invalid/openai/v1/",
            "https://secret@resource.openai.azure.com/openai/v1/",
            "https://resource.openai.azure.com/openai/v1/?api-key=secret",
            "https://resource.openai.azure.com/openai/v1/#secret",
            "https://resource.openai.azure.com:8443/openai/v1/",
            "https://resource.openai.azure.com/another/path",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    providers.validate_azure_base_url(value)


if __name__ == "__main__":
    unittest.main()
