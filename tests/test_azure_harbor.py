"""Azure credential refresh and Harbor launcher lifecycle, without live calls."""

import json
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdbench.cli import azure_harbor


class AzureHarborTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

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
        payloads = [{"accessToken": token, "expires_on": expiry}
                    for token, expiry in (("SECRET", float("nan")), ("SECRET", float("inf")),
                                          ("SECRET WITH SPACE", 2000), ("SECRET", 1100))]
        payloads.append({"secret": "SECRET"})
        for payload in payloads:
            result = subprocess.CompletedProcess(["az"], 0,
                json.dumps(payload), "")
            with self.subTest(payload=payload), \
                 patch.object(azure_harbor.subprocess, "run", return_value=result), \
                 patch.object(azure_harbor.time, "time", return_value=1000):
                with self.assertRaises(RuntimeError) as raised:
                    azure_harbor.refresh_token(self.root, "tenant-id")
                self.assertNotIn("SECRET", str(raised.exception))
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
