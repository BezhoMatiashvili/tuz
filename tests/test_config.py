import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from warehouse_sync.config import Settings
from warehouse_sync.errors import ConfigurationError


class ProductionSettingsTests(unittest.TestCase):
    def _write_credential(self, directory: Path, name: str, value: str) -> str:
        path = directory / name
        if path.exists():
            path.chmod(0o600)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o400)
        return str(path)

    def _production_environment(self, directory: Path) -> dict[str, str]:
        database_url = (
            "postgresql://warehouse_sync_writer:not-a-real-password@db.example.test/"
            "postgres?sslmode=verify-full"
        )
        return {
            "WAREHOUSE_APP_ENV": "production",
            "WAREHOUSE_BASE_URL": "https://warehouse.example.test",
            "WAREHOUSE_PRODUCTS_PATH": "/app/Warehouse/app/Ajax/GetProducts.php",
            "WAREHOUSE_ALLOWED_SOURCE_HOSTS": "warehouse.example.test",
            "WAREHOUSE_ALLOWED_DATABASE_HOSTS": "db.example.test",
            "WAREHOUSE_SOURCE_USERNAME": "warehouse-reader",
            "WAREHOUSE_SOURCE_PASSWORD_FILE": self._write_credential(
                directory, "source-password", "source-password"
            ),
            "WAREHOUSE_HMAC_KEY_FILE": self._write_credential(
                directory, "hmac-key", base64.b64encode(b"x" * 32).decode()
            ),
            "WAREHOUSE_DATABASE_URL_FILE": self._write_credential(
                directory, "database-url", database_url
            ),
        }

    def test_production_accepts_only_credentials_files_and_tls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = self._production_environment(Path(temporary_directory))
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.load(
                    require_database=True, require_source_auth=True, require_hmac_key=True
                )
        self.assertTrue(settings.is_production)
        self.assertEqual(settings.source_username, "warehouse-reader")
        self.assertIsNotNone(settings.hmac_key)

    def test_production_rejects_http_cookie_and_untrusted_hosts(self) -> None:
        cases = (
            {"WAREHOUSE_BASE_URL": "http://warehouse.example.test"},
            {"WAREHOUSE_COOKIE": "browser-cookie-must-never-be-used"},
            {"WAREHOUSE_BASE_URL": "https://other.example.test"},
            {"WAREHOUSE_ALLOWED_DATABASE_HOSTS": "other-db.example.test"},
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            for override in cases:
                environment = self._production_environment(Path(temporary_directory))
                environment.update(override)
                with self.subTest(override=override), patch.dict(
                    os.environ, environment, clear=True
                ):
                    with self.assertRaises(ConfigurationError):
                        Settings.load(
                            require_database=True, require_source_auth=True, require_hmac_key=True
                        )

    def test_production_rejects_database_without_verify_full(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self._production_environment(directory)
            environment["WAREHOUSE_DATABASE_URL_FILE"] = self._write_credential(
                directory,
                "insecure-database-url",
                (
                    "postgresql://warehouse_sync_writer:password@db.example.test/"
                    "postgres?sslmode=require"
                ),
            )
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.load(
                        require_database=True, require_source_auth=True, require_hmac_key=True
                    )
