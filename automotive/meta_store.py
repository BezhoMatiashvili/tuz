"""SQL for the Garage Facebook integration: switches, connection, inbox, chats, replies, jobs.

One class serves the two logins that reach these tables (migration 0013), and every method names
the login it needs. The privileges are the fence; this module only stays inside them:

- the worker, ``MESSENGER_DATABASE_URL`` (``garage_messenger``), consumes the inbox, stores chats
  and reply attempts, records job runs and appends audit rows (INSERT only, so never RETURNING);
- the dashboard and ``messenger_setup``, ``AUTOMOTIVE_SITE_DATABASE_URL`` (``garage_site``), own
  the connection and the switches, read chats, and see only the inbox's bookkeeping columns.

Access tokens are stored AES-256-GCM sealed (``meta_crypto``) and are decrypted only by
``token()``; nothing else returns them. Audit rows never carry message text or tokens, and name a
chat by the last four digits of its PSID. "Today" is the Asia/Tbilisi day.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from automotive.meta_crypto import PURPOSES, TokenCipher, TokenCipherError
from automotive.meta_db import MetaDatabase

ACTION = re.compile(r"^[a-z][a-z0-9_.]{1,47}$")
_DIGITS = re.compile(r"^[0-9]{1,30}$")
_JOB = re.compile(r"^[a-z_]{1,40}$")
_CONVERSATION = re.compile(r"^t_[0-9A-Za-z_-]{1,120}$")
# Midnight of the current Tbilisi day, as a timestamptz.
TODAY = sql.SQL("(date_trunc('day', now() AT TIME ZONE 'Asia/Tbilisi') "
                "AT TIME ZONE 'Asia/Tbilisi')")
# The privacy page promises webhook copies are gone within 7 days (meta_receiver.SHOP); the purge
# runs once a day, so a copy can wait a day past this cut.
INBOX_RETENTION_DAYS = 6
# meta_comments.BACKFILL_DAYS (30) and a day: after that, no comment a forgotten row stops can be
# read again (the backfill keeps 30 days, a webhook comment over 7 days old is never stored).
FORGOTTEN_DAYS = 31
MAX_DETAIL_BYTES = 4000
MAX_ENGINE_STATE_BYTES = 262144
CHANNELS = frozenset({"messenger", "comment", "command", "notice"})
SENDERS = frozenset({"customer", "bot", "shop"})
SOURCES = frozenset({"", "ads", "shortlink", "comment", "other"})
REPLY_STATUSES = frozenset({
    "pending", "sent", "skipped_off", "skipped_chat_off", "skipped_old",
    "skipped_shop_answered", "skipped_superseded", "skipped_cap", "skipped_paused", "failed",
    "blocked_by_meta", "fallback_sent", "paused_provider", "abandoned"})
# Reasons an attempt can stop before the engine runs. The caps count engine runs, which the
# worker marks by setting engine_status (a skip after the engine ran still counts), and the
# attempts still open or left open by a stopped worker (UNFINISHED), whose engine may have run.
PRE_ENGINE_SKIPS = ("skipped_off", "skipped_chat_off", "skipped_old", "skipped_shop_answered",
                    "skipped_cap", "skipped_paused")
UNFINISHED = ("pending", "abandoned")
RUN_STATUSES = frozenset({"ok", "error", "skipped"})
THREAD_FILTERS = {"all": sql.SQL("TRUE"), "attention": sql.SQL("t.needs_attention"),
                  "off": sql.SQL("NOT t.bot_enabled"), "admin": sql.SQL("t.is_admin"),
                  "ads": sql.SQL("t.source = 'ads'")}
# Editable through update_settings; the switches have their own audited methods.
# retention_days governs reply attempts, job runs and requests and the alarms, never the
# messages (kept for good). The dashboard accepts 30-365; the column's own CHECK allows 3650.
SETTINGS_FIELDS = {"max_reply_age_minutes": (int, 5, 1440), "daily_turn_limit": (int, 0, 5000),
                   "customer_daily_turn_limit": (int, 0, 500),
                   "comment_public_reply": (str, 0, 300), "fallback_ka": (str, 0, 500),
                   "fallback_en": (str, 0, 500), "retention_days": (int, 30, 365)}
REPLY_FIELDS = frozenset({"error_code", "error_subcode", "error_text", "engine_status",
                          "sent_mids", "usage", "cost_usd", "duration_ms"})

THREAD_COLUMNS = ("psid", "page_id", "customer_name", "conversation_id", "bot_enabled",
                  "is_admin", "source", "source_ad_id", "first_seen_at", "last_customer_at",
                  "last_shop_at", "last_bot_at", "needs_attention", "attention_reason",
                  "engine_state_at", "updated_at", "version")


def _columns(*names: str, prefix: str = "") -> sql.Composed:
    return sql.SQL(", ").join(
        sql.Identifier(prefix, name) if prefix else sql.Identifier(name) for name in names)


THREADS = _columns(*THREAD_COLUMNS)
MESSAGE_NAMES = ("message_id", "mid", "psid", "sender", "body", "attachments", "quick_reply",
                 "referral_ad_id", "app_id", "sent_at", "stored_at")
MESSAGES = _columns(*MESSAGE_NAMES)
SEALED_ROW_INFO = _columns("purpose", "app_id", "subject_id", "subject_name", "scopes",
                           "expires_at", "data_access_expires_at", "status", "status_detail",
                           "checked_at", "connected_at", "connected_by")
PAGE = _columns("page_id", "page_name", "app_id", "subscribed_fields", "subscribed_at",
                "connected_at", "connected_by", "version")
REPLIES = sql.SQL("reply_id, channel, psid, trigger_key, trigger_keys, status, error_code, "
                  "error_subcode, error_text, engine_status, sent_mids, usage, "
                  "cost_usd::float8 AS cost_usd, started_at, finished_at, duration_ms")
# The only inbox columns the dashboard login may read.
EVENTS = _columns("inbox_id", "field", "received_at", "processed_at", "attempts", "outcome")
RUNS = _columns("run_id", "job", "started_at", "finished_at", "status", "records", "detail")


class MetaConflict(RuntimeError):
    """The row changed since the caller read it (stale version)."""


class AutoReplyNotConfirmed(ValueError):
    """Turning the bot on needs the admin's word that Meta's own "Auto reply" is off."""


class InvalidFields(ValueError):
    """Field names mapped to a code (UNKNOWN, INVALID, EMPTY), like the dashboard's 422s."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("Invalid fields: " + ", ".join(sorted(fields)))
        self.fields = fields


def mask_psid(psid: str | None) -> str:
    """How audit rows, logs and the CLI name a customer: the last four digits only."""
    return "…" + str(psid)[-4:] if psid else ""


def bot_state(settings: Mapping[str, Any]) -> str:
    """'off', 'paused' (on, but stopped by the AI provider until resumed) or 'on'."""
    if not settings.get("bot_enabled"):
        return "off"
    return "paused" if settings.get("bot_paused_reason") else "on"


def _actor(actor: Any) -> tuple[int | None, str]:
    """(user id, name) for the audit row and the *_changed_by stamps.

    The dashboard passes its signed-in user (``user_id``, ``username``); processes pass a label
    such as ``"cli"`` or ``"worker"``.
    """
    if isinstance(actor, Mapping):
        user_id = actor.get("user_id")
        return (user_id if isinstance(user_id, int) and not isinstance(user_id, bool) else None,
                str(actor.get("username") or "")[:32])
    if isinstance(actor, str):
        return None, actor.strip()[:32]
    return None, ""


def _address(client_ip: str | None) -> str | None:
    if not client_ip:
        return None
    try:
        return str(ipaddress.ip_network(client_ip, strict=False))
    except ValueError:
        return None


def _detail(detail: Mapping[str, Any] | None) -> dict:
    value = dict(detail or {})
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > MAX_DETAIL_BYTES:
        return {"truncated": True}
    return json.loads(encoded)


