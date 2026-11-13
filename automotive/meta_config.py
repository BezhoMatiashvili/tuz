"""Settings for the Garage Facebook integration: Messenger bot, comment replies, page stats, ads.

Read from the environment (and the private ``.env``) by the receiver, the worker, the dashboard
and ``messenger_setup``. Kept apart from ``automotive.config.Settings`` on purpose: nothing here
reaches the chat engine's settings or its logs, and a missing Meta block never stops the website.

Secret fields carry ``repr=False``; never log or serialize a ``MetaSettings``. Errors name the
variable, never its value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
GRAPH_HOST = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v26.0"
WEBHOOK_PATH = "/meta/webhook"
# Webhook fields the page is subscribed to (app level and page level alike).
SUBSCRIBED_FIELDS = ("messages", "message_echoes", "messaging_postbacks",
                     "messaging_referrals", "feed")
# Permissions the owner ticks in Graph API Explorer before pasting the token.
REQUIRED_SCOPES = ("pages_show_list", "pages_messaging", "pages_manage_metadata",
                   "pages_read_engagement", "pages_read_user_content",
                   "pages_manage_engagement", "read_insights", "ads_read")

_DIGITS = re.compile(r"^[0-9]{1,30}$")
_SECRET = re.compile(r"^[0-9A-Za-z]{16,128}$")
_VERIFY = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_KEY = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION = re.compile(r"^v[0-9]{2}\.[0-9]$")


class MetaConfigError(ValueError):
    """A Meta setting is malformed. The message names the variable only."""


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise MetaConfigError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise MetaConfigError(f"{name} must be between {low:g} and {high:g}")
    return value


def _matching(env: Mapping[str, str], name: str, pattern: re.Pattern, what: str) -> str:
    value = (env.get(name) or "").strip()
    if value and not pattern.match(value):
        raise MetaConfigError(f"{name} must be {what}")
    return value


def _public_url(env: Mapping[str, str]) -> str:
    value = (env.get("MESSENGER_PUBLIC_URL") or "").strip().rstrip("/")
    if not value:
        return ""
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment
            or parts.username or parts.password):
        raise MetaConfigError("MESSENGER_PUBLIC_URL must be an https:// address without "
                              "credentials, query or fragment")
    return value


@dataclass(frozen=True)
class MetaSettings:
    """Everything the Facebook integration reads from the environment."""

    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    verify_token: str = field(default="", repr=False)
    token_key: bytes = field(default=b"", repr=False)
    graph_version: str = DEFAULT_GRAPH_VERSION
    # Optional pin: when the pasted token can reach several pages, connect this one.
    page_id: str = ""
    public_url: str = ""
    receiver_host: str = "127.0.0.1"
    receiver_port: int = 8771
    receiver_database_url: str = field(default="", repr=False)
    database_url: str = field(default="", repr=False)
    data_dir: str = "data/messenger"
    quiet_seconds: float = 5.0
    max_wait_seconds: float = 45.0
    concurrent_turns: int = 3
    send_timeout_seconds: float = 20.0

    @property
    def graph_base(self) -> str:
        return f"{GRAPH_HOST}/{self.graph_version}/"

    @property
    def webhook_url(self) -> str:
        return f"{self.public_url}{WEBHOOK_PATH}" if self.public_url else ""

    def missing_for_receiver(self) -> list[str]:
        """Variable names the public receiver cannot start without."""
        return [name for name, value in (
            ("META_APP_SECRET", self.app_secret),
            ("META_WEBHOOK_VERIFY_TOKEN", self.verify_token),
            ("MESSENGER_RECEIVER_DATABASE_URL", self.receiver_database_url),
        ) if not value]

    def missing_for_worker(self) -> list[str]:
        return [name for name, value in (
            ("META_APP_ID", self.app_id),
            ("META_APP_SECRET", self.app_secret),
            ("META_TOKEN_ENCRYPTION_KEY", self.token_key),
            ("MESSENGER_DATABASE_URL", self.database_url),
        ) if not value]

    def missing_for_dashboard(self) -> list[str]:
        """What the dashboard's Connect step needs (the site database login is separate)."""
        return [name for name, value in (
            ("META_APP_ID", self.app_id),
            ("META_APP_SECRET", self.app_secret),
            ("META_TOKEN_ENCRYPTION_KEY", self.token_key),
            ("META_WEBHOOK_VERIFY_TOKEN", self.verify_token),
            ("MESSENGER_PUBLIC_URL", self.public_url),
        ) if not value]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MetaSettings:
        """Read and validate. ``env`` replaces the process environment (tests)."""
        if env is None:
            load_dotenv(ROOT / ".env", override=False)
            env = os.environ
        key_hex = _matching(env, "META_TOKEN_ENCRYPTION_KEY", _KEY, "64 hexadecimal characters")
        version = _matching(env, "META_GRAPH_API_VERSION", _VERSION, "like v26.0")
        return cls(
            app_id=_matching(env, "META_APP_ID", _DIGITS, "the numeric App ID"),
            app_secret=_matching(env, "META_APP_SECRET", _SECRET, "the App Secret"),
            verify_token=_matching(env, "META_WEBHOOK_VERIFY_TOKEN", _VERIFY,
                                   "32-256 letters, digits, '-' or '_'"),
            token_key=bytes.fromhex(key_hex) if key_hex else b"",
            graph_version=version or DEFAULT_GRAPH_VERSION,
            page_id=_matching(env, "META_PAGE_ID", _DIGITS, "the numeric Page ID"),
            public_url=_public_url(env),
            receiver_port=int(_number(env, "MESSENGER_RECEIVER_PORT", 8771, 1024, 65535)),
            receiver_database_url=(env.get("MESSENGER_RECEIVER_DATABASE_URL") or "").strip(),
            database_url=(env.get("MESSENGER_DATABASE_URL") or "").strip(),
            data_dir=(env.get("MESSENGER_DATA_DIR") or "").strip() or "data/messenger",
            quiet_seconds=_number(env, "MESSENGER_QUIET_SECONDS", 5.0, 1.0, 60.0),
            max_wait_seconds=_number(env, "MESSENGER_MAX_WAIT_SECONDS", 45.0, 5.0, 300.0),
            concurrent_turns=int(_number(env, "MESSENGER_CONCURRENT_TURNS", 3, 1, 8)),
            send_timeout_seconds=_number(env, "MESSENGER_SEND_TIMEOUT_SECONDS", 20.0, 5.0, 60.0),
        )
