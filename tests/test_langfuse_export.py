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


class FullExportTests(unittest.TestCase):
    def setUp(self):
        from sdbench.isolation.common import canonical_hash
        from sdbench.isolation.recorded import render_selection
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for path in ('agent', 'verifier', 'artifacts/gatekeeper-evidence'):
            (self.root / path).mkdir(parents=True)
        self.case = json.loads((Path(__file__).parent / 'fixtures/recorded_episode.json').read_text())
        self.case_path = self.root / 'case.json'
        self.write('case.json', self.case)
        finding = render_selection(self.case, 'CBC', ['cbc'])
        missing = render_selection(self.case, 'MRI', [])
        self.events = [{'event': 'tests', 'sequence': 2, 'timestamp': 1767268801.0,
                        'requested_test_names': ['CBC', 'MRI'], 'test_results': [finding, missing]},
                       {'event': 'terminal', 'status': 'completed', 'diagnosis': 'Multiple myeloma'}]
        self.write('artifacts/gatekeeper-evidence/events.jsonl', self.events, lines=True)
        self.verified = {
            'trial_id': 'fictional-full-trial', 'status': 'completed', 'case_id': self.case['case_id'],
            'case_hash': canonical_hash(self.case), 'evidence_hash': canonical_hash(self.events),
            'diagnosis': 'Multiple myeloma', 'diagnosis_score': 5, 'correct': True,
            'api_mode': 'mock', 'mocked': True, 'protocol_hash': 'fixture-protocol',
            'diagnosis_model': 'test-model', 'gatekeeper_model': 'test-model', 'judge_model': 'test-model',
            'gatekeeper_mode': 'recorded_only', 'recorded_evidence_integrity': 'validated',
            'test_logs': [{**{k: v for k, v in item.items() if k != 'results'}, 'result': item['results']}
                          for item in (finding, missing)],
            'judge': {'check_diagnosis_output': '**REASONING**: Fixture. **SCORE**: 5'},
        }
        self.write('verifier/result.json', self.verified)
        self.write('result.json', {'finished_at': '2026-01-01T12:00:02+00:00', 'exception_info': None})
        self.write('agent/diagnosis.json', {'status': 'completed', 'diagnosis': 'Multiple myeloma'})
        self.ledger, self.raw = [], []
        for index, role in enumerate(('diagnosis', 'gatekeeper', 'judge')):
            common = {'trial_id': 'fictional-full-trial', 'call_id': role, 'role': role,
                      'status': 'completed', 'api_mode': 'mock',
                      'started_at': f'2026-01-01T12:00:0{index}+00:00',
                      'finished_at': f'2026-01-01T12:00:0{index}.500000+00:00'}
            self.ledger.append({**common, 'model': 'test-model', 'attempt': 1, 'latency_seconds': .5,
                                'protocol_hash': 'fixture-protocol',
                                'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15,
                                          'cached_input_tokens': None, 'reasoning_tokens': None}})
            self.raw.append({**common, 'request': {'model': 'test-model'},
                'messages': [{'role': 'system', 'content': self.case['full_text']
                              if role == 'judge' else 'No reference in this role.'}],
                'response': {'object': 'response', 'usage': {'input_tokens': 10, 'output_tokens': 5},
                             'output': [{'type': 'reasoning', 'encrypted_content': 'HIDDEN_DO_NOT_EXPORT'},
                                        {'type': 'message', 'phase': 'final_answer',
                                         'content': [{'type': 'output_text', 'text': 'Visible response'}]}]}})
        self.write_calls()

    def write(self, name, value, lines=False):
        (self.root / name).write_text(''.join(json.dumps(x) + '\n' for x in value) if lines else json.dumps(value))

    def write_calls(self):
        for collection, name in ((self.ledger, 'usage.jsonl'), (self.raw, 'raw_calls.jsonl')):
            self.write('artifacts/gatekeeper-evidence/' + name, collection[:2], lines=True)
            target = 'judge_api_calls.jsonl' if name == 'usage.jsonl' else name
            self.write('verifier/' + target, collection[2:], lines=True)

    def build(self):
        from sdbench.harbor_integration.tracing import build_full_export
        return build_full_export(self.root, self.case_path)

    def test_full_tree_exposes_exact_findings_and_reference_only_under_judge(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('socket.create_connection', side_effect=AssertionError('No network')):
            payload = self.build()
        self.assertEqual(payload, self.build())
        self.assertNotIn('HIDDEN_DO_NOT_EXPORT', json.dumps(payload))
        roles = payload['roles']
        for item in (payload['input'], payload['output'], payload['metadata'], roles[0], roles[1]):
            self.assertNotIn('FICTIONAL_REFERENCE_ONLY_MARKER', json.dumps(item))
        self.assertIn('FICTIONAL_REFERENCE_ONLY_MARKER', json.dumps(roles[2]))
        self.assertEqual(roles[1]['children'][-1]['children'][0]['output'], self.events[0]['test_results'][0])
        self.assertEqual(roles[1]['children'][-1]['children'][1]['output']['evidence_status'], 'not_documented')
        self.assertEqual(payload['metadata']['api_attempts'], 3)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_source_case_and_usage_tampering_fail_closed(self):
        from sdbench.harbor_integration.tracing import build_full_export
        self.case['full_text'] += 'changed'
        self.write('case.json', self.case)
        with self.assertRaisesRegex(ValueError, 'reference case'):
            build_full_export(self.root, self.case_path)
        self.case['full_text'] = self.case['full_text'][:-7]
        self.write('case.json', self.case)
        self.raw[0]['response']['usage']['input_tokens'] = 99
        self.write_calls()
        with self.assertRaisesRegex(ValueError, 'token counts'):
            self.build()

    def test_missing_raw_call_wrong_role_and_timestamp_mismatch_fail(self):
        self.raw[0]['role'] = 'judge'
        self.write_calls()
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.build()
        self.raw[0]['role'] = 'diagnosis'
        self.raw[0]['finished_at'] = None
        self.write_calls()
        with self.assertRaisesRegex(ValueError, 'timestamps'):
            self.build()

    def test_full_send_rejects_non_loopback_before_sdk_construction(self):
        from sdbench.harbor_integration.tracing import send_full_export
        for host in ('https://cloud.langfuse.com', 'http://localhost:3001', 'http://192.168.1.2:3001'):
            with self.subTest(host=host), patch.dict(os.environ, {'LANGFUSE_BASE_URL': host}, clear=True):
                with self.assertRaisesRegex(ValueError, 'literal loopback'):
                    send_full_export(self.build())

    def test_failed_api_attempt_is_preserved_with_unknown_usage(self):
        common = {"call_id": "failed-diagnosis", "role": "diagnosis", "status": "error",
                  "trial_id": "fictional-full-trial", "api_mode": "mock"}
        failed_ledger = {**self.ledger[0], **common, "usage": {}, "attempt": 2}
        failed_raw = {**self.raw[0], **common, "response": None}
        self.write('artifacts/gatekeeper-evidence/usage.jsonl', [*self.ledger[:2], failed_ledger], lines=True)
        self.write('artifacts/gatekeeper-evidence/raw_calls.jsonl', [*self.raw[:2], failed_raw], lines=True)
        payload = self.build()
        failed = payload['roles'][0]['children'][-1]
        self.assertEqual(failed['metadata']['status'], 'error')
        self.assertEqual(failed['usage_details'], {})
        self.assertEqual(failed['output'], [])
        self.assertEqual(payload['metadata']['api_attempts'], 4)

    @unittest.skipUnless(importlib.util.find_spec('langfuse'), 'Optional Langfuse SDK is not installed')
    def test_typed_sdk_hierarchy_timestamps_usage_and_acknowledgements(self):
        from types import SimpleNamespace
        from sdbench.harbor_integration.tracing import _full_ingestion_events, send_full_export
        payload = self.build()
        events = _full_ingestion_events(payload)
        bodies = [event.body for event in events]
        generations = [body for body in bodies if hasattr(body, 'usage_details')]
        self.assertEqual(len(generations), 3)
        self.assertEqual(sum(body.usage_details['total'] for body in generations), 45)
        self.assertEqual(generations[0].start_time.isoformat(), self.ledger[0]['started_at'])
        self.assertEqual(generations[0].end_time.isoformat(), self.ledger[0]['finished_at'])
        self.assertTrue(all(body.model is None and body.cost_details is None for body in generations))
        by_id = {body.id: body for body in bodies}
        for gen in generations:
            self.assertTrue(by_id[gen.parent_observation_id].name.startswith('sdbench.'))
        ids = [body.id for body in bodies]
        self.assertEqual(ids, [event.body.id for event in _full_ingestion_events(payload)])
        typed = [body for body in bodies if hasattr(body, 'type')]
        self.assertTrue(all(body.type in {'SPAN', 'GENERATION', 'EVENT'} for body in typed))
        tools = [body for body in bodies if getattr(body, 'metadata', None)
                 and body.metadata.get('workflow_observation_type') == 'retriever']
        self.assertEqual(len(tools), 2)
        self.assertTrue(all(body.start_time == body.end_time for body in tools))
        self.assertTrue(all(body.start_time.timestamp() == self.events[0]['timestamp'] for body in tools))
        client = Mock()
        client.api.ingestion.batch.side_effect = lambda *, batch: SimpleNamespace(
            errors=[], successes=[SimpleNamespace(id=item.id) for item in batch])
        result = send_full_export(payload, client=client)
        self.assertTrue(result['server_acceptance_verified'])
        client.api.ingestion.batch.return_value = SimpleNamespace(errors=['bad'], successes=[])
        client.api.ingestion.batch.side_effect = None
        with self.assertRaisesRegex(RuntimeError, 'acknowledge'):
            send_full_export(payload, client=client)

    @unittest.skipUnless(importlib.util.find_spec('langfuse'), 'Optional Langfuse SDK is not installed')
    def test_missing_and_reversed_timestamps_are_not_invented(self):
        from sdbench.harbor_integration.tracing import _full_ingestion_events
        for collection in (self.ledger, self.raw):
            collection[0].pop('started_at')
            collection[0].pop('finished_at')
            collection[1]['started_at'], collection[1]['finished_at'] = (
                collection[1]['finished_at'], collection[1]['started_at'])
        self.write_calls()
        generations = [event.body for event in _full_ingestion_events(self.build())
                       if event.type == 'generation-create']
        for body in generations[:2]:
            self.assertEqual(body.start_time, body.end_time)
            self.assertIn('export-time placeholder', body.metadata['displayed_timing'])

    def test_full_cli_preview_is_private_and_does_not_send(self):
        import stat
        output = self.root / 'full-preview.json'
        with patch('sdbench.harbor_integration.tracing._configured_client') as client:
            self.assertEqual(main(['--trial-dir', str(self.root), '--case', str(self.case_path),
                                   '--content', 'full', '--output', str(output)]), 0)
        client.assert_not_called()
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
