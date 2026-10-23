"""Prepare one synthetic encounter without live API calls."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdbench.cli import isolation
from sdbench.isolation.common import canonical_hash, sdbench_config


class AzureSmokeCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = {
            "dataset": "synthetic", "case_id": "0", "case_title": "Fixture",
            "case_summary": "Public initial presentation",
            "full_text": "PRIVATE_CASE_MARKER", "final_diagnosis": "PRIVATE_ANSWER_MARKER",
        }
        self.env_patch = patch.dict(os.environ)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def arguments(self, **overrides):
        values = dict(model="gpt-5.4", base_url="https://fixture.openai.azure.com/openai/v1",
                      case_id="0", max_turns=3, max_model_calls=4,
                      max_completion_tokens=128, reasoning_effort="low",
                      jobs=self.root / "viewer/jobs", output=self.root / "prepared")
        values.update(overrides)
        return argparse.Namespace(**values)

    def prepare(self, args=None, *, hashes=None):
        built = []

        def build(case, trial, path):
            built.append((dict(case), dict(trial), path))
            path.mkdir(parents=True)
            return path

        with patch.object(isolation.case_data, "get_case_set", return_value=[self.case]), \
             patch.object(isolation, "source_hash", side_effect=hashes, return_value="c" * 64), \
             patch("sdbench.harbor_integration.tasks.build_task", side_effect=build), \
             patch("sdbench.isolation.providers.build_backend", side_effect=AssertionError("No model calls during prepare")), \
             redirect_stdout(io.StringIO()):
            isolation.prepare_smoke(args or self.arguments())
        return built

    def test_one_case_one_native_trial_with_fixed_role_settings(self):
        built = self.prepare()
        manifest = json.loads((self.root / "prepared/manifest.json").read_text())
        job = json.loads((self.root / "prepared/job.json").read_text())
        self.assertEqual(len(built), 1)
        self.assertEqual(manifest["experiment_kind"], "single_model_smoke")
        self.assertEqual(len(manifest["trials"]), 1)
        trial = manifest["trials"][0]
        self.assertEqual(trial["provider_base_url"], "https://fixture.openai.azure.com/openai/v1/")
        self.assertEqual((trial["provider"], trial["api_transport"], trial["api_mode"]),
                         ("azure", "responses", "live"))
        for role in ("diagnosis", "gatekeeper", "judge"):
            self.assertEqual(trial[f"{role}_model"], "gpt-5.4")
            self.assertEqual(trial[f"{role}_model_args"], {
                "max_completion_tokens": 128, "reasoning_effort": "low",
            })
        self.assertEqual((trial["max_turns"], trial["max_model_calls"]), (3, 4))
        self.assertEqual(trial["case_hash"], canonical_hash(self.case))
        self.assertEqual(trial["rate_cards"], {})
        self.assertNotIn("gk_models", trial)
        self.assertNotIn("PRIVATE_CASE_MARKER", json.dumps(manifest))
        self.assertNotIn("PRIVATE_ANSWER_MARKER", json.dumps(manifest))
        protocol = {key: value for key, value in trial.items()
                    if key not in {"protocol_hash", "pair_id", "trial_id", "execution_index"}}
        self.assertEqual(trial["protocol_hash"], canonical_hash(protocol))
        # Exercise the actual service/judge configuration consumer.
        native = sdbench_config(trial)
        self.assertEqual(native.gatekeeper_model, "gpt-5.4")
        self.assertFalse(native.compute_costs)
        self.assertIsNone(native.max_tests_per_turn)
        self.assertEqual((job["n_attempts"], job["n_concurrent_trials"]), (1, 1))
        self.assertEqual(job["jobs_dir"], str((self.root / "viewer/jobs").resolve()))
        self.assertEqual(job["datasets"], [{"path": str((self.root / "prepared/tasks").resolve())}])
        self.assertEqual(job["agents"][0]["model_name"], "gpt-5.4")
        self.assertNotIn("orchestrator", job)

    def test_changed_model_limit_or_endpoint_changes_protocol_identity(self):
        hashes = []
        settings = [{}, {"model": "other-deployment"}, {"max_model_calls": 5},
                    {"base_url": "https://other.openai.azure.com/openai/v1/"}]
        for index, override in enumerate(settings):
            output = self.root / f"prepare-{index}"
            self.prepare(self.arguments(output=output, **override))
            manifest = json.loads((output / "manifest.json").read_text())
            hashes.append(manifest["trials"][0]["protocol_hash"])
        self.assertEqual(len(set(hashes)), len(settings))

    def test_invalid_case_endpoint_or_budget_fails_before_task_build(self):
        for override in ({"case_id": "missing"}, {"max_model_calls": 0},
                         {"max_completion_tokens": 0}, {"model": " "},
                         {"base_url": "https://fixture.openai.azure.com.evil.example/openai/v1/"}):
            with self.subTest(override=override), \
                 patch.object(isolation.case_data, "get_case_set", return_value=[self.case]), \
                 patch("sdbench.harbor_integration.tasks.build_task") as build:
                with self.assertRaises(ValueError):
                    isolation.prepare_smoke(self.arguments(**override))
                build.assert_not_called()
                self.assertFalse((self.root / "prepared").exists())

    def test_source_drift_never_publishes_ready_manifest_or_job(self):
        with self.assertRaisesRegex(ValueError, "Source changed"):
            self.prepare(hashes=["a" * 64, "b" * 64])
        self.assertFalse((self.root / "prepared/manifest.json").exists())
        self.assertFalse((self.root / "prepared/job.json").exists())

    def test_smoke_is_not_accepted_by_paired_summary(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "single-model smoke"):
            isolation.summarize(argparse.Namespace(
                manifest=self.root / "prepared/manifest.json", jobs=self.root / "viewer/jobs", output=None,
            ))



if __name__ == "__main__":
    unittest.main()
