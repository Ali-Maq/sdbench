"""Protocol integrity and missing-data scenarios, without clinical data or model calls."""

from dataclasses import FrozenInstanceError, replace
import json
import os
import unittest
from unittest.mock import patch

from sdbench.isolation.experiment import (
    ExperimentConfig, analyze_trials, build_trial_configs, canonical_hash, hash_case_data,
)


class ExperimentTests(unittest.TestCase):
    def config(self, **kwargs):
        values = dict(dataset="synthetic", case_set="train", case_ids=("a", "b"),
                      diagnosis_model="fixed-diagnostician", seed=41)
        values.update(kwargs)
        return ExperimentConfig(**values)

    def manifest(self, config=None, **kwargs):
        config = config or self.config()
        values = dict(code_hash="git-revision", prompt_hashes={
            "baseline.py": canonical_hash("baseline"),
            "gatekeeper/prompt.yaml": canonical_hash("gatekeeper"),
            "judge/prompt.yaml": canonical_hash("judge"),
        }, case_hashes={case: hash_case_data({"id": case, "full_text": "sensitive-marker"})
                       for case in config.case_ids})
        values.update(kwargs)
        return build_trial_configs(config, **values)

    def records(self, manifest, outcome=lambda trial: True):
        return [{**trial, "status": "completed", "correct": outcome(trial)} for trial in manifest]

    def test_fixed_prompts_match_packaged_science_config(self):
        from sdbench.config import get_sdbench_config

        with patch.dict(os.environ, {"SDBENCH_LOCAL_DIR": "disable"}):
            for dataset, reference in (("nejm", "nejm"), ("mimic", "mimic"),
                                       ("synthetic", "nejm")):
                with self.subTest(dataset=dataset):
                    native = get_sdbench_config(reference, "science")
                    frozen = self.config(dataset=dataset)
                    self.assertEqual(frozen.gk_prompt, native.gatekeeper_prompt_version)
                    self.assertEqual(frozen.judge_prompt, native.judge_prompt_version)
                    self.assertEqual(frozen.judge_model, native.judge_model)
                    self.assertEqual(frozen.max_turns, native.max_turns)

    def test_protocol_pins_content_not_clinical_text_and_pairs_every_case(self):
        config = self.config(repetitions=3)
        trials = self.manifest(config)
        self.assertEqual(len(trials), 12)
        self.assertEqual(len({t["trial_id"] for t in trials}), 12)
        self.assertEqual(len({t["pair_id"] for t in trials}), 6)
        self.assertEqual(len({t["protocol_hash"] for t in trials}), 1)
        self.assertNotIn("sensitive-marker", json.dumps(trials))
        self.assertTrue(all(t["max_model_calls"] == 150 for t in trials))
        for pair_id in {t["pair_id"] for t in trials}:
            pair = [t for t in trials if t["pair_id"] == pair_id]
            self.assertEqual({t["gatekeeper_model"] for t in pair}, {"gpt-5", "o4-mini"})
            self.assertEqual(pair[0]["case_hash"], pair[1]["case_hash"])
        self.assertEqual(trials, self.manifest(config))
        self.assertEqual(trials, self.manifest(replace(config, case_ids=("b", "a"))))
        changed = self.manifest(config, code_hash="changed-revision")
        self.assertNotEqual(trials[0]["protocol_hash"], changed[0]["protocol_hash"])
        changed = self.manifest(config, case_hashes={"a": canonical_hash("changed-case"),
                                                    "b": hash_case_data({"id": "b", "full_text": "sensitive-marker"})})
        self.assertNotEqual(trials[0]["protocol_hash"], changed[0]["protocol_hash"])

    def test_nested_model_settings_are_immutable_and_detached_from_caller(self):
        args = {"temperature": 0, "reasoning": {"effort": "high"}}
        config = self.config(gatekeeper_model_args=args)
        before = self.manifest(config)
        args["reasoning"]["effort"] = "low"
        self.assertEqual(before, self.manifest(config))
        with self.assertRaises(TypeError):
            config.gatekeeper_model_args["reasoning"]["effort"] = "low"
        with self.assertRaises(FrozenInstanceError):
            config.repetitions = 5

    def test_rejects_protocol_ambiguity(self):
        for kwargs in ({"case_ids": ("a", "a")}, {"repetitions": 0},
                       {"max_model_calls": True}, {"api_mode": "production"},
                       {"gk_models": ("gpt-5", "o4-mini")},
                       {"dataset": "mimic", "gk_prompt": "v6_fixed"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.config(**kwargs)
        with self.assertRaises(ValueError):
            self.manifest(case_hashes={"a": canonical_hash("a")})
        with self.assertRaises(ValueError):
            self.manifest(prompt_hashes={"prompt": "not-a-content-hash"})

    def test_pricing_is_validated_immutable_and_part_of_protocol_identity(self):
        rates = {"gpt-5": {"effective_date": "2026-10-22", "input_per_million": 1,
                            "output_per_million": 2}}
        config = self.config(rate_cards=rates)
        original = self.manifest(config)
        rates["gpt-5"]["input_per_million"] = 10
        self.assertEqual(original, self.manifest(config))
        changed = self.manifest(self.config(rate_cards=rates))
        self.assertNotEqual(original[0]["protocol_hash"], changed[0]["protocol_hash"])
        self.assertEqual(original[0]["rate_cards"]["gpt-5"]["input_per_million"], 1)
        with self.assertRaises(TypeError):
            config.rate_cards["gpt-5"]["input_per_million"] = 10
        with self.assertRaises(ValueError):
            self.config(rate_cards={"gpt-5": {"effective_date": "missing-prices"}})

    def test_failures_and_entirely_missing_pair_remain_in_denominator(self):
        trials = self.manifest()
        # The entire b pair is absent; the schedule must still count both attempts.
        records = self.records([t for t in trials if t["case_id"] == "a"])
        records[0]["status"] = "failed"
        report = analyze_trials(records, expected_trials=trials, bootstrap_samples=100)
        data = report["datasets"]["synthetic"]
        self.assertEqual(data["case_count"], 2)
        self.assertEqual(data["pair_count"], 2)
        self.assertEqual(len(data["paired"]["missing_pairs"]), 1)
        failed_model = records[0]["gatekeeper_model"]
        stats = data["arms"][failed_model]
        self.assertEqual(stats["attempted_episodes"], 2)
        self.assertEqual(stats["failed_episodes"], 2)
        self.assertEqual(stats["accuracy_primary"], 0)
        self.assertIsNone(stats["accuracy_completed"])
        self.assertIsNone(stats["leakage_rate"])
        other = next(model for model in data["arms"] if model != failed_model)
        self.assertEqual(data["arms"][other]["accuracy_primary"], .5)
        self.assertEqual(data["arms"][other]["accuracy_completed"], 1)

    def test_bootstrap_weights_cases_not_repetitions(self):
        trials = [t for t in self.manifest(self.config(repetitions=5))
                  if t["case_id"] == "a" or t["repetition"] == 0]
        # Five +1 episodes in a, one -1 episode in b: case-level effect is zero.
        records = self.records(trials, lambda t: (t["case_id"] == "a") == (t["gatekeeper_model"] == "gpt-5"))
        result = analyze_trials(records, expected_trials=trials, seed=2, bootstrap_samples=500)
        paired = result["datasets"]["synthetic"]["paired"]
        self.assertEqual(paired["mean_accuracy_difference"], 0)
        self.assertEqual(paired["case_differences"], {"a": 1, "b": -1})
        self.assertEqual(paired["ci95"], [-1, 1])
        self.assertTrue(paired["uneven_repetitions"])
        self.assertEqual(result, analyze_trials(records, expected_trials=trials, seed=2, bootstrap_samples=500))

    def test_one_case_does_not_claim_population_uncertainty(self):
        trials = self.manifest(self.config(case_ids=("a",), repetitions=20))
        report = analyze_trials(self.records(trials), expected_trials=trials, bootstrap_samples=10)
        self.assertIsNone(report["datasets"]["synthetic"]["paired"]["ci95"])

    def test_duplicate_or_mixed_protocol_records_fail_loudly(self):
        trials = self.manifest()
        records = self.records(trials)
        with self.assertRaises(ValueError):
            analyze_trials(records + [records[0]], expected_trials=trials)
        changed = [{**record} for record in records]
        changed[0]["case_hash"] = canonical_hash("different-case")
        with self.assertRaises(ValueError):
            analyze_trials(changed, expected_trials=trials)
        changed = [{**record} for record in records]
        changed[0]["protocol_hash"] = "other-protocol"
        with self.assertRaises(ValueError):
            analyze_trials(changed)
        with self.assertRaises(ValueError):
            analyze_trials(records[:-1], expected_trials=trials[:-1])

    def test_no_manifest_is_explicitly_limited_and_audits_are_optional(self):
        trials = self.manifest()
        records = self.records(trials)
        records[0]["audit"] = {"leakage": False}
        records[1]["correct"] = "true"  # Invalid completion is not silently successful.
        report = analyze_trials(records, bootstrap_samples=10)
        self.assertEqual(report["denominator_scope"], "observed_pairs_only")
        self.assertFalse(report["expected_manifest_provided"])
        arms = report["datasets"]["synthetic"]["arms"]
        self.assertEqual(sum(stats["completed_episodes"] for stats in arms.values()), 3)
        self.assertEqual(sum(stats["audited_episodes"] for stats in arms.values()), 1)

    def test_datasets_are_stratified(self):
        trials = self.manifest() + self.manifest(self.config(dataset="mimic", case_set="test"))
        result = analyze_trials(self.records(trials), expected_trials=trials, bootstrap_samples=10)
        self.assertEqual(set(result["datasets"]), {"synthetic", "mimic"})
        self.assertEqual(result["datasets"]["mimic"]["case_count"], 2)


if __name__ == "__main__":
    unittest.main()
