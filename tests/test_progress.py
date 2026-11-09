"""Verify terminal progress reports without contacting the source or database."""

import unittest
from unittest.mock import MagicMock, patch

from warehouse_sync.config import Settings
from warehouse_sync.models import SyncCounters
from warehouse_sync.service import sync


class SyncProgressTests(unittest.TestCase):
    def test_logs_each_page_and_cumulative_counts(self) -> None:
        settings = Settings(
            environment="test",
            database_url="postgresql://example.invalid/test",
            base_url="http://example.invalid",
            products_path="/products",
            source_username="reader",
            source_password="secret",
            cookie_header=None,
            hmac_key=b"k" * 32,
            timeout_seconds=1,
            request_delay_seconds=0.01,
            max_retries=1,
            max_response_bytes=4096,
        )
        client = MagicMock()
        client.fetch_products_page.side_effect = [
            '<tr class="product_names" id="1"><td>A</td></tr>LoadMoreData(2)',
            '<tr class="product_names" id="2"><td>B</td></tr>',
            '<tr class="product_names" id="1"><td>A</td></tr>LoadMoreData(2)',
            '<tr class="product_names" id="2"><td>B</td></tr>',
        ]
        repository = MagicMock()
        repository.acquire_single_runner_lock.return_value = True
        repository.start_run.return_value = 1
        repository.record_page.side_effect = [
            SyncCounters(pages_fetched=1, rows_seen=1, rows_added=1),
            SyncCounters(pages_fetched=1, rows_seen=1, rows_unchanged=1),
        ]
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch("warehouse_sync.service.WarehouseRepository", return_value=repository),
            patch("warehouse_sync.service.time.sleep"),
            self.assertLogs("warehouse_sync.service", level="INFO") as captured,
        ):
            sync(settings)

        self.assertIn("Page 1/2 (50.0%) | seen=1 added=1 changed=0 unchanged=0", captured.output[1])
        self.assertIn(
            "Page 2/2 (100.0%) | seen=2 added=1 changed=0 unchanged=1", captured.output[2]
        )
        self.assertNotIn("secret", " ".join(captured.output))
