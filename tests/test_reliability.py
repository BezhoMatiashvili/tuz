"""Reliability regressions which run without a network or PostgreSQL server."""

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

import psycopg
import requests

from warehouse_sync.client import WarehouseClient
from warehouse_sync.config import Settings
from warehouse_sync.errors import AuthenticationError, SourceProtocolError
from warehouse_sync.models import SyncCounters
from warehouse_sync.parser import parse_products_page
from warehouse_sync.service import sync


def _settings() -> Settings:
    return Settings(
        environment="test", database_url="postgresql://example.invalid/test",
        base_url="http://example.invalid", products_path="/products",
        source_username="reader", source_password="private-password", cookie_header=None,
        hmac_key=b"k" * 32, timeout_seconds=1, request_delay_seconds=0.01,
        max_retries=3, max_response_bytes=4096,
    )


def _html(product_id: str, value: str = "A", pages: int = 2) -> str:
    return f'<tr class="product_names" id="{product_id}"><td>{value}</td></tr>LoadMoreData({pages})'


class ReliabilityTests(unittest.TestCase):
    def test_missing_page_does_not_finish_or_deactivate(self) -> None:
        client = MagicMock()
        client.fetch_products_page.side_effect = [_html("1")] + ["<html>login</html>"] * 3
        repository = MagicMock()
        repository.acquire_single_runner_lock.return_value = True
        repository.record_page.return_value = SyncCounters(pages_fetched=1, rows_seen=1)
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch("warehouse_sync.service.WarehouseRepository", return_value=repository),
            patch("warehouse_sync.service.time.sleep"),
        ):
            with self.assertRaisesRegex(SourceProtocolError, "Page 2"):
                sync(_settings())
        repository.finish_run.assert_not_called()
        repository.fail_run.assert_called_once()

    def test_a_stop_signal_records_the_run_as_interrupted(self) -> None:
        from warehouse_sync.errors import SyncInterrupted

        client = MagicMock()
        client.fetch_products_page.side_effect = [_html("1"), SyncInterrupted("signal 15")]
        repository = MagicMock()
        repository.acquire_single_runner_lock.return_value = True
        repository.start_run.return_value = 42
        repository.record_page.return_value = SyncCounters(pages_fetched=1, rows_seen=1)
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch("warehouse_sync.service.WarehouseRepository", return_value=repository),
            patch("warehouse_sync.service.time.sleep"),
        ):
            with self.assertRaises(SyncInterrupted):
                sync(_settings())
        repository.close_abandoned_runs.assert_called_once_with(42)
        self.assertEqual(repository.fail_run.call_args.args[2], "interrupted")
        repository.finish_run.assert_not_called()

    def test_a_run_started_with_nohup_keeps_ignoring_a_closed_terminal(self) -> None:
        import signal

        from warehouse_sync import __main__ as cli
        from warehouse_sync.errors import SyncInterrupted

        numbers = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)  # all three are put back
        previous = {number: signal.getsignal(number) for number in numbers}
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)  # what nohup leaves behind
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            cli._stop_on_signals(SyncInterrupted)
            self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
            self.assertTrue(callable(signal.getsignal(signal.SIGTERM)))  # systemd's stop
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)

    def test_a_second_sync_says_so_instead_of_blaming_the_configuration(self) -> None:
        from warehouse_sync.errors import SyncAlreadyRunning

        client = MagicMock()
        client.fetch_products_page.return_value = _html("1", pages=1)
        repository = MagicMock()
        repository.acquire_single_runner_lock.return_value = False
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch("warehouse_sync.service.WarehouseRepository", return_value=repository),
        ):
            with self.assertRaises(SyncAlreadyRunning):
                sync(_settings())
        repository.start_run.assert_not_called()

    def test_the_command_line_names_a_second_sync_and_a_stop(self) -> None:
        import io
        from contextlib import redirect_stderr

        from warehouse_sync import __main__ as cli
        from warehouse_sync.errors import SyncAlreadyRunning, SyncInterrupted

        for error, code, said in ((SyncAlreadyRunning("x"), 3, "already running"),
                                  (SyncInterrupted("signal 15"), 130, "interrupted")):
            output = io.StringIO()
            with (
                patch("sys.argv", ["warehouse_sync"]),
                patch("warehouse_sync.config.Settings.load", return_value=_settings()),
                patch("warehouse_sync.service.sync", side_effect=error),
                patch.object(cli, "_stop_on_signals"),
                redirect_stderr(output),
            ):
                self.assertEqual(cli.main(), code)
            self.assertIn(said, output.getvalue())

    def test_changed_second_pass_stops_finalization(self) -> None:
        client = MagicMock()
        client.fetch_products_page.side_effect = [
            _html("1"), _html("2"), _html("1"), _html("2", "changed"),
        ]
        repository = MagicMock()
        repository.acquire_single_runner_lock.return_value = True
        repository.record_page.return_value = SyncCounters(pages_fetched=1, rows_seen=1)
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch("warehouse_sync.service.WarehouseRepository", return_value=repository),
            patch("warehouse_sync.service.time.sleep"),
        ):
            with self.assertRaisesRegex(SourceProtocolError, "page 2 changed"):
                sync(replace(_settings(), max_retries=1))
        repository.finish_run.assert_not_called()

    def test_duplicate_across_pages_stops_finalization(self) -> None:
        client = MagicMock()
        client.fetch_products_page.side_effect = [_html("1"), _html("1")]
        repository = MagicMock()
        repository.acquire_single_runner_lock.return_value = True
        repository.record_page.return_value = SyncCounters(pages_fetched=1, rows_seen=1)
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch("warehouse_sync.service.WarehouseRepository", return_value=repository),
            patch("warehouse_sync.service.time.sleep"),
        ):
            with self.assertRaisesRegex(SourceProtocolError, "Duplicate product across pages"):
                sync(replace(_settings(), max_retries=1))
        repository.finish_run.assert_not_called()

    def test_restarts_from_first_page_after_lost_database_connection(self) -> None:
        client = MagicMock()
        client.fetch_products_page.return_value = _html("1", pages=1)
        first, second = MagicMock(), MagicMock()
        first.acquire_single_runner_lock.return_value = True
        first.record_page.side_effect = psycopg.OperationalError("secret details")
        second.acquire_single_runner_lock.return_value = True
        second.record_page.return_value = SyncCounters(pages_fetched=1, rows_seen=1)
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch("warehouse_sync.service.WarehouseRepository", side_effect=[first, second]),
            patch("warehouse_sync.service.time.sleep"),
            self.assertLogs("warehouse_sync.service", level="WARNING") as captured,
        ):
            result = sync(_settings())
        self.assertTrue(result.complete)
        self.assertEqual(client.fetch_products_page.call_args_list[0].args, (1,))
        self.assertEqual(client.fetch_products_page.call_args_list[1].args, (1,))
        self.assertFalse(second.finish_run.call_args.kwargs["complete"])
        self.assertNotIn("secret details", " ".join(captured.output))

    def test_changed_source_restarts_a_full_scan(self) -> None:
        client = MagicMock()
        client.fetch_products_page.side_effect = [
            _html("1", pages=1), _html("1", "changed", pages=1),
            _html("1", "changed", pages=1), _html("1", "changed", pages=1),
        ]
        first, second = MagicMock(), MagicMock()
        for repository in (first, second):
            repository.acquire_single_runner_lock.return_value = True
            repository.record_page.return_value = SyncCounters(pages_fetched=1, rows_seen=1)
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch("warehouse_sync.service.WarehouseRepository", side_effect=[first, second]),
            patch("warehouse_sync.service.time.sleep"),
        ):
            result = sync(_settings())
        self.assertTrue(result.complete)
        first.finish_run.assert_not_called()
        self.assertFalse(second.finish_run.call_args.kwargs["complete"])
        self.assertEqual(client.fetch_products_page.call_count, 4)

    def test_database_authentication_error_is_not_retried(self) -> None:
        client = MagicMock()
        client.fetch_products_page.return_value = _html("1", pages=1)
        with (
            patch("warehouse_sync.service.WarehouseClient", return_value=client),
            patch(
                "warehouse_sync.service.WarehouseRepository",
                side_effect=psycopg.errors.InvalidPassword("sensitive database detail"),
            ) as repository,
        ):
            with self.assertRaises(psycopg.errors.InvalidPassword):
                sync(_settings())
        self.assertEqual(repository.call_count, 1)

    def test_truncated_product_row_rejected(self) -> None:
        with self.assertRaises(SourceProtocolError):
            parse_products_page('<tr class="product_names" id="1"><td>truncated')

    def test_transient_http_response_is_retried(self) -> None:
        client = WarehouseClient(_settings())
        temporary = MagicMock()
        temporary.status_code = 503
        temporary.headers = {}
        good = MagicMock()
        good.status_code = 200
        good.headers = {"Content-Type": "text/html"}
        good.encoding = "utf-8"
        good.iter_content.return_value = [b"content"]
        temporary.__enter__.return_value = temporary
        good.__enter__.return_value = good
        client._session.get = MagicMock(side_effect=[temporary, good])
        with patch("warehouse_sync.client.time.sleep"):
            self.assertEqual(client.fetch_products_page(1), "content")
        self.assertEqual(client._session.get.call_count, 2)
        client.close()

    def test_authentication_failure_is_not_retried(self) -> None:
        client = WarehouseClient(_settings())
        response = MagicMock()
        response.status_code = 403
        response.__enter__.return_value = response
        client._session.get = MagicMock(return_value=response)
        with self.assertRaises(AuthenticationError):
            client.fetch_products_page(1)
        self.assertEqual(client._session.get.call_count, 1)
        client.close()

    def test_network_error_text_is_not_printed_in_exception(self) -> None:
        client = WarehouseClient(_settings())
        client._session.get = MagicMock(
            side_effect=requests.ConnectionError("secret url and cookie")
        )
        with patch("warehouse_sync.client.time.sleep"):
            with self.assertRaises(SourceProtocolError) as captured:
                client.fetch_products_page(4)
        self.assertNotIn("secret", str(captured.exception))
        client.close()
