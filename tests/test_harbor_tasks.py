import asyncio
import json
import hashlib
import importlib.util
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import tomllib
from types import SimpleNamespace

import yaml

from sdbench.harbor_integration.tasks import AGENT_TIMEOUT_SECONDS, HARBOR_VERSION, build_task


def _case():
    return {
        "dataset": "synthetic", "case_id": "1",
        "case_summary": "A patient has fatigue.",
        "case_title": "PRIVATE_TITLE_MARKER",
        "full_text": "PRIVATE_FULL_CASE_MARKER",
        "final_diagnosis": "PRIVATE_REFERENCE_MARKER",
    }


class HarborTaskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp_path = Path(self.directory.name)
        self.case = _case()

    def test_role_build_contexts_do_not_disclose_private_case(self):
        tmp_path, case = self.tmp_path, self.case
        task = build_task(case, {}, tmp_path / "task")
        compose = yaml.safe_load((task / "environment/docker-compose.yaml").read_text())
        public = task / "environment/diagnosis"
        assert compose["services"]["main"]["build"]["context"] == "./diagnosis"
        assert compose["networks"]["clinical"]["internal"] is True
        assert compose["services"]["main"]["networks"] == ["clinical"]
        assert compose["services"]["gatekeeper"]["networks"] == ["clinical"]
        assert "environment" not in compose["services"]["main"]
        for path in public.rglob("*"):
            if path.is_file():
                data = path.read_text()
                assert "PRIVATE_REFERENCE_MARKER" not in data
                assert "PRIVATE_FULL_CASE_MARKER" not in data
                assert "PRIVATE_TITLE_MARKER" not in data
        assert not list(public.rglob("*.yaml"))
        assert not (public / "sdbench/isolation/service.py").exists()
        assert not (public / "private").exists()
        private = json.loads((task / "environment/gatekeeper/private/trial.json").read_text())
        assert private["api_mode"] == "mock"
        assert (public / "diagnosis-token").read_text().strip() == private["diagnosis_token"]
        assert len(private["diagnosis_token"]) >= 40
        assert json.loads((task / "tests/private/case.json").read_text()) == case


    def test_separate_verifier_receives_authoritative_sidecar_evidence(self):
        tmp_path, case = self.tmp_path, self.case
        task = build_task(case, {}, tmp_path / "task")
        config = tomllib.loads((task / "task.toml").read_text())
        assert config["verifier"]["environment_mode"] == "separate"
        judge_compose = yaml.safe_load((task / "tests/docker-compose.yaml").read_text())
        assert judge_compose["services"]["main"]["network_mode"] == "none"
        evidence = next(item for item in config["artifacts"] if item.get("service") == "gatekeeper")
        assert evidence["source"] == "/private/evidence"
        assert "--evidence /private/evidence/events.jsonl" in (task / "tests/test.sh").read_text()
        assert "COPY test.sh /tests/test.sh" in (task / "tests/Dockerfile").read_text()
        assert config["metadata"]["harbor_version"] == HARBOR_VERSION

    def test_runtime_provenance_matches_copied_code_and_pinned_dependencies(self):
        task = build_task(self.case, {}, self.tmp_path / "task")
        provenance = json.loads((task / "isolation.json").read_text())["runtime_provenance"]
        for role, context in (("diagnosis", "environment/diagnosis"),
                              ("gatekeeper", "environment/gatekeeper"), ("judge", "tests")):
            directory = task / context
            record = provenance[role]
            for relative, digest in record["source_inventory"].items():
                self.assertEqual(hashlib.sha256((directory / relative).read_bytes()).hexdigest(), digest)
            requirements = (directory / "requirements.txt").read_bytes()
            self.assertEqual(hashlib.sha256(requirements).hexdigest(), record["requirements_sha256"])
            self.assertTrue(all("==" in line for line in requirements.decode().splitlines()))
            self.assertIn("@sha256:", (directory / "Dockerfile").read_text())


    def test_live_access_is_role_scoped_and_credentials_not_read(self):
        tmp_path, case = self.tmp_path, self.case
        with patch.dict("os.environ", {"OPENAI_API_KEY": "PRIVATE_PROVIDER_KEY_MARKER"}):
            task = build_task(case, {"api_mode": "live"}, tmp_path / "task")
        compose = yaml.safe_load((task / "environment/docker-compose.yaml").read_text())
        assert compose["services"]["main"]["networks"] == ["clinical"]
        assert compose["services"]["gatekeeper"]["networks"] == ["clinical", "model_api"]
        assert compose["services"]["gatekeeper"]["environment"]["OPENAI_API_KEY"] == "${OPENAI_API_KEY:-}"
        config = tomllib.loads((task / "task.toml").read_text())
        assert config["verifier"]["env"]["OPENAI_API_KEY"] == "${OPENAI_API_KEY:-}"
        for path in task.rglob("*"):
            if path.is_file():
                assert "PRIVATE_PROVIDER_KEY_MARKER" not in path.read_text()


    def test_rejects_baked_credentials_and_does_not_overwrite(self):
        tmp_path, case = self.tmp_path, self.case
        target = tmp_path / "task"
        with self.assertRaisesRegex(ValueError, "runtime environment"):
            build_task(case, {"model_args": {"api_key": "bad"}}, target)
        assert not target.exists()
        build_task(case, {}, target)
        with self.assertRaises(FileExistsError):
            build_task(case, {}, target)
        with self.assertRaisesRegex(ValueError, "mode"):
            build_task(case, {"api_mode": "typo"}, tmp_path / "bad")

    def test_non_baseline_policies_are_rejected_before_task_creation(self):
        for policy in ("experimental_panel", "untrusted.module"):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "baseline diagnostic_agent"):
                build_task(self.case, {"diagnostic_agent": policy}, self.tmp_path / "invalid")
            self.assertFalse((self.tmp_path / "invalid").exists())

    def test_azure_credentials_only_mounted_in_trusted_roles_at_runtime(self):
        with patch.dict("os.environ", {"SDBENCH_AZURE_TOKEN_DIR": "/PRIVATE_HOST_PATH"}):
            task = build_task(self.case, {"api_mode": "live", "provider": "azure"}, self.tmp_path / "azure")
        compose = yaml.safe_load((task / "environment/docker-compose.yaml").read_text())
        judge = yaml.safe_load((task / "tests/docker-compose.yaml").read_text())
        main = compose["services"]["main"]
        self.assertNotIn("volumes", main)
        self.assertNotIn("environment", main)
        for role in (compose["services"]["gatekeeper"], judge["services"]["main"]):
            self.assertTrue(role["volumes"][0]["read_only"])
            self.assertEqual(role["volumes"][0]["target"], "/run/sdbench-azure")
            self.assertIn("${SDBENCH_AZURE_TOKEN_DIR", role["volumes"][0]["source"])
            self.assertEqual(role["environment"], {"SDBENCH_AZURE_TOKEN_FILE": "/run/sdbench-azure/token.json"})
        config = tomllib.loads((task / "task.toml").read_text())
        self.assertNotIn("env", config["verifier"])
        for path in task.rglob("*"):
            if path.is_file():
                self.assertNotIn("/PRIVATE_HOST_PATH", path.read_text())


    @unittest.skipUnless(importlib.util.find_spec("harbor"), "Optional Harbor is not installed")
    def test_generated_config_matches_installed_harbor_schema(self):
        tmp_path, case = self.tmp_path, self.case
        from harbor.models.task import config as harbor
        task = build_task(case, {}, tmp_path / "task")
        model = harbor.TaskConfig.model_validate(tomllib.loads((task / "task.toml").read_text()))
        assert model.verifier.environment_mode.value == "separate"
        assert model.artifacts[1].service == "gatekeeper"
        assert model.agent.timeout_sec == AGENT_TIMEOUT_SECONDS


    @unittest.skipUnless(importlib.util.find_spec("harbor"), "Optional Harbor is not installed")
    def test_installed_adapter_runs_client_inside_environment_and_propagates_failure(self):
        tmp_path = self.tmp_path
        from harbor.models.agent.context import AgentContext
        from sdbench.harbor_integration.agent import SDBenchAgent

        class Environment:
            return_code = 0
            calls = []

            async def exec(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(return_code=self.return_code, stdout="", stderr="")

        environment = Environment()
        agent = SDBenchAgent(logs_dir=tmp_path)
        context = AgentContext()
        asyncio.run(agent.install(environment))
        asyncio.run(agent.run("Public instruction", environment, context))
        assert len(environment.calls) == 2
        assert "python -m sdbench.isolation.diagnosis" in environment.calls[-1]["command"]
        assert "--token-file /run/diagnosis-token" in environment.calls[-1]["command"]
        command = shlex.split(environment.calls[-1]["command"])
        client_timeout = float(command[command.index("--timeout") + 1])
        task = build_task(self.case, {}, tmp_path / "task")
        task_config = tomllib.loads((task / "task.toml").read_text())
        self.assertEqual(client_timeout, task_config["agent"]["timeout_sec"])
        self.assertEqual(client_timeout, AGENT_TIMEOUT_SECONDS)
        assert "PRIVATE" not in environment.calls[-1]["command"]
        assert context.is_empty()  # Required for Harbor's post-run conversion hook.
        agent.populate_context_post_run(context)
        assert context.metadata["isolation"] == "docker-containers-shared-kernel"
        environment.return_code = 1
        with self.assertRaises(RuntimeError):
            asyncio.run(agent.run("Public instruction", environment, AgentContext()))


if __name__ == "__main__":
    unittest.main()
