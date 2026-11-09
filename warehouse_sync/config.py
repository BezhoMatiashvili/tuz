"""Validated configuration with a deliberately fail-closed production mode."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from warehouse_sync.errors import ConfigurationError

DEFAULT_BASE_URL = "http://45.9.45.34:6068"
DEFAULT_PRODUCTS_PATH = "/app/Warehouse/app/Ajax/GetProducts.php"
PRODUCTION_ENVIRONMENT = "production"
WRITER_ROLE = "warehouse_sync_writer"
MIGRATION_OWNER_ROLE = "warehouse_migration_owner"


def _configuration_error() -> ConfigurationError:
    """Avoid including an environment value in errors, as it may be a secret."""
    return ConfigurationError("Configuration is missing, malformed, or unsafe.")


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise _configuration_error() from exc
    if value <= 0:
        raise _configuration_error()
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise _configuration_error() from exc
    if value <= 0:
        raise _configuration_error()
    return value


def _read_secret_file(variable_name: str) -> str | None:
    path_value = os.getenv(variable_name, "").strip()
    if not path_value:
        return None
    try:
        path = Path(path_value)
        metadata = path.stat()
        if not path.is_file() or metadata.st_mode & 0o077:
            raise _configuration_error()
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise _configuration_error() from exc
    return value or None


def _secret(name: str, *, file_name: str, production: bool) -> str | None:
    from_file = _read_secret_file(file_name)
    from_environment = os.getenv(name, "").strip() or None
    if from_file and from_environment:
        raise _configuration_error()
    if production and from_environment:
        # systemd encrypted credentials are exposed as 0400 files at runtime.
        raise _configuration_error()
    return from_file or from_environment


def _decode_hmac_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise _configuration_error() from exc
    if len(key) < 32:
        raise _configuration_error()
    return key


def _required_host_set(value: str) -> frozenset[str]:
    hosts = frozenset(host.strip().lower() for host in value.split(",") if host.strip())
    if not hosts:
        raise _configuration_error()
    return hosts


def _database_url_is_production_safe(database_url: str) -> bool:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        return False
    options = parse_qs(parsed.query, keep_blank_values=True)
    return options.get("sslmode") == ["verify-full"]


def _load_environment() -> tuple[str, bool]:
    environment = os.getenv("WAREHOUSE_APP_ENV", "development").strip().lower()
    if environment not in {"development", "test", PRODUCTION_ENVIRONMENT}:
        raise _configuration_error()
    production = environment == PRODUCTION_ENVIRONMENT
    if not production:
        load_dotenv(override=False)
    return environment, production


def load_migration_database_url() -> str:
    """Load the separate migration-owner connection without accepting a writer URL."""
    _, production = _load_environment()
    database_url = _secret(
        "WAREHOUSE_MIGRATION_DATABASE_URL",
        file_name="WAREHOUSE_MIGRATION_DATABASE_URL_FILE",
        production=production,
    )
    if not database_url and not production:
        database_url = _secret(
            "DATABASE_URL", file_name="WAREHOUSE_DATABASE_URL_FILE", production=False
        )
    if not database_url:
        raise _configuration_error()
    if production:
        database = urlparse(database_url)
        allowed_database_hosts = _required_host_set(
            os.getenv("WAREHOUSE_ALLOWED_DATABASE_HOSTS", "")
        )
        if (
            not _database_url_is_production_safe(database_url)
            or database.hostname is None
            or database.hostname.lower() not in allowed_database_hosts
            or database.username != MIGRATION_OWNER_ROLE
        ):
            raise _configuration_error()
    return database_url


def load_maintenance_hmac_key() -> bytes:
    """Load the off-database key for a one-off migration-owner maintenance task."""
    _, production = _load_environment()
    material = _secret(
        "WAREHOUSE_HMAC_KEY",
        file_name="WAREHOUSE_HMAC_KEY_FILE",
        production=production,
    )
    if not material:
        raise _configuration_error()
    return _decode_hmac_key(material)


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration for exactly one source and destination."""

    environment: str
    database_url: str | None
    base_url: str
    products_path: str
    source_username: str | None
    source_password: str | None
    cookie_header: str | None
    hmac_key: bytes | None
    timeout_seconds: float
    request_delay_seconds: float
    max_retries: int
    max_response_bytes: int

    @property
    def is_production(self) -> bool:
        return self.environment == PRODUCTION_ENVIRONMENT

    @property
    def products_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.products_path.lstrip('/')}"

    @classmethod
    def load(
        cls,
        *,
        require_database: bool,
        require_source_auth: bool,
        require_hmac_key: bool = False,
    ) -> Settings:
        environment, production = _load_environment()

        database_url = _secret(
            "DATABASE_URL", file_name="WAREHOUSE_DATABASE_URL_FILE", production=production
        )
        base_url = os.getenv("WAREHOUSE_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        products_path = os.getenv("WAREHOUSE_PRODUCTS_PATH", DEFAULT_PRODUCTS_PATH).strip()
        cookie_header = os.getenv("WAREHOUSE_COOKIE", "").strip() or None
        source_username = os.getenv("WAREHOUSE_SOURCE_USERNAME", "").strip() or None
        source_password = _secret(
            "WAREHOUSE_SOURCE_PASSWORD",
            file_name="WAREHOUSE_SOURCE_PASSWORD_FILE",
            production=production,
        )
        hmac_material = _secret(
            "WAREHOUSE_HMAC_KEY",
            file_name="WAREHOUSE_HMAC_KEY_FILE",
            production=production,
        )

        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise _configuration_error()
        if not products_path.startswith("/") or "?" in products_path or "#" in products_path:
            raise _configuration_error()
        if require_database and not database_url:
            raise _configuration_error()
        if require_source_auth and not ((source_username and source_password) or cookie_header):
            raise _configuration_error()
        if cookie_header and (source_username or source_password):
            raise _configuration_error()
        if require_hmac_key and not hmac_material:
            raise _configuration_error()

        hmac_key = _decode_hmac_key(hmac_material) if hmac_material else None
        if production:
            if parsed.scheme != "https" or parsed.port not in {None, 443} or cookie_header:
                raise _configuration_error()
            allowed_source_hosts = _required_host_set(
                os.getenv("WAREHOUSE_ALLOWED_SOURCE_HOSTS", "")
            )
            if parsed.hostname.lower() not in allowed_source_hosts:
                raise _configuration_error()
            if not source_username or not source_password or not hmac_key:
                raise _configuration_error()
            if not database_url or not _database_url_is_production_safe(database_url):
                raise _configuration_error()
            database = urlparse(database_url)
            allowed_database_hosts = _required_host_set(
                os.getenv("WAREHOUSE_ALLOWED_DATABASE_HOSTS", "")
            )
            if database.hostname is None or database.hostname.lower() not in allowed_database_hosts:
                raise _configuration_error()
            if database.username != WRITER_ROLE:
                raise _configuration_error()

        return cls(
            environment=environment,
            database_url=database_url,
            base_url=base_url,
            products_path=products_path,
            source_username=source_username,
            source_password=source_password,
            cookie_header=cookie_header,
            hmac_key=hmac_key,
            timeout_seconds=_positive_float("WAREHOUSE_TIMEOUT_SECONDS", 30),
            request_delay_seconds=_positive_float("WAREHOUSE_REQUEST_DELAY_SECONDS", 0.30),
            max_retries=_positive_int("WAREHOUSE_MAX_RETRIES", 3),
            max_response_bytes=_positive_int("WAREHOUSE_MAX_RESPONSE_BYTES", 4 * 1024 * 1024),
        )
