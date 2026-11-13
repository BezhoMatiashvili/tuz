"""Connecting the Garage page and the checks around it, for the dashboard and messenger_setup.

``connect`` turns the short-lived user token the owner pastes from Graph API Explorer (app
GARAGE 1886) into a long-lived login token and the page's non-expiring page token, checks both
with ``debug_token``, stores them sealed, and subscribes the app and the page to the webhook
fields. Every check runs before anything is written, so a bad paste never replaces a working
connection. A subscription that fails afterwards keeps the connection and is reported: the owner
fixes the tunnel and retries without pasting again (``ensure_subscribed``, which the worker also
runs every hour because Meta can drop a subscription after long delivery failures).

``probe`` is the go-live gate for sending. It sends a typing indicator with the send token to a
customer whose 24-hour window has closed, so nothing can appear on their screen, and reads Meta's
refusal: "outside the window" means the app may message real customers, a bare code 10 means Meta
still gates it. ``check``, ``discover_ads`` and ``sync_names`` only read from Meta.

ConnectError carries a code and a detail made of codes, ids and page names, never a token.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from automotive.meta_config import REQUIRED_SCOPES, SUBSCRIBED_FIELDS
from automotive.meta_context import MetaContext, NotConnected
from automotive.meta_crypto import TokenCipherError
from automotive.meta_graph import GraphError, TokenChoice
from automotive.meta_store import mask_psid

LOGGER = logging.getLogger(__name__)

# Without these three the bot cannot list the page, receive its webhooks or answer at all.
ESSENTIAL_SCOPES = ("pages_messaging", "pages_manage_metadata", "pages_show_list")
CONNECT_CODES = frozenset({"NOT_CONFIGURED", "TOKEN_INVALID", "WRONG_APP", "NOT_USER_TOKEN",
                           "MISSING_SCOPES", "EXCHANGE_FAILED", "NO_PAGES", "MULTIPLE_PAGES",
                           "PAGE_TOKEN_INVALID", "SUBSCRIBE_FAILED"})
# "More than 24 hours", with an hour of margin for clocks and Meta's own rounding.
QUIET_HOURS = 25
PROBE_CANDIDATES = 5
WINDOW_CLOSED_SUBCODE = 2018278
EXPIRY_WARNING_DAYS = 14
ADS_FIELDS = ("id,name,effective_status,creative{object_story_spec{page_id},"
              "effective_object_story_id},adset{promoted_object}")
_DIGITS = re.compile(r"^[0-9]{1,30}$")
_PASTED = re.compile(r"^[A-Za-z0-9_.|-]{20,2048}$")


class ConnectError(RuntimeError):
    """A connect step failed; ``code`` is one of CONNECT_CODES, ``detail`` holds no token."""

    def __init__(self, code: str, detail: Mapping[str, Any] | None = None) -> None:
        if code not in CONNECT_CODES:
            raise ValueError("Unknown connect error code")
        super().__init__(code)
        self.code = code
        self.detail = dict(detail or {})


def _graph_detail(error: GraphError, **extra: Any) -> dict:
    """``{"meta": codes plus Meta's own message}`` (scrubbed of the call's secrets) for the
    owner to read; audit rows take only the codes."""
    return {**extra, "meta": {**error.summary(), "message": error.message}}


def _epoch(value: Any) -> datetime | None:
    """Meta's epoch seconds; 0 or nothing means "never"."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return datetime.fromtimestamp(value, UTC)


def _meta_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    for parse in (lambda text: datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z"),
                  datetime.fromisoformat):
        try:
            parsed = parse(value)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _days_left(when: datetime | None, now: datetime) -> int | None:
    return None if when is None else (when - now).days


def _audit_quietly(ctx: MetaContext, actor: Any, action: str, target: str, detail: dict,
                   client_ip: str | None) -> None:
    """An audit row that must never turn a reported failure into a different one."""
    try:
        ctx.store.audit(actor, action, target, detail, client_ip=client_ip)
    except Exception as error:  # the original outcome is what the caller needs
        LOGGER.warning("meta_audit_failed %s %s", action, type(error).__name__)


# -- connect -------------------------------------------------------------------------------------

