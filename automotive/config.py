"""Server-only automotive configuration; never reuse the sync writer credential."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


def _seconds(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number of seconds.") from None
    if not 0.2 <= value <= 10:
        raise ValueError(f"{name} must be between 0.2 and 10 seconds.")
    return value


def _days(name: str) -> int:
    raw = os.getenv(name, "0").strip() or "0"
    if not raw.isdigit():
        raise ValueError(f"{name} must be a whole number of days.")
    return int(raw)


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = field(default="", repr=False)
    enrichment_database_url: str = field(default="", repr=False)
    operator_token: str = field(default="", repr=False)
    vin_hmac_key: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)
    anthropic_workspace_id: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    catalog_api_key: str = field(default="", repr=False)
    catalog_api_base_url: str = "https://auto-parts-catalog.p.rapidapi.com"
    catalog_api_host: str = "auto-parts-catalog.p.rapidapi.com"
    catalog_api_key_header: str = "x-rapidapi-key"
    motorium_enabled: bool = True
    fuchs_enabled: bool = True
    shell_enabled: bool = True
    pakelo_enabled: bool = True
    jsfilter_enabled: bool = True
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    allowed_origins: tuple[str, ...] = ("http://localhost:8767", "http://127.0.0.1:8767")
    rate_limit_per_minute: int = 60
    expensive_rate_limit_per_minute: int = 10
    # Empty path disables the local chat log; from_env enables it by default.
    chat_log_path: str = ""
    chat_log_retention_days: int = 0
    # Let the model reword result replies; a fact checker falls back to the backend sentence.
    chat_rephrase: bool = False
    chat_rephrase_seconds: float = 1.2

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(override=False)
        settings = cls(
            database_url=os.getenv("AUTOMOTIVE_DATABASE_URL", ""),
            enrichment_database_url=os.getenv("AUTOMOTIVE_ENRICHMENT_DATABASE_URL", ""),
            operator_token=os.getenv("AUTOMOTIVE_OPERATOR_TOKEN", ""),
            vin_hmac_key=os.getenv("AUTOMOTIVE_VIN_HMAC_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"),
            catalog_api_key=os.getenv("CATALOG_API_KEY", ""),
            catalog_api_base_url=os.getenv(
                "CATALOG_API_BASE_URL", "https://auto-parts-catalog.p.rapidapi.com"),
            catalog_api_host=os.getenv("CATALOG_API_HOST", "auto-parts-catalog.p.rapidapi.com"),
            catalog_api_key_header=os.getenv("CATALOG_API_KEY_HEADER", "x-rapidapi-key"),
            motorium_enabled=_flag("MOTORIUM_ENABLED"),
            fuchs_enabled=_flag("FUCHS_ENABLED"),
            shell_enabled=_flag("SHELL_ENABLED"),
            pakelo_enabled=_flag("PAKELO_ENABLED"),
            jsfilter_enabled=_flag("JSFILTER_ENABLED"),
            chat_log_path=os.getenv("AUTOMOTIVE_CHAT_LOG_PATH", "data/chat_logs/chat_log.sqlite3"),
            chat_log_retention_days=_days("AUTOMOTIVE_CHAT_LOG_RETENTION_DAYS"),
            chat_rephrase=_flag("AUTOMOTIVE_CHAT_REPHRASE", "false"),
            chat_rephrase_seconds=_seconds("AUTOMOTIVE_CHAT_REPHRASE_SECONDS", 1.2),
        )
        if settings.catalog_api_key and not settings.catalog_api_base_url.startswith("https://"):
            raise ValueError("The catalog API base URL must use HTTPS.")
        if settings.vin_hmac_key and len(settings.vin_hmac_key.encode()) < 32:
            raise ValueError("Automotive VIN key must contain at least 32 bytes.")
        if settings.operator_token and len(settings.operator_token) < 32:
            raise ValueError("Automotive operator token must contain at least 32 characters.")
        if (settings.database_url and settings.enrichment_database_url
                and settings.database_url == settings.enrichment_database_url):
            raise ValueError("Use separate automotive runtime and enrichment credentials.")
        return settings
