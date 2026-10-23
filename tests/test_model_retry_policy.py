"""Permanent model-call limits bypass backoff; ordinary provider failures still retry."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sdbench import models


class ModelRetryPolicyTests(unittest.TestCase):
    def test_sync_call_limit_is_not_retried(self):
        backend = Mock(side_effect=models.ModelCallLimitError("Budget exhausted"))
        with patch.object(models.time, "sleep") as sleep:
            with self.assertRaises(models.ModelCallLimitError):
                models.call_llm_with_retry(backend, messages=[])
        backend.assert_called_once()
        sleep.assert_not_called()

    def test_other_sync_failures_keep_existing_retry_behavior(self):
        result = object()
        backend = Mock(side_effect=[RuntimeError("Temporary failure"), result])
        with patch.object(models.time, "sleep") as sleep, patch.object(models, "_log_retry"):
            self.assertIs(models.call_llm_with_retry(backend, messages=[]), result)
        self.assertEqual(backend.call_count, 2)
        sleep.assert_called_once()

    def test_async_call_limit_is_not_retried(self):
        backend = AsyncMock(side_effect=models.ModelCallLimitError("Budget exhausted"))
        wrapped = models._retry_with_backoff_async()(backend)
        with patch.object(models.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(models.ModelCallLimitError):
                asyncio.run(wrapped())
        backend.assert_awaited_once()
        sleep.assert_not_awaited()

    def test_other_async_failures_keep_existing_retry_behavior(self):
        result = object()
        backend = AsyncMock(side_effect=[RuntimeError("Temporary failure"), result])
        wrapped = models._retry_with_backoff_async()(backend)
        with patch.object(models.asyncio, "sleep", new_callable=AsyncMock) as sleep, \
             patch.object(models, "_log_retry"):
            self.assertIs(asyncio.run(wrapped()), result)
        self.assertEqual(backend.await_count, 2)
        sleep.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
