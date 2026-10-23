"""Deterministic CLI regressions; no model, storage, or credential access."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, call, patch

from sdbench.cli import azure_storage, test_runner
from sdbench.config import SDBenchConfig


class TestRunnerSummaryTests(unittest.TestCase):
    def run_cli(self, *, fail_second_batch=False):
        config = SDBenchConfig(
            max_turns=5, gatekeeper_prompt_version="v6_fixed", gatekeeper_model="fixture",
            judge_prompt_version="v3_reasoning", judge_model="fixture",
        )
        batches = 0

        def observations(*, test_names, **kwargs):
            nonlocal batches
            batches += 1
            if fail_second_batch and batches == 2:
                raise RuntimeError("Fixture observation failure")
            return [{"test_name": name, "results": "Fictional observation", "is_synthetic": True}
                    for name in test_names]

        output = io.StringIO()
        with (
            patch.object(test_runner.dotenv, "load_dotenv"),
            patch.object(SDBenchConfig, "from_command_line", return_value=config),
            patch.object(test_runner.case_data, "get_case", return_value={"case_title": "Synthetic fixture"}),
            patch("sdbench.gatekeeper.order_tests", side_effect=observations),
            patch("sys.argv", ["test_runner", "synthetic", "fixture", "A", "B", "C", "D", "--batch-size", "2"]),
            redirect_stdout(output),
        ):
            if fail_second_batch:
                with self.assertRaises(SystemExit) as raised:
                    test_runner.main()
                self.assertEqual(raised.exception.code, 1)
            else:
                test_runner.main()
        return output.getvalue()

    def test_two_batches_report_two_turns_not_sum_of_cumulative_snapshots(self):
        output = self.run_cli()
        self.assertIn("Successful batches: 2/2", output)
        self.assertIn("Total tests run: 4", output)
        self.assertIn("Total turns used: 2", output)
        self.assertNotIn("Total turns used: 3", output)

    def test_failed_batch_still_counts_the_turn_consumed_by_runner(self):
        output = self.run_cli(fail_second_batch=True)
        self.assertIn("Successful batches: 1/2", output)
        self.assertIn("Total turns used: 2", output)


class StorageCleanupTests(unittest.TestCase):
    def test_cleanup_only_lists_and_deletes_blobs_created_by_this_cli(self):
        owned = "test/azure_cli_test_20200101_000000.json"
        unrelated = "test/other-tool-fixture.json"
        container = Mock()
        service = Mock()
        service.get_container_client.return_value = container

        def list_blobs(account, container_name, *, prefix):
            return [name for name in (owned, unrelated) if name.startswith(prefix)]

        with (
            patch.object(azure_storage.dotenv, "load_dotenv"),
            patch.object(azure_storage, "validate_azure_access"),
            patch.object(azure_storage, "get_blob_service_client", return_value=service),
            patch.object(azure_storage, "list_blobs", side_effect=list_blobs) as listed,
            patch("builtins.input", return_value="y"),
            patch("sys.argv", ["azure_storage", "--account-name", "fixture-account",
                               "--container-name", "fixture-container", "--delete-test-blobs"]),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            azure_storage.main()

        self.assertEqual(raised.exception.code, 0)
        listed.assert_called_once_with("fixture-account", "fixture-container", prefix="test/azure_cli_test_")
        self.assertEqual(container.delete_blob.call_args_list, [call(owned)])


if __name__ == "__main__":
    unittest.main()