def _digits(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _DIGITS.match(value):
        raise ValueError(f"{what} must be a numeric id")
    return value


def _when(value: Any, what: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{what} must be a timezone-aware datetime")
    return value


def _sent_at(value: Any) -> datetime:
    """Meta's timestamps are epoch milliseconds; a datetime must carry its timezone."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, UTC)
    when = _when(value, "sent_at")
    if when is None:
        raise ValueError("sent_at is required")
    return when


def _scopes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError("scopes must be a list of names")
    names = [str(item)[:80] for item in value]
    return sorted(set(names))[:100]


def _limit(value: int, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


def _attachments(value: Any) -> list[dict]:
    """Attachment types only ([{"type": "image"}]); never URLs or payloads."""
    kinds: list[dict] = []
    for item in value or ():
        kind = item.get("type") if isinstance(item, Mapping) else item
        if isinstance(kind, str) and kind:
            kinds.append({"type": kind[:30]})
    return kinds[:20]


def _outcome(prefix: str, text: Any) -> str:
    clean = str(text or "").strip()
    return f"{prefix}{clean}"[:300] if prefix else (clean or "done")[:300]


# -- forgotten customers (migration 0014) ------------------------------------------------------


def forgotten_key(subject: str) -> str:
    """How garage.meta_forgotten names an erased author, comment or Messenger chat
    ('author:<id>', 'comment:<id>', 'chat:<psid>'): its SHA-256, so the table never holds the id
    itself. No key is needed for the two logins to agree, and the rows are dropped after
    FORGOTTEN_DAYS (longer than the catch-up job ever reads back: meta_catchup.FIRST_DAYS)."""
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def _forgetting(conn: Any) -> bool:
    """Whether migration 0014 is in place and this login may delete comments. Before it is,
    a deletion leaves comments alone (and reports None) instead of failing."""
    row = conn.execute("SELECT to_regclass('garage.meta_forgotten') IS NOT NULL AS ready, "
                       "has_table_privilege('garage.meta_comments', 'DELETE') AS allowed"
                       ).fetchone()
    return bool(row["ready"] and row["allowed"])


class AlertsNotDeletable(RuntimeError):
    """Migration 0015 is applied but this login may not delete alarms (provisioning was not
    rerun). A deletion that skipped them would claim a customer's phone number was erased."""


def _alerts_deletable(conn: Any) -> bool:
    """Whether migration 0015 is in place, so alarms are deleted with a customer's data and by
    retention. False before it is (nothing fails). Raises ``AlertsNotDeletable`` when the table
    exists and this login may not delete from it: failing is better than a silent skip (CASE:
    ``has_table_privilege`` raises for a table that does not exist, and AND does not promise to
    stop early)."""
    row = conn.execute(
        "SELECT to_regclass('garage.live_alerts') IS NOT NULL AS there, "
        "CASE WHEN to_regclass('garage.live_alerts') IS NULL THEN FALSE "
        "ELSE has_table_privilege('garage.live_alerts', 'DELETE') END AS allowed").fetchone()
    if row["there"] and not row["allowed"]:
        raise AlertsNotDeletable("this login may not delete from garage.live_alerts")
    return bool(row["there"])


def forgotten_since(conn: Any, subjects: Iterable[str]) -> dict[str, datetime]:
    """When each of ``subjects`` ('author:<id>', 'comment:<id>') was forgotten, for those that
    were; {} before migration 0014. Either login, inside the caller's transaction."""
    wanted = {forgotten_key(subject): subject for subject in subjects}
    if not wanted or conn.execute("SELECT to_regclass('garage.meta_forgotten') IS NULL "
                                  "AS missing").fetchone()["missing"]:
        return {}
    rows = conn.execute("SELECT subject_key, forgotten_at FROM garage.meta_forgotten "
                        "WHERE subject_key = ANY(%s::text[])", (list(wanted),)).fetchall()
    return {wanted[row["subject_key"]]: row["forgotten_at"] for row in rows}


def forget_comments(conn: Any, psid: str) -> int | None:
    """Delete the stored comments of the customer whose chat is ``psid``: those answered into
    that chat and every other comment of the same author, inside the caller's transaction.
    Their author and comment ids are recorded (hashed) first, so the comment backfill and a
    late webhook never store them again (``meta_comments.CommentStore.save_many``); comments
    the person writes afterwards are stored as any other. None before migration 0014."""
    _digits(psid, "psid")
    if not _forgetting(conn):
        return None
    rows = conn.execute(
        "SELECT comment_id, author_id FROM garage.meta_comments WHERE replied_psid = %(psid)s "
        "OR author_id IN (SELECT author_id FROM garage.meta_comments "
        "WHERE replied_psid = %(psid)s AND author_id <> '')", {"psid": psid}).fetchall()
    if not rows:
        return 0
    subjects = {f"comment:{row['comment_id']}" for row in rows}
    subjects.update(f"author:{row['author_id']}" for row in rows if row["author_id"])
    conn.execute("INSERT INTO garage.meta_forgotten (subject_key) "
                 "SELECT unnest(%s::text[]) ON CONFLICT (subject_key) "
                 "DO UPDATE SET forgotten_at = now()",
                 (sorted(forgotten_key(subject) for subject in subjects),))
    return conn.execute("DELETE FROM garage.meta_comments WHERE comment_id = ANY(%s::text[])",
                        ([row["comment_id"] for row in rows],)).rowcount


class MetaStore:
    """Short transactions, one connection per call (``MetaDatabase``)."""

    def __init__(self, db: MetaDatabase, cipher: TokenCipher | None = None) -> None:
        self.db = db
        self.cipher = cipher

    # -- audit (both logins; the worker may only insert) ---------------------------------------

    def audit(self, actor: Any, action: str, target: str = "",
              detail: Mapping[str, Any] | None = None, *, client_ip: str | None = None,
              connection: Any = None) -> None:
        """Append to garage.audit_log. ``detail`` holds codes and counts, never text or tokens."""
        if not isinstance(action, str) or not ACTION.match(action):
            raise ValueError("Audit actions look like 'messenger.bot_on'")
        actor_id, name = _actor(actor)
        statement = ("INSERT INTO garage.audit_log (actor_id, actor_username, action, target, "
                     "detail, client_ip) VALUES (%s, %s, %s, %s, %s, %s::inet)")
        values = (actor_id, name, action, str(target or "")[:120], Jsonb(_detail(detail)),
                  _address(client_ip))
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.db.transaction() as conn:
            conn.execute(statement, values)

    # -- settings and switches (both logins) ----------------------------------------------------

    def settings(self) -> dict:
        """The one settings row, read fresh (switches are re-read before every send)."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM garage.meta_settings WHERE settings_id = 1").fetchone()
        if row is None:
            raise RuntimeError("garage.meta_settings has no row; migration 0013 seeds it")
        return row

    def update_settings(self, fields: Mapping[str, Any], actor: Any,
                        expected_version: int | None = None, *,
                        client_ip: str | None = None) -> dict:
        """Change limits and texts (SETTINGS_FIELDS). A stale ``expected_version`` raises
        MetaConflict; bad values raise InvalidFields before anything is written."""
        clean: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name, value in dict(fields).items():
            rule = SETTINGS_FIELDS.get(name)
            if rule is None:
                errors[name] = "UNKNOWN"
                continue
            kind, low, high = rule
            if kind is int:
                if (isinstance(value, bool) or not isinstance(value, int)
                        or not low <= value <= high):
                    errors[name] = "INVALID"
                    continue
            elif not isinstance(value, str) or len(value.strip()) > high:
                errors[name] = "INVALID"
                continue
            else:
                value = value.strip()
            clean[name] = value
        if errors:
            raise InvalidFields(errors)
        if not clean:
            raise InvalidFields({"fields": "EMPTY"})
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(name)) for name in clean)
        with self.db.transaction() as conn:
            row = conn.execute(
                sql.SQL("UPDATE garage.meta_settings SET {}, version = version + 1, "
                        "updated_at = now() WHERE settings_id = 1 "
                        "AND (%s::int IS NULL OR version = %s::int) RETURNING *")
                .format(assignments),
                [*clean.values(), expected_version, expected_version]).fetchone()
            if row is None:
                raise MetaConflict("settings")
            self.audit(actor, "messenger.settings_changed", "settings",
                       {"fields": sorted(clean), "version": row["version"]},
                       client_ip=client_ip, connection=conn)
        return row

    def set_bot(self, enabled: bool, actor: Any, auto_reply_confirmed: bool | None = None, *,
                client_ip: str | None = None) -> dict:
        """The admin's global switch.

        ON requires ``auto_reply_confirmed_off``: already stored, or passed true now. Passing
        false with ON refuses and leaves everything as it was; passing false with OFF stores
        it. ``bot_enabled_at`` is stamped only when the switch goes
        from off to on, so customers who wrote before that are never bulk-answered. Either way
        a provider pause is cleared and the change is audited.
        """
        _, name = _actor(actor)
        with self.db.transaction() as conn:
            current = conn.execute("SELECT * FROM garage.meta_settings WHERE settings_id = 1 "
                                   "FOR UPDATE").fetchone()
            confirmed = (current["auto_reply_confirmed_off"] if auto_reply_confirmed is None
                         else bool(auto_reply_confirmed))
            if enabled and not confirmed:
                raise AutoReplyNotConfirmed("Confirm that Meta's Auto reply is off first")
            row = conn.execute(
                "UPDATE garage.meta_settings SET bot_enabled = %(on)s, "
                "bot_enabled_at = CASE WHEN %(on)s AND NOT bot_enabled THEN now() "
                "ELSE bot_enabled_at END, bot_changed_at = now(), bot_changed_by = %(by)s, "
                "bot_paused_reason = '', bot_paused_at = NULL, "
                "auto_reply_confirmed_off = %(confirmed)s, version = version + 1, "
                "updated_at = now() WHERE settings_id = 1 RETURNING *",
                {"on": bool(enabled), "by": name, "confirmed": confirmed}).fetchone()
            self.audit(actor, "messenger.bot_on" if enabled else "messenger.bot_off", "settings",
                       {"changed": bool(enabled) != current["bot_enabled"],
                        "was_paused": bool(current["bot_paused_reason"]),
                        "auto_reply_confirmed_off": confirmed},
                       client_ip=client_ip, connection=conn)
        return row

    def resume_bot(self, actor: Any, *, client_ip: str | None = None) -> dict:
        """Clear a provider pause (the switch itself is left as it is). No-op when not paused."""
        with self.db.transaction() as conn:
            current = conn.execute("SELECT * FROM garage.meta_settings WHERE settings_id = 1 "
                                   "FOR UPDATE").fetchone()
            if not current["bot_paused_reason"]:
                return current
            row = conn.execute(
                "UPDATE garage.meta_settings SET bot_paused_reason = '', bot_paused_at = NULL, "
                "version = version + 1, updated_at = now() WHERE settings_id = 1 "
                "RETURNING *").fetchone()
            self.audit(actor, "messenger.bot_resumed", "settings",
                       {"reason": current["bot_paused_reason"]}, client_ip=client_ip,
                       connection=conn)
        return row

    def pause_for_provider(self, reason: str = "ai_provider") -> bool:
        """The worker stops the bot when the AI account fails; True when this call paused it."""
        if reason != "ai_provider":
            raise ValueError("The only pause reason is ai_provider")
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE garage.meta_settings SET bot_paused_reason = %s, bot_paused_at = now(), "
                "version = version + 1, updated_at = now() "
                "WHERE settings_id = 1 AND bot_paused_reason = '' RETURNING settings_id",
                (reason,)).fetchone()
            if row is not None:
                self.audit("worker", "messenger.bot_paused", "settings", {"reason": reason},
                           connection=conn)
        return row is not None

    def set_comments(self, enabled: bool, actor: Any, *, client_ip: str | None = None) -> dict:
        """The comment-reply switch; ``comments_enabled_at`` is stamped on off->on only."""
        _, name = _actor(actor)
        with self.db.transaction() as conn:
            current = conn.execute("SELECT comments_enabled FROM garage.meta_settings "
                                   "WHERE settings_id = 1 FOR UPDATE").fetchone()
            row = conn.execute(
                "UPDATE garage.meta_settings SET comments_enabled = %(on)s, "
                "comments_enabled_at = CASE WHEN %(on)s AND NOT comments_enabled THEN now() "
                "ELSE comments_enabled_at END, comments_changed_at = now(), "
                "comments_changed_by = %(by)s, version = version + 1, updated_at = now() "
                "WHERE settings_id = 1 RETURNING *", {"on": bool(enabled), "by": name}).fetchone()
            self.audit(actor, "messenger.comments_on" if enabled else "messenger.comments_off",
                       "settings", {"changed": bool(enabled) != current["comments_enabled"]},
                       client_ip=client_ip, connection=conn)
        return row

    # -- connection (save_connection and disconnect: dashboard login only) -----------------------

    def page(self) -> dict | None:
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL("SELECT {} FROM garage.meta_page WHERE page_row = 1")
                                .format(PAGE)).fetchone()

    def save_connection(self, page_id: str, page_name: str, app_id: str,
                        tokens: Mapping[str, Mapping[str, Any]], actor: Any, *,
                        client_ip: str | None = None) -> dict:
        """Store the page and its sealed tokens in one transaction (dashboard login).

        ``tokens`` maps a purpose (page, user, send_page, ads_user) to ``token``, ``app_id``
        (defaults to ``app_id``), ``subject_id``, ``subject_name``, ``scopes``, ``expires_at``
        and ``data_access_expires_at`` (aware datetimes or None for "never"). Purposes not passed
        are kept, unless the page itself changed: tokens for the old page are then deleted.
        Returns the page row; no token is ever returned.
        """
        if self.cipher is None:
            raise TokenCipherError("META_TOKEN_ENCRYPTION_KEY is not configured")
        _digits(page_id, "page_id")
        _digits(app_id, "app_id")
        if not tokens:
            raise ValueError("At least one token is required")
        sealed = []
        for purpose, entry in tokens.items():
            if purpose not in PURPOSES:
                raise ValueError("Unknown token purpose")
            sealed.append({
                "purpose": purpose,
                "app_id": _digits(str(entry.get("app_id") or app_id), "token app_id"),
                "subject_id": _digits(str(entry.get("subject_id") or ""), "subject_id"),
                "subject_name": str(entry.get("subject_name") or "")[:200],
                "token_enc": self.cipher.encrypt(entry.get("token"), purpose=purpose),
                "scopes": _scopes(entry.get("scopes")),
                "expires_at": _when(entry.get("expires_at"), "expires_at"),
                "data_access_expires_at": _when(entry.get("data_access_expires_at"),
                                                "data_access_expires_at")})
        _, name = _actor(actor)
        with self.db.transaction() as conn:
            previous = conn.execute("SELECT page_id FROM garage.meta_page WHERE page_row = 1 "
                                    "FOR UPDATE").fetchone()
            page_changed = previous is not None and previous["page_id"] != page_id
            # The subscription state survives a reconnect of the same page through the same app.
            row = conn.execute(sql.SQL(
                "INSERT INTO garage.meta_page (page_row, page_id, page_name, app_id, "
                "connected_at, connected_by) VALUES (1, %(page)s, %(name)s, %(app)s, now(), "
                "%(by)s) ON CONFLICT (page_row) DO UPDATE SET page_id = EXCLUDED.page_id, "
                "page_name = EXCLUDED.page_name, app_id = EXCLUDED.app_id, "
                "subscribed_fields = CASE WHEN garage.meta_page.page_id = EXCLUDED.page_id "
                "AND garage.meta_page.app_id = EXCLUDED.app_id "
                "THEN garage.meta_page.subscribed_fields ELSE '{{}}'::text[] END, "
                "subscribed_at = CASE WHEN garage.meta_page.page_id = EXCLUDED.page_id "
                "AND garage.meta_page.app_id = EXCLUDED.app_id "
                "THEN garage.meta_page.subscribed_at ELSE NULL END, "
                "connected_at = now(), connected_by = EXCLUDED.connected_by, "
                "version = garage.meta_page.version + 1 RETURNING {}").format(PAGE),
                {"page": page_id, "name": str(page_name or "")[:200], "app": app_id,
                 "by": name}).fetchone()
            if page_changed:
                conn.execute("DELETE FROM garage.meta_tokens WHERE NOT (purpose = ANY(%s::text[]))",
                             ([entry["purpose"] for entry in sealed],))
            for entry in sealed:
                conn.execute(
                    "INSERT INTO garage.meta_tokens (purpose, app_id, subject_id, subject_name, "
                    "token_enc, scopes, expires_at, data_access_expires_at, status, "
                    "status_detail, checked_at, connected_at, connected_by) VALUES "
                    "(%(purpose)s, %(app_id)s, %(subject_id)s, %(subject_name)s, %(token_enc)s, "
                    "%(scopes)s::text[], %(expires_at)s, %(data_access_expires_at)s, 'ok', '', "
                    "now(), now(), %(by)s) ON CONFLICT (purpose) DO UPDATE SET "
                    "app_id = EXCLUDED.app_id, subject_id = EXCLUDED.subject_id, "
                    "subject_name = EXCLUDED.subject_name, token_enc = EXCLUDED.token_enc, "
                    "scopes = EXCLUDED.scopes, expires_at = EXCLUDED.expires_at, "
                    "data_access_expires_at = EXCLUDED.data_access_expires_at, status = 'ok', "
                    "status_detail = '', checked_at = now(), connected_at = now(), "
                    "connected_by = EXCLUDED.connected_by", {**entry, "by": name})
            self.audit(actor, "messenger.connected", page_id,
                       {"purposes": sorted(entry["purpose"] for entry in sealed),
                        "page_changed": page_changed}, client_ip=client_ip, connection=conn)
        return row

    def token(self, purpose: str) -> str | None:
        """The decrypted token for ``purpose``, or None when none is stored."""
        if purpose not in PURPOSES:
            raise ValueError("Unknown token purpose")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT token_enc FROM garage.meta_tokens WHERE purpose = %s",
                               (purpose,)).fetchone()
        if row is None:
            return None
        if self.cipher is None:
            raise TokenCipherError("META_TOKEN_ENCRYPTION_KEY is not configured")
        return self.cipher.decrypt(row["token_enc"], purpose=purpose)

    def token_rows(self) -> list[dict]:
        """Every stored token's metadata, without the sealed value."""
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL("SELECT {} FROM garage.meta_tokens ORDER BY purpose")
                                .format(SEALED_ROW_INFO)).fetchall()

    def mark_token(self, purpose: str, status: str, detail: str = "", *,
                   expiry: Mapping[str, Any] | None = None) -> bool:
        """Record a health check. ``expiry`` (scopes, expires_at, data_access_expires_at)
        replaces the stored values when Meta reported them."""
        if purpose not in PURPOSES or status not in ("ok", "broken"):
            raise ValueError("Unknown token purpose or status")
        values: dict[str, Any] = {"purpose": purpose, "status": status,
                                  "detail": str(detail or "")[:300]}
        extra = sql.SQL("")
        if expiry is not None:
            extra = sql.SQL(", scopes = %(scopes)s::text[], expires_at = %(expires_at)s, "
                            "data_access_expires_at = %(data_access_expires_at)s")
            values.update(scopes=_scopes(expiry.get("scopes")),
                          expires_at=_when(expiry.get("expires_at"), "expires_at"),
                          data_access_expires_at=_when(expiry.get("data_access_expires_at"),
                                                       "data_access_expires_at"))
        with self.db.transaction() as conn:
            row = conn.execute(sql.SQL(
                "UPDATE garage.meta_tokens SET status = %(status)s, status_detail = %(detail)s, "
                "checked_at = now(){} WHERE purpose = %(purpose)s RETURNING purpose")
                .format(extra), values).fetchone()
        return row is not None

    def set_subscription(self, fields: Sequence[str]) -> bool:
        """Record the webhook fields the page is subscribed to (empty: none)."""
        names = [str(name)[:40] for name in fields]
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE garage.meta_page SET subscribed_fields = %(fields)s::text[], "
                "subscribed_at = CASE WHEN cardinality(%(fields)s::text[]) > 0 THEN now() "
                "ELSE NULL END WHERE page_row = 1 RETURNING page_row",
                {"fields": names}).fetchone()
        return row is not None

    def disconnect(self, actor: Any, *, client_ip: str | None = None) -> dict:
        """Delete every stored token and the page row (dashboard login), audited."""
        with self.db.transaction() as conn:
            page = conn.execute("SELECT page_id FROM garage.meta_page WHERE page_row = 1 "
                                "FOR UPDATE").fetchone()
            purposes = sorted(row["purpose"] for row in conn.execute(
                "DELETE FROM garage.meta_tokens RETURNING purpose").fetchall())
            conn.execute("DELETE FROM garage.meta_page")
            page_id = page["page_id"] if page else ""
            self.audit(actor, "messenger.disconnected", page_id, {"tokens": purposes},
                       client_ip=client_ip, connection=conn)
        return {"page_id": page_id, "tokens_removed": purposes}

    # -- inbox ---------------------------------------------------------------------------------

    def claim_inbox(self, limit: int = 20, *, lease_seconds: int = 300,
                    max_attempts: int = 5) -> list[dict]:
        """Take up to ``limit`` pending webhook items, oldest first (worker login).

        A claimed row is leased, not locked: ``processed_at`` holds the claim time and
        ``outcome`` is 'claimed' until finish_inbox or fail_inbox, so the next poll (or a second
        worker) skips it. A lease older than ``lease_seconds`` (the worker died mid-item) is put
        back as 'retry: lease_expired', or ends as 'failed: lease_expired' after
        ``max_attempts`` claims. Rows: inbox_id, event_key, field, page_id, payload,
        received_at, attempts (this claim included).
        """
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE garage.meta_inbox SET "
                "processed_at = CASE WHEN attempts >= %(max)s THEN now() ELSE NULL END, "
                "outcome = CASE WHEN attempts >= %(max)s THEN 'failed: lease_expired' "
                "ELSE 'retry: lease_expired' END "
                "WHERE outcome = 'claimed' AND processed_at IS NOT NULL "
                "AND processed_at < now() - make_interval(secs => %(lease)s)",
                {"max": int(max_attempts), "lease": int(lease_seconds)})
            rows = conn.execute(
                "WITH picked AS (SELECT inbox_id FROM garage.meta_inbox "
                "WHERE processed_at IS NULL ORDER BY inbox_id LIMIT %s FOR UPDATE SKIP LOCKED) "
                "UPDATE garage.meta_inbox AS inbox SET processed_at = now(), outcome = 'claimed', "
                "attempts = inbox.attempts + 1 FROM picked WHERE inbox.inbox_id = picked.inbox_id "
                "RETURNING inbox.inbox_id, inbox.event_key, inbox.field, inbox.page_id, "
                "inbox.payload, inbox.received_at, inbox.attempts",
                (_limit(limit, 20, 500),)).fetchall()
        return sorted(rows, key=lambda row: row["inbox_id"])

    def finish_inbox(self, inbox_id: int, outcome: str = "done") -> bool:
        """Mark a claimed item done. ``outcome`` is a short code, never message text."""
        text = _outcome("", outcome)
        if text == "claimed":
            text = "done"
        with self.db.transaction() as conn:
            row = conn.execute("UPDATE garage.meta_inbox SET processed_at = now(), outcome = %s "
                               "WHERE inbox_id = %s RETURNING inbox_id",
                               (text, int(inbox_id))).fetchone()
        return row is not None

    def fail_inbox(self, inbox_id: int, error: str, max_attempts: int = 5) -> bool:
        """Put a claimed item back for another try ('retry: <error>'); after ``max_attempts``
        claims it ends as 'failed: <error>'. True while it will be retried."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE garage.meta_inbox SET "
                "processed_at = CASE WHEN attempts >= %(max)s THEN now() ELSE NULL END, "
                "outcome = CASE WHEN attempts >= %(max)s THEN %(final)s ELSE %(retry)s END "
                "WHERE inbox_id = %(id)s RETURNING processed_at IS NULL AS retrying",
                {"max": int(max_attempts), "final": _outcome("failed: ", error),
                 "retry": _outcome("retry: ", error), "id": int(inbox_id)}).fetchone()
        return bool(row and row["retrying"])

    def recent_events(self, limit: int = 20) -> list[dict]:
        """Newest webhook items, bookkeeping columns only (both logins)."""
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL("SELECT {} FROM garage.meta_inbox "
                                        "ORDER BY inbox_id DESC LIMIT %s").format(EVENTS),
                                (_limit(limit, 20, 200),)).fetchall()

    def last_event_at(self) -> dict | None:
        """The newest webhook's received_at and field, for the status card (both logins)."""
        with self.db.transaction() as conn:
            return conn.execute("SELECT received_at, field FROM garage.meta_inbox "
                                "ORDER BY inbox_id DESC LIMIT 1").fetchone()

    # -- threads ---------------------------------------------------------------------------------

    def upsert_thread(self, psid: str, page_id: str, *, customer_name: str | None = None,
                      conversation_id: str | None = None, source: str | None = None,
                      source_ad_id: str | None = None) -> dict:
        """Create a chat or update the given fields; None leaves a field as it is (worker)."""
        _digits(psid, "psid")
        _digits(page_id, "page_id")
        if source is not None and source not in SOURCES:
            raise ValueError("Unknown thread source")
        if source_ad_id not in (None, "") and not _DIGITS.match(str(source_ad_id)):
            raise ValueError("source_ad_id must be a numeric id")
        if conversation_id not in (None, "") and not _CONVERSATION.match(str(conversation_id)):
            raise ValueError("conversation_id is malformed")
        values = {"psid": psid, "page": page_id,
                  "name": None if customer_name is None else str(customer_name)[:200],
                  "conversation": conversation_id, "source": source, "ad": source_ad_id}
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL(
                "INSERT INTO garage.messenger_threads (psid, page_id, customer_name, "
                "conversation_id, source, source_ad_id) VALUES (%(psid)s, %(page)s, "
                "COALESCE(%(name)s::text, ''), COALESCE(%(conversation)s::text, ''), "
                "COALESCE(%(source)s::text, ''), COALESCE(%(ad)s::text, '')) "
                "ON CONFLICT (psid) DO UPDATE SET page_id = EXCLUDED.page_id, "
                "customer_name = COALESCE(%(name)s::text, garage.messenger_threads.customer_name), "
                "conversation_id = COALESCE(%(conversation)s::text, "
                "garage.messenger_threads.conversation_id), "
                "source = COALESCE(%(source)s::text, garage.messenger_threads.source), "
                "source_ad_id = COALESCE(%(ad)s::text, garage.messenger_threads.source_ad_id), "
                "updated_at = now() RETURNING {}").format(THREADS), values).fetchone()

    def get_thread(self, psid: str, *, with_state: bool = False) -> dict | None:
        """One chat; ``engine_state`` only when asked (it is the engine's, not the browser's)."""
        columns = sql.SQL(", ").join([THREADS, sql.Identifier("engine_state")]) if with_state \
            else THREADS
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL("SELECT {} FROM garage.messenger_threads "
                                        "WHERE psid = %s").format(columns), (psid,)).fetchone()

    thread = get_thread

    def list_threads(self, filter: str = "all", limit: int = 50, offset: int = 0) -> list[dict]:
        """Chats newest first with their latest message (sender, first 120 characters, time).

        ``filter``: all, attention, off (chat switch off), admin, ads.
        """
        if filter not in THREAD_FILTERS:
            raise ValueError("Unknown thread filter")
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL(
                "SELECT {}, last.sender AS last_sender, left(last.body, 120) AS last_body, "
                "last.sent_at AS last_message_at FROM garage.messenger_threads t "
                "LEFT JOIN LATERAL (SELECT sender, body, sent_at FROM garage.messenger_messages m "
                "WHERE m.psid = t.psid ORDER BY m.sent_at DESC, m.message_id DESC LIMIT 1) last "
                "ON TRUE WHERE {} "
                "ORDER BY COALESCE(t.last_customer_at, t.first_seen_at) DESC, t.psid "
                "LIMIT %s OFFSET %s").format(_columns(*THREAD_COLUMNS, prefix="t"),
                                             THREAD_FILTERS[filter]),
                (_limit(limit, 50, 200), max(0, int(offset)))).fetchall()

    threads = list_threads

    def _switch(self, column: str, psid: str, value: bool, actor: Any, action: str,
                expected_version: int | None, client_ip: str | None) -> dict | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                sql.SQL("UPDATE garage.messenger_threads SET {} = %s, updated_at = now(), "
                        "version = version + 1 WHERE psid = %s "
                        "AND (%s::int IS NULL OR version = %s::int) RETURNING {}").format(
                    sql.Identifier(column), THREADS),
                (bool(value), psid, expected_version, expected_version)).fetchone()
            if row is None:
                if expected_version is not None and conn.execute(
                        "SELECT 1 FROM garage.messenger_threads WHERE psid = %s",
                        (psid,)).fetchone():
                    raise MetaConflict("thread")
                return None
            self.audit(actor, action, mask_psid(psid), {"version": row["version"]},
                       client_ip=client_ip, connection=conn)
        return row

    def set_thread_switch(self, psid: str, enabled: bool, actor: Any, *,
                          expected_version: int | None = None,
                          client_ip: str | None = None) -> dict | None:
        """The per-chat switch (both logins); None when the chat does not exist."""
        return self._switch("bot_enabled", psid, enabled, actor,
                            "messenger.chat_on" if enabled else "messenger.chat_off",
                            expected_version, client_ip)

    def set_admin(self, psid: str, is_admin: bool, actor: Any, *,
                  expected_version: int | None = None,
                  client_ip: str | None = None) -> dict | None:
        """Mark the owner's own chat so '#bot' commands are accepted from it (both logins)."""
        return self._switch("is_admin", psid, is_admin, actor,
                            "messenger.chat_admin_on" if is_admin else "messenger.chat_admin_off",
                            expected_version, client_ip)

    def save_engine_state(self, psid: str, state: Mapping[str, Any] | None) -> bool:
        """Persist the engine's per-chat state after a turn (worker login)."""
        value = None
        if state is not None:
            if len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > MAX_ENGINE_STATE_BYTES:
                raise ValueError("The engine state is larger than 256 KiB")
            value = Jsonb(dict(state))
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE garage.messenger_threads SET engine_state = %s, engine_state_at = now(), "
                "updated_at = now() WHERE psid = %s RETURNING psid", (value, psid)).fetchone()
        return row is not None

    def flag_attention(self, psid: str, reason: str, *, needs: bool = True, actor: Any = None,
                       client_ip: str | None = None) -> bool:
        """Set (or, with ``needs=False``, clear) "needs attention". Audited when an admin
        ``actor`` is given; the worker's flags are not."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE garage.messenger_threads SET needs_attention = %s, "
                "attention_reason = %s, updated_at = now() WHERE psid = %s RETURNING psid",
                (bool(needs), str(reason or "")[:120] if needs else "", psid)).fetchone()
            if row is not None and actor is not None:
                self.audit(actor, "messenger.attention_flagged" if needs
                           else "messenger.attention_cleared", mask_psid(psid), {},
                           client_ip=client_ip, connection=conn)
        return row is not None

    def probe_candidates(self, page_id: str, *, quiet_hours: float = 25,
                         limit: int = 5) -> list[dict]:
        """Non-admin chats whose newest customer message is older than ``quiet_hours``."""
        with self.db.transaction() as conn:
            return conn.execute(
                "SELECT psid, last_customer_at FROM garage.messenger_threads "
                "WHERE page_id = %s AND NOT is_admin AND last_customer_at IS NOT NULL "
                "AND last_customer_at < now() - make_interval(secs => %s) "
                "ORDER BY last_customer_at DESC LIMIT %s",
                (page_id, float(quiet_hours) * 3600, _limit(limit, 5, 50))).fetchall()

    def apply_conversation_names(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Fill customer_name and conversation_id of chats that already exist (worker login)."""
        changed = 0
        with self.db.transaction() as conn:
            for row in rows:
                psid = str(row.get("psid") or "")
                if not _DIGITS.match(psid):
                    continue
                conversation = str(row.get("conversation_id") or "")
                conversation = conversation if _CONVERSATION.match(conversation) else None
                name = str(row.get("name") or "")[:200] or None
                changed += conn.execute(
                    "UPDATE garage.messenger_threads SET "
                    "customer_name = COALESCE(%(name)s::text, customer_name), "
                    "conversation_id = COALESCE(%(conversation)s::text, conversation_id), "
                    "updated_at = now() WHERE psid = %(psid)s AND ("
                    "customer_name IS DISTINCT FROM COALESCE(%(name)s::text, customer_name) OR "
                    "conversation_id IS DISTINCT FROM "
                    "COALESCE(%(conversation)s::text, conversation_id))",
                    {"name": name, "conversation": conversation, "psid": psid}).rowcount
        return changed

    # -- messages --------------------------------------------------------------------------------

    def add_message(self, psid: str, mid: str, sender: str, sent_at: datetime | int, *,
                    page_id: str, body: str = "", attachments: Sequence[Any] | None = None,
                    quick_reply: str = "", referral_ad_id: str = "", app_id: str = "") -> bool:
        """Store one message; False when its ``mid`` is already stored (worker login).

        Creates the chat when it is new and moves its last_customer_at / last_shop_at /
        last_bot_at forward. ``sent_at`` is Meta's time (a datetime, or epoch milliseconds).
        Attachments keep their types only.
        """
        _digits(psid, "psid")
        _digits(page_id, "page_id")
        if sender not in SENDERS:
            raise ValueError("sender must be customer, bot or shop")
        if not isinstance(mid, str) or not 1 <= len(mid) <= 200:
            raise ValueError("mid must be 1-200 characters")
        if referral_ad_id and not _DIGITS.match(referral_ad_id):
            raise ValueError("referral_ad_id must be a numeric id")
        if app_id and not _DIGITS.match(app_id):
            raise ValueError("app_id must be a numeric id")
        when = _sent_at(sent_at)
        column = {"customer": "last_customer_at", "shop": "last_shop_at", "bot": "last_bot_at"}
        with self.db.transaction() as conn:
            # A late webhook copy (the worker was stopped) of a message sent before staff
            # deleted this customer's data: as a duplicate, never stored, never answered.
            forgotten = forgotten_since(conn, [f"chat:{psid}"]).get(f"chat:{psid}")
            if forgotten is not None and when <= forgotten:
                return False
            conn.execute("INSERT INTO garage.messenger_threads (psid, page_id) VALUES (%s, %s) "
                         "ON CONFLICT (psid) DO NOTHING", (psid, page_id))
            row = conn.execute(
                "INSERT INTO garage.messenger_messages (mid, psid, sender, body, attachments, "
                "quick_reply, referral_ad_id, app_id, sent_at) VALUES (%s, %s, %s, %s, %s, %s, "
                "%s, %s, %s) ON CONFLICT (mid) DO NOTHING RETURNING message_id",
                (mid, psid, sender, str(body or "")[:4000], Jsonb(_attachments(attachments)),
                 str(quick_reply or "")[:1000], referral_ad_id or "", app_id or "",
                 when)).fetchone()
            if row is None:
                return False
            conn.execute(sql.SQL("UPDATE garage.messenger_threads SET {0} = GREATEST({0}, %s), "
                                 "updated_at = now() WHERE psid = %s")
                         .format(sql.Identifier(column[sender])), (when, psid))
        return True

    def add_caught_up(self, psid: str, page_id: str,
                      messages: Sequence[Mapping[str, Any]]) -> dict:
        """Store messages the webhook missed (``meta_catchup``), in one transaction (worker).

        Each message is {mid, sender ('customer' or 'shop'), sent_at (a datetime), body,
        attachments}. One whose mid is stored already is skipped: Graph's message id is the
        webhook's mid (all 86 stored messages, bot replies and shop echoes included, matched
        by id on 2026-11-20), and a customer or the shop sending the same words twice within
        seconds are two messages, both kept. The chat is created when it is new and its
        last_customer_at / last_shop_at move forward.

        Every customer message stored here that nothing the page wrote came after is claimed
        as handled for both reply channels (``skipped_old``, error_text "catchup"), so neither
        a burst nor the start-up recovery turns any of them into a late bot reply, whichever
        the worker would take as its trigger; the Live page lists the chat as waiting for staff
        instead. {stored, claimed: claim rows written}."""
        _digits(psid, "psid")
        _digits(page_id, "page_id")
        stored = claimed = 0
        caught: set[str] = set()  # the customer messages this call stored
        column = {"customer": "last_customer_at", "shop": "last_shop_at"}
        with self.db.transaction() as conn:
            # A customer whose data staff deleted: nothing they wrote before that comes back.
            forgotten = forgotten_since(conn, [f"chat:{psid}"]).get(f"chat:{psid}")
            if forgotten is not None:
                messages = [message for message in messages
                            if isinstance(message.get("sent_at"), datetime)
                            and message["sent_at"] > forgotten]
                if not messages:
                    return {"stored": 0, "claimed": 0}
            conn.execute("INSERT INTO garage.messenger_threads (psid, page_id) VALUES (%s, %s) "
                         "ON CONFLICT (psid) DO NOTHING", (psid, page_id))
            for message in messages:
                sender, mid = message.get("sender"), message.get("mid")
                when = message.get("sent_at")
                if (sender not in column or not isinstance(mid, str) or not 1 <= len(mid) <= 200
                        or not isinstance(when, datetime) or when.tzinfo is None):
                    continue
                body = str(message.get("body") or "")[:4000]
                row = conn.execute(
                    "INSERT INTO garage.messenger_messages (mid, psid, sender, body, attachments, "
                    "sent_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (mid) DO NOTHING "
                    "RETURNING message_id",
                    (mid, psid, sender, body, Jsonb(_attachments(message.get("attachments"))),
                     when)).fetchone()
                if row is None:
                    continue
                stored += 1
                conn.execute(sql.SQL("UPDATE garage.messenger_threads SET {0} = GREATEST({0}, %s), "
                                     "updated_at = now() WHERE psid = %s")
                             .format(sql.Identifier(column[sender])), (when, psid))
                if sender == "customer":
                    caught.add(mid)
            if caught:
                # Every customer message this call stored that nothing the page wrote came after
                # (ordered as the worker orders them: sent time, then storage order): whichever
                # of them the worker would take as a burst's trigger (the newest, either of two
                # in one second, the newest that is not the owner's command), it is claimed.
                unanswered = conn.execute(
                    "SELECT m.mid FROM garage.messenger_messages m WHERE m.psid = %(psid)s "
                    "AND m.sender = 'customer' AND m.mid = ANY(%(caught)s::text[]) "
                    "AND NOT EXISTS (SELECT 1 FROM garage.messenger_messages a "
                    "WHERE a.psid = %(psid)s AND a.sender <> 'customer' "
                    "AND (a.sent_at, a.message_id) > (m.sent_at, m.message_id)) "
                    "ORDER BY m.sent_at, m.message_id",
                    {"psid": psid, "caught": sorted(caught)}).fetchall()
                for row in unanswered:
                    for channel in ("messenger", "notice"):
                        claimed += conn.execute(
                            "INSERT INTO garage.messenger_replies (channel, psid, trigger_key, "
                            "trigger_keys, status, error_text, finished_at, duration_ms) "
                            "VALUES (%s, %s, %s, %s::text[], 'skipped_old', 'catchup', now(), 0) "
                            "ON CONFLICT (channel, trigger_key) DO NOTHING",
                            (channel, psid, row["mid"], [row["mid"]])).rowcount
        return {"stored": stored, "claimed": claimed}

    def deleted_after(self, psid: str, when: datetime | int) -> bool:
        """Whether staff deleted this customer's data at or after ``when``, Meta's time of a
        message or event that arrives late (the worker was stopped, or Meta retried): such a
        copy must not bring the chat back. Worker login."""
        _digits(psid, "psid")
        with self.db.transaction() as conn:
            forgotten = forgotten_since(conn, [f"chat:{psid}"]).get(f"chat:{psid}")
        return forgotten is not None and _sent_at(when) <= forgotten

    def known_mids(self, mids: Sequence[str]) -> set[str]:
        """Which of these message ids are stored (both logins): the catch-up's dry run."""
        wanted = [mid for mid in mids if isinstance(mid, str) and 1 <= len(mid) <= 200][:1000]
        if not wanted:
            return set()
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT mid FROM garage.messenger_messages WHERE mid = ANY(%s)",
                                (wanted,)).fetchall()
        return {row["mid"] for row in rows}

    def last_ok_run(self, job: str) -> datetime | None:
        """When the newest successful run of a job started (both logins)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT max(started_at) AS at FROM garage.meta_sync_runs "
                               "WHERE job = %s AND status = 'ok'", (job,)).fetchone()
        return row["at"] if row else None

    def messages(self, psid: str, limit: int = 50, *, before_id: int | None = None) -> list[dict]:
        """The newest ``limit`` messages of a chat, oldest first (both logins).

        ``before_id`` (the oldest message shown) pages in the order shown, by (sent_at,
        message_id): a bot reply is stored after a later customer message but stamped just
        after the one it answers, so its id alone would skip it. A ``before_id`` that is gone
        meanwhile pages by id."""
        with self.db.transaction() as conn:
            rows = conn.execute(sql.SQL(
                "WITH anchor AS (SELECT sent_at, message_id FROM garage.messenger_messages "
                "WHERE message_id = %(before)s::bigint AND psid = %(psid)s) "
                "SELECT {} FROM garage.messenger_messages m WHERE m.psid = %(psid)s "
                "AND (%(before)s::bigint IS NULL "
                "OR EXISTS (SELECT 1 FROM anchor a "
                "WHERE (m.sent_at, m.message_id) < (a.sent_at, a.message_id)) "
                "OR (NOT EXISTS (SELECT 1 FROM anchor) AND m.message_id < %(before)s::bigint)) "
                "ORDER BY m.sent_at DESC, m.message_id DESC LIMIT %(limit)s")
                .format(_columns(*MESSAGE_NAMES, prefix="m")),
                {"psid": psid, "before": before_id,
                 "limit": _limit(limit, 50, 500)}).fetchall()
        return rows[::-1]

    def unanswered_customer_messages(self, psid: str, *,
                                     ignore_app_ids: Sequence[str] = ()) -> list[dict]:
        """Customer messages newer than the chat's newest bot or shop message, oldest first.

        ``ignore_app_ids``: shop echoes sent through these apps do not count as an answer.
        """
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL(
                "WITH last_answer AS (SELECT sent_at, message_id FROM garage.messenger_messages "
                "WHERE psid = %(psid)s AND sender <> 'customer' "
                "AND NOT (sender = 'shop' AND app_id = ANY(%(ignored)s::text[])) "
                "ORDER BY sent_at DESC, message_id DESC LIMIT 1) "
                "SELECT {} FROM garage.messenger_messages m "
                "WHERE m.psid = %(psid)s AND m.sender = 'customer' AND NOT EXISTS ("
                "SELECT 1 FROM last_answer a WHERE (m.sent_at, m.message_id) "
                "<= (a.sent_at, a.message_id)) ORDER BY m.sent_at, m.message_id")
                .format(_columns(*MESSAGE_NAMES, prefix="m")),
                {"psid": psid, "ignored": [str(app) for app in ignore_app_ids]}).fetchall()

    def last_page_message_at(self, psid: str) -> datetime | None:
        """When the page (bot or shop) last wrote in this chat."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT max(sent_at) AS sent_at FROM garage.messenger_messages "
                               "WHERE psid = %s AND sender <> 'customer'", (psid,)).fetchone()
        return row["sent_at"] if row else None

    # -- reply attempts -------------------------------------------------------------------------

    def start_reply(self, channel: str, psid: str | None, trigger_key: str,
                    trigger_keys: Sequence[str] = ()) -> int | None:
        """Open a 'pending' attempt; None when this trigger was already handled (worker)."""
        if channel not in CHANNELS:
            raise ValueError("Unknown reply channel")
        if psid is not None:
            _digits(psid, "psid")
        if not isinstance(trigger_key, str) or not 1 <= len(trigger_key) <= 200:
            raise ValueError("trigger_key must be 1-200 characters")
        keys = [str(key)[:200] for key in trigger_keys][:100]
        with self.db.transaction() as conn:
            row = conn.execute(
                "INSERT INTO garage.messenger_replies (channel, psid, trigger_key, trigger_keys, "
                "status) VALUES (%s, %s, %s, %s::text[], 'pending') "
                "ON CONFLICT (channel, trigger_key) DO NOTHING RETURNING reply_id",
                (channel, psid, trigger_key, keys)).fetchone()
        return row["reply_id"] if row else None

    def add_sent_mid(self, reply_id: int, mid: str) -> bool:
        """Record one delivered part as soon as Meta returns its mid (worker)."""
        with self.db.transaction() as conn:
            row = conn.execute("UPDATE garage.messenger_replies SET sent_mids = "
                               "array_append(sent_mids, %s) WHERE reply_id = %s "
                               "RETURNING reply_id", (str(mid)[:200], int(reply_id))).fetchone()
        return row is not None

    def finish_reply(self, reply_id: int, status: str, **fields: Any) -> bool:
        """Close an attempt with its final status and any of REPLY_FIELDS (worker).

        ``duration_ms`` defaults to the time since start_reply; ``usage`` is the engine's
        token/cost record (a JSON object up to 4 KB); ``error_text`` is Meta's scrubbed message.
        """
        if status not in REPLY_STATUSES or status == "pending":
            raise ValueError("Unknown final reply status")
        unknown = set(fields) - REPLY_FIELDS
        if unknown:
            raise ValueError("Unknown reply fields: " + ", ".join(sorted(unknown)))
        values: dict[str, Any] = {}
        for name, value in fields.items():
            if name in ("error_code", "error_subcode", "duration_ms"):
                values[name] = None if value is None else int(value)
            elif name == "error_text":
                values[name] = str(value or "")[:300]
            elif name == "engine_status":
                values[name] = str(value or "")[:40]
            elif name == "sent_mids":
                values[name] = [str(mid)[:200] for mid in value or ()]
            elif name == "usage":
                usage = dict(value or {})
                if len(json.dumps(usage, default=str).encode("utf-8")) > 4000:
                    usage = {"truncated": True}
                values[name] = Jsonb(json.loads(json.dumps(usage, default=str)))
            elif name == "cost_usd":
                values[name] = None if value is None else Decimal(str(value))
        assignments = [sql.SQL("status = %(status)s"), sql.SQL("finished_at = now()")]
        for name in values:
            cast = "::text[]" if name == "sent_mids" else ""
            assignments.append(sql.SQL("{} = %({})s" + cast).format(sql.Identifier(name),
                                                                     sql.SQL(name)))
        if "duration_ms" not in values:
            assignments.append(sql.SQL(
                "duration_ms = GREATEST(0, (extract(epoch FROM clock_timestamp() - started_at) "
                "* 1000)::int)"))
        with self.db.transaction() as conn:
            row = conn.execute(
                sql.SQL("UPDATE garage.messenger_replies SET {} WHERE reply_id = %(reply_id)s "
                        "RETURNING reply_id").format(sql.SQL(", ").join(assignments)),
                {**values, "status": status, "reply_id": int(reply_id)}).fetchone()
        return row is not None

    def abandon_stale_replies(self, older_than_seconds: int = 900) -> int:
        """Close 'pending' attempts a dead worker left behind (worker, at start-up)."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE garage.messenger_replies SET status = 'abandoned', finished_at = now() "
                "WHERE status = 'pending' "
                "AND started_at < now() - make_interval(secs => %s)",
                (int(older_than_seconds),)).rowcount

    def turns_today(self, psid: str | None = None) -> int:
        """Engine turns started today (Tbilisi day), for the spending caps.

        Counts the messenger and comment attempts in which the engine ran (the worker sets
        ``engine_status`` once it has), plus those that may still run it ('pending') or whose
        worker stopped without saying ('abandoned'). An attempt closed before the engine, by a
        switch, a dead token or Meta refusing the typing indicator, spent nothing and does not
        count, so a day of refused sends cannot use up the cap.
        """
        with self.db.transaction() as conn:
            row = conn.execute(sql.SQL(
                "SELECT count(*) AS turns FROM garage.messenger_replies "
                "WHERE started_at >= {} AND channel IN ('messenger', 'comment') "
                "AND (engine_status <> '' OR status = ANY(%s::text[])) "
                "AND (%s::text IS NULL OR psid = %s::text)").format(TODAY),
                (list(UNFINISHED), psid, psid)).fetchone()
        return int(row["turns"])

    def cost_today(self) -> float:
        """Recorded engine cost of today's attempts, in US dollars."""
        with self.db.transaction() as conn:
            row = conn.execute(sql.SQL("SELECT COALESCE(sum(cost_usd), 0)::float8 AS cost "
                                       "FROM garage.messenger_replies WHERE started_at >= {}")
                               .format(TODAY)).fetchone()
        return float(row["cost"])

    def replies(self, *, psid: str | None = None, channel: str | None = None,
                status: str | None = None, limit: int = 50,
                before_id: int | None = None) -> list[dict]:
        """Attempts newest first, for the dashboard (both logins)."""
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL(
                "SELECT {} FROM garage.messenger_replies "
                "WHERE (%(psid)s::text IS NULL OR psid = %(psid)s::text) "
                "AND (%(channel)s::text IS NULL OR channel = %(channel)s::text) "
                "AND (%(status)s::text IS NULL OR status = %(status)s::text) "
                "AND (%(before)s::bigint IS NULL OR reply_id < %(before)s::bigint) "
                "ORDER BY reply_id DESC LIMIT %(limit)s").format(REPLIES),
                {"psid": psid, "channel": channel, "status": status, "before": before_id,
                 "limit": _limit(limit, 50, 200)}).fetchall()

    # -- jobs ------------------------------------------------------------------------------------

    def start_run(self, job: str) -> int:
        """Open a job run (worker)."""
        if not isinstance(job, str) or not _JOB.match(job):
            raise ValueError("Job names are lower-case words")
        with self.db.transaction() as conn:
            return conn.execute("INSERT INTO garage.meta_sync_runs (job) VALUES (%s) "
                                "RETURNING run_id", (job,)).fetchone()["run_id"]

    def finish_run(self, run_id: int, status: str, records: int = 0, detail: str = "") -> bool:
        if status not in RUN_STATUSES:
            raise ValueError("A run ends ok, error or skipped")
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE garage.meta_sync_runs SET finished_at = now(), status = %s, records = %s, "
                "detail = %s WHERE run_id = %s RETURNING run_id",
                (status, max(0, int(records)), str(detail or "")[:500], int(run_id))).fetchone()
        return row is not None

    def last_runs(self, limit: int = 20, job: str | None = None) -> list[dict]:
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL(
                "SELECT {} FROM garage.meta_sync_runs "
                "WHERE (%s::text IS NULL OR job = %s::text) ORDER BY run_id DESC LIMIT %s")
                .format(RUNS), (job, job, _limit(limit, 20, 200))).fetchall()

    def latest_runs(self) -> list[dict]:
        """The newest run of every job."""
        with self.db.transaction() as conn:
            return conn.execute(sql.SQL("SELECT DISTINCT ON (job) {} FROM garage.meta_sync_runs "
                                        "ORDER BY job, run_id DESC").format(RUNS)).fetchall()

    def request_job(self, job: str, actor: Any, *, client_ip: str | None = None) -> int:
        """Ask the worker to run ``job`` now (dashboard login). An open request for the same job
        is reused, so repeated clicks queue one run."""
        if not isinstance(job, str) or not _JOB.match(job):
            raise ValueError("Job names are lower-case words")
        _, name = _actor(actor)
        with self.db.transaction() as conn:
            open_request = conn.execute(
                "SELECT request_id FROM garage.meta_job_requests WHERE job = %s "
                "AND taken_at IS NULL ORDER BY request_id LIMIT 1", (job,)).fetchone()
            if open_request is not None:
                return open_request["request_id"]
            request_id = conn.execute(
                "INSERT INTO garage.meta_job_requests (job, requested_by) VALUES (%s, %s) "
                "RETURNING request_id", (job, name)).fetchone()["request_id"]
            self.audit(actor, "messenger.job_requested", job, {}, client_ip=client_ip,
                       connection=conn)
        return request_id

    def take_job_requests(self, limit: int = 20) -> list[dict]:
        """Claim the open job requests, oldest first (worker)."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "WITH picked AS (SELECT request_id FROM garage.meta_job_requests "
                "WHERE taken_at IS NULL ORDER BY request_id LIMIT %s FOR UPDATE SKIP LOCKED) "
                "UPDATE garage.meta_job_requests AS r SET taken_at = now() FROM picked "
                "WHERE r.request_id = picked.request_id "
                "RETURNING r.request_id, r.job, r.requested_at, r.requested_by",
                (_limit(limit, 20, 200),)).fetchall()
        return sorted(rows, key=lambda row: row["request_id"])

    # -- retention, counts, deletion ---------------------------------------------------------------

    def purge(self, retention_days: int | None = None) -> dict:
        """Delete what retention allows (worker login).

        Messages are never purged: they are kept until "Delete data" (``delete_customer``).
        Webhook items after 6 days (INBOX_RETENTION_DAYS). Reply attempts, job runs and job
        requests after ``retention_days`` (default: the settings value). A chat whose last
        activity is older than that and that has no message left goes too, with its engine
        state. Live chats alarms (0015) go after the same ``retention_days`` and are left alone
        before that migration. The hashed ids of forgotten customers go after FORGOTTEN_DAYS
        (``forgotten`` is None before 0014).
        """
        with self.db.transaction() as conn:
            days = retention_days
            if days is None:
                days = conn.execute("SELECT retention_days FROM garage.meta_settings "
                                    "WHERE settings_id = 1").fetchone()["retention_days"]
            days = int(days)
            if not 1 <= days <= 3650:
                raise ValueError("retention_days must be 1-3650")
            counts = {
                "inbox": conn.execute(
                    "DELETE FROM garage.meta_inbox "
                    "WHERE received_at < now() - make_interval(days => %s)",
                    (INBOX_RETENTION_DAYS,)).rowcount,
                # Messages are kept for good (owner's decision, 2026-11-20): no purge deletes
                # them, so their chats stay too. Only "Delete data" erases a customer's chat.
                "replies": conn.execute(
                    "DELETE FROM garage.messenger_replies "
                    "WHERE started_at < now() - make_interval(days => %s)", (days,)).rowcount,
                "threads": conn.execute(
                    "DELETE FROM garage.messenger_threads t WHERE GREATEST(t.first_seen_at, "
                    "t.last_customer_at, t.last_shop_at, t.last_bot_at) "
                    "< now() - make_interval(days => %s) AND NOT EXISTS (SELECT 1 FROM "
                    "garage.messenger_messages m WHERE m.psid = t.psid)", (days,)).rowcount,
                "runs": conn.execute(
                    "DELETE FROM garage.meta_sync_runs "
                    "WHERE started_at < now() - make_interval(days => %s)", (days,)).rowcount,
                "job_requests": conn.execute(
                    "DELETE FROM garage.meta_job_requests "
                    "WHERE requested_at < now() - make_interval(days => %s)", (days,)).rowcount,
                "forgotten": None,
            }
            if conn.execute("SELECT to_regclass('garage.meta_forgotten') IS NOT NULL "
                            "AS ready").fetchone()["ready"]:
                counts["forgotten"] = conn.execute(
                    "DELETE FROM garage.meta_forgotten "
                    "WHERE forgotten_at < now() - make_interval(days => %s)",
                    (FORGOTTEN_DAYS,)).rowcount
        # Live chats alarms (0015) follow the chats' retention, counted from when they were
        # raised (closing or merging one never extends it). In a transaction of their own: a
        # login that may not delete them (provisioning was not rerun) fails loudly here without
        # undoing the deletions above, which the privacy page promises.
        with self.db.transaction() as conn:
            if _alerts_deletable(conn):
                # Messenger alarms only: the website's follow its chat log's retention
                # (``LiveStore.web_prune``, run by the site).
                conn.execute("DELETE FROM garage.live_alerts WHERE channel = 'messenger' "
                             "AND created_at < now() - make_interval(days => %s)", (days,))
        return counts

    def counts_today(self) -> dict:
        """Today's numbers for the status card (both logins)."""
        with self.db.transaction() as conn:
            row = conn.execute(sql.SQL(
                "SELECT "
                "(SELECT count(*) FROM garage.meta_inbox WHERE received_at >= {today}) "
                "AS webhooks, "
                "(SELECT count(*) FROM garage.messenger_messages WHERE sender = 'customer' "
                "AND sent_at >= {today}) AS customer_messages, "
                "(SELECT count(*) FROM garage.messenger_threads "
                "WHERE first_seen_at >= {today}) AS new_chats, "
                "(SELECT count(*) FROM garage.messenger_replies "
                "WHERE started_at >= {today} AND status = 'sent') AS replies_sent, "
                "(SELECT count(*) FROM garage.messenger_replies "
                "WHERE started_at >= {today} AND status = 'fallback_sent') AS fallbacks, "
                "(SELECT count(*) FROM garage.messenger_replies WHERE started_at >= "
                "{today} AND status IN ('failed', 'blocked_by_meta', 'abandoned')) AS failed, "
                "(SELECT count(*) FROM garage.messenger_replies "
                "WHERE started_at >= {today} AND starts_with(status, 'skipped_')) AS skipped, "
                "(SELECT count(*) FROM garage.messenger_threads WHERE needs_attention) "
                "AS attention").format(today=TODAY)).fetchone()
        return {**{key: int(value) for key, value in row.items()},
                "turns": self.turns_today(), "cost_usd": self.cost_today()}

    def delete_customer(self, psid: str, actor: Any, *, client_ip: str | None = None) -> dict:
        """Erase one customer's data: thread, messages, engine state, reply attempts, Live chats
        alarms (migration 0015; they may hold a phone number) and stored comments
        (``forget_comments``: kept out of later backfills too), in one transaction.

        Works with the dashboard login. ``comments`` is None before migration 0014. Webhook
        items naming the PSID are deleted too when the login may (the worker); otherwise
        ``inbox`` is None and they go with the 7-day purge.
        """
        _digits(psid, "psid")
        with self.db.transaction() as conn:
            # Comments first: the ones answered into this chat are found by its PSID.
            comments = forget_comments(conn, psid)
            replies = conn.execute("DELETE FROM garage.messenger_replies WHERE psid = %s",
                                   (psid,)).rowcount
            messages = conn.execute("DELETE FROM garage.messenger_messages WHERE psid = %s",
                                    (psid,)).rowcount
            threads = conn.execute("DELETE FROM garage.messenger_threads WHERE psid = %s",
                                   (psid,)).rowcount
            # The chat itself is remembered (hashed) as forgotten, so the catch-up job
            # (meta_catchup) never reads its messages back from the page's conversations.
            if conn.execute("SELECT to_regclass('garage.meta_forgotten') IS NOT NULL "
                            "AS ready").fetchone()["ready"]:
                conn.execute("INSERT INTO garage.meta_forgotten (subject_key) VALUES (%s) "
                             "ON CONFLICT (subject_key) DO UPDATE SET forgotten_at = now()",
                             (forgotten_key(f"chat:{psid}"),))
            if _alerts_deletable(conn):  # the alarms may carry the phone number they typed
                conn.execute("DELETE FROM garage.live_alerts "
                             "WHERE channel = 'messenger' AND chat_key = %s", (psid,))
            inbox = None
            if conn.execute("SELECT has_table_privilege('garage.meta_inbox', 'DELETE') "
                            "AS allowed").fetchone()["allowed"]:
                inbox = conn.execute(
                    "DELETE FROM garage.meta_inbox WHERE payload -> 'sender' ->> 'id' = %s "
                    "OR payload -> 'recipient' ->> 'id' = %s", (psid, psid)).rowcount
            counts = {"threads": threads, "messages": messages, "replies": replies,
                      "comments": comments, "inbox": inbox}
            self.audit(actor, "messenger.customer_deleted", mask_psid(psid), counts,
                       client_ip=client_ip, connection=conn)
        return counts
