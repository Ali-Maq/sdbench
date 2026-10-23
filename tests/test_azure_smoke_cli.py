"""Single-encounter preparation and runtime credentials, with no live API calls."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdbench.cli import azure_harbor, isolation
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
        settings = [{}, {"model": "other-deployment"}, {"max_model_calls": 5}, {"diagnostic_agent": "experimental_panel"},
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

    def test_token_refresh_is_atomic_private_and_discards_account_metadata(self):
        credential_dir = self.root / "credentials"
        credential_dir.mkdir(mode=0o700)
        payloads = [dict(accessToken="FIRST_SECRET", expires_on=2000, tenant="PRIVATE_TENANT"),
                    dict(accessToken="SECOND_SECRET", expires_on=2100, subscription="PRIVATE_SUBSCRIPTION")]
        results = [subprocess.CompletedProcess(["az"], 0, json.dumps(value), "PRIVATE_STDERR")
                   for value in payloads]
        with patch.object(azure_harbor.subprocess, "run", side_effect=results) as run, \
             patch.object(azure_harbor.time, "time", return_value=1000):
            self.assertEqual(azure_harbor.refresh_token(credential_dir, "tenant-id"), 2000)
            with (credential_dir / "token.json").open() as original:
                self.assertEqual(azure_harbor.refresh_token(credential_dir, "tenant-id"), 2100)
                self.assertEqual(json.load(original)["accessToken"], "FIRST_SECRET")
            current = json.loads((credential_dir / "token.json").read_text())
        self.assertEqual(current, {"accessToken": "SECOND_SECRET", "expires_on": 2100})
        self.assertEqual(stat.S_IMODE((credential_dir / "token.json").stat().st_mode), 0o600)
        self.assertFalse((credential_dir / ".token.next").exists())
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertNotIn("SECRET", str(run.call_args.args))

    def test_cli_authentication_error_never_includes_captured_secret(self):
        failure = subprocess.CalledProcessError(1, ["az"], output="TOKEN_SECRET", stderr="PRIVATE_STDERR")
        with patch.object(azure_harbor.subprocess, "run", side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                azure_harbor.refresh_token(self.root, "tenant-id")
        self.assertNotIn("TOKEN_SECRET", str(raised.exception))
        self.assertNotIn("PRIVATE_STDERR", str(raised.exception))
        self.assertFalse((self.root / "token.json").exists())

    def test_malformed_or_expiring_tokens_are_rejected_before_file_creation(self):
        for token, expiry in (("SECRET", float("nan")), ("SECRET", float("inf")),
                              ("SECRET WITH SPACE", 2000), ("SECRET", 1100)):
            result = subprocess.CompletedProcess(["az"], 0,
                json.dumps({"accessToken": token, "expires_on": expiry}), "")
            with self.subTest(expiry=expiry), \
                 patch.object(azure_harbor.subprocess, "run", return_value=result), \
                 patch.object(azure_harbor.time, "time", return_value=1000):
                with self.assertRaises(RuntimeError) as raised:
                    azure_harbor.refresh_token(self.root, "tenant-id")
                self.assertNotIn(token, str(raised.exception))
                self.assertFalse((self.root / "token.json").exists())

    def test_launcher_passes_only_runtime_directory_and_cleans_it(self):
        config = self.root / "job.json"
        config.write_text("{}")
        process = Mock()
        process.wait.return_value = 7
        process.poll.return_value = 7
        captured = {}

        def start(command, *, env):
            directory = Path(env["SDBENCH_AZURE_TOKEN_DIR"])
            captured.update(directory=directory, command=command)
            self.assertTrue((directory / "token.json").is_file())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertNotIn("RUNTIME_SECRET", json.dumps(env))
            self.assertNotIn("RUNTIME_SECRET", json.dumps(command))
            return process

        result = subprocess.CompletedProcess(["az"], 0,
            json.dumps({"accessToken": "RUNTIME_SECRET", "expires_on": 2000}), "")
        with patch.object(azure_harbor.shutil, "which", return_value="/safe/bin/harbor"), \
             patch.object(azure_harbor.subprocess, "run", return_value=result), \
             patch.object(azure_harbor.subprocess, "Popen", side_effect=start), \
             patch.object(azure_harbor.time, "time", return_value=1000):
            self.assertEqual(azure_harbor.run(config, "tenant-id"), 7)
        self.assertEqual(captured["command"], ["/safe/bin/harbor", "run", "--config", str(config.resolve())])
        self.assertFalse(captured["directory"].exists())
        self.assertEqual(config.read_text(), "{}")

    def test_failed_refresh_stops_harbor_before_removing_runtime_directory(self):
        config = self.root / "job.json"
        config.write_text("{}")
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("harbor", 10), 0]
        process.poll.return_value = None
        directories = []

        def refresh(directory, tenant):
            directories.append(directory)
            if len(directories) == 2:
                raise RuntimeError("Refresh failed")
            return 1200

        with patch.object(azure_harbor.shutil, "which", return_value="/safe/bin/harbor"), \
             patch.object(azure_harbor, "refresh_token", side_effect=refresh), \
             patch.object(azure_harbor.subprocess, "Popen", return_value=process), \
             patch.object(azure_harbor.time, "time", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "Refresh failed"):
                azure_harbor.run(config, "tenant-id")
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.kill.assert_not_called()
        self.assertEqual(len(directories), 2)
        self.assertFalse(directories[0].exists())


if __name__ == "__main__":
    unittest.main()
