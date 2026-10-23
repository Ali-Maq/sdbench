"""Private episode preparation freezes inputs without starting model requests."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdbench.cli import isolation
from sdbench.isolation.common import canonical_hash
from sdbench.isolation.recorded import gatekeeper_case, SELECTOR_PROMPT_VERSION


FIXTURE = Path(__file__).parent / "fixtures" / "recorded_episode.json"


class RecordedPreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.args = argparse.Namespace(
            case=FIXTURE, diagnosis_model="fixed-diagnoser", gatekeeper_model="fixed-selector",
            judge_model="fixed-judge", api_mode="mock", provider="openai", base_url=None,
            max_turns=3, max_model_calls=8, max_completion_tokens=512, reasoning_effort=None,
            output=self.root / "prepared", jobs=self.root / "jobs",
        )

    def prepare(self):
        with redirect_stdout(io.StringIO()), patch("sdbench.isolation.providers.build_backend") as backend:
            isolation.prepare_recorded(self.args)
        backend.assert_not_called()
        return json.loads((self.args.output / "manifest.json").read_text())

    def test_preparation_freezes_distinct_role_payloads_without_clinical_text_in_manifest(self):
        manifest = self.prepare()
        case = json.loads(FIXTURE.read_text())
        self.assertEqual(manifest["experiment_kind"], "recorded_episode")
        self.assertEqual(len(manifest["trials"]), 1)
        trial = manifest["trials"][0]
        self.assertEqual(trial["api_mode"], "mock")
        self.assertEqual(trial["gatekeeper_mode"], "recorded_only")
        self.assertEqual(trial["gk_prompt"], SELECTOR_PROMPT_VERSION)
        self.assertEqual(trial["diagnostic_agent"], "baseline")
        self.assertEqual(trial["judge_prompt"], "v3_reasoning")
        self.assertEqual(trial["case_hash"], canonical_hash(case))
        self.assertEqual(trial["gatekeeper_case_hash"], canonical_hash(gatekeeper_case(case)))
        serialized = json.dumps(manifest)
        for private in (case["case_summary"], case["full_text"], case["final_diagnosis"],
                        case["recorded_episode"]["evidence"][0]["text"]):
            self.assertNotIn(private, serialized)
        self.assertNotIn("diagnosis_token", serialized)
        job = json.loads((self.args.output / "job.json").read_text())
        self.assertEqual(job["jobs_dir"], str(self.args.jobs.resolve()))
        self.assertEqual(job["n_attempts"], 1)
        self.assertEqual(job["n_concurrent_trials"], 1)
        self.assertEqual(Path(manifest["task_paths"][0]).name, "episode-0001")
        self.assertEqual(job["agents"][0]["import_path"], "sdbench.harbor_integration.agent:SDBenchAgent")
        self.assertFalse(self.args.jobs.exists())
        with self.assertRaises(FileExistsError):
            isolation.prepare_recorded(self.args)

    def test_zero_budget_blank_model_and_invalid_azure_route_fail_before_writing(self):
        invalid = [
            ("max_turns", 0), ("max_model_calls", 0), ("max_completion_tokens", 0),
            ("diagnosis_model", " "), ("gatekeeper_model", ""), ("judge_model", " "),
            ("base_url", "https://example.invalid/openai/v1/"),
        ]
        for field, value in invalid:
            original = getattr(self.args, field)
            setattr(self.args, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                isolation.prepare_recorded(self.args)
            self.assertFalse(self.args.output.exists())
            setattr(self.args, field, original)
        self.args.provider = "azure"
        self.args.base_url = "https://example.invalid/openai/v1/"
        with self.assertRaisesRegex(ValueError, "Azure endpoint"):
            isolation.prepare_recorded(self.args)
        self.assertFalse(self.args.output.exists())

    def test_azure_preparation_freezes_transport_and_role_settings_without_authentication(self):
        self.args.provider = "azure"
        self.args.base_url = "https://fixture-resource.openai.azure.com/openai/v1/"
        self.args.reasoning_effort = "medium"
        self.args.api_mode = "live"
        manifest = self.prepare()
        trial = manifest["trials"][0]
        self.assertEqual(trial["api_transport"], "responses")
        self.assertEqual(trial["provider_base_url"], self.args.base_url)
        self.assertEqual(trial["api_mode"], "live")
        for role in ("diagnosis", "gatekeeper", "judge"):
            self.assertEqual(trial[f"{role}_model"], getattr(self.args, f"{role}_model"))
            self.assertEqual(trial[f"{role}_model_args"],
                             {"max_completion_tokens": 512, "reasoning_effort": "medium"})

    def test_source_drift_does_not_publish_a_finished_manifest_or_job(self):
        with patch.object(isolation, "source_hash", side_effect=["a" * 64, "b" * 64]):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                isolation.prepare_recorded(self.args)
        self.assertFalse((self.args.output / "manifest.json").exists())
        self.assertFalse((self.args.output / "job.json").exists())

    def test_single_recorded_episode_is_not_summarized_as_a_paired_experiment(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "single recorded episode"):
            isolation.summarize(argparse.Namespace(
                manifest=self.args.output / "manifest.json", jobs=self.args.jobs, output=None,
            ))

    def test_cli_requires_an_explicit_model_for_every_role_and_defaults_to_mock(self):
        base = ["isolation", "prepare-recorded", "--case", str(FIXTURE),
                "--jobs", str(self.args.jobs), "--output", str(self.args.output)]
        for missing in ("diagnosis", "gatekeeper", "judge"):
            argv = list(base)
            for role in ("diagnosis", "gatekeeper", "judge"):
                if role != missing:
                    argv.extend([f"--{role}-model", f"fixed-{role}"])
            with self.subTest(missing=missing), patch("sys.argv", argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    isolation.main()
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(self.args.output.exists())
        argv = list(base)
        for role in ("diagnosis", "gatekeeper", "judge"):
            argv.extend([f"--{role}-model", f"fixed-{role}"])
        with patch("sys.argv", argv), redirect_stdout(io.StringIO()):
            isolation.main()
        manifest = json.loads((self.args.output / "manifest.json").read_text())
        self.assertEqual(manifest["api_mode"], "mock")


if __name__ == "__main__":
    unittest.main()