def connect(ctx: MetaContext, user_token: str, *, actor: Any, page_id: str | None = None,
            client_ip: str | None = None) -> dict:
    """Connect the page with a pasted Graph API Explorer user token (plan steps 1-7).

    Returns page_id, page_name, tasks, scopes, missing_scopes (non-essential permissions the
    token lacks: stats or ads stay unavailable until a new paste), user_expires_at,
    data_access_expires_at, page_expires_at (None: never) and ``subscription`` (ok, app, page,
    error). Raises ConnectError before anything is stored; failures are audited.
    """
    try:
        return _connect(ctx, user_token, actor=actor, page_id=page_id, client_ip=client_ip)
    except ConnectError as error:
        _audit_quietly(ctx, actor, "messenger.connect_failed", "", {"code": error.code},
                       client_ip)
        raise


def _connect(ctx: MetaContext, user_token: str, *, actor: Any, page_id: str | None,
             client_ip: str | None) -> dict:
    settings, graph = ctx.settings, ctx.graph
    missing = settings.missing_for_dashboard()
    if missing or ctx.cipher is None:
        raise ConnectError("NOT_CONFIGURED", {"missing": missing or ["META_TOKEN_ENCRYPTION_KEY"]})
    pasted = (user_token or "").strip()
    if not _PASTED.match(pasted):
        raise ConnectError("TOKEN_INVALID", {"reason": "format"})

    # 1. Our app's token, a person's (not a page's), with the permissions the bot cannot lack.
    try:
        info = graph.debug_token(pasted)
    except GraphError as error:
        raise ConnectError("TOKEN_INVALID", _graph_detail(error)) from None
    if not info.get("is_valid"):
        problem = info.get("error") if isinstance(info.get("error"), dict) else {}
        raise ConnectError("TOKEN_INVALID", {"reason": "not_valid", "code": problem.get("code")})
    if str(info.get("app_id") or "") != settings.app_id:
        raise ConnectError("WRONG_APP", {"app_id": str(info.get("app_id") or "")})
    if info.get("type") != "USER":
        raise ConnectError("NOT_USER_TOKEN", {"type": str(info.get("type") or "")})
    scopes = sorted({str(scope) for scope in info.get("scopes") or ()})
    missing_scopes = [scope for scope in REQUIRED_SCOPES if scope not in scopes]
    essential = [scope for scope in ESSENTIAL_SCOPES if scope in missing_scopes]
    if essential:
        raise ConnectError("MISSING_SCOPES", {"missing": missing_scopes, "essential": essential})
    user_id = str(info.get("user_id") or "")

    # 2. A long-lived login token (about 60 days) instead of the one-hour Explorer token.
    try:
        exchanged = graph.exchange_user_token(pasted)
    except GraphError as error:
        raise ConnectError("EXCHANGE_FAILED", _graph_detail(error)) from None
    long_token = exchanged["access_token"]
    user_expires = (ctx.now() + timedelta(seconds=exchanged["expires_in"])
                    if exchanged.get("expires_in") else None)
    user_name = ""
    try:
        me = graph.get("me", long_token, {"fields": "id,name"})
        user_name = str(me.get("name") or "")[:200]
        user_id = user_id or str(me.get("id") or "")
    except GraphError as error:
        LOGGER.info("meta_connect_name_unavailable code=%s", error.code)
    if not _DIGITS.match(user_id):
        raise ConnectError("TOKEN_INVALID", {"reason": "no_user_id"})

    # 3. The page among the pages this login manages; its token does not expire.
    try:
        accounts = graph.paged("me/accounts", long_token,
                               {"fields": "id,name,access_token,tasks", "limit": 100})
    except GraphError as error:
        raise ConnectError("NO_PAGES", _graph_detail(error)) from None
    pages = [page for page in accounts if _DIGITS.match(str(page.get("id") or ""))
             and isinstance(page.get("access_token"), str) and page.get("access_token")]
    choices = [{"id": str(page["id"]), "name": str(page.get("name") or "")[:200]}
               for page in pages]
    pinned = settings.page_id
    if pinned and page_id and page_id != pinned:
        raise ConnectError("NO_PAGES", {"page_id": page_id, "pinned_page_id": pinned,
                                        "choices": choices})
    wanted = pinned or page_id
    if wanted:
        chosen = next((page for page in pages if str(page["id"]) == wanted), None)
        if chosen is None:
            raise ConnectError("NO_PAGES", {"page_id": wanted, "choices": choices})
    elif not pages:
        raise ConnectError("NO_PAGES", {"choices": []})
    elif len(pages) > 1:
        raise ConnectError("MULTIPLE_PAGES", {"choices": choices})
    else:
        chosen = pages[0]
    chosen_id, chosen_name = str(chosen["id"]), str(chosen.get("name") or "")[:200]
    page_token = chosen["access_token"]
    tasks = sorted({str(task) for task in chosen.get("tasks") or ()})

    # 4. Meta's own word that this is our app's token for exactly this page.
    try:
        page_info = graph.debug_token(page_token)
    except GraphError as error:
        raise ConnectError("PAGE_TOKEN_INVALID", _graph_detail(error)) from None
    if (not page_info.get("is_valid") or page_info.get("type") != "PAGE"
            or str(page_info.get("profile_id") or "") != chosen_id
            or str(page_info.get("app_id") or "") != settings.app_id):
        raise ConnectError("PAGE_TOKEN_INVALID", {
            "valid": bool(page_info.get("is_valid")), "type": str(page_info.get("type") or ""),
            "page_matches": str(page_info.get("profile_id") or "") == chosen_id})

    # 5. Both tokens sealed, in one transaction.
    page_expires = _epoch(page_info.get("expires_at"))
    data_access = _epoch(info.get("data_access_expires_at"))
    ctx.store.save_connection(chosen_id, chosen_name, settings.app_id, {
        "page": {"token": page_token, "subject_id": chosen_id, "subject_name": chosen_name,
                 "scopes": page_info.get("scopes") or scopes, "expires_at": page_expires,
                 "data_access_expires_at": _epoch(page_info.get("data_access_expires_at"))},
        "user": {"token": long_token, "subject_id": user_id, "subject_name": user_name,
                 "scopes": scopes, "expires_at": user_expires,
                 "data_access_expires_at": data_access},
    }, actor, client_ip=client_ip)

    # 6-7. Webhooks: a failure here keeps the connection and is reported.
    subscription = _subscribe(ctx, chosen_id, page_token)
    return {"page_id": chosen_id, "page_name": chosen_name, "tasks": tasks, "scopes": scopes,
            "missing_scopes": missing_scopes, "user_expires_at": user_expires,
            "data_access_expires_at": data_access, "page_expires_at": page_expires,
            "subscription": subscription}


