"""CLI experiment preparation and authoritative-result accounting with temp fixtures."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdbench.cli import isolation
from sdbench.isolation.accounting import RateCard, usage_entry
from sdbench.isolation.experiment import ExperimentConfig, build_trial_configs, canonical_hash


class IsolationCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, path, value):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def write_lines(self, path, values):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(value) + "\n" for value in values))
        return path

    def trial_manifest(self, *, mode="mock"):
        config = ExperimentConfig(dataset="synthetic", case_set="train", case_ids=("fixture",),
                                  diagnosis_model="gpt-5", api_mode=mode)
        trials = build_trial_configs(config, code_hash="fixed-source", prompt_hashes={
            "gatekeeper/prompt.yaml": canonical_hash("gatekeeper"),
            "judge/prompt.yaml": canonical_hash("judge"),
        }, case_hashes={"fixture": canonical_hash("private-case")})
        manifest = {"trials": trials, "api_mode": mode, "isolation": "test-fixture",
                    "interpretation": "Fixture; no clinical inference"}
        return self.write("manifest.json", manifest), trials

    def summarize(self, manifest):
        output = io.StringIO()
        with redirect_stdout(output):
            isolation.summarize(argparse.Namespace(manifest=manifest, jobs=self.root / "jobs", output=None))
        return json.loads(output.getvalue())

    def entry(self, trial, call_id, *, role="gatekeeper"):
        entry = usage_entry(role=role, model=trial["gatekeeper_model"] if role == "gatekeeper" else "gpt-5",
                            attempt=1, usage={"input_tokens": 10, "output_tokens": 2},
                            api_mode=trial["api_mode"], call_id=call_id,
                            rate_card=RateCard("2026-10-22", 1, 2))
        return {**entry, "trial_id": trial["trial_id"], "protocol_hash": trial["protocol_hash"]}

    def prepare_args(self, **overrides):
        args = dict(dataset="synthetic", case_set="train", case_id=None,
                    diagnosis_model="gpt-5", repetitions=2, max_turns=5, max_model_calls=9,
                    api_mode="mock", seed=3, reasoning_effort=None, max_completion_tokens=100,
                    output=self.root / "prepared", rate_cards=None, concurrency=2)
        args.update(overrides)
        return argparse.Namespace(**args)

    def cases(self):
        return [{"dataset": "synthetic", "case_id": "fixture", "case_title": "Private title",
                 "case_summary": "Public initial presentation", "full_text": "PRIVATE_FIXTURE_TEXT",
                 "final_diagnosis": "Private reference"}]

    def test_prepare_is_api_free_and_binds_selected_case_to_both_arms(self):
        built = []

        def fake_build(case, trial, path):
            built.append((dict(case), dict(trial), path))
            path.mkdir(parents=True)
            return path

        with patch.object(isolation.case_data, "get_case_set", return_value=self.cases()), \
             patch("sdbench.harbor_integration.tasks.build_task", side_effect=fake_build), \
             patch("sdbench.isolation.providers.build_backend", side_effect=AssertionError("No API backend during preparation")), \
             redirect_stdout(io.StringIO()):
            isolation.prepare(self.prepare_args())
        manifest = json.loads((self.root / "prepared/manifest.json").read_text())
        self.assertEqual(len(built), 4)
        self.assertEqual(len({t["pair_id"] for t in manifest["trials"]}), 2)
        self.assertEqual({t["gatekeeper_model"] for t in manifest["trials"]}, {"o4-mini", "gpt-5"})
        self.assertEqual(len({t["protocol_hash"] for t in manifest["trials"]}), 1)
        self.assertTrue(all(t["max_model_calls"] == 9 for t in manifest["trials"]))
        self.assertNotIn("PRIVATE_FIXTURE_TEXT", json.dumps(manifest))
        self.assertTrue(all(t["case_hash"] == canonical_hash(self.cases()[0]) for t in manifest["trials"]))
        self.assertTrue((self.root / "prepared/job.json").is_file())

    def test_prepare_rejects_unknown_selected_case_before_building(self):
        with patch.object(isolation.case_data, "get_case_set", return_value=self.cases()), \
             patch("sdbench.harbor_integration.tasks.build_task") as build:
            with self.assertRaises(ValueError):
                isolation.prepare(self.prepare_args(case_id=["not-in-split"]))
            build.assert_not_called()
        self.assertFalse((self.root / "prepared").exists())

    def test_summary_ignores_agent_and_harbor_result_files_and_counts_missing_arm(self):
        manifest, trials = self.trial_manifest()
        completed = {**trials[0], "status": "completed", "correct": True}
        self.write("jobs/a/verifier/result.json", completed)
        # Same shape in an untrusted agent directory or Harbor's own root must not count.
        forged = {**trials[1], "status": "completed", "correct": True}
        self.write("jobs/a/agent/result.json", forged)
        self.write("jobs/result.json", forged)
        self.write("jobs/unrelated/verifier/result.json", {**forged, "trial_id": "foreign"})
        report = self.summarize(manifest)
        dataset = report["datasets"]["synthetic"]
        self.assertEqual(dataset["pair_count"], 1)
        self.assertEqual(sum(a["completed_episodes"] for a in dataset["arms"].values()), 1)
        self.assertEqual(sum(a["missing_episodes"] for a in dataset["arms"].values()), 1)
        self.assertEqual(dataset["paired"]["complete_pairs"], 0)

    def test_summary_deduplicates_identical_copies_and_includes_judge_ledger(self):
        manifest, trials = self.trial_manifest(mode="live")
        for index, trial in enumerate(trials):
            self.write(f"jobs/{index}/verifier/result.json", {**trial, "status": "completed", "correct": False})
            entry = self.entry(trial, f"gk-{index}")
            self.write_lines(f"jobs/{index}/artifacts/gatekeeper-evidence/usage.jsonl", [entry])
            self.write_lines(f"jobs/copied-{index}/artifacts/gatekeeper-evidence/usage.jsonl", [entry])
            judge = self.entry(trial, f"judge-{index}", role="judge")
            self.write_lines(f"jobs/{index}/verifier/judge_api_calls.jsonl", [judge])
        report = self.summarize(manifest)
        self.assertEqual(report["usage"]["calls"], 4)
        self.assertEqual(report["usage"]["by_role"]["judge"]["calls"], 2)
        self.assertEqual(report["usage"]["known_total_tokens"], 48)
        self.assertAlmostEqual(report["usage"]["known_estimated_cost_usd"], 56 / 1_000_000)

    def test_conflicting_copies_of_same_call_are_rejected(self):
        manifest, trials = self.trial_manifest(mode="live")
        entry = self.entry(trials[0], "same-call")
        self.write_lines("jobs/original/artifacts/gatekeeper-evidence/usage.jsonl", [entry])
        self.write_lines("jobs/copy/artifacts/gatekeeper-evidence/usage.jsonl", [{**entry, "estimated_cost_usd": 1000}])
        with self.assertRaises(ValueError):
            self.summarize(manifest)

    def test_live_missing_usage_does_not_become_zero_cost(self):
        manifest, _ = self.trial_manifest(mode="live")
        report = self.summarize(manifest)
        self.assertIsNone(report["usage"]["estimated_cost_usd"])
        self.assertEqual(report["usage"]["known_estimated_cost_usd"], 0)
        self.assertIsNone(report["usage"]["actual_paid_usd"])
        self.assertEqual(len(report["usage_coverage"]["trials_without_usage"]), 2)
        for stats in report["usage_by_gatekeeper"].values():
            self.assertIsNone(stats["estimated_cost_usd"])
            self.assertTrue(stats["incomplete_evidence"])

    def test_gatekeeper_usage_is_required_when_observations_were_returned(self):
        manifest, trials = self.trial_manifest(mode="live")
        for index, trial in enumerate(trials):
            self.write(f"jobs/{index}/verifier/result.json", {
                **trial, "status": "completed", "correct": False,
                "test_logs": [{"test_name": "CBC", "result": "Observation"}],
            })
            self.write_lines(f"jobs/{index}/artifacts/gatekeeper-evidence/usage.jsonl", [
                self.entry(trial, f"diagnosis-{index}", role="diagnosis"),
            ])
            self.write_lines(f"jobs/{index}/verifier/judge_api_calls.jsonl", [
                self.entry(trial, f"judge-{index}", role="judge"),
            ])
        report = self.summarize(manifest)
        self.assertIsNone(report["usage"]["estimated_cost_usd"])
        self.assertTrue(report["usage"]["incomplete_evidence"])
        self.assertEqual(report["usage_coverage"]["missing_roles_on_completed_trials"], {
            trial["trial_id"]: ["gatekeeper"] for trial in trials
        })
        for stats in report["usage_by_gatekeeper"].values():
            self.assertIsNone(stats["estimated_cost_usd"])
            self.assertTrue(stats["incomplete_evidence"])

    def test_foreign_usage_does_not_count_toward_this_experiment(self):
        manifest, trials = self.trial_manifest()
        entry = {**self.entry(trials[0], "foreign-call"), "trial_id": "foreign-trial"}
        self.write_lines("jobs/foreign/artifacts/gatekeeper-evidence/usage.jsonl", [entry])
        report = self.summarize(manifest)
        self.assertEqual(report["usage"]["calls"], 0)

    def test_agent_ledgers_and_nested_artifact_forgeries_are_ignored(self):
        manifest, trials = self.trial_manifest()
        real = self.entry(trials[0], "real-call")
        self.write_lines("jobs/a/artifacts/gatekeeper-evidence/usage.jsonl", [real])
        locations = [
            "jobs/a/agent/usage.jsonl",
            "jobs/a/agent/artifacts/gatekeeper-evidence/usage.jsonl",
            "jobs/a/agent/verifier/judge_api_calls.jsonl",
            "jobs/a/artifacts/logs/artifacts/gatekeeper-evidence/usage.jsonl",
            "jobs/a/artifacts/logs/verifier/judge_api_calls.jsonl",
        ]
        for index, location in enumerate(locations):
            fake = self.entry(trials[0], f"fake-call-{index}")
            self.write_lines(location, [fake])
        self.write("jobs/a/artifacts/logs/verifier/result.json",
                   {**trials[0], "status": "completed", "correct": True})
        self.write("jobs/a/agent/verifier/result.json",
                   {**trials[1], "status": "completed", "correct": True})
        report = self.summarize(manifest)
        self.assertEqual(report["usage"]["calls"], 1)
        self.assertEqual(report["usage"]["known_total_tokens"], 12)
        arms = report["datasets"]["synthetic"]["arms"]
        self.assertEqual(sum(stats["completed_episodes"] for stats in arms.values()), 0)
        self.assertEqual(sum(stats["missing_episodes"] for stats in arms.values()), 2)


if __name__ == "__main__":
    unittest.main()
