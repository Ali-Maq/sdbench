"""Metadata export contracts; no live API credentials or patient data required."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdbench.harbor_integration.tracing import build_metadata_export, main, send_metadata_export


class MetadataExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.first = self.root / "artifacts/gatekeeper-evidence/usage.jsonl"
        self.last = self.root / "verifier/judge_api_calls.jsonl"
        self.first.parent.mkdir(parents=True)
        self.last.parent.mkdir(parents=True)
        self.entries = [self.entry("diagnosis", "a"), self.entry("gatekeeper", "b")]
        self.judge = self.entry("judge", "c")
        self.write()

    def entry(self, role, call):
        return {
            "trial_id": "PATIENT_IDENTIFIER_MUST_NOT_LEAVE", "call_id": call,
            "role": role, "model": "PRIVATE_DEPLOYMENT_NAME", "resolved_model": "PRIVATE_MODEL_VERSION",
            "api_mode": "live", "attempt": 1, "status": "completed", "latency_seconds": .5,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "cached_input_tokens": 40, "reasoning_tokens": 10},
            "provider_base_url": "https://secret-resource.example", "messages": "PRIVATE_NOTE",
            "error_type": "PRIVATE_ERROR", "estimated_cost_usd": .01,
            "effective_model_args": {"private": "PRIVATE_ARGUMENT"},
        }

    def write(self):
        self.first.write_text("\n".join(json.dumps(x) for x in self.entries) + "\n")
        self.last.write_text(json.dumps(self.judge) + "\n")

    def test_export_drops_free_text_and_hashes_identifiers(self):
        fake = self.root / "agent/usage.jsonl"
        fake.parent.mkdir()
        fake.write_text("not even valid JSON")
        payload = build_metadata_export(self.root)
        text = json.dumps(payload)
        for secret in ("PATIENT_IDENTIFIER", "PRIVATE_", "secret-resource", '"estimated_cost_usd"'):
            self.assertNotIn(secret, text)
        self.assertEqual(payload, build_metadata_export(self.root))
        self.assertEqual(len(payload["generations"]), 3)
        first = payload["generations"][0]
        self.assertEqual(first["usage_details"], {"input": 100, "output": 20, "total": 120})
        self.assertIsNone(first["metadata"]["actual_paid_usd"])
        self.assertEqual(first["metadata"]["tokens"]["reasoning_tokens"], 10)

    def test_missing_usage_stays_unknown_and_failed_attempt_remains(self):
        self.entries[0].update(status="error", usage={})
        self.write()
        payload = build_metadata_export(self.root)
        first = payload["generations"][0]
        self.assertEqual(first["usage_details"], {})
        self.assertEqual(first["metadata"]["status"], "error")
        self.assertTrue(all(value is None for value in first["metadata"]["tokens"].values()))

    def test_invalid_roles_mixed_trials_and_counts_are_rejected(self):
        for mutate in (
            lambda: self.judge.update(role="diagnosis"),
            lambda: self.judge.update(trial_id="different"),
            lambda: self.judge.update(status="free text could contain private data"),
            lambda: self.judge["usage"].update(input_tokens=True),
            lambda: self.judge["usage"].update(total_tokens=999),
            lambda: self.judge["usage"].update(reasoning_tokens=999),
        ):
            self.judge = self.entry("judge", "c")
            mutate()
            self.write()
            with self.assertRaises(ValueError):
                build_metadata_export(self.root)

    def test_duplicates_deduplicate_but_conflicting_calls_fail(self):
        self.entries.append(dict(self.entries[0]))
        self.write()
        self.assertEqual(len(build_metadata_export(self.root)["generations"]), 3)
        self.entries[-1] = {**self.entries[-1], "attempt": 2}
        self.write()
        with self.assertRaises(ValueError):
            build_metadata_export(self.root)

    def test_symlinked_source_is_rejected(self):
        outside = self.root / "wrong.jsonl"
        self.first.rename(outside)
        self.first.symlink_to(outside)
        with self.assertRaises(ValueError):
            build_metadata_export(self.root)

    def test_default_cli_does_not_construct_network_client(self):
        out = self.root / "preview.json"
        with patch("sdbench.harbor_integration.tracing._configured_client") as client:
            self.assertEqual(main(["--trial-dir", str(self.root), "--output", str(out)]), 0)
        client.assert_not_called()
        self.assertEqual(json.loads(out.read_text())["metadata"]["api_attempts"], 3)
        with self.assertRaises(FileExistsError):
            main(["--trial-dir", str(self.root), "--output", str(out)])

    def test_send_requires_explicit_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "LANGFUSE_BASE_URL"):
                send_metadata_export(build_metadata_export(self.root))

    def test_sdk_calls_do_not_include_model_text_content_or_inferred_costs(self):
        client = Mock()
        receipt = send_metadata_export(build_metadata_export(self.root), client=client)
        root = client.start_observation.return_value
        self.assertEqual(root.start_observation.call_count, 3)
        for call in root.start_observation.call_args_list:
            for forbidden in ("input", "output", "model", "cost_details", "prompt", "model_parameters"):
                self.assertNotIn(forbidden, call.kwargs)
        client.flush.assert_called_once()
        self.assertFalse(receipt["server_acceptance_verified"])

    @unittest.skipUnless(importlib.util.find_spec("langfuse"), "Optional Langfuse SDK is not installed")
    def test_real_sdk_in_memory_export_has_parent_child_spans_without_network(self):
        from langfuse import Langfuse
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        client = Langfuse(
            public_key="pk-lf-sdbench-unit-test", secret_key="test-secret",
            base_url="http://127.0.0.1:1", tracer_provider=provider,
            span_exporter=exporter,
        )
        try:
            payload = build_metadata_export(self.root)
            send_metadata_export(payload, client=client)
            spans = exporter.get_finished_spans()
            self.assertEqual(len(spans), 4)
            parent = next(span for span in spans if span.name == "sdbench.harbor.metadata-export")
            for span in spans:
                self.assertEqual(format(span.context.trace_id, "032x"), payload["trace_id"])
                if span is not parent:
                    self.assertEqual(span.parent.span_id, parent.context.span_id)
                self.assertNotIn("PRIVATE_", json.dumps(dict(span.attributes)))
        finally:
            client.shutdown()


if __name__ == "__main__":
    unittest.main()