def _subscribe(ctx: MetaContext, page_id: str, page_token: str) -> dict:
    """Subscribe the app (callback, verify token, fields) and the page; report each."""
    fields = list(SUBSCRIBED_FIELDS)
    result: dict[str, Any] = {"ok": False, "app": False, "page": False, "error": None}
    try:
        ctx.graph.subscribe_app(ctx.settings.webhook_url, ctx.settings.verify_token, fields)
        result["app"] = True
    except GraphError as error:
        result["error"] = _graph_detail(error, code="SUBSCRIBE_FAILED", step="app")
    try:
        ctx.graph.subscribe_page(page_id, page_token, fields)
        result["page"] = True
        ctx.store.set_subscription(fields)
    except GraphError as error:
        result["error"] = result["error"] or _graph_detail(error, code="SUBSCRIBE_FAILED",
                                                           step="page")
    result["ok"] = result["app"] and result["page"]
    return result


def ensure_subscribed(ctx: MetaContext) -> dict:
    """Self-heal: re-subscribe the page and the app when ours or a field is missing.

    Reads the page's subscribed apps and the app's subscriptions; writes only what is missing.
    Returns page_id, page_resubscribed, app_resubscribed. Raises NotConnected, or ConnectError
    NOT_CONFIGURED / SUBSCRIBE_FAILED.
    """
    settings = ctx.settings
    if not settings.webhook_url or not settings.verify_token:
        raise ConnectError("NOT_CONFIGURED", {"missing": [
            name for name, value in (("MESSENGER_PUBLIC_URL", settings.public_url),
                                     ("META_WEBHOOK_VERIFY_TOKEN", settings.verify_token))
            if not value]})
    page_id = ctx.page_id
    token = ctx.page_token()
    fields = list(SUBSCRIBED_FIELDS)
    result = {"page_id": page_id, "page_resubscribed": False, "app_resubscribed": False}
    try:
        apps = ctx.graph.page_subscriptions(page_id, token).get("data") or []
        ours = next((entry for entry in apps if isinstance(entry, dict)
                     and str(entry.get("id") or "") == settings.app_id), None)
        have = set(ours.get("subscribed_fields") or ()) if ours else set()
        if ours is None or not set(fields) <= have:
            ctx.graph.subscribe_page(page_id, token, fields)
            result["page_resubscribed"] = True
        objects = ctx.graph.app_subscriptions().get("data") or []
        page_object = next((entry for entry in objects if isinstance(entry, dict)
                            and entry.get("object") == "page"), None) or {}
        names = {field.get("name") for field in page_object.get("fields") or ()
                 if isinstance(field, dict)}
        if (not page_object.get("active") or page_object.get("callback_url") != settings.webhook_url
                or not set(fields) <= names):
            ctx.graph.subscribe_app(settings.webhook_url, settings.verify_token, fields)
            result["app_resubscribed"] = True
    except GraphError as error:
        raise ConnectError("SUBSCRIBE_FAILED", _graph_detail(error)) from None
    # Written only on a change, so the status card's "since" keeps the real subscription time.
    stored = (ctx.store.page() or {}).get("subscribed_fields") or []
    if result["page_resubscribed"] or sorted(stored) != sorted(fields):
        ctx.store.set_subscription(fields)
    return result


