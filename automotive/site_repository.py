"""PostgreSQL access for the garage site: staff accounts, sessions, content, media, audit.

Uses the dedicated ``garage_site`` login (AUTOMOTIVE_SITE_DATABASE_URL). Public search and
chat code never receive this repository; they keep the read-only runtime credential.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

USER_COLUMNS = ("user_id, username, display_name, role, is_active, created_at, updated_at, "
                "password_changed_at, last_login_at")
SERVICE_COLUMNS = ("service_id, icon, title_ka, title_en, summary_ka, summary_en, "
                   "price_from::float8 AS price_from, position, is_published, version, "
                   "updated_at")
MEDIA_COLUMNS = ("media_id, sha256, content_type, width, height, alt_ka, alt_en, position, "
                 "is_published, created_at, octet_length(data) AS byte_size")
SERVICE_FIELDS = ("icon", "title_ka", "title_en", "summary_ka", "summary_en", "price_from",
                  "is_published")
MEDIA_FIELDS = ("alt_ka", "alt_en", "is_published")
USER_FIELDS = ("display_name", "role", "is_active")


class SiteDatabaseNotConfigured(RuntimeError):
    """The site login is missing; never fall back to another credential."""


class Conflict(RuntimeError):
    """A unique value is taken or a row changed since the caller read it."""


class LastAdmin(RuntimeError):
    """The change would leave the dashboard without an active admin."""


def _assignments(fields: dict[str, Any], allowed: tuple[str, ...]) -> tuple[sql.Composed, list]:
    names = [name for name in allowed if name in fields]
    if not names:
        raise ValueError("No editable fields were supplied")
    clause = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(name)) for name in names)
    return clause, [fields[name] for name in names]


class SiteRepository:
    """Short transactions; one connection per call, like AutomotiveRepository."""

    def __init__(self, database_url: str, statement_timeout_ms: int = 5000) -> None:
        self.database_url = database_url or ""
        self.statement_timeout_ms = statement_timeout_ms

    @property
    def configured(self) -> bool:
        return bool(self.database_url.strip())

    def _connect(self) -> psycopg.Connection:
        if not self.configured:
            raise SiteDatabaseNotConfigured("Garage site database is not configured")
        connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)
        try:
            connection.execute("SELECT set_config('statement_timeout', %s, false)",
                               (str(self.statement_timeout_ms),))
        except psycopg.Error:
            connection.close()
            raise
        return connection

    # -- accounts ----------------------------------------------------------------------

    def find_user(self, username: str) -> dict | None:
        """Includes the password hash; callers must never serialize this row."""
        with self._connect() as conn:
            return conn.execute(
                sql.SQL("SELECT {}, password_hash FROM garage.staff_users WHERE username = %s")
                .format(sql.SQL(USER_COLUMNS)), (username,)).fetchone()

    def user_with_hash(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            return conn.execute(
                sql.SQL("SELECT {}, password_hash FROM garage.staff_users WHERE user_id = %s")
                .format(sql.SQL(USER_COLUMNS)), (user_id,)).fetchone()

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            return conn.execute(
                sql.SQL("SELECT {} FROM garage.staff_users ORDER BY username")
                .format(sql.SQL(USER_COLUMNS))).fetchall()

    def create_user(self, username: str, display_name: str, role: str,
                    password_hash: str) -> dict:
        try:
            with self._connect() as conn:
                return conn.execute(
                    sql.SQL("INSERT INTO garage.staff_users "
                            "(username, display_name, role, password_hash) "
                            "VALUES (%s, %s, %s, %s) RETURNING {}")
                    .format(sql.SQL(USER_COLUMNS)),
                    (username, display_name, role, password_hash)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise Conflict("username") from None

    def update_user(self, user_id: int, fields: dict[str, Any]) -> dict | None:
        """Change display name, role or active flag; refuses to remove the last admin."""
        clause, values = _assignments(fields, USER_FIELDS)
        with self._connect() as conn:
            # Lock the active admins so two concurrent demotions cannot both pass the check.
            admins = [row["user_id"] for row in conn.execute(
                "SELECT user_id FROM garage.staff_users "
                "WHERE role = 'admin' AND is_active ORDER BY user_id FOR UPDATE")]
            loses_admin = (fields.get("role", "admin") != "admin"
                           or fields.get("is_active", True) is False)
            if loses_admin and admins == [user_id]:
                raise LastAdmin("last active admin")
            row = conn.execute(
                sql.SQL("UPDATE garage.staff_users SET {}, updated_at = now() "
                        "WHERE user_id = %s RETURNING {}")
                .format(clause, sql.SQL(USER_COLUMNS)), (*values, user_id)).fetchone()
            if row and (fields.get("is_active") is False or "role" in fields):
                # A disabled or re-scoped account must sign in again.
                conn.execute("DELETE FROM garage.staff_sessions WHERE user_id = %s", (user_id,))
            return row

    def set_password(self, user_id: int, password_hash: str, keep_session: str | None = None,
                     ) -> bool:
        """Store a new hash and end every other session of the account."""
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE garage.staff_users SET password_hash = %s, updated_at = now(), "
                "password_changed_at = now() WHERE user_id = %s", (password_hash, user_id),
            ).rowcount
            conn.execute(
                "DELETE FROM garage.staff_sessions WHERE user_id = %s "
                "AND (%s::text IS NULL OR token_hash <> %s)", (user_id, keep_session, keep_session))
            return bool(changed)

    # -- sessions ----------------------------------------------------------------------

    def create_session(self, token_hash: str, user_id: int, lifetime_seconds: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO garage.staff_sessions (token_hash, user_id, expires_at) "
                "VALUES (%s, %s, now() + make_interval(secs => %s))",
                (token_hash, user_id, lifetime_seconds))
            conn.execute("UPDATE garage.staff_users SET last_login_at = now() WHERE user_id = %s",
                         (user_id,))
            conn.execute("DELETE FROM garage.staff_sessions WHERE expires_at < now()")

    def resolve_session(self, token_hash: str, idle_seconds: int) -> dict | None:
        """The active account behind a session digest; slides the idle window."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE garage.staff_sessions s SET last_seen_at = now() "
                "FROM garage.staff_users u "
                "WHERE s.token_hash = %s AND u.user_id = s.user_id AND u.is_active "
                "AND s.expires_at > now() "
                "AND s.last_seen_at > now() - make_interval(secs => %s) "
                "RETURNING u.user_id, u.username, u.display_name, u.role",
                (token_hash, idle_seconds)).fetchone()

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM garage.staff_sessions WHERE token_hash = %s", (token_hash,))

    def delete_user_sessions(self, user_id: int) -> int:
        with self._connect() as conn:
            return conn.execute("DELETE FROM garage.staff_sessions WHERE user_id = %s",
                                (user_id,)).rowcount

    # -- content -----------------------------------------------------------------------

    def load_content(self) -> dict:
        """Everything the landing page renders, in one connection."""
        with self._connect() as conn:
            sections = {row["section_key"]: row for row in conn.execute(
                "SELECT section_key, document, version, updated_at FROM garage.site_sections")}
            services = conn.execute(
                sql.SQL("SELECT {} FROM garage.services ORDER BY position, service_id")
                .format(sql.SQL(SERVICE_COLUMNS))).fetchall()
            media = conn.execute(
                sql.SQL("SELECT {} FROM garage.media ORDER BY position, media_id")
                .format(sql.SQL(MEDIA_COLUMNS))).fetchall()
        return {"sections": sections, "services": services, "media": media}

    def save_section(self, key: str, document: dict, expected_version: int,
                     actor_id: int | None) -> int:
        """Insert or replace one section; ``expected_version`` 0 means "not stored yet"."""
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO garage.site_sections AS s (section_key, document, updated_by) "
                "VALUES (%s, %s, %s) ON CONFLICT (section_key) DO UPDATE SET "
                "document = EXCLUDED.document, version = s.version + 1, updated_at = now(), "
                "updated_by = EXCLUDED.updated_by WHERE s.version = %s RETURNING version",
                (key, Jsonb(document), actor_id, expected_version)).fetchone()
        if row is None or (expected_version == 0 and row["version"] != 1):
            raise Conflict("version")
        return row["version"]

    def create_service(self, fields: dict[str, Any]) -> dict:
        names = [name for name in SERVICE_FIELDS if name in fields]
        with self._connect() as conn:
            position = conn.execute(
                "SELECT coalesce(max(position), 0) + 1 AS next FROM garage.services",
            ).fetchone()["next"]
            return conn.execute(
                sql.SQL("INSERT INTO garage.services ({}, position) VALUES ({}, %s) RETURNING {}")
                .format(sql.SQL(", ").join(map(sql.Identifier, names)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in names),
                        sql.SQL(SERVICE_COLUMNS)),
                (*[fields[name] for name in names], position)).fetchone()

    def update_service(self, service_id: int, fields: dict[str, Any],
                       expected_version: int) -> dict | None:
        """None when the row is gone; Conflict when someone else saved first."""
        clause, values = _assignments(fields, SERVICE_FIELDS)
        with self._connect() as conn:
            row = conn.execute(
                sql.SQL("UPDATE garage.services SET {}, version = version + 1, "
                        "updated_at = now() WHERE service_id = %s AND version = %s RETURNING {}")
                .format(clause, sql.SQL(SERVICE_COLUMNS)),
                (*values, service_id, expected_version)).fetchone()
            if row is None and conn.execute(
                    "SELECT 1 FROM garage.services WHERE service_id = %s",
                    (service_id,)).fetchone():
                raise Conflict("version")
            return row

    def delete_service(self, service_id: int) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("DELETE FROM garage.services WHERE service_id = %s",
                                     (service_id,)).rowcount)

    def reorder(self, table: str, identifiers: list[int]) -> None:
        """Set positions from the given id order; ids not listed keep their place."""
        target, key = {"services": ("services", "service_id"),
                       "media": ("media", "media_id")}[table]
        with self._connect() as conn:
            conn.execute(
                sql.SQL("UPDATE garage.{} AS t SET position = o.position "
                        "FROM unnest(%s::bigint[]) WITH ORDINALITY AS o(id, position) "
                        "WHERE t.{} = o.id").format(sql.Identifier(target), sql.Identifier(key)),
                (identifiers,))

    # -- media -------------------------------------------------------------------------

    def add_media(self, *, sha256: str, content_type: str, data: bytes, width: int,
                  height: int, alt_ka: str = "", alt_en: str = "", published: bool = False,
                  actor_id: int | None = None) -> dict:
        """Store an image once per content hash; a repeat upload returns the existing row."""
        with self._connect() as conn:
            position = conn.execute(
                "SELECT coalesce(max(position), 0) + 1 AS next FROM garage.media",
            ).fetchone()["next"]
            conn.execute(
                "INSERT INTO garage.media (sha256, content_type, data, width, height, alt_ka, "
                "alt_en, position, is_published, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING",
                (sha256, content_type, data, width, height, alt_ka, alt_en, position,
                 published, actor_id))
            return conn.execute(
                sql.SQL("SELECT {} FROM garage.media WHERE sha256 = %s")
                .format(sql.SQL(MEDIA_COLUMNS)), (sha256,)).fetchone()

    def update_media(self, media_id: int, fields: dict[str, Any]) -> dict | None:
        clause, values = _assignments(fields, MEDIA_FIELDS)
        with self._connect() as conn:
            return conn.execute(
                sql.SQL("UPDATE garage.media SET {} WHERE media_id = %s RETURNING {}")
                .format(clause, sql.SQL(MEDIA_COLUMNS)), (*values, media_id)).fetchone()

    def delete_media(self, media_id: int) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("DELETE FROM garage.media WHERE media_id = %s",
                                     (media_id,)).rowcount)

    def media_bytes(self, sha256: str, published_only: bool = True) -> dict | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT content_type, data FROM garage.media WHERE sha256 = %s "
                "AND (is_published OR NOT %s)", (sha256, published_only)).fetchone()

    # -- audit -------------------------------------------------------------------------

    def audit(self, actor: dict | None, action: str, target: str = "",
              detail: dict | None = None, client_ip: str | None = None) -> None:
        actor = actor or {}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO garage.audit_log (actor_id, actor_username, action, target, "
                "detail, client_ip) VALUES (%s, %s, %s, %s, %s, %s::inet)",
                (actor.get("user_id"), str(actor.get("username") or "")[:32], action,
                 target[:120], Jsonb(detail or {}), client_ip))

    def audit_rows(self, limit: int = 50, before: int | None = None) -> list[dict]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT audit_id, occurred_at, actor_username, action, target, detail "
                "FROM garage.audit_log WHERE (%s::bigint IS NULL OR audit_id < %s) "
                "ORDER BY audit_id DESC LIMIT %s",
                (before, before, max(1, min(limit, 200)))).fetchall()

    # -- reports (read-only; tolerate a missing or changed trade module) ------------------

    def sync_runs(self, limit: int = 14) -> list[dict]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT run_id, status, pages_fetched, rows_seen, rows_added, rows_changed, "
                "started_at, finished_at FROM warehouse.warehouse_sync_runs "
                "ORDER BY run_id DESC LIMIT %s", (max(1, min(limit, 60)),)).fetchall()

    def trade_daily(self, days: int = 30) -> dict | None:
        """Inferred units sold and received per Tbilisi day; None when trade is unreadable."""
        query = ("SELECT (period_end AT TIME ZONE 'Asia/Tbilisi')::date AS day, "
                 "sum(quantity)::float8 AS units{} FROM trade.{} "
                 "WHERE period_end >= now() - make_interval(days => %s) GROUP BY 1 ORDER BY 1")
        try:
            with self._connect() as conn:
                sales = conn.execute(
                    sql.SQL(query).format(sql.SQL(", sum(amount)::float8 AS amount"),
                                          sql.Identifier("sales")), (days,)).fetchall()
                purchases = conn.execute(
                    sql.SQL(query).format(sql.SQL(""), sql.Identifier("purchases")),
                    (days,)).fetchall()
                first = conn.execute(
                    "SELECT least((SELECT min(period_end) FROM trade.sales), "
                    "(SELECT min(period_end) FROM trade.purchases)) AS since").fetchone()
        except psycopg.Error:
            return None
        return {"sales": sales, "purchases": purchases, "since": first["since"]}

    def best_sellers(self, days: int = 30, limit: int = 15) -> list[dict] | None:
        try:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT source_product_id, sum(quantity)::float8 AS units, "
                    "sum(amount)::float8 AS amount "
                    "FROM trade.sales WHERE period_end >= now() - make_interval(days => %s) "
                    "GROUP BY 1 ORDER BY units DESC, source_product_id LIMIT %s",
                    (days, max(1, min(limit, 50)))).fetchall()
        except psycopg.Error:
            return None

    # -- first-run seed ----------------------------------------------------------------

    def seed(self, sections: dict[str, dict], services: list[dict]) -> dict[str, int]:
        """Store packaged defaults once: only into tables that are still empty."""
        added = {"sections": 0, "services": 0}
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM garage.site_sections LIMIT 1").fetchone():
                for key, document in sections.items():
                    conn.execute(
                        "INSERT INTO garage.site_sections (section_key, document) "
                        "VALUES (%s, %s)", (key, Jsonb(document)))
                    added["sections"] += 1
            if not conn.execute("SELECT 1 FROM garage.services LIMIT 1").fetchone():
                for position, service in enumerate(services, start=1):
                    conn.execute(
                        "INSERT INTO garage.services (icon, title_ka, title_en, summary_ka, "
                        "summary_en, position) VALUES (%s, %s, %s, %s, %s, %s)",
                        (service["icon"], service["title_ka"], service["title_en"],
                         service["summary_ka"], service["summary_en"], position))
                    added["services"] += 1
        return added
