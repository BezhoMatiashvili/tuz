import base64
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from warehouse_sync.client import WarehouseClient
from warehouse_sync.config import Settings
from warehouse_sync.errors import AuthenticationError, SourceProtocolError


class _MockSourceHandler(BaseHTTPRequestHandler):
    mode = "ok"
    last_authorization = ""

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        type(self).last_authorization = self.headers.get("Authorization", "")
        if type(self).mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://attacker.example.test/")
            self.end_headers()
            return
        if type(self).mode == "expired":
            self.send_response(403)
            self.end_headers()
            return
        if type(self).mode == "large":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "999999")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'<tr class="product_names" id="1"><td>A</td></tr>')

    def log_message(self, _format: str, *_args: object) -> None:
        return


class WarehouseClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _MockSourceHandler.mode = "ok"
        _MockSourceHandler.last_authorization = ""
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockSourceHandler)
        except PermissionError:
            self.skipTest("the current sandbox disallows a loopback mock-source listener")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _settings(self, *, cookie: str | None = None, response_limit: int = 4096) -> Settings:
        return Settings(
            environment="test",
            database_url=None,
            base_url=f"http://127.0.0.1:{self.server.server_port}",
            products_path="/products",
            source_username=None if cookie else "reader",
            source_password=None if cookie else "password",
            cookie_header=cookie,
            hmac_key=b"h" * 32,
            timeout_seconds=2,
            request_delay_seconds=0.01,
            max_retries=1,
            max_response_bytes=response_limit,
        )

    def test_mock_source_uses_read_only_auth_and_parses_success(self) -> None:
        client = WarehouseClient(self._settings())
        try:
            self.assertIn('product_names', client.fetch_products_page(1))
        finally:
            client.close()
        self.assertEqual(
            _MockSourceHandler.last_authorization,
            "Basic " + base64.b64encode(b"reader:password").decode(),
        )

    def test_redirect_credential_expiry_and_response_limit_are_rejected(self) -> None:
        cases = (
            ("redirect", AuthenticationError, 4096),
            ("expired", AuthenticationError, 4096),
            ("large", SourceProtocolError, 64),
        )
        for mode, exception_type, response_limit in cases:
            with self.subTest(mode=mode):
                _MockSourceHandler.mode = mode
                client = WarehouseClient(self._settings(response_limit=response_limit))
                try:
                    with self.assertRaises(exception_type):
                        client.fetch_products_page(1)
                finally:
                    client.close()

    def test_cookie_url_and_library_error_text_are_not_logged(self) -> None:
        secret_cookie = "session=do-not-log-this-value"
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        _MockSourceHandler.mode = "expired"
        client = WarehouseClient(self._settings(cookie=secret_cookie))
        try:
            with self.assertRaises(AuthenticationError) as raised:
                client.fetch_products_page(1)
        finally:
            client.close()
            root.removeHandler(handler)
        rendered = "\n".join(record.getMessage() for record in records)
        self.assertNotIn(secret_cookie, str(raised.exception))
        self.assertNotIn(secret_cookie, rendered)
        self.assertNotIn(self._settings(cookie=secret_cookie).products_url, rendered)