# -- token health ----------------------------------------------------------------------------------

def check(ctx: MetaContext) -> list[dict]:
    """Ask Meta about every stored token, mark dead ones broken, and report expiry.

    Our app's tokens go through debug_token (and refresh the stored expiry); a token minted
    through another app is tried with a plain ``GET /me``. A throttled or unanswered check leaves
    the stored status alone. Rows: purpose, status, detail, checked, expires_at,
    data_access_expires_at, days_left, data_access_days_left, warn (expiry within 14 days).
    """
    now = ctx.now()
    results = []
    for row in ctx.store.token_rows():
        purpose = row["purpose"]
        entry = {"purpose": purpose, "status": row["status"], "detail": row["status_detail"],
                 "checked": False, "expires_at": row["expires_at"],
                 "data_access_expires_at": row["data_access_expires_at"]}
        outcome = _check_one(ctx, row)
        if outcome is not None:
            status, detail, expiry = outcome
            ctx.store.mark_token(purpose, status, detail, expiry=expiry)
            entry.update(status=status, detail=detail, checked=True)
            if expiry is not None:
                entry.update(expires_at=expiry["expires_at"],
                             data_access_expires_at=expiry["data_access_expires_at"])
        else:
            entry["detail"] = "unchecked"
        entry["days_left"] = _days_left(entry["expires_at"], now)
        entry["data_access_days_left"] = _days_left(entry["data_access_expires_at"], now)
        entry["warn"] = entry["status"] != "ok" or any(
            days is not None and days <= EXPIRY_WARNING_DAYS
            for days in (entry["days_left"], entry["data_access_days_left"]))
        results.append(entry)
    return results


def _check_one(ctx: MetaContext, row: Mapping[str, Any]) -> tuple[str, str, dict | None] | None:
    purpose = row["purpose"]
    try:
        token = ctx.store.token(purpose)
    except TokenCipherError:
        return "broken", "decrypt_failed", None
    if not token:
        return None
    try:
        if row["app_id"] == ctx.settings.app_id:
            info = ctx.graph.debug_token(token)
            if not info.get("is_valid"):
                problem = info.get("error") if isinstance(info.get("error"), dict) else {}
                return "broken", f"meta_code_{problem.get('code') or 'invalid'}", None
            return "ok", "", {"scopes": info.get("scopes") or row["scopes"],
                              "expires_at": _epoch(info.get("expires_at")),
                              "data_access_expires_at": _epoch(
                                  info.get("data_access_expires_at"))}
        ctx.graph.get("me", TokenChoice(token, False, purpose), {"fields": "id"})
        return "ok", "", None
    except GraphError as error:
        if error.token_dead:
            return "broken", f"meta_code_{error.code}", None
        LOGGER.info("meta_token_check_skipped purpose=%s code=%s http=%s", purpose, error.code,
                    error.http_status)
        return None


def disconnect(ctx: MetaContext, actor: Any, *, unsubscribe: bool = False,
               client_ip: str | None = None) -> dict:
    """Delete the stored tokens and page (dashboard login), optionally first removing our app
    from the page's webhooks. An unsubscribe failure is reported, never blocks the delete."""
    result: dict[str, Any] = {"unsubscribed": None, "error": None}
    if unsubscribe:
        try:
            ctx.graph.unsubscribe_page(ctx.page_id, ctx.page_token())
            result["unsubscribed"] = True
        except GraphError as error:
            result.update(unsubscribed=False, error=_graph_detail(error))
        except (NotConnected, TokenCipherError) as error:
            result.update(unsubscribed=False, error={"type": type(error).__name__})
    result.update(ctx.store.disconnect(actor, client_ip=client_ip))
    return result


