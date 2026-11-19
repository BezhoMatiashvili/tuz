"""Dashboard API for the Garage Facebook page: the Messenger bot switch, the connection, chats.

``site.install_site`` mounts this router under ``/api/admin/facebook``. The site guard has
already required a staff session, an allow-listed Origin and the CSRF header for every route
(its ``/api/admin/`` rule); each route here then requires the admin role (``site_admin._admin``)
before it reads a body or touches the database.

Data goes through ``MetaStore`` on the site's own login (``AUTOMOTIVE_SITE_DATABASE_URL``, a
member of ``garage_site``), and migration 0013's grants are the fence: the switches, the
connection and job requests are writable; chats are readable and only their switch columns
writable; the webhook inbox shows its bookkeeping columns only. The store writes every change
together with its ``garage.audit_log`` row; this module adds audit rows only for what the store
and ``meta_connect`` do not record themselves (token checks and webhook repairs).

Access tokens stay inside ``meta_connect`` and the store. The pasted token is read from the
request body, handed to ``meta_connect.connect`` and dropped: no answer, log line or audit row
carries it, and an error detail that somehow held it is scrubbed.

The Meta block is read from the process environment, which ``Settings.from_env`` has already
filled from ``.env``. While it is incomplete, ``GET /status`` names the missing variables and
every other route answers 503 META_NOT_CONFIGURED; the rest of the site is unaffected.
"""

# No ``from __future__ import annotations`` here: the routes depend on closures over this
# router's context, and FastAPI resolves string annotations against module globals only.

import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, NamedTuple

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from automotive import meta_connect
from automotive.meta_config import REQUIRED_SCOPES, MetaConfigError, MetaSettings
from automotive.meta_connect import EXPIRY_WARNING_DAYS, ConnectError
from automotive.meta_context import MetaContext, NotConnected
from automotive.meta_crypto import TokenCipher, TokenCipherError
from automotive.meta_db import MetaDatabase
from automotive.meta_graph import GraphClient
from automotive.meta_store import (
    SETTINGS_FIELDS,
    AutoReplyNotConfirmed,
    InvalidFields,
    MetaConflict,
    MetaStore,
    bot_state,
)
from automotive.site_admin import _address, _admin, _fail

LOGGER = logging.getLogger(__name__)

PREFIX = "/api/admin/facebook"
# Jobs the dashboard may ask the worker to run now (MetaStore.request_job): exactly
# messenger_worker.JOB_NAMES, which a test pins. Not imported from there, so the site never
# loads the worker and its engine; a name the worker does not know would be "unknown_job".
JOBS = ("subscription", "names", "comments", "posts", "page_stats", "ads", "token_health",
        "purge")
THREAD_FILTERS = ("all", "attention", "off", "ads", "admin")
EVENTS_SHOWN = 10
# Connected, yet nothing from Meta for this long: the tunnel, the receiver or the subscription.
WEBHOOK_QUIET = timedelta(hours=24)
# Texts the bot sends must not carry control characters; tab and line breaks are fine.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DIGITS = re.compile(r"[0-9]{1,30}")
ERROR_CODES = {"extra_forbidden": "UNKNOWN", "missing": "REQUIRED", "string_too_long": "TOO_LONG"}
RANGE_ERRORS = frozenset({"greater_than", "greater_than_equal", "less_than", "less_than_equal"})
# HTTP status of a connect failure; every other code means the pasted token cannot be used (422).
CONNECT_STATUS = {"MULTIPLE_PAGES": 409}
MAX_VERSION = 2_147_483_647

PAGE_SHOWN = ("page_id", "page_name", "app_id", "subscribed_fields", "subscribed_at",
              "connected_at", "connected_by")
TOKEN_SHOWN = ("purpose", "subject_name", "scopes", "expires_at", "data_access_expires_at",
               "status", "status_detail", "checked_at", "connected_at", "connected_by")
THREAD_SHOWN = ("psid", "customer_name", "bot_enabled", "is_admin", "source", "source_ad_id",
                "first_seen_at", "last_customer_at", "last_shop_at", "last_bot_at",
                "needs_attention", "attention_reason", "version", "last_sender", "last_body",
                "last_message_at")
# sent_at orders the chat; a bot reply's sent_at is the answered message's time plus 1 ms (the
# worker's choice), so the browser shows stored_at, the real time, for the bot's rows.
MESSAGE_SHOWN = ("message_id", "sender", "body", "attachments", "referral_ad_id", "sent_at",
                 "stored_at")
