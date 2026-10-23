import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sdbench.isolation.common import canonical_hash
from sdbench.isolation.service import TrialService, make_server


class ServiceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.case = {"case_id": "boundary-case", "dataset": "synthetic", "case_summary": "Public presentation.",
                     "full_text": "PRIVATE_FULL_CASE_SENTINEL", "final_diagnosis": "PRIVATE_ANSWER_SENTINEL"}
        self.config = {"trial_id": "trial-a", "case_id": "boundary-case", "case_hash": canonical_hash(self.case),
                       "protocol_hash": "protocol", "diagnosis_token": "trial-token-" + "a" * 32,
                       "diagnosis_model": "gpt-5", "gatekeeper_model": "o4-mini", "judge_model": "gpt-5",
                       "gk_prompt": "v6_fixed", "judge_prompt": "v3_reasoning", "api_mode": "mock",
                       "max_turns": 2, "max_model_calls": 2}
        self.service = TrialService(self.config, self.case, Path(self.temp.name))
        self.server = make_server(self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, token=None):
        headers = {"Authorization": "Bearer " + (token or self.config["diagnosis_token"])}
        data = None if body is None else json.dumps(body).encode()
        try:
            with urlopen(Request(self.url + path, data=data, headers=headers), timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_public_session_does_not_expose_private_record(self):
        status, body = self.request("/session")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"case_summary", "max_turns", "turns_left"})
        self.assertNotIn("PRIVATE_", json.dumps(body))
        self.assertNotIn("token", json.dumps(body))

    def test_cross_trial_authentication_and_private_paths_are_denied(self):
        self.assertEqual(self.request("/session", token="different-trial-token")[0], 401)
        for path in ("/case", "/logs", "/private/case.json", "/judge", "/../private/case.json"):
            status, body = self.request(path)
            self.assertEqual(status, 404)
            self.assertNotIn("PRIVATE_", json.dumps(body))

    def test_cannot_override_model_role_or_case(self):
        valid_messages = [{"role": "user", "content": "hello"}]
        for injected in ({"model": "gpt-5"}, {"role": "gatekeeper"}, {"case_id": "other"}):
            self.assertEqual(self.request("/model", {"messages": valid_messages, **injected})[0], 400)
        self.assertEqual(self.service.diagnosis_model.calls, 0)
        self.assertEqual(self.request("/tests", {"test_names": ["CBC"], "case_id": "other"})[0], 400)

    def test_complete_encounter_uses_server_owned_trace(self):
        status, response = self.request("/tests", {"test_names": ["Test to order: CBC"]})
        self.assertEqual(status, 200)
        self.assertEqual(response["turns_left"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(response))
        self.assertEqual(self.request("/diagnosis", {"diagnosis": "submitted answer"})[0], 200)
        self.assertEqual(self.request("/diagnosis", {"diagnosis": "replacement"})[0], 400)
        self.assertEqual(self.request("/model", {"messages": [{"role": "user", "content": "again"}]})[0], 400)
        events = [json.loads(line) for line in (Path(self.temp.name) / "events.jsonl").read_text().splitlines()]
        self.assertEqual([event["event"] for event in events], ["session", "tests", "diagnosis", "terminal"])
        self.assertEqual(events[-1]["diagnosis"], "submitted answer")
        self.assertTrue(all(event["config_hash"] == canonical_hash(self.config) for event in events))

    def test_model_limit_closes_encounter_and_retains_failed_terminal(self):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertEqual(self.request("/model", body)[0], 200)
        self.assertEqual(self.request("/model", body)[0], 200)
        status, response = self.request("/model", body)
        self.assertEqual(status, 503)
        self.assertNotIn("PRIVATE_", json.dumps(response))
        self.assertTrue(self.service.closed)
        events = [json.loads(line) for line in (Path(self.temp.name) / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["status"], "failed")

    def test_reusing_evidence_directory_fails(self):
        with self.assertRaisesRegex(ValueError, "Refusing to reuse"):
            TrialService(self.config, self.case, self.temp.name)


if __name__ == "__main__":
    unittest.main()