# -- the sending gate ----------------------------------------------------------------------------

def _participants(conversation: Mapping[str, Any], page_id: str) -> list[dict]:
    block = conversation.get("participants")
    data = block.get("data") if isinstance(block, dict) else None
    return [person for person in data or () if isinstance(person, dict)
            and _DIGITS.match(str(person.get("id") or "")) and str(person["id"]) != page_id]


def _conversation_times(ctx: MetaContext, page_id: str,
                        max_pages: int = 4) -> dict[str, datetime | None] | None:
    """Each participant's newest conversation updated_time (None: Meta gave no usable time),
    or None when the conversations cannot be read.

    A conversation's updated_time moves with every message, so an old one means the
    customer's last message is at least that old too.
    """
    try:
        conversations = ctx.graph.paged(f"{page_id}/conversations", ctx.page_token(),
                                        {"fields": "participants,updated_time", "limit": 50},
                                        max_pages=max_pages)
    except GraphError as error:
        LOGGER.info("meta_probe_conversations_unreadable code=%s", error.code)
        return None
    times: dict[str, datetime | None] = {}
    for conversation in conversations:
        updated = _meta_time(conversation.get("updated_time"))
        for person in _participants(conversation, page_id):
            key = str(person["id"])
            if key not in times:
                times[key] = updated
            elif times[key] is None or updated is None:
                times[key] = None
            else:
                times[key] = max(times[key], updated)
    return times


def _candidates(ctx: MetaContext, page_id: str, psid: str | None) -> tuple[list[str], str]:
    """Customers the probe may use, or the reason there is none.

    Never an admin chat and never a customer whose window may be open: the database must say
    their last message is older than QUIET_HOURS, or Meta's conversation list must, and neither
    may show anything newer. Database candidates come first; the conversation list both checks
    them and supplies candidates when the database has none.
    """
    cutoff = ctx.now() - timedelta(hours=QUIET_HOURS)
    times = _conversation_times(ctx, page_id)

    def recent_on_meta(person: str) -> bool:
        return times is not None and person in times and (times[person] is None
                                                          or times[person] >= cutoff)

    def unusable(thread: Mapping[str, Any] | None) -> bool:
        return bool(thread) and (thread["is_admin"] or (
            thread["last_customer_at"] is not None and thread["last_customer_at"] >= cutoff))

    if psid is not None:
        if not _DIGITS.match(psid):
            return [], "invalid_psid"
        thread = ctx.store.get_thread(psid)
        if thread and thread["is_admin"]:
            return [], "admin_chat"
        if unusable(thread) or recent_on_meta(psid):
            return [], "window_may_be_open"
        quiet_in_db = bool(thread) and thread["last_customer_at"] is not None
        if quiet_in_db or (times is not None and psid in times):
            return [psid], ""
        return [], "conversations_unreadable" if times is None else "not_found"
    stored = [row["psid"] for row in ctx.store.probe_candidates(
        page_id, quiet_hours=QUIET_HOURS, limit=PROBE_CANDIDATES * 4)]
    chosen = [person for person in stored if not recent_on_meta(person)]
    if not chosen and times is not None:
        quiet = sorted(((person, updated) for person, updated in times.items()
                        if updated is not None and updated < cutoff),
                       key=lambda item: item[1], reverse=True)
        for person, _ in quiet:
            if not unusable(ctx.store.get_thread(person)):
                chosen.append(person)
            if len(chosen) >= PROBE_CANDIDATES:
                break
    if chosen:
        return chosen[:PROBE_CANDIDATES], ""
    return [], "conversations_unreadable" if times is None and not stored else "no_candidate"