MAX_ID = 9_223_372_036_854_775_807  # bigint

PsidPath = Annotated[str, Path(pattern=r"^[0-9]{1,30}$")]
JobPath = Annotated[str, Path(pattern="^(" + "|".join(JOBS) + ")$")]


# -- request bodies ----------------------------------------------------------------------------

async def _json_object(request: Request) -> dict[str, Any]:
    """The JSON object a route reads. Parsed here, so no error ever repeats what was sent."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise _fail(422, "INVALID_REQUEST") from None
    if not isinstance(data, dict):
        raise _fail(422, "INVALID_REQUEST")
    return data


Payload = Annotated[dict[str, Any], Depends(_json_object)]
AdminUser = Annotated[dict, Depends(_admin)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class SettingsChange(_Strict):
    """PUT /settings. The bounds are migration 0013's CHECK constraints, except
    ``retention_days`` (30-365): how long reply attempts, job runs and alarms are kept;
    messages are kept for good."""

    version: int = Field(ge=1, le=MAX_VERSION)
    bot_enabled: bool | None = None
    auto_reply_confirmed_off: bool | None = None
    comments_enabled: bool | None = None
    max_reply_age_minutes: int | None = Field(default=None, ge=5, le=1440)
    daily_turn_limit: int | None = Field(default=None, ge=0, le=5000)
    customer_daily_turn_limit: int | None = Field(default=None, ge=0, le=500)
    comment_public_reply: str | None = Field(default=None, max_length=300)
    fallback_ka: str | None = Field(default=None, max_length=500)
    fallback_en: str | None = Field(default=None, max_length=500)
    retention_days: int | None = Field(default=None, ge=30, le=365)

    @field_validator("comment_public_reply", "fallback_ka", "fallback_en")
    @classmethod
    def _printable(cls, value: str | None) -> str | None:
        if value is not None and CONTROL.search(value):
            raise ValueError("control characters")
        return value


class ThreadChange(_Strict):
    bot_enabled: bool | None = None
    is_admin: bool | None = None
    needs_attention: bool | None = None
    version: int | None = Field(default=None, ge=1, le=MAX_VERSION)


class Confirm(_Strict):
    confirm: bool = False


class DisconnectBody(Confirm):
    # Also remove our app from the page's webhooks (one call to Meta). Off by default: a
    # reconnect then works without repairing the subscription.
    unsubscribe: bool = False


class ProbeBody(_Strict):
    psid: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")


def _field_codes(error: ValidationError) -> dict[str, str]:
    """Field name -> UNKNOWN, REQUIRED, TOO_LONG, OUT_OF_RANGE or INVALID; never a value."""
    codes: dict[str, str] = {}
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = item.get("loc") or ("",)
        kind = str(item.get("type") or "")
        code = ERROR_CODES.get(kind) or ("OUT_OF_RANGE" if kind in RANGE_ERRORS else "INVALID")
        codes.setdefault(str(location[0])[:60], code)
    return codes


def _parse(model: type[BaseModel], payload: Mapping[str, Any]) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise _fail(422, "INVALID_FIELDS", _field_codes(error)) from None


def _connect_fields(payload: Mapping[str, Any]) -> tuple[str, str | None]:
    """The pasted token and the optional page choice, checked by hand: no error repeats a value
    or even a key name, so a token pasted into the wrong place stays out of the answer too.
    The token's own format is meta_connect's to judge (TOKEN_INVALID, audited)."""
    errors: dict[str, str] = {}
    if set(payload) - {"user_token", "page_id"}:
        errors[""] = "UNKNOWN"
    token = payload.get("user_token")
    if not isinstance(token, str) or not token.strip():
        errors["user_token"] = ERROR_CODES["missing"]  # "REQUIRED", like every other route
    page_id = payload.get("page_id")
    if page_id in (None, ""):
        page_id = None
    elif not isinstance(page_id, str) or not DIGITS.fullmatch(page_id):
        errors["page_id"] = "INVALID"
    if errors:
        raise _fail(422, "INVALID_FIELDS", errors)
    return token, page_id


# -- answers -----------------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    """JSON-ready: dates as ISO 8601 strings, decimals as numbers."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(item) for item in value]
    return value


