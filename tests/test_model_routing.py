"""Routing one request must not change the caller's or another request's settings."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from sdbench import models


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(models.os.environ, {
            "SDBENCH_FORCE_AZURE": "0", "SDBENCH_OPENROUTER_ZDR_ONLY": "1",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_reusing_caller_settings_does_not_retain_previous_routing(self):
        body = {"provider": {"order": ["fixture-provider"]}}
        original = deepcopy(body)
        _, _, first = models._configure_completion("gpt-5", extra_body=body)
        _, _, second = models._configure_completion(
            "gpt-5", extra_body=body, openrouter_zdr_only=False,
        )
        self.assertTrue(first["extra_body"]["provider"]["zdr"])
        self.assertNotIn("zdr", second["extra_body"]["provider"])
        self.assertEqual(body, original)
        first["extra_body"]["provider"]["order"].append("another-provider")
        self.assertEqual(second["extra_body"]["provider"]["order"], ["fixture-provider"])
        self.assertEqual(body, original)

    def test_model_defaults_remain_unchanged_between_calls(self):
        original = deepcopy(models.MODEL_ARG_DEFAULTS)
        _, _, first = models._configure_completion("glm-4.7")
        first["extra_body"]["reasoning"]["effort"] = "low"
        _, _, second = models._configure_completion("glm-4.7", openrouter_zdr_only=False)
        self.assertEqual(models.MODEL_ARG_DEFAULTS, original)
        self.assertEqual(second["extra_body"]["reasoning"], {"effort": "high"})
        self.assertNotIn("zdr", second["extra_body"]["provider"])

    def test_explicit_provider_override_precedence_is_preserved(self):
        body = {"provider": {"data_collection": "allow", "zdr": False}, "custom": [1]}
        _, _, args = models._configure_completion("glm-4.7", extra_body=body)
        self.assertEqual(args["extra_body"], body)
        self.assertNotIn("reasoning", args["extra_body"])
        self.assertIsNot(args["extra_body"], body)

    def test_azure_and_local_bodies_are_also_detached(self):
        for model, route_args, expected in (
            ("gpt-5", {"force_azure": True}, "azure"),
            ("local-fixture", {}, "local"),
        ):
            with self.subTest(provider=expected):
                body = {"nested": {"values": [1]}}
                route, _, args = models._configure_completion(model, extra_body=body, **route_args)
                self.assertEqual(route, expected)
                args["extra_body"]["nested"]["values"].append(2)
                self.assertEqual(body, {"nested": {"values": [1]}})

    def test_concurrent_calls_do_not_share_routing_state(self):
        body = {"provider": {"order": ["fixture-provider"]}}
        original = deepcopy(body)
        settings = [bool(i % 2) for i in range(40)]

        def configure(enabled):
            return models._configure_completion(
                "gpt-5", extra_body=body, openrouter_zdr_only=enabled,
            )[2]["extra_body"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            outputs = list(executor.map(configure, settings))
        for enabled, output in zip(settings, outputs):
            self.assertEqual(output["provider"].get("zdr", False), enabled)
        self.assertEqual(body, original)
        self.assertEqual(len({id(output) for output in outputs}), len(outputs))

    def test_sync_and_async_entrypoints_use_independent_configuration(self):
        body = {"provider": {"order": ["fixture-provider"]}}
        with patch.object(models, "_get_openrouter_client"), \
             patch.object(models, "_get_async_openrouter_client"), \
             patch.object(models, "_chat_completion_with_retry") as sync, \
             patch.object(models, "_chat_completion_with_retry_async", new_callable=AsyncMock) as async_call:
            models.create_chat_completion("gpt-5", [], extra_body=body)
            asyncio.run(models.create_chat_completion_async(
                "gpt-5", [], extra_body=body, openrouter_zdr_only=False,
            ))
        self.assertTrue(sync.call_args.kwargs["extra_body"]["provider"]["zdr"])
        self.assertNotIn("zdr", async_call.call_args.kwargs["extra_body"]["provider"])
        self.assertEqual(body, {"provider": {"order": ["fixture-provider"]}})

    def test_azure_client_cache_tracks_endpoint_version_and_credential(self):
        models._cached_azure_client.cache_clear()
        self.addCleanup(models._cached_azure_client.cache_clear)
        for getter, constructor in (
            (models._get_azure_client, "AzureOpenAI"),
            (models._get_async_azure_client, "AsyncAzureOpenAI"),
        ):
            with self.subTest(constructor=constructor), \
                 patch.object(models, constructor, side_effect=lambda **kwargs: object()) as create, \
                 patch("builtins.print"), patch.dict(models.os.environ, {
                     "AZURE_OPENAI_API_KEY": "fixture-key-one",
                     "AZURE_OPENAI_ENDPOINT": "https://first.invalid",
                     "AZURE_OPENAI_API_VERSION": "fixture-version-one",
                 }):
                first = getter()
                self.assertIs(getter(), first)
                for name, value in (
                    ("AZURE_OPENAI_ENDPOINT", "https://second.invalid"),
                    ("AZURE_OPENAI_API_VERSION", "fixture-version-two"),
                    ("AZURE_OPENAI_API_KEY", "fixture-key-two"),
                ):
                    models.os.environ[name] = value
                    next_client = getter()
                    self.assertIsNot(next_client, first)
                    self.assertIs(getter(), next_client)
                    first = next_client
                self.assertEqual(create.call_count, 4)
                self.assertEqual(create.call_args.kwargs, {
                    "api_key": "fixture-key-two", "azure_endpoint": "https://second.invalid",
                    "api_version": "fixture-version-two",
                })

    def test_openrouter_client_cache_tracks_credentials_for_sync_and_async(self):
        models._cached_openrouter_client.cache_clear()
        self.addCleanup(models._cached_openrouter_client.cache_clear)
        for getter, constructor in (
            (models._get_openrouter_client, "OpenAI"),
            (models._get_async_openrouter_client, "AsyncOpenAI"),
        ):
            with self.subTest(constructor=constructor), \
                 patch.object(models, constructor, side_effect=lambda **kwargs: object()) as create, \
                 patch.dict(models.os.environ, {"TEST_ROUTER_KEY": "fixture-one"}):
                first = getter("TEST_ROUTER_KEY")
                self.assertIs(getter("TEST_ROUTER_KEY"), first)
                models.os.environ["TEST_ROUTER_KEY"] = "fixture-two"
                second = getter("TEST_ROUTER_KEY")
                self.assertIsNot(second, first)
                self.assertIs(getter("TEST_ROUTER_KEY"), second)
                self.assertEqual(create.call_count, 2)
                models.os.environ["TEST_ROUTER_KEY"] = ""
                with self.assertRaisesRegex(ValueError, "TEST_ROUTER_KEY"):
                    getter("TEST_ROUTER_KEY")


if __name__ == "__main__":
    unittest.main()