def probe(ctx: MetaContext, psid: str | None = None, *, actor: Any = "system",
          client_ip: str | None = None) -> dict:
    """The zero-delivery gate probe. Returns result ('passed', 'gated', 'inconclusive'),
    reason, code, subcode, psid (masked to its last four digits), send_purpose and tried.

    Only a success, or code 10/100 with subcode 2018278 (the customer's window is closed, so the
    call itself was allowed), passes. The gate (``GraphError.gated``: a bare code 10, or Meta's
    documented 200/2018028) fails. A person who is unavailable (``GraphError.unavailable``)
    moves on to the next customer; anything else is inconclusive. Audited.
    """
    page_id = ctx.page_id
    choice = ctx.send_token()
    candidates, reason = _candidates(ctx, page_id, psid)
    result: dict[str, Any] = {"result": "inconclusive", "reason": reason, "code": None,
                              "subcode": None, "psid": "", "send_purpose": choice.purpose,
                              "tried": 0}
    for candidate in candidates[:PROBE_CANDIDATES]:
        result.update(tried=result["tried"] + 1, psid=mask_psid(candidate))
        try:
            ctx.graph.sender_action(page_id, choice, candidate, "typing_on")
        except GraphError as error:
            result.update(code=error.code, subcode=error.subcode)
            if error.code in (10, 100) and error.subcode == WINDOW_CLOSED_SUBCODE:
                result.update(result="passed", reason="outside_window")
            elif error.gated:
                result.update(result="gated", reason="gated")
            elif error.unavailable:
                result["reason"] = "unavailable"
                continue
            else:
                result["reason"] = "unexpected_error"
            break
        result.update(result="passed", reason="accepted", code=None, subcode=None)
        break
    _audit_quietly(ctx, actor, "messenger.probe", page_id,
                   {key: result[key] for key in ("result", "reason", "code", "subcode", "psid",
                                                 "send_purpose", "tried")}, client_ip)
    return result


# -- read-only discovery ------------------------------------------------------------------------

def promotes_page(ad: Mapping[str, Any], page_id: str) -> bool:
    """Whether an ad (fields ADS_FIELDS) promotes ``page_id``: its creative's page, the page
    of the post it boosts, or its ad set's promoted page."""
    creative = ad.get("creative") if isinstance(ad.get("creative"), dict) else {}
    spec = creative.get("object_story_spec") if isinstance(
        creative.get("object_story_spec"), dict) else {}
    if str(spec.get("page_id") or "") == page_id:
        return True
    story = str(creative.get("effective_object_story_id") or "")
    if story.split("_", 1)[0] == page_id:
        return True
    adset = ad.get("adset") if isinstance(ad.get("adset"), dict) else {}
    promoted = adset.get("promoted_object") if isinstance(adset.get("promoted_object"),
                                                          dict) else {}
    return str(promoted.get("page_id") or "") == page_id


def discover_ads(ctx: MetaContext, *, max_pages: int = 5) -> list[dict]:
    """Every ad account the ads token can read and how many of its ads promote our page.

    Rows: account_id, name, currency, account_status, garage_ads, ads_scanned, error (codes when
    one account could not be read). Read-only; nothing is stored.
    """
    page_id = ctx.page_id
    token = ctx.ads_token()
    accounts = ctx.graph.paged("me/adaccounts", token,
                               {"fields": "account_id,name,currency,account_status",
                                "limit": 100}, max_pages=max_pages)
    results = []
    for account in accounts:
        account_id = str(account.get("account_id") or "")
        if not _DIGITS.match(account_id):
            continue
        entry: dict[str, Any] = {
            "account_id": account_id, "name": str(account.get("name") or "")[:200],
            "currency": str(account.get("currency") or ""),
            "account_status": account.get("account_status"), "garage_ads": 0,
            "ads_scanned": 0, "error": None}
        try:
            ads = ctx.graph.paged(f"act_{account_id}/ads", token,
                                  {"fields": ADS_FIELDS, "limit": 100}, max_pages=max_pages)
        except GraphError as error:
            entry["error"] = error.summary()
        else:
            entry["ads_scanned"] = len(ads)
            entry["garage_ads"] = sum(1 for ad in ads if promotes_page(ad, page_id))
        results.append(entry)
    return results


def sync_names(ctx: MetaContext, *, max_pages: int = 10) -> dict:
    """Fill customer_name and conversation_id of chats we already store, from the page's
    conversations. Reads Meta only; updates threads, so it runs with the worker login."""
    page_id = ctx.page_id
    conversations = ctx.graph.paged(f"{page_id}/conversations", ctx.page_token(),
                                    {"fields": "participants,updated_time,id", "limit": 100},
                                    max_pages=max_pages)
    rows = [{"psid": str(person["id"]), "name": person.get("name"),
             "conversation_id": conversation.get("id")}
            for conversation in conversations for person in _participants(conversation, page_id)]
    return {"conversations": len(conversations),
            "updated": ctx.store.apply_conversation_names(rows)}