def _scrub(value: Any, secret: str) -> Any:
    """``value`` with every copy of ``secret`` replaced. Defence in depth: the Graph client
    already removes the call's secrets from Meta's messages."""
    secret = (secret or "").strip()
    if len(secret) < 8:
        return value
    text = json.dumps(value, ensure_ascii=False)
    return json.loads(text.replace(secret, "[redacted]")) if secret in text else value


def _switches(row: Mapping[str, Any]) -> dict:
    """The settings row (switches with who/when, limits, texts, version) and the bot state."""
    return {**{key: value for key, value in row.items() if key != "settings_id"},
            "bot_state": bot_state(row)}


def _days_left(when: datetime | None, now: datetime) -> int | None:
    return None if when is None else (when - now).days


def _token_view(row: Mapping[str, Any], now: datetime, app_id: str) -> dict:
    """A stored token's metadata: never the token, never its sealed form."""
    view = {key: row.get(key) for key in TOKEN_SHOWN}
    days = _days_left(row.get("expires_at"), now)
    access = _days_left(row.get("data_access_expires_at"), now)
    scopes = set(row.get("scopes") or ())
    view.update(
        days_left=days, data_access_days_left=access, other_app=row.get("app_id") != app_id,
        # The login token carries the permissions the owner ticked in Graph API Explorer.
        missing_scopes=[name for name in REQUIRED_SCOPES if name not in scopes]
        if row.get("purpose") == "user" else [],
        warn=row.get("status") != "ok" or any(
            left is not None and left <= EXPIRY_WARNING_DAYS for left in (days, access)))
    return view


def _warnings(settings: Mapping[str, Any], page: Mapping[str, Any] | None, tokens: list[dict],
              last: Mapping[str, Any] | None, now: datetime) -> list[dict]:
    """What the owner should act on, most urgent first. Codes only; the browser words them."""
    found: list[dict] = []
    if settings.get("bot_paused_reason"):
        found.append({"code": "bot_paused", "reason": settings["bot_paused_reason"],
                      "since": settings.get("bot_paused_at")})
    if page is None:
        found.append({"code": "not_connected"})
    else:
        if not page.get("subscribed_fields"):
            found.append({"code": "not_subscribed"})
        received = last.get("received_at") if last else None
        if received is None or now - received > WEBHOOK_QUIET:
            found.append({"code": "no_recent_webhook", "last": received})
    for token in tokens:
        if token["status"] != "ok":
            found.append({"code": "token_broken", "purpose": token["purpose"]})
            continue
        for key, kind in (("days_left", "token"), ("data_access_days_left", "data_access")):
            left = token[key]
            if left is not None and left <= EXPIRY_WARNING_DAYS:
                found.append({"code": f"{kind}_expired" if left < 0 else f"{kind}_expiring",
                              "purpose": token["purpose"], "days": max(left, 0)})
    if not settings.get("auto_reply_confirmed_off"):
        found.append({"code": "auto_reply_not_confirmed"})
    return found


def _status(ctx: MetaContext) -> dict:
    store, now = ctx.store, ctx.now()
    settings = store.settings()
    page = store.page()
    tokens = [_token_view(row, now, ctx.settings.app_id) for row in store.token_rows()]
    last = store.last_event_at()
    return {
        "configured": True,
        "connected": page is not None,
        "switches": _switches(settings),
        "page": None if page is None else {key: page.get(key) for key in PAGE_SHOWN},
        "tokens": tokens,
        "last_event": last,
        "events": store.recent_events(EVENTS_SHOWN),
        "today": store.counts_today(),
        "runs": sorted(store.latest_runs(), key=lambda run: run["job"]),
        "jobs": list(JOBS),
        "required_scopes": list(REQUIRED_SCOPES),
        "warnings": _warnings(settings, page, tokens, last, now),
        "checked_at": now,
    }


def _thread_view(row: Mapping[str, Any]) -> dict:
    return {key: row[key] for key in THREAD_SHOWN if key in row}


def _message_view(row: Mapping[str, Any]) -> dict:
    return {key: row.get(key) for key in MESSAGE_SHOWN}


# -- configuration ---------------------------------------------------------------------------

