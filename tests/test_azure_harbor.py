import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sdbench.cli.azure_harbor import refresh_token


class AzureCredentialTests(unittest.TestCase):
    def test_refresh_is_private_and_keeps_only_access_token_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            expiry = int(time.time()) + 3600
            with patch("sdbench.cli.azure_harbor.subprocess.run", return_value=SimpleNamespace(
                stdout=json.dumps({"accessToken": "test-only-value", "expires_on": expiry, "tenant": "irrelevant"})
            )) as run:
                self.assertEqual(refresh_token(path, "test-tenant"), expiry)
            token_path = path / "token.json"
            self.assertEqual(json.loads(token_path.read_text()), {"accessToken": "test-only-value", "expires_on": expiry})
            self.assertEqual(os.stat(token_path).st_mode & 0o777, 0o600)
            self.assertNotIn("test-only-value", str(run.call_args))
            self.assertFalse((path / ".token.next").exists())

    def test_expired_and_bad_tokens_fail_without_revealing_cli_output(self):
        for response in ({"accessToken": "PRIVATE", "expires_on": 0}, {"secret": "PRIVATE"}):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                with patch("sdbench.cli.azure_harbor.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(response))):
                    with self.assertRaises(RuntimeError) as error:
                        refresh_token(Path(directory), "test-tenant")
                    self.assertNotIn("PRIVATE", str(error.exception))
                self.assertFalse((Path(directory) / "token.json").exists())


if __name__ == "__main__":
    unittest.main()
