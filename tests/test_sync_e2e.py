"""End-to-end sync checks against CI's disposable PostgreSQL and mock source."""

import os
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import psycopg
except ImportError:
    psycopg = None

if psycopg is not None:
    from warehouse_sync.config import Settings
    from warehouse_sync.integrity import verify_version_integrity_tag
    from warehouse_sync.migrations import apply_migrations_on_connection
    from warehouse_sync.service import sync


ADMIN_DATABASE_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")


class _E2ESourceHandler(BaseHTTPRequestHandler):
    product_id = "e2e-product"

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            (
                f'<tr class="product_names" id="{type(self).product_id}">'
                '<td>SKU</td><td>Mock product</td><td>Model</td><td>Origin</td>'
                '<td>Duplicate</td><td>0</td><td>0</td><td>Brand</td></tr>'
                '<button onclick="LoadMoreData(1)">1</button>'
            ).encode()
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return


@unittest.skipUnless(
    ADMIN_DATABASE_URL and psycopg is not None,
    "requires psycopg and the disposable CI PostgreSQL service",
)
class SyncEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _E2ESourceHandler)
        except PermissionError:
            raise unittest.SkipTest("the current sandbox disallows a loopback mock-source listener")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        assert ADMIN_DATABASE_URL is not None
        with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as connection:
            role_script = (
                Path(__file__).resolve().parents[1] / "ops/sql/provision_production_roles.sql"
            )
            connection.execute(role_script.read_text())
            connection.execute("GRANT warehouse_sync_writer TO CURRENT_USER")
            connection.execute(
                "ALTER ROLE warehouse_sync_writer LOGIN PASSWORD 'writer-test-password'"
            )
        with psycopg.connect(ADMIN_DATABASE_URL) as connection:
            connection.execute("SET ROLE warehouse_migration_owner")
            apply_migrations_on_connection(connection)
            connection.commit()

        parsed = urlparse(ADMIN_DATABASE_URL)
        cls.writer_database_url = (
            f"postgresql://warehouse_sync_writer:writer-test-password@{parsed.hostname}:"
            f"{parsed.port or 5432}{parsed.path}"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_hourly_sync_is_idempotent_without_storing_source_html(self) -> None:
        product_id = f"e2e-{uuid.uuid4().hex}"
        _E2ESourceHandler.product_id = product_id
        settings = Settings(
            environment="test",
            database_url=self.writer_database_url,
            base_url=f"http://127.0.0.1:{self.server.server_port}",
            products_path="/products",
            source_username="reader",
            source_password="password",
            cookie_header=None,
            hmac_key=b"k" * 32,
            timeout_seconds=3,
            request_delay_seconds=0.01,
            max_retries=1,
            max_response_bytes=4096,
        )

        first = sync(settings)
        second = sync(settings)

        self.assertEqual((first.counters.rows_added, second.counters.rows_added), (1, 0))
        self.assertEqual(second.counters.rows_unchanged, 1)
        with psycopg.connect(ADMIN_DATABASE_URL) as connection:
            version = connection.execute(
                """
                SELECT observed_run_id, source_page, source_columns, row_hash, integrity_tag
                FROM warehouse.warehouse_product_versions
                WHERE source_product_id = %s
                """,
                (product_id,),
            ).fetchone()
            column_exists = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'warehouse' AND table_name = 'warehouse_products'
                      AND column_name = 'source_row_html'
                )
                """
            ).fetchone()[0]
        self.assertIsNotNone(version)
        assert version is not None
        self.assertTrue(
            verify_version_integrity_tag(
                b"k" * 32,
                version[4],
                source_product_id=product_id,
                observed_run_id=version[0],
                source_page=version[1],
                source_columns=version[2],
                row_hash=version[3],
            )
        )
        self.assertFalse(column_exists)