def _context(site_settings: Any, store: Any, graph: Any,
             meta_settings: MetaSettings | None) -> tuple[MetaContext | None, list[str], list[str]]:
    """(context, missing variable names, malformed variable names). Never raises."""
    meta = meta_settings
    if meta is None:
        try:
            # The environment only: Settings.from_env has loaded .env already, and a process
            # that never did (a test) must not have the real file loaded behind its back.
            meta = MetaSettings.from_env(os.environ)
        except MetaConfigError as error:  # the message starts with the variable's name
            return None, [], [str(error).split(" ", 1)[0]]
    missing = meta.missing_for_dashboard()
    url = str(getattr(site_settings, "site_database_url", "") or "")
    if store is None and not url:
        missing.append("AUTOMOTIVE_SITE_DATABASE_URL")
    if missing:
        return None, missing, []
    try:
        cipher = TokenCipher(meta.token_key)
    except TokenCipherError:
        return None, [], ["META_TOKEN_ENCRYPTION_KEY"]
    if store is None:
        store = MetaStore(MetaDatabase(url, application_name="garage-site-facebook"), cipher)
    if graph is None:
        try:
            graph = GraphClient(meta)
        except MetaConfigError:
            return None, [], ["META_GRAPH_API_VERSION"]
    return MetaContext(meta, store, graph, cipher), [], []


class Ready(NamedTuple):
    """The signed-in admin and the integration a route works with."""

    user: dict
    ctx: MetaContext


def _audit(ready: Ready, request: Request, action: str, target: str, detail: dict) -> None:
    """An audit row for what the store does not record itself. A failed insert is logged by its
    type only and never hides the outcome the admin is waiting for."""
    try:
        ready.ctx.store.audit(ready.user, action, target, detail, client_ip=_address(request))
    except Exception as error:
        LOGGER.warning("facebook_audit_failed %s %s", action, type(error).__name__)


# -- routes ------------------------------------------------------------------------------------

