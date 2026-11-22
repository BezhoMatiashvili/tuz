"""Server-only automotive configuration; never reuse the sync writer credential."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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


def _site_settings() -> dict:
    """Garage site settings. A public origin must be https, and the staff login cannot be
    switched off once any public origin is configured."""
    origins = tuple(dict.fromkeys(
        item.strip().rstrip("/").lower()
        for item in os.getenv("AUTOMOTIVE_PUBLIC_ORIGINS", "").split(",") if item.strip()))
    hosts = []
    for origin in origins:
        parts = urlsplit(origin)
        loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parts.scheme not in {"http", "https"} or not parts.hostname or "*" in origin
                or parts.path or parts.query or parts.fragment or parts.username
                or (parts.scheme != "https" and not loopback)):
            raise ValueError(
                "AUTOMOTIVE_PUBLIC_ORIGINS must list https origins such as https://example.ge.")
        hosts.append(parts.hostname)
    # Fail closed: only an explicit off value disables the staff login; a typo such as
    # "on " or an empty value must never open the staff API.
    staff_auth = os.getenv("AUTOMOTIVE_STAFF_AUTH", "true").strip().lower() not in {
        "0", "false", "no", "off"}
    if origins and not staff_auth:
        raise ValueError("AUTOMOTIVE_STAFF_AUTH cannot be off while public origins are set.")
    proxies = tuple(item.strip()
                    for item in os.getenv("AUTOMOTIVE_TRUSTED_PROXIES", "").split(",")
                    if item.strip())
    for proxy in proxies:
        try:
            network = ipaddress.ip_network(proxy, strict=False)
        except ValueError:
            raise ValueError(
                "AUTOMOTIVE_TRUSTED_PROXIES must list IP addresses or networks.") from None
        # Whoever is trusted here may state any client address, so it names the proxy
        # itself or its own range, never a large part of the internet.
        if network.prefixlen < (8 if network.version == 4 else 32):
            raise ValueError("AUTOMOTIVE_TRUSTED_PROXIES must name the proxy, not a range "
                             "wider than /8 (IPv4) or /32 (IPv6).")
    if not proxies and any(host not in {"localhost", "127.0.0.1", "::1"} for host in hosts):
        # A public origin is https, so a proxy sits in front. Unless it is named here every
        # visitor appears to come from the proxy's address and shares one rate limit.
        raise ValueError(
            "AUTOMOTIVE_TRUSTED_PROXIES must name the HTTPS proxy when "
            "AUTOMOTIVE_PUBLIC_ORIGINS is set.")
    limits = {}
    for key, env, default, maximum in (
            ("site_chat_daily_turns", "AUTOMOTIVE_SITE_CHAT_DAILY_TURNS", 300, 100_000),
            ("site_vin_daily_decodes", "AUTOMOTIVE_SITE_VIN_DAILY_DECODES", 200, 100_000),
            ("site_chat_log_retention_days", "AUTOMOTIVE_SITE_CHAT_LOG_RETENTION_DAYS", 30, 3650)):
        raw = os.getenv(env, str(default)).strip()
        if not raw.isdigit() or int(raw) > maximum:
            raise ValueError(f"{env} must be a whole number up to {maximum}.")
        limits[key] = int(raw)
    return {
        "public_origins": origins, "trusted_proxies": proxies, "staff_auth": staff_auth,
        "allowed_hosts": ("localhost", "127.0.0.1", *dict.fromkeys(hosts)),
        "allowed_origins": ("http://localhost:8767", "http://127.0.0.1:8767", *origins),
        "site_database_url": os.getenv("AUTOMOTIVE_SITE_DATABASE_URL", ""),
        "site_chat_log_path": os.getenv("AUTOMOTIVE_SITE_CHAT_LOG_PATH",
                                        "data/chat_logs/site_chat_log.sqlite3"),
        **limits,
    }


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = field(default="", repr=False)
    enrichment_database_url: str = field(default="", repr=False)
    operator_token: str = field(default="", repr=False)
    vin_hmac_key: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)
    openrouter_api_key: str = field(default="", repr=False)
    anthropic_workspace_id: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    catalog_api_key: str = field(default="", repr=False)
    catalog_api_base_url: str = "https://auto-parts-catalog.p.rapidapi.com"
    catalog_api_host: str = "auto-parts-catalog.p.rapidapi.com"
    catalog_api_key_header: str = "x-rapidapi-key"
    motorium_enabled: bool = False
    fuchs_enabled: bool = True
    shell_enabled: bool = True
    pakelo_enabled: bool = True
    jsfilter_enabled: bool = True
    # Live Brembo lookups for one car per brake question; never a bulk download.
    brembo_enabled: bool = True
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
    # Web research for part numbers takes 20-40 s, so a turn may run this long.
    chat_turn_seconds: float = 75
    # Direct construction retains the legacy path for integrations and rollback tests.
    chat_orchestration: bool = False
    # Which conversation engine answers: assistant (the model works through read tools),
    # orchestrated or scripted. Empty keeps the chat_orchestration flag above, so direct
    # construction selects the engine it always did; from_env defaults to assistant.
    chat_engine: str = ""
    # The assistant engine. An empty model means anthropic_model; an empty effort leaves the
    # API default. The limits bound one turn.
    assistant_model: str = ""
    assistant_effort: str = "low"
    # "off" asks the model not to think before it answers (its lowest setting where it
    # cannot be switched off); empty leaves the model's own default.
    assistant_thinking: str = ""
    assistant_turn_seconds: float = 30
    assistant_max_requests: int = 4
    assistant_max_tool_calls: int = 10
    # How long the provider keeps the fixed part of the prompt (tools, instructions, guide)
    # ready: 5m or 1h. An hour costs more to write and spares the first message after a
    # quiet spell the wait and the price of writing it again.
    assistant_cache_ttl: str = "1h"
    # The file that holds what the catalogue's part names mean, read by a model from the
    # rows that carry them (python -m automotive --read-part-names) and printed in the
    # guide the assistant reads. Empty or missing: the guide lists the names alone.
    assistant_names_path: str = ""
    # Web research inside a turn (assistant_research): off, staff (the staff chat only) or
    # all (the public widget too, once the provider path has been checked). It runs beside
    # the catalogue search, waits at most AUTOMOTIVE_RESEARCH_SECONDS, and fills its cache.
    assistant_research: str = "staff"
    assistant_research_model: str = "claude-haiku-5-5"
    assistant_research_seconds: float = 8.0
    assistant_research_cache_path: str = "data/assistant/research_cache.sqlite3"
    # What the catalogue's vehicle texts mean, read once by a model
    # (python -m automotive.assistant_vehicles) and shown beside search results. Empty or
    # missing: the results carry the texts alone.
    assistant_vehicle_readings_path: str = ""
    # The same for the catalogue's part names (python -m automotive.assistant_vehicles
    # --names): what each part is and the positions its marks state.
    assistant_name_readings_path: str = ""
    automotive_cache_path: str = "data/automotive-cache.sqlite3"
    knowledge_enabled: bool = False
    knowledge_validator_database_url: str = field(default="", repr=False)
    knowledge_judge_model: str = "claude-sonnet-5-5"
    knowledge_daily_attempts: int = 25
    knowledge_concurrency: int = 1
    knowledge_max_searches: int = 5
    knowledge_max_fetches: int = 5
    knowledge_deadline_seconds: int = 120
    knowledge_freshness_days: int = 180
    # Garage site (automotive/site*.py). Direct construction keeps the legacy API open for
    # injected-settings tests; from_env turns the staff login on.
    public_origins: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = ()
    staff_auth: bool = False
    site_database_url: str = field(default="", repr=False)
    site_chat_daily_turns: int = 300
    site_vin_daily_decodes: int = 200
    site_chat_log_path: str = ""
    site_chat_log_retention_days: int = 30

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(override=False)
        settings = cls(
            database_url=os.getenv("AUTOMOTIVE_DATABASE_URL", ""),
            enrichment_database_url=os.getenv("AUTOMOTIVE_ENRICHMENT_DATABASE_URL", ""),
            operator_token=os.getenv("AUTOMOTIVE_OPERATOR_TOKEN", ""),
            vin_hmac_key=os.getenv("AUTOMOTIVE_VIN_HMAC_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            anthropic_workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"),
            catalog_api_key=os.getenv("CATALOG_API_KEY", ""),
            catalog_api_base_url=os.getenv(
                "CATALOG_API_BASE_URL", "https://auto-parts-catalog.p.rapidapi.com"),
            catalog_api_host=os.getenv("CATALOG_API_HOST", "auto-parts-catalog.p.rapidapi.com"),
            catalog_api_key_header=os.getenv("CATALOG_API_KEY_HEADER", "x-rapidapi-key"),
            motorium_enabled=_flag("MOTORIUM_ENABLED", "false"),
            fuchs_enabled=_flag("FUCHS_ENABLED"),
            shell_enabled=_flag("SHELL_ENABLED"),
            pakelo_enabled=_flag("PAKELO_ENABLED"),
            jsfilter_enabled=_flag("JSFILTER_ENABLED"),
            brembo_enabled=_flag("BREMBO_ENABLED"),
            chat_log_path=os.getenv("AUTOMOTIVE_CHAT_LOG_PATH", "data/chat_logs/chat_log.sqlite3"),
            chat_log_retention_days=_days("AUTOMOTIVE_CHAT_LOG_RETENTION_DAYS"),
            chat_rephrase=_flag("AUTOMOTIVE_CHAT_REPHRASE", "false"),
            chat_rephrase_seconds=_seconds("AUTOMOTIVE_CHAT_REPHRASE_SECONDS", 1.2),
            chat_turn_seconds=float(os.getenv("AUTOMOTIVE_CHAT_TURN_SECONDS", "75")),
            chat_orchestration=_flag("AUTOMOTIVE_CHAT_ORCHESTRATION"),
            chat_engine=os.getenv("AUTOMOTIVE_CHAT_ENGINE", "assistant").strip().lower(),
            # The model the test conversations chose: it finds more of the right rows and
            # offers fewer wrong ones than the smaller model, in less time per turn. An
            # empty value falls back to ANTHROPIC_MODEL.
            assistant_model=(os.getenv("AUTOMOTIVE_ASSISTANT_MODEL", "claude-sonnet-5-5").strip()
                             or os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")),
            assistant_effort=os.getenv("AUTOMOTIVE_ASSISTANT_EFFORT", "low").strip().lower(),
            assistant_thinking=os.getenv("AUTOMOTIVE_ASSISTANT_THINKING", "").strip().lower(),
            assistant_turn_seconds=float(os.getenv("AUTOMOTIVE_ASSISTANT_TURN_SECONDS", "30")),
            assistant_max_requests=int(os.getenv("AUTOMOTIVE_ASSISTANT_MAX_REQUESTS", "4")),
            assistant_max_tool_calls=int(os.getenv("AUTOMOTIVE_ASSISTANT_MAX_TOOL_CALLS", "10")),
            assistant_cache_ttl=os.getenv("AUTOMOTIVE_ASSISTANT_CACHE_TTL", "1h").strip().lower(),
            assistant_names_path=os.getenv(
                "AUTOMOTIVE_ASSISTANT_NAMES_PATH", "data/assistant/part_names.json").strip(),
            assistant_research=os.getenv("AUTOMOTIVE_RESEARCH", "staff").strip().lower(),
            assistant_research_model=os.getenv(
                "AUTOMOTIVE_RESEARCH_MODEL", "claude-haiku-5-5").strip(),
            assistant_research_seconds=float(os.getenv("AUTOMOTIVE_RESEARCH_SECONDS", "8")),
            assistant_research_cache_path=os.getenv(
                "AUTOMOTIVE_RESEARCH_CACHE_PATH", "data/assistant/research_cache.sqlite3").strip(),
            assistant_vehicle_readings_path=os.getenv(
                "AUTOMOTIVE_VEHICLE_READINGS_PATH", "data/assistant/vehicle_readings.json").strip(),
            assistant_name_readings_path=os.getenv(
                "AUTOMOTIVE_NAME_READINGS_PATH", "data/assistant/name_readings.json").strip(),
            automotive_cache_path=os.getenv(
                "AUTOMOTIVE_CACHE_PATH", "data/automotive-cache.sqlite3"),
            knowledge_enabled=_flag("AUTOMOTIVE_KNOWLEDGE_ENABLED", "false"),
            knowledge_validator_database_url=os.getenv("AUTOMOTIVE_VALIDATOR_DATABASE_URL", ""),
            knowledge_judge_model=os.getenv("AUTOMOTIVE_JUDGE_MODEL", "claude-sonnet-5-5"),
            **{key: int(os.getenv(env, str(default))) for key, env, default in (
                ("knowledge_daily_attempts", "AUTOMOTIVE_KNOWLEDGE_DAILY_ATTEMPTS", 25),
                ("knowledge_concurrency", "AUTOMOTIVE_KNOWLEDGE_CONCURRENCY", 1),
                ("knowledge_max_searches", "AUTOMOTIVE_KNOWLEDGE_MAX_SEARCHES", 5),
                ("knowledge_max_fetches", "AUTOMOTIVE_KNOWLEDGE_MAX_FETCHES", 5),
                ("knowledge_deadline_seconds", "AUTOMOTIVE_KNOWLEDGE_DEADLINE_SECONDS", 120),
                ("knowledge_freshness_days", "AUTOMOTIVE_KNOWLEDGE_FRESHNESS_DAYS", 180),
            )},
            **_site_settings(),
        )
        if settings.site_database_url and settings.site_database_url in {
                settings.database_url, settings.enrichment_database_url,
                settings.knowledge_validator_database_url}:
            raise ValueError("Use a separate garage site credential.")
        if settings.catalog_api_key and not settings.catalog_api_base_url.startswith("https://"):
            raise ValueError("The catalog API base URL must use HTTPS.")
        if settings.vin_hmac_key and len(settings.vin_hmac_key.encode()) < 32:
            raise ValueError("Automotive VIN key must contain at least 32 bytes.")
        if settings.operator_token and len(settings.operator_token) < 32:
            raise ValueError("Automotive operator token must contain at least 32 characters.")
        if (settings.database_url and settings.enrichment_database_url
                and settings.database_url == settings.enrichment_database_url):
            raise ValueError("Use separate automotive runtime and enrichment credentials.")
        if not 10 <= settings.chat_turn_seconds <= 180:
            raise ValueError("AUTOMOTIVE_CHAT_TURN_SECONDS must be between 10 and 180 seconds.")
        if settings.chat_engine not in {"", "assistant", "orchestrated", "scripted"}:
            raise ValueError(
                "AUTOMOTIVE_CHAT_ENGINE must be assistant, orchestrated or scripted.")
        if settings.assistant_effort not in {"", "low", "medium", "high"}:
            raise ValueError("AUTOMOTIVE_ASSISTANT_EFFORT must be low, medium, high or empty.")
        if settings.assistant_thinking not in {"", "off"}:
            raise ValueError("AUTOMOTIVE_ASSISTANT_THINKING must be off or empty.")
        if not 10 <= settings.assistant_turn_seconds <= 120:
            raise ValueError(
                "AUTOMOTIVE_ASSISTANT_TURN_SECONDS must be between 10 and 120 seconds.")
        if not 1 <= settings.assistant_max_requests <= 8:
            raise ValueError("AUTOMOTIVE_ASSISTANT_MAX_REQUESTS must be between 1 and 8.")
        if not 1 <= settings.assistant_max_tool_calls <= 20:
            raise ValueError("AUTOMOTIVE_ASSISTANT_MAX_TOOL_CALLS must be between 1 and 20.")
        if settings.assistant_cache_ttl not in {"5m", "1h"}:
            raise ValueError("AUTOMOTIVE_ASSISTANT_CACHE_TTL must be 5m or 1h.")
        if settings.assistant_research not in {"off", "staff", "all"}:
            raise ValueError("AUTOMOTIVE_RESEARCH must be off, staff or all.")
        if not 1 <= settings.assistant_research_seconds <= 30:
            raise ValueError("AUTOMOTIVE_RESEARCH_SECONDS must be between 1 and 30.")
        validator = settings.knowledge_validator_database_url
        if validator and validator in {settings.database_url, settings.enrichment_database_url}:
            raise ValueError("Use a separate automotive validator credential.")
        for key, maximum in (("knowledge_daily_attempts", 25), ("knowledge_concurrency", 4),
                             ("knowledge_max_searches", 5), ("knowledge_max_fetches", 5),
                             ("knowledge_deadline_seconds", 120),
                             ("knowledge_freshness_days", 3650)):
            if not 1 <= getattr(settings, key) <= maximum:
                raise ValueError(f"{key} must be between 1 and {maximum}.")
        return settings
