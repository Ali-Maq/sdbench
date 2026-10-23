"""The host-only source appendix preserves the recorded episode's role boundaries."""

import html
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from sdbench.harbor_integration.reporting import export_trial_report
from sdbench.isolation.common import canonical_hash
from sdbench.isolation.recorded import render_selection


class RecordedReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trial = self.root / "trial"
        for directory in ("agent", "verifier", "artifacts/gatekeeper-evidence"):
            (self.trial / directory).mkdir(parents=True)
        self.case = json.loads((Path(__file__).parent / "fixtures/recorded_episode.json").read_text())
        # Escaping is important here: source notes are data, not HTML instructions.
        self.case["case_summary"] += " <script>opening()</script>"
        self.case["full_text"] += " <script>reference()</script>"
        self.case["recorded_episode"]["evidence"][0]["text"] += " <script>finding()</script>"
        self.case_path = self.root / "case.json"
        self.case_path.write_text(json.dumps(self.case))
        observation = render_selection(self.case, "Test to order: CBC", ["cbc"])
        missing = render_selection(self.case, "Test to order: undocumented test", [])
        self.logs = []
        for result in (observation, missing):
            log = dict(result)
            log["result"] = log.pop("results")
            self.logs.append(log)
        events = [{"event": "tests", "test_results": [observation, missing]},
                  {"event": "terminal", "status": "completed", "diagnosis": "Multiple myeloma"}]
        (self.trial / "artifacts/gatekeeper-evidence/events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        self.write("result.json", {"finished_at": "2026-10-23T12:00:00", "exception_info": None})
        self.write("agent/diagnosis.json", {
            "status": "completed", "diagnosis": "Multiple myeloma",
            "agent_messages": [{"role": "user", "content": self.case["case_summary"]}],
        })
        self.verifier = {
            "trial_id": "recorded-t1", "status": "completed", "case_id": self.case["case_id"],
            "dataset": "synthetic", "case_hash": canonical_hash(self.case),
            "evidence_hash": canonical_hash(events), "diagnosis": "Multiple myeloma",
            "correct": True, "diagnosis_score": 5, "diagnosis_model": "mock-diagnosis",
            "gatekeeper_model": "mock-gatekeeper", "judge_model": "mock-judge",
            "api_mode": "mock", "mocked": True, "gatekeeper_mode": "recorded_only",
            "recorded_evidence_integrity": "validated", "test_logs": self.logs,
            "judge": {"check_diagnosis_output": "Scripted fixture score: 5",
                      "check_diagnosis_messages": [{"role": "assistant", "content": "Scripted fixture score: 5"}]},
        }
        self.write("verifier/result.json", self.verifier)
        calls = [self.call("diagnosis"), self.call("gatekeeper")]
        (self.trial / "artifacts/gatekeeper-evidence/raw_calls.jsonl").write_text(
            "".join(json.dumps(call) + "\n" for call in calls)
        )
        self.write("verifier/raw_calls.jsonl", self.call("judge"), newline=True)

    def write(self, name, value, newline=False):
        (self.trial / name).write_text(json.dumps(value) + ("\n" if newline else ""))

    def call(self, role):
        return {
            "trial_id": "recorded-t1", "role": role, "call_id": role,
            "status": "completed", "provider_status": "completed",
            "messages": [{"role": "system", "content": "Fictional test call"}],
            "response": {"choices": [{"message": {"role": "assistant", "content": "Fixture response"}}]},
        }

    def test_appendix_shows_opening_catalog_reference_selected_ids_and_limits(self):
        originals = {path: path.read_bytes() for path in self.trial.rglob("*") if path.is_file()}
        with patch("socket.create_connection", side_effect=AssertionError("No network allowed")):
            overview = export_trial_report(self.trial, self.case_path)
        report = overview.parent
        html = (report / "evidence.html").read_text()
        for text in ("Opening seen by the diagnostic agent", "Allowed evidence", "Judge-only expert reference",
                     "FICTIONAL_REFERENCE_ONLY_MARKER", "selected evidence IDs: cbc", "not documented",
                     "Event day", "Source-record day", "not a clock-accurate replay",
                     "not clinical performance", "does not reopen those files"):
            self.assertIn(text, html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;finding()&lt;/script&gt;", html)
        self.assertIn('href="evidence.html"', overview.read_text())
        self.assertIn("Recorded-only observations were validated", overview.read_text())
        self.assertIn("Mock scores are scripted execution checks", overview.read_text())
        self.assertNotIn("Observation flags reproduce gatekeeper metadata; they do not verify", overview.read_text())
        derived = json.loads((report / "evidence.json").read_text())
        self.assertFalse(derived["authoritative"])
        self.assertEqual(derived["opening"]["text"], self.case["case_summary"])
        self.assertEqual(derived["allowed_evidence"], self.case["recorded_episode"]["evidence"])
        self.assertEqual(derived["reference"]["expert_record"], self.case["full_text"])
        self.assertEqual(derived["observations"][0]["evidence_ids"], ["cbc"])
        self.assertEqual(derived["observations"][1]["evidence_ids"], [])
        manifest = json.loads((report / "manifest.json").read_text())
        self.assertEqual(set(manifest["source_appendix"]), {"evidence.json", "evidence.html"})
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o700)
        for path in report.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.trial / "agent/evidence.json").exists())

    def test_forged_observation_cannot_be_rendered_as_verified_recorded_evidence(self):
        for field, value in (("result", "Invented finding"), ("evidence_ids", ["absent"]),
                             ("source_refs", []), ("evidence_status", "not_documented")):
            with self.subTest(field=field):
                original = self.verifier["test_logs"][0][field]
                self.verifier["test_logs"][0][field] = value
                self.write("verifier/result.json", self.verifier)
                with self.assertRaises(ValueError):
                    export_trial_report(self.trial, self.case_path)
                self.assertFalse((self.trial / "artifacts/clinical-report").exists())
                self.verifier["test_logs"][0][field] = original

    def test_missing_integrity_or_wrong_mode_fails_before_writing(self):
        for field, value in (("recorded_evidence_integrity", None), ("gatekeeper_mode", "stock")):
            with self.subTest(field=field):
                original = self.verifier[field]
                self.verifier[field] = value
                self.write("verifier/result.json", self.verifier)
                with self.assertRaisesRegex(ValueError, "validated recorded evidence"):
                    export_trial_report(self.trial, self.case_path)
                self.assertFalse((self.trial / "artifacts/clinical-report").exists())
                self.verifier[field] = original

    def test_valid_catalog_selection_not_returned_by_service_is_rejected(self):
        replacement = render_selection(self.case, "Test to order: CBC", ["chemistry"])
        replacement["result"] = replacement.pop("results")
        self.verifier["test_logs"][0] = replacement
        self.write("verifier/result.json", self.verifier)
        with self.assertRaisesRegex(ValueError, "hashed service evidence"):
            export_trial_report(self.trial, self.case_path)
        self.assertFalse((self.trial / "artifacts/clinical-report").exists())

    def test_custom_metadata_is_escaped_in_stock_report(self):
        self.case["case_id"] = '<img src=x onerror="alert(1)">'
        self.case["dataset"] = '</title><script>alert(2)</script>'
        self.case_path.write_text(json.dumps(self.case))
        self.verifier.update(case_id=self.case["case_id"], dataset=self.case["dataset"],
                             case_hash=canonical_hash(self.case))
        self.write("verifier/result.json", self.verifier)
        report = export_trial_report(self.trial, self.case_path)
        content = report.read_text()
        self.assertIn(html.escape(self.case["case_id"]), content)
        self.assertIn(html.escape(self.case["dataset"].upper()), content)
        self.assertNotIn(self.case["case_id"], content)
        self.assertNotIn(self.case["dataset"].upper(), content)

        # The optional case-set suffix uses the same title/header insertion path.
        from sdbench.config import HarnessConfig
        from sdbench.reporting import generate_html_overview

        mapped = json.loads((report.parent / "result.json").read_text())
        destination = self.root / "filtered-report"
        destination.mkdir()
        case_set = '</title><img src=x onerror="alert(3)">'
        filtered = Path(generate_html_overview(
            [mapped], str(destination), HarnessConfig(dataset="synthetic", azure_storage_enabled=False),
            case_set=case_set,
        )).read_text()
        self.assertIn(html.escape(case_set), filtered)
        self.assertNotIn(case_set, filtered)

    def test_recorded_origin_is_not_inferred_from_dataset_label(self):
        for index, label in enumerate(("synthetic", "myeloma-expert-records")):
            with self.subTest(dataset=label):
                self.case["dataset"] = label
                self.case_path.write_text(json.dumps(self.case))
                self.verifier.update(dataset=label, case_hash=canonical_hash(self.case))
                self.write("verifier/result.json", self.verifier)
                report = export_trial_report(self.trial, self.case_path)
                content = report.read_text()
                self.assertIn(f"Recorded-only retrospective episode; dataset label: {label}", content)
                self.assertIn("origin is not inferred from that label", content)
                self.assertNotIn("SYNTHETIC development/demo case", content)
                self.assertNotIn("belong to an authored fictional case", content)
                self.assertNotIn(f"{label} benchmark case", content)
                report.parent.rename(self.root / f"report-{index}")

    def test_configured_aliases_are_separate_from_returned_model_identities(self):
        self.verifier["gatekeeper_model"] = "mutable-gatekeeper-alias"
        self.write("verifier/result.json", self.verifier)
        calls = [self.call("diagnosis"), self.call("gatekeeper"),
                 self.call("gatekeeper"), self.call("gatekeeper")]
        calls[0]["response"]["model"] = "diagnoser-version-1"
        calls[1]["response"]["model"] = "gatekeeper-version-1"
        calls[2]["response"]["model"] = "gatekeeper-version-2"
        calls[3]["status"] = "error"
        calls[3]["response"] = None
        for index, call in enumerate(calls):
            call["call_id"] = f"attempt-{index}"
        (self.trial / "artifacts/gatekeeper-evidence/raw_calls.jsonl").write_text(
            "".join(json.dumps(call) + "\n" for call in calls)
        )
        report = export_trial_report(self.trial, self.case_path)
        mapped = json.loads((report.parent / "result.json").read_text())
        identities = mapped["export_metadata"]["model_identities"]
        self.assertEqual(identities["gatekeeper"], {
            "configured_model": "mutable-gatekeeper-alias",
            "provider_returned_models": ["gatekeeper-version-1", "gatekeeper-version-2"],
            "attempts_without_model": 1,
            "multiple_provider_models": True,
        })
        self.assertEqual(identities["diagnosis"]["provider_returned_models"], ["diagnoser-version-1"])
        self.assertFalse(identities["diagnosis"]["multiple_provider_models"])
        self.assertEqual(identities["judge"]["provider_returned_models"], [])
        self.assertEqual(identities["judge"]["attempts_without_model"], 1)
        manifest = json.loads((report.parent / "manifest.json").read_text())
        self.assertEqual(manifest["model_identities"], identities)
        content = report.read_text()
        for text in ("Configured models:", "gatekeeper=mutable-gatekeeper-alias",
                     "Provider-returned model identities:", "gatekeeper-version-1, gatekeeper-version-2",
                     "attempts without model=1; multiple identities=yes", "judge=[unknown]",
                     "a configured alias is not a resolved model version"):
            self.assertIn(text, content)


if __name__ == "__main__":
    unittest.main()