def build_meta_admin_router(site_settings: Any, *, store: Any = None, graph: Any = None,
                            meta_settings: MetaSettings | None = None) -> APIRouter:
    """The /api/admin/facebook routes, admin only. ``store``, ``graph`` and ``meta_settings``
    replace the real ones in tests; by default the Meta block comes from the environment and
    the store uses the site login. Never raises: an incomplete block switches the section off."""
    router = APIRouter(prefix=PREFIX, include_in_schema=False)
    ctx, missing, invalid = _context(site_settings, store, graph, meta_settings)

    def ready(user: AdminUser) -> Ready:
        if ctx is None:
            raise _fail(503, "META_NOT_CONFIGURED")
        return Ready(user, ctx)

    Admin = Annotated[Ready, Depends(ready)]

    # -- status and switches -----------------------------------------------------------------

    @router.get("/status")
    def status(_: AdminUser) -> dict:
        if ctx is None:
            return {"configured": False, "missing": list(missing), "invalid": list(invalid)}
        return _plain(_status(ctx))

    @router.put("/settings")
    def save_settings(request: Request, admin: Admin, payload: Payload) -> dict:
        """Switches, limits and texts in one call.

        Turning the bot on needs Meta's Auto reply confirmed off (stored, or in the same body);
        ``bot_enabled_at`` is stamped by the store on off->on only. ``version`` must be the one
        last read, except for a body that only turns the bot off: that stop always works."""
        body = _parse(SettingsChange, payload)
        changes = {name: getattr(body, name) for name in body.model_fields_set
                   if name != "version"}
        nulls = {name: "INVALID" for name, value in changes.items() if value is None}
        if nulls:
            raise _fail(422, "INVALID_FIELDS", nulls)
        if not changes:
            raise _fail(422, "INVALID_FIELDS", {"": "REQUIRED"})
        store, user, ip = admin.ctx.store, admin.user, _address(request)
        current = store.settings()
        if changes != {"bot_enabled": False} and body.version != current["version"]:
            raise _fail(409, "VERSION_CONFLICT")
        confirmed = changes.get("auto_reply_confirmed_off")
        enabled = changes.get("bot_enabled", current["bot_enabled"])
        if enabled and not (current["auto_reply_confirmed_off"] if confirmed is None
                            else confirmed):
            # Checked before anything is written, so a refused body changes nothing.
            raise _fail(409, "AUTO_REPLY_NOT_CONFIRMED")
        limits = {name: value for name, value in changes.items() if name in SETTINGS_FIELDS}
        row = current
        try:
            if limits:
                row = store.update_settings(limits, user, body.version, client_ip=ip)
            # Only a real change: set_bot also clears a provider pause, and that takes the
            # explicit Resume, never a repeated "on".
            if enabled != row["bot_enabled"] or (
                    confirmed is not None and confirmed != row["auto_reply_confirmed_off"]):
                row = store.set_bot(enabled, user, confirmed, client_ip=ip)
            if "comments_enabled" in changes:
                row = store.set_comments(changes["comments_enabled"], user, client_ip=ip)
        except MetaConflict:
            raise _fail(409, "VERSION_CONFLICT") from None
        except AutoReplyNotConfirmed:
            raise _fail(409, "AUTO_REPLY_NOT_CONFIRMED") from None
        except InvalidFields as error:
            raise _fail(422, "INVALID_FIELDS", error.fields) from None
        return _plain({"switches": _switches(row)})

    @router.post("/resume")
    def resume(request: Request, admin: Admin) -> dict:
        """Clear the pause the worker sets when the AI provider account fails (audited when
        there was one)."""
        row = admin.ctx.store.resume_bot(admin.user, client_ip=_address(request))
        return _plain({"switches": _switches(row)})

    # -- connection ----------------------------------------------------------------------------

    @router.post("/connect")
    def connect(request: Request, admin: Admin, payload: Payload) -> dict:
        token, page_id = _connect_fields(payload)
        try:
            result = meta_connect.connect(admin.ctx, token, actor=admin.user, page_id=page_id,
                                          client_ip=_address(request))
        except ConnectError as error:  # audited by meta_connect, by code only
            if error.code == "NOT_CONFIGURED":
                raise _fail(503, "META_NOT_CONFIGURED") from None
            detail = _scrub(_plain(error.detail), token)
            raise _fail(CONNECT_STATUS.get(error.code, 422), error.code, detail or None) from None
        except Exception as error:  # whatever it was, its text must not travel with the token
            LOGGER.warning("facebook_connect_failed %s", type(error).__name__)
            raise _fail(502, "CONNECT_FAILED") from None
        return _scrub(_plain({"connected": result}), token)

    @router.post("/check")
    def check(request: Request, admin: Admin) -> dict:
        """Ask Meta about every stored token; dead ones are marked broken."""
        rows = meta_connect.check(admin.ctx)
        # Flat, so the audit page can show it: {"page": "ok", "user": "broken", "warn": [...]}.
        _audit(admin, request, "messenger.tokens_checked", "", {
            **{row["purpose"]: row["status"] for row in rows},
            "warn": sorted(row["purpose"] for row in rows if row["warn"])})
        return _plain({"tokens": rows})

    @router.post("/subscribe")
    def subscribe(request: Request, admin: Admin) -> dict:
        """Repair the page's and the app's webhook subscriptions when Meta dropped them."""
        try:
            result = meta_connect.ensure_subscribed(admin.ctx)
        except NotConnected:
            raise _fail(409, "NOT_CONNECTED") from None
        except TokenCipherError:
            raise _fail(409, "TOKEN_UNREADABLE") from None
        except ConnectError as error:
            _audit(admin, request, "messenger.subscribe", "", {"ok": False, "code": error.code})
            if error.code == "NOT_CONFIGURED":
                raise _fail(503, "META_NOT_CONFIGURED") from None
            raise _fail(502, "SUBSCRIBE_FAILED", _plain(error.detail) or None) from None
        _audit(admin, request, "messenger.subscribe", result["page_id"], {
            "ok": True, "page_resubscribed": result["page_resubscribed"],
            "app_resubscribed": result["app_resubscribed"]})
        return {"subscription": result}

    @router.post("/disconnect")
    def disconnect(request: Request, admin: Admin, payload: Payload) -> dict:
        """Delete the stored tokens and page. The bot and the comment replies are switched off
        first, so a later reconnect can never start answering a backlog."""
        body = _parse(DisconnectBody, payload)
        if not body.confirm:
            raise _fail(422, "CONFIRM_REQUIRED")
        store, user, ip = admin.ctx.store, admin.user, _address(request)
        current = store.settings()
        if current["bot_enabled"]:
            store.set_bot(False, user, client_ip=ip)
        if current["comments_enabled"]:
            store.set_comments(False, user, client_ip=ip)
        result = meta_connect.disconnect(admin.ctx, user, unsubscribe=body.unsubscribe,
                                         client_ip=ip)
        return _plain({"disconnected": result, "switches": _switches(store.settings())})

    @router.post("/probe")
    def probe(request: Request, admin: Admin, payload: Payload) -> dict:
        """The zero-delivery sending check (meta_connect.probe, audited there)."""
        body = _parse(ProbeBody, payload)
        try:
            result = meta_connect.probe(admin.ctx, body.psid, actor=admin.user,
                                        client_ip=_address(request))
        except NotConnected:
            raise _fail(409, "NOT_CONNECTED") from None
        except TokenCipherError:
            raise _fail(409, "TOKEN_UNREADABLE") from None
        return {"probe": result}

    @router.post("/jobs/{job}")
    def run_job(job: JobPath, request: Request, admin: Admin) -> dict:
        """Ask the worker to run a job now; repeated clicks share one open request."""
        request_id = admin.ctx.store.request_job(job, admin.user, client_ip=_address(request))
        return {"job": job, "request_id": request_id}

    # -- chats ---------------------------------------------------------------------------------

    @router.get("/threads")
    def threads(admin: Admin,
                kind: Annotated[str, Query(alias="filter",
                                           pattern="^(" + "|".join(THREAD_FILTERS) + ")$")] = "all",
                limit: Annotated[int, Query(ge=1, le=100)] = 30,
                offset: Annotated[int, Query(ge=0, le=100_000)] = 0) -> dict:
        rows = admin.ctx.store.list_threads(kind, limit + 1, offset)
        return _plain({"threads": [_thread_view(row) for row in rows[:limit]],
                       "has_more": len(rows) > limit, "filter": kind, "offset": offset})

    @router.get("/threads/{psid}")
    def thread(psid: PsidPath, admin: Admin, limit: Annotated[int, Query(ge=1, le=200)] = 50,
               before: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None) -> dict:
        """One chat and its newest messages, oldest first (sender customer, bot or shop)."""
        row = admin.ctx.store.get_thread(psid)
        if row is None:
            raise _fail(404, "NOT_FOUND")
        messages = admin.ctx.store.messages(psid, limit + 1, before_id=before)
        return _plain({"thread": _thread_view(row),
                       "messages": [_message_view(item) for item in messages[-limit:]],
                       "has_older": len(messages) > limit})

    @router.put("/threads/{psid}")
    def change_thread(psid: PsidPath, request: Request, admin: Admin, payload: Payload) -> dict:
        """The chat's own bot switch, its admin-account flag (accepts '#bot off|on|status' from
        that Facebook account) and "needs attention". Each change is audited by the store."""
        body = _parse(ThreadChange, payload)
        changes = {name: getattr(body, name) for name in ("bot_enabled", "is_admin",
                                                          "needs_attention")
                   if getattr(body, name) is not None}
        if not changes:
            raise _fail(422, "INVALID_FIELDS", {"": "REQUIRED"})
        store, user, ip = admin.ctx.store, admin.user, _address(request)
        current = store.get_thread(psid)
        if current is None:
            raise _fail(404, "NOT_FOUND")
        if body.version is not None and body.version != current["version"]:
            raise _fail(409, "VERSION_CONFLICT")
        expected = body.version
        try:
            for name, method in (("bot_enabled", store.set_thread_switch),
                                 ("is_admin", store.set_admin)):
                if name in changes and changes[name] != current[name]:
                    row = method(psid, changes[name], user, expected_version=expected,
                                 client_ip=ip)
                    if row is None:
                        raise _fail(404, "NOT_FOUND")
                    expected = None if expected is None else row["version"]
        except MetaConflict:
            raise _fail(409, "VERSION_CONFLICT") from None
        wanted = changes.get("needs_attention")
        if wanted is not None and wanted != current["needs_attention"]:
            if not store.flag_attention(psid, "admin" if wanted else "", needs=wanted,
                                        actor=user, client_ip=ip):
                raise _fail(404, "NOT_FOUND")
        row = store.get_thread(psid)
        if row is None:
            raise _fail(404, "NOT_FOUND")
        return _plain({"thread": _thread_view(row)})

    @router.delete("/threads/{psid}")
    def delete_thread(psid: PsidPath, request: Request, admin: Admin, payload: Payload) -> dict:
        """Erase one customer's chat and their stored comments from our database (nothing on
        Facebook); the comments are kept out of later backfills. Webhook copies the site login
        may not delete go with the 7-day inbox purge."""
        body = _parse(Confirm, payload)
        if not body.confirm:
            raise _fail(422, "CONFIRM_REQUIRED")
        if admin.ctx.store.get_thread(psid) is None:
            raise _fail(404, "NOT_FOUND")
        counts = admin.ctx.store.delete_customer(psid, admin.user, client_ip=_address(request))
        return {"deleted": counts}

    return router
