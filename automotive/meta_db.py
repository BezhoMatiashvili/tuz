"""PostgreSQL connections for the Garage Facebook integration.

One short connection per call, like ``SiteRepository``. Each process passes its own login:
the receiver ``MESSENGER_RECEIVER_DATABASE_URL`` (member of ``garage_webhook``), the worker
``MESSENGER_DATABASE_URL`` (``garage_messenger``), the dashboard ``AUTOMOTIVE_SITE_DATABASE_URL``
(``garage_site``). A missing URL raises; nothing ever falls back to another credential.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


class MetaDatabaseNotConfigured(RuntimeError):
    """The login this process needs is not configured."""


class MetaDatabase:
    """Connections with a statement timeout and a name that shows in pg_stat_activity."""

    def __init__(self, database_url: str, *, statement_timeout_ms: int = 5000,
                 application_name: str = "garage-meta") -> None:
        self.database_url = (database_url or "").strip()
        self.statement_timeout_ms = int(statement_timeout_ms)
        self.application_name = application_name

    @property
    def configured(self) -> bool:
        return bool(self.database_url)

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection:
        if not self.configured:
            raise MetaDatabaseNotConfigured("The Facebook integration's database login is not "
                                            "configured")
        connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5,
                                     autocommit=autocommit)
        try:
            connection.execute("SELECT set_config('statement_timeout', %s, false), "
                               "set_config('application_name', %s, false)",
                               (str(self.statement_timeout_ms), self.application_name))
        except psycopg.Error:
            connection.close()
            raise
        return connection

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """One connection, one transaction: committed on success, rolled back on error."""
        with self.connect() as connection:
            with connection.transaction():
                yield connection
