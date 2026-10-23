"""Recost preserves diagnostic evidence and uses completed cases only."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdbench.cli import recost
from sdbench.config import CostingConfig, SDBenchConfig


class RecostIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "case_k0_fixture.json"
        self.record = {
            "result_status": "success", "model_diagnosis": "Fictional diagnosis — café",
            "diagnosis_score": 5, "correct": True, "check_diagnosis_output": "5",
        }
        self.original = json.dumps(self.record, ensure_ascii=False).encode("utf-8")
        self.path.write_bytes(self.original)
        self.config = CostingConfig.from_sdbench_config(SDBenchConfig(
            max_turns=3, gatekeeper_prompt_version="v6_fixed", gatekeeper_model="fixture",
            judge_prompt_version="v3_reasoning", judge_model="fixture", costing_method="llm_only",
        ))

    def test_interrupted_write_preserves_original_and_removes_temporary(self):
        def interrupt(_value, stream, **_kwargs):
            stream.write('{"partial":')
            raise KeyboardInterrupt()

        with patch.object(recost.json, "dump", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            recost._replace_case_json(str(self.path), {**self.record, "new": "price"})
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob(".recost-*")), [])

    def test_successful_write_is_utf8_and_replace_failure_keeps_original(self):
        updated = {**self.record, "new": "Fictional résumé"}
        with patch.object(recost.os, "replace", side_effect=OSError("Scripted disk error")):
            with self.assertRaises(OSError):
                recost._replace_case_json(str(self.path), updated)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob(".recost-*")), [])
        recost._replace_case_json(str(self.path), updated)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), updated)

    def test_failed_and_incomplete_diagnoses_are_not_priced(self):
        for override in ({"result_status": "failed"}, {"check_diagnosis_output": ""}):
            with self.subTest(override=override):
                self.path.write_text(json.dumps({**self.record, **override}), encoding="utf-8")
                with patch.object(recost, "compute_agent_costs") as price, redirect_stdout(io.StringIO()) as output:
                    counts = recost.recost_case(str(self.path), self.config, force=True, analyze_only=False)
                price.assert_not_called()
                self.assertEqual(counts["cases"], 0)
                self.assertEqual(counts["excluded_cases"], 1)
                self.assertIn("excluded incomplete diagnosis", output.getvalue())

    def test_pricing_exception_counts_unknown_completed_case_and_preserves_file(self):
        with (patch.object(recost, "compute_agent_costs", side_effect=ValueError("Scripted invalid input")),
              redirect_stdout(io.StringIO()) as output):
            counts = recost.recost_case(str(self.path), self.config, force=True, analyze_only=False)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(counts["cases"], 1)
        self.assertEqual(counts["unknown_cases"], 1)
        self.assertEqual(counts["excluded_cases"], 0)
        self.assertIn("original file preserved", output.getvalue())


if __name__ == "__main__":
    unittest.main()
