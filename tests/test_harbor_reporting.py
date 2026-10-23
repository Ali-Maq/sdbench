"""The host report reuses saved evidence without changing it or calling models."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdbench.harbor_integration.reporting import export_trial_report
from sdbench.isolation.common import canonical_hash


class IsolatedReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trial = self.root / "trial"
        for directory in ("agent", "verifier", "artifacts/gatekeeper-evidence"):
            (self.trial / directory).mkdir(parents=True)
        self.case = {"case_id": "1", "dataset": "synthetic", "case_title": "Synthetic example",
                     "case_summary": "Visible vignette", "final_diagnosis": "REFERENCE_CONDITION",
                     "full_text": "Private reference narrative"}
        self.case_path = self.root / "case.json"
        self.case_path.write_text(json.dumps(self.case))
        self.events = [{"event": "terminal", "status": "completed", "diagnosis": "Agent condition"}]
        self.write("artifacts/gatekeeper-evidence/events.jsonl", self.events[0], jsonl=True)
        self.write("result.json", {"finished_at": "2026-10-22T19:00:00", "exception_info": None})
        self.write("agent/diagnosis.json", {
            "status": "completed", "diagnosis": "Agent condition",
            "agent_messages": [{"role": "user", "content": "Visible vignette"},
                               {"role": "assistant", "content": "Visible diagnosis dialogue"}],
        })
        self.verifier = {
            "trial_id": "t1", "status": "completed", "case_id": "1", "dataset": "synthetic",
            "case_hash": canonical_hash(self.case), "evidence_hash": canonical_hash(self.events),
            "diagnosis": "Agent condition", "correct": True, "diagnosis_score": 5,
            "diagnosis_model": "diagnoser", "gatekeeper_model": "gatekeeper", "judge_model": "grader",
            "api_mode": "live", "mocked": False,
            "test_logs": [{"test_name": "CBC", "result": "RECORDED_TEST_RESULT", "is_synthetic": True}],
            "judge": {"check_diagnosis_messages": [
                {"role": "system", "content": "Exact judge prompt"},
                {"role": "assistant", "content": "Saved judge explanation and score"},
            ]},
        }
        self.write("verifier/result.json", self.verifier)
        self.calls = [self.call("diagnosis", "d1"), self.call("gatekeeper", "g1"),
                      self.call("gatekeeper", "g2", failed=True)]
        self.write_calls("artifacts/gatekeeper-evidence/raw_calls.jsonl", self.calls)
        self.write_calls("verifier/raw_calls.jsonl", [self.call("judge", "j1")])

    def write(self, name, value, jsonl=False):
        (self.trial / name).write_text(json.dumps(value) + ("\n" if jsonl else ""))

    def write_calls(self, name, calls):
        (self.trial / name).write_text("".join(json.dumps(call, ensure_ascii=False) + "\n" for call in calls))

    def call(self, role, call_id, failed=False):
        return {
            "trial_id": "t1", "role": role, "call_id": call_id,
            "status": "error" if failed else "completed",
            "provider_status": "incomplete" if failed else "completed",
            "messages": [{"role": "system", "content": f"Exact {role} prompt"}],
            "response": {"object": "response", "output": [
                {"type": "reasoning", "encrypted_content": "HIDDEN_REASONING_MARKER"},
                {"type": "message", "phase": "final_answer", "content": [{
                    "type": "output_text", "text": "Partial visible reply" if failed else "Exact visible reply <tag>",
                }]},
            ]},
        }

    def test_existing_renderer_shows_saved_roles_reference_and_unknown_costs(self):
        sources = {path: path.read_bytes() for path in self.trial.rglob("*") if path.is_file()}
        with patch("socket.create_connection", side_effect=AssertionError("No network allowed")):
            report = export_trial_report(self.trial, self.case_path)
        html = report.read_text()
        for expected in ("SYNTHETIC development/demo case", "LIVE model calls", "REFERENCE_CONDITION",
                         "Agent condition", "Visible diagnosis dialogue", "Exact judge prompt",
                         "Exact gatekeeper prompt", "Partial visible reply", "RECORDED_TEST_RESULT",
                         "status=error", "provider_status=incomplete", "final_answer",
                         "Clinical workup costs: not computed", "API dollars: unknown"):
            self.assertIn(expected, html)
        self.assertIn("Exact visible reply &lt;tag&gt;", html)
        self.assertNotIn("HIDDEN_REASONING_MARKER", html)
        self.assertNotIn("$0", html)
        self.assertNotIn("Synthetic/Real", html)
        self.assertIn("authored fictional case", html)
        self.assertIn("Gatekeeper flag: synthetic: 1, non-synthetic: 0, unknown: 0", html)
        self.assertIn("reported metadata, not verified source evidence", html)
        self.assertIn("Observation flags reproduce gatekeeper metadata; they do not verify", html)
        self.assertNotIn("Observation provenance distinguishes recorded", html)
        mapped = json.loads((report.parent / "result.json").read_text())
        self.assertEqual(mapped["export_metadata"]["api_attempts_by_role"],
                         {"diagnosis": 1, "gatekeeper": 2, "judge": 1})
        self.assertIsNone(mapped["clinical_workup_cost_usd"])
        self.assertIsNone(mapped["api_actual_paid_usd"])
        for path, original in sources.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.trial / "agent/clinical-report").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            export_trial_report(self.trial, self.case_path)

    def test_parser_defaults_are_not_presented_as_documented_patient_evidence(self):
        from sdbench.gatekeeper import _parse_xml_test_response

        names = ["Test to order: observation A", "Test to order: observation B"]
        observations, successful = _parse_xml_test_response(
            names, '<test_result id="1"><result>Unverified fixture observation</result></test_result>', False
        )
        # Exercise both upstream fallbacks: a missing flag and a missing result.
        self.assertEqual(successful, 1)
        self.assertEqual([item["is_synthetic"] for item in observations], [False, False])
        self.verifier["test_logs"] = [
            {"test_name": item["test_name"], "result": item["results"], "is_synthetic": item["is_synthetic"]}
            for item in observations
        ]
        self.write("verifier/result.json", self.verifier)
        report = export_trial_report(self.trial, self.case_path)
        html = report.read_text()
        self.assertIn("Gatekeeper flag: synthetic: 0, non-synthetic: 2, unknown: 0", html)
        self.assertIn("Gatekeeper flag: non-synthetic", html)
        self.assertIn("does not establish that a finding was documented", html)
        self.assertNotIn("Case-recorded", html)
        self.assertNotIn("Recorded in case narrative", html)
        mapped = json.loads((report.parent / "result.json").read_text())
        self.assertEqual([item["is_synthetic"] for item in mapped["test_logs"]], [False, False])

    def test_missing_or_invalid_saved_flags_remain_unknown_in_the_report(self):
        self.verifier["test_logs"] = [
            {"test_name": "Missing flag", "result": "Fixture"},
            {"test_name": "Null flag", "result": "Fixture", "is_synthetic": None},
            {"test_name": "String flag", "result": "Fixture", "is_synthetic": "false"},
        ]
        self.write("verifier/result.json", self.verifier)
        html = export_trial_report(self.trial, self.case_path).read_text()
        self.assertIn("Gatekeeper flag: synthetic: 0, non-synthetic: 0, unknown: 3", html)
        self.assertEqual(html.count("— Gatekeeper flag: unknown"), 3)

    def test_oversized_call_is_losslessly_split_into_viewer_sized_utf8_files(self):
        self.calls[1]["messages"][0]["content"] = "Multibyte 🧪" * 100_000
        self.write_calls("artifacts/gatekeeper-evidence/raw_calls.jsonl", self.calls)
        source = (self.trial / "artifacts/gatekeeper-evidence/raw_calls.jsonl").read_bytes()
        report = export_trial_report(self.trial, self.case_path)
        manifest = json.loads((report.parent / "manifest.json").read_text())
        entry = next(call for call in manifest["calls"] if call["call_id"] == "g1")
        self.assertGreater(len(entry["parts"]), 1)
        pieces = []
        for part in entry["parts"]:
            data = (report.parent / part["path"]).read_bytes()
            self.assertLess(len(data), 1024 * 1024)
            data.decode("utf-8")
            pieces.append(data)
        self.assertEqual(b"".join(pieces), source.splitlines(keepends=True)[1])
        self.assertEqual((self.trial / "artifacts/gatekeeper-evidence/raw_calls.jsonl").read_bytes(), source)

    def test_wrong_reference_or_changed_evidence_is_rejected_before_writing(self):
        self.case_path.write_text(json.dumps({**self.case, "final_diagnosis": "Wrong condition"}))
        with self.assertRaisesRegex(ValueError, "reference case"):
            export_trial_report(self.trial, self.case_path)
        self.case_path.write_text(json.dumps(self.case))
        self.write("artifacts/gatekeeper-evidence/events.jsonl", {"diagnosis": "Changed"}, jsonl=True)
        with self.assertRaisesRegex(ValueError, "evidence hash"):
            export_trial_report(self.trial, self.case_path)
        self.assertFalse((self.trial / "artifacts/clinical-report").exists())

    def test_cross_trial_call_or_failed_trial_is_rejected(self):
        self.calls[1]["trial_id"] = "other-trial"
        self.write_calls("artifacts/gatekeeper-evidence/raw_calls.jsonl", self.calls)
        with self.assertRaisesRegex(ValueError, "call identity"):
            export_trial_report(self.trial, self.case_path)
        self.write("result.json", {"finished_at": None})
        with self.assertRaisesRegex(ValueError, "finished Harbor"):
            export_trial_report(self.trial, self.case_path)


if __name__ == "__main__":
    unittest.main()
