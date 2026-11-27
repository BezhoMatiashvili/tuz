"""The parts assistant answering Facebook Messenger, with each chat's state kept in the database.

The website's ``ConversationStore`` keeps a conversation in memory for minutes. A Messenger chat
lasts for days and outlives restarts of the worker, so here every turn runs in a conversation of
its own: ``MessengerEngine.respond`` restores the chat's saved state into a fresh conversation,
runs one turn of the assistant engine for the public audience, reads the turn the way the
website's chat widget does (``site_chat.public_turn``) and hands
back the state for ``messenger_threads.engine_state``.

The store's own code does the work, and ``conversations.py`` is not edited. A saved state is
exactly the fields ``ConversationStore.create()`` puts in a conversation (a test pins them, so a
field added there fails loudly here), minus the two that only make sense in memory (``touched``
and ``turns``), plus the three the engine adds later (``memory``, ``vin_typed``, ``identity``).

Nothing here reads what the customer wrote: the model does. This module moves state, enforces
the public audience and the size of what is saved, and maps how a turn ended to a status the
worker can act on.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from psycopg import sql

from automotive.assistant import ACCOUNT_STATUSES, TRANSCRIPT_TURNS
from automotive.assistant_tools import HANDLE
from automotive.chat import VIN_PATTERN, _redact
from automotive.conversations import ConversationError, ConversationStore
from automotive.live_alerts import TurnAlerts, turn_alerts
from automotive.meta_config import ROOT
from automotive.site_chat import SAID_LIMIT, plain, public_turn
from automotive.site_repository import SERVICE_COLUMNS
from automotive.vin import validate_vin

LOGGER = logging.getLogger(__name__)

TTL_SECONDS = 48 * 3600.0  # only a backstop: a conversation here lives for one turn
TURN_SECONDS = 60.0
MAXIMUM = 300
HISTORY_LIMIT = 12  # the deque ConversationStore.create() makes
MESSAGE_LIMIT = 4000  # what the store keeps of one message
TRANSCRIPT_TEXT = 1500  # what the assistant keeps of one side of a transcript turn
CARD_LIMIT = 100  # the most cards ConversationStore.get hands out at once
# messenger_threads.engine_state allows 262,144 bytes of jsonb text; this leaves room for the
# difference between that text and the measure below.
STATE_BYTES = 200_000
POLL_SECONDS = 0.1
CACHE_FILE = "automotive-cache.sqlite3"
# The chat log an earlier version kept in the data folder; removed when the engine is built.
OLD_CHAT_LOG_FILE = "chat_log.sqlite3"
RESEARCH_CACHE_FILE = "research_cache.sqlite3"

# What ConversationStore.create() puts in a conversation that is worth keeping...
CREATED_STATE_KEYS = ("vehicle", "vin", "category", "history", "language", "pending", "shown",
                      "vehicle_revision", "customer_stated", "resolution_candidates",
                      "selected_configuration", "unresolved_fields", "clarification_choices")
# ...what only makes sense in this process's memory...
RUNTIME_KEYS = ("touched", "turns")
# ...and what the store adds once the assistant engine has answered.
ENGINE_STATE_KEYS = ("memory", "vin_typed", "identity")
STATE_KEYS = CREATED_STATE_KEYS + ENGINE_STATE_KEYS
STATUSES = ("ok", "failed", "cancelled", "timeout")

_SURROGATE = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True)
class EngineResult:
    """How one turn ended.

    ``status``: ``ok`` (a reply to send), ``failed``, ``cancelled`` (``cancel`` was set) or
    ``timeout``. ``public`` is ``site_chat.public_turn``'s view of the turn (empty when no turn
    ran to an end) for ``messenger_render.render``. ``engine_status`` is the engine's own
    (``OK``, ``CLARIFICATION``, ``PROVIDER_BILLING``...), '' when it reported none.
    ``language`` is the reply's, ``ka`` or ``en`` as the engine's API knows them (a Russian
    reply reports ``en``), for the channel's own texts. ``usage`` is the engine's token count.
    ``error_code`` is '' when ``ok``, else the reason: an engine status such as
    ``PROVIDER_BILLING``, ``AUTHENTICATION_ERROR`` or ``ACCESS_DENIED`` (the AI provider's
    account refuses every request: pause the bot), ``CONFLICTING_VINS`` (ask for one VIN),
    ``TURN_DEADLINE`` or ``TIMEOUT``, ``CHAT_UNAVAILABLE``, ``EMPTY_ANSWER``, ``EMPTY_MESSAGE``.

    Two states, both JSON-ready and under ``STATE_BYTES``:

    - ``state``: save it when something was sent for this turn (the reply, or a fallback
      notice). After ``ok`` it holds the engine's answer. Otherwise it keeps the customer's
      words, as the engine's own record of a failed turn or as an unanswered message, so the
      next turn still sees the question.
    - ``unsent_state``: save it instead when nothing was sent (the chat's switch went off, the
      shop answered first, a newer message superseded the turn). It is the state the turn
      started from with this message added as unanswered, which the next turn reads with its own
      message. Keeping the old state and sending this message again with the next one also
      works; at worst a message is read twice, never lost.

    ``alerts`` is what the turn means for the shop's staff (``live_alerts.turn_alerts``: a part
    not found, a visit or service request, a handoff, a phone number the customer typed); empty
    unless ``ok``. The worker records it whether or not the reply could be sent.
    """

    status: str
    public: dict
    state: dict
    engine_status: str
    language: str
    usage: dict
    error_code: str
    unsent_state: dict = field(default_factory=dict)
    alerts: TurnAlerts = field(default_factory=TurnAlerts)
    # For a turn that ended in an error: its class and the engine step it came from
    # ("ValueError@model"), which the stuck chat on dashboard > Live chats shows. '' otherwise.
    cause: str = ""


class MessengerConversations(ConversationStore):
    """The website's ``ConversationStore``, for Messenger customers: the public audience (no stock
    numbers of the shop), 60 s per turn, room for 300, and a conversation's state taken out and
    put back whole. Each conversation here serves one turn; the 48 h idle expiry only catches
    one a crash left behind.

    ``create()`` starts the chat service's ``prepare`` for every new conversation: it builds the
    catalogue index and has the provider read the fixed prompt into its cache. Here every turn
    is a new conversation, so that request would leave together with the turn's own first one
    and the prompt would be paid for twice. The store therefore sees the service without
    ``prepare``; ``MessengerEngine.prepare`` runs it when a message opens the quiet window
    before a turn. ``service`` is the chat service itself."""

    def __init__(self, chat_service: Any, search_service: Any, *,
                 ttl_seconds: float = TTL_SECONDS, turn_seconds: float = TURN_SECONDS,
                 maximum: int = MAXIMUM, clock: Any = time.monotonic,
                 chat_log: Any = None) -> None:
        super().__init__(_Unprepared(chat_service), search_service, ttl_seconds=ttl_seconds,
                         turn_seconds=turn_seconds, maximum=maximum, clock=clock,
                         chat_log=chat_log, audience="public")
        self.service = chat_service

    def export_state(self, identifier: str) -> dict:
        """The saved form of a live conversation: a JSON-ready deep copy, taken under the lock."""
        with self._lock:
            conversation = self._conversations.get(identifier)
            if conversation is None:
                raise ConversationError("CONVERSATION_EXPIRED", 404)
            return _export(conversation)

    def restore(self, state: Any) -> str:
        """A new conversation holding ``state``; its id. A state that is not one (an old row,
        wrong types, unknown keys) is taken for what it holds, never refused: see
        ``clean_state``."""
        fields = clean_state(state)
        with self._lock:
            identifier = self.create()["conversation_id"]
            conversation = self._conversations[identifier]
            for key, value in fields.items():
                conversation[key] = (deque(value, maxlen=HISTORY_LIMIT) if key == "history"
                                     else value)
            return identifier

    def begin(self, state: Any, message: str, *, language: str = "auto",
              choice: int | None = None) -> tuple[str, str, dict]:
        """Restore ``state`` and start a turn on ``message``, as one step under the lock.

        Returns the conversation's id, the turn's id and the state as it stands once the turn
        has started (a VIN in the message is already taken), with ``message`` added as not yet
        answered. The lock keeps the turn's worker from publishing its answer before that copy
        is taken. Raises ``ConversationError`` as ``submit`` does (``CONFLICTING_VINS``,
        capacity), and ``ASSISTANT_REQUIRED`` when the chat service runs another engine: the
        older engines read messages with hand-written rules, and Messenger is the assistant's."""
        if self.engine != "assistant":
            raise ConversationError("ASSISTANT_REQUIRED", 503)
        with self._lock:
            identifier = self.restore(state)
            try:
                started = self.submit(identifier, message, language=language, choice=choice)
                unsent = self.unanswered(_export(self._conversations[identifier]), message)
            except BaseException:
                self._remove(identifier, "refused")
                raise
        return identifier, started["turn_id"], unsent

    def release(self, identifier: str) -> dict | None:
        """The conversation's state, and the conversation removed, in one step: a turn that
        finishes a moment later can change neither. None when it is already gone."""
        with self._lock:
            conversation = self._conversations.get(identifier)
            state = _export(conversation) if conversation is not None else None
            self._remove(identifier, "released")
        return state

    def unanswered(self, state: Any, message: str) -> dict:
        """``state`` with ``message`` added to its history as not yet answered, the way the
        store keeps a message a newer one overtook: the next turn reads it with its own."""
        result = clean_state(state)
        entry = {"question": self._safe(_message(message))[:MESSAGE_LIMIT], "answer": "",
                 "shown": []}
        result["history"] = [*result["history"], entry][-HISTORY_LIMIT:]
        return result


class MessengerEngine:
    """Turns of the assistant for Messenger chats; one turn per chat at a time, any number of
    chats at once (the store's four workers run them)."""

    def __init__(self, store: MessengerConversations, catalog_store: Any, *, content: Any = None,
                 poll_seconds: float = POLL_SECONDS, closers: tuple = ()) -> None:
        self.store, self.catalog_store, self.content = store, catalog_store, content
        self.poll_seconds = poll_seconds
        self._closers = tuple(closers)
        self._closing = threading.Event()
        self._preparing = threading.Lock()

    def prepare(self) -> bool:
        """Get the next turn's slow parts ready off its path: the catalogue index, and the
        provider reading the fixed prompt into its cache. Call it when a customer's message
        opens the quiet window before a turn, so the turn finds both ready. Best effort, in the
        background, one at a time; the engine itself skips the provider request when the model
        was used in the last four minutes. False when one is already running or there is
        nothing to run."""
        task = getattr(getattr(self.store, "service", None), "prepare", None)
        if not callable(task) or self._closing.is_set() or not self._preparing.acquire(False):
            return False

        def run() -> None:
            try:
                task()
            except Exception as exc:  # An optimisation: the turn does the work itself.
                LOGGER.info("messenger_prepare_skipped %s", type(exc).__name__)
            finally:
                self._preparing.release()

        threading.Thread(target=run, name="messenger-prepare", daemon=True).start()
        return True

    @property
    def ready(self) -> bool:
        """Whether the catalogue the assistant searches has loaded. Before that every turn ends
        in ``DATA_UNAVAILABLE``, so the worker waits rather than answering with the fallback."""
        return getattr(self.catalog_store, "loaded", True) is not False

    @property
    def closing(self) -> bool:
        """Whether ``close`` was called. A turn it cut short failed by our own doing (its
        conversation was removed under it), so no fallback is sent for it."""
        return self._closing.is_set()

    def shop_phone(self) -> str:
        """The phone from the dashboard's brand settings, for the fallback and cap notices;
        '' when none is set or the settings cannot be read."""
        try:
            phone = self.content.snapshot()["sections"]["brand"].get("phone")
        except Exception:
            return ""
        return " ".join(phone.split()) if isinstance(phone, str) else ""

    def respond(self, state: Any, text: Any, *, choice: int | None = None,
                language: str = "auto", timeout: float = TURN_SECONDS,
                cancel: threading.Event | None = None) -> EngineResult:
        """One turn of the assistant on ``text`` for the chat whose saved state is ``state``
        (None for a new chat). Never raises.

        ``text`` is everything the customer wrote that is still unanswered, oldest first, one
        message per line. ``choice`` is the number of the answer they picked
        (``messenger_render.parse_choice``). ``language`` 'auto' lets the engine keep the
        conversation's language and the model follow the customer's. The call waits up to
        ``timeout`` seconds, or until ``cancel`` is set; either way the turn is stopped and
        nothing it would still produce is used."""
        prior = clean_state(state)
        fallback = prior.get("language") or "ka"
        message = _message(text)
        if not message:
            return _result("failed", prior, prior, language=fallback, error_code="EMPTY_MESSAGE")
        if self._closing.is_set():
            unsent = compact(self.store.unanswered(prior, message))
            return _result("failed", unsent, unsent, language=fallback, error_code="CHAT_CLOSED")
        try:
            identifier, turn_id, unsent = self.store.begin(
                prior, message, language=_language(language), choice=_choice(choice))
        except Exception as exc:
            code = exc.code if isinstance(exc, ConversationError) else "CHAT_UNAVAILABLE"
            if not isinstance(exc, ConversationError):
                LOGGER.warning("messenger_engine_failed stage=start error=%s", type(exc).__name__)
            unsent = compact(self.store.unanswered(prior, message))
            return _result("failed", unsent, unsent, language=fallback, error_code=code)
        unsent = compact(unsent)
        try:
            outcome, payload = self._wait(identifier, turn_id, timeout, cancel)
            final = self.store.release(identifier) if outcome == "done" else None
        except Exception as exc:
            code = exc.code if isinstance(exc, ConversationError) else "CHAT_UNAVAILABLE"
            if not isinstance(exc, ConversationError):
                LOGGER.warning("messenger_engine_failed stage=wait error=%s", type(exc).__name__)
            return _result("failed", unsent, unsent, language=fallback, error_code=code)
        finally:
            self.store.delete(identifier)  # stops a turn still running; a no-op once released
        if outcome != "done":
            return _result(outcome, unsent, unsent, language=fallback,
                           error_code="TIMEOUT" if outcome == "timeout" else "")
        try:
            return self._finished(payload, final, unsent, prior, message)
        except Exception as exc:  # A reply that cannot be read is not sent.
            LOGGER.warning("messenger_engine_failed stage=read error=%s", type(exc).__name__)
            return _result("failed", unsent, unsent, language=fallback,
                           error_code="CHAT_UNAVAILABLE")

    def _wait(self, identifier: str, turn_id: str, timeout: Any,
              cancel: threading.Event | None) -> tuple[str, dict | None]:
        """Poll the turn until it ends: ('done', payload), ('cancelled', None) or
        ('timeout', None). Raises ``ConversationError`` when the conversation is gone."""
        try:
            seconds = float(timeout)
        except (TypeError, ValueError):
            seconds = TURN_SECONDS
        if not math.isfinite(seconds):
            seconds = TURN_SECONDS
        deadline = time.monotonic() + max(0.0, seconds)
        waiter = cancel if cancel is not None else self._closing
        while True:
            if cancel is not None and cancel.is_set():
                return "cancelled", None
            payload = self.store.get(identifier, turn_id, 0, CARD_LIMIT)
            if payload.get("status") != "RUNNING":
                return "done", payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            if self._closing.is_set():
                raise ConversationError("CHAT_CLOSED", 503)
            waiter.wait(min(self.poll_seconds, remaining))

    def _finished(self, payload: dict, final: dict | None, unsent: dict, prior: dict,
                  message: str) -> EngineResult:
        engine_status = payload.get("engine_status")
        engine_status = engine_status if isinstance(engine_status, str) else ""
        warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
        public = public_turn(payload, self.catalog_store.current(), said=_said(prior, message))
        if engine_status in ACCOUNT_STATUSES:
            status, code = "failed", engine_status
        elif "TURN_DEADLINE" in warnings:  # the store's own time limit ended the turn
            status, code = "timeout", "TURN_DEADLINE"
        elif "CHAT_UNAVAILABLE" in warnings:  # the engine raised; the store wrote an apology
            status, code = "failed", "CHAT_UNAVAILABLE"
        elif engine_status == "TIMEOUT":
            status, code = "timeout", "TIMEOUT"
        elif payload.get("status") == "FAILED":
            status, code = "failed", engine_status or "FAILED"
        elif not (public["answer"].strip() or public["cards"]):
            status, code = "failed", "EMPTY_ANSWER"
        else:
            status, code = "ok", ""
        # The engine wrote its record of the turn (the customer's words and its reply) into the
        # conversation only when it reported a status; a turn the deadline or a fault ended
        # left the state as it was, so the message is kept as unanswered instead.
        state = compact(final) if engine_status and final is not None else unsent
        usage = payload.get("usage")
        # The customer's own words stand in for a note the model did not write: redacted like
        # everything else the engine stores, and never kept for a turn that sent no reply.
        alerts = turn_alerts(payload, _redact(message)) if status == "ok" else TurnAlerts()
        return EngineResult(status=status, public=public, state=state,
                            engine_status=engine_status, language=public["language"],
                            usage=_json_safe(usage) if isinstance(usage, dict) else {},
                            error_code=code, unsent_state=unsent, alerts=alerts,
                            cause="" if status == "ok" else _cause(payload))

    def close(self) -> None:
        """Stop the store and everything ``build_engine`` started (catalogue poller, search
        coordinator, chat log, the model client)."""
        self._closing.set()
        self.store.close()
        for target in self._closers:
            for name in ("stop", "close"):
                method = getattr(target, name, None)
                if callable(method):
                    with contextlib.suppress(Exception):
                        method()
                    break


class ShopFacts:
    """The shop's brand and services for ``SiteContent``, read with the worker's own login.

    ``SiteRepository.load_content`` also reads ``garage.media``, which ``garage_messenger`` may
    not read; ``SiteContent`` would swallow that refusal and quote its packaged defaults, which
    have no phone. This reads the two tables migration 0013 opens to the worker and nothing
    else. The assistant's ``shop_info`` quotes no photos, so none are handed on."""

    def __init__(self, database: Any) -> None:
        self.database = database

    @property
    def configured(self) -> bool:
        return bool(getattr(self.database, "configured", False))

    def load_content(self) -> dict:
        with self.database.connect() as connection:
            sections = {row["section_key"]: row for row in connection.execute(
                "SELECT section_key, document, version, updated_at FROM garage.site_sections")}
            services = connection.execute(
                sql.SQL("SELECT {} FROM garage.services ORDER BY position, service_id")
                .format(sql.SQL(SERVICE_COLUMNS))).fetchall()
        return {"sections": sections, "services": services, "media": []}


def data_directory(meta_settings: Any) -> Path:
    """Where the Messenger engine keeps its files: ``MESSENGER_DATA_DIR`` (default
    ``data/messenger``), a relative path read from the repository root."""
    path = Path(str(getattr(meta_settings, "data_dir", "") or "data/messenger")).expanduser()
    return path if path.is_absolute() else ROOT / path


def _resolved(value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def remove_old_chat_log(folder: Path, in_use: tuple = ()) -> int:
    """Delete the chat log an earlier version kept in ``folder``, with its SQLite side files:
    customers' words under random session ids, which the dashboard's Delete data cannot reach
    and which nothing prunes any more. A log the application itself writes (``in_use``: its
    ``chat_log_path`` and ``site_chat_log_path``) is never touched. How many files went."""
    stale = folder / OLD_CHAT_LOG_FILE
    if stale.resolve() in {_resolved(path) for path in in_use if path}:
        return 0
    removed = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(f"{stale}{suffix}").unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:  # left for the operator; the engine itself never needs it
            LOGGER.warning("messenger_old_chat_log_kept error=%s", type(exc).__name__)
            continue
        removed += 1
    if removed:
        LOGGER.info("messenger_old_chat_log_removed files=%d", removed)
    return removed


def build_engine(meta_settings: Any, app_settings: Any = None) -> MessengerEngine:
    """The worker's engine, built the way ``api.create_app`` and ``site.install_site`` build the
    website's, with the public chat widget's configuration (``site_chat.build_public_chat``): no
    paid catalogue, no Motorium, no learning, the public audience, and web research only where
    ``AUTOMOTIVE_RESEARCH`` gives it to the public widget too (``all``).

    The assistant engine is required whatever ``AUTOMOTIVE_CHAT_ENGINE`` says, and its files
    (source cache, research cache) live under ``meta_settings.data_dir``, so it never shares
    one with the application on port 8767. It keeps no chat log. The shop's facts come through
    ``ShopFacts`` on ``MESSENGER_DATABASE_URL``; the catalogue, the VIN decoder and the crawled
    fitments
    through the application's own runtime login, as on the website. Nothing here sends a request;
    the catalogue poller starts loading at once."""
    from automotive.assistant_extras import extras_for, research_audiences
    from automotive.chat import ChatService
    from automotive.config import Settings
    from automotive.meta_db import MetaDatabase
    from automotive.repository import AutomotiveRepository
    from automotive.rules import load_rules
    from automotive.site_catalog import CatalogStore
    from automotive.site_chat import build_public_chat
    from automotive.site_content import SiteContent
    from automotive.vin import VinService

    base = app_settings if app_settings is not None else Settings.from_env()
    folder = data_directory(meta_settings)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    remove_old_chat_log(folder, (getattr(base, "chat_log_path", ""),
                                 getattr(base, "site_chat_log_path", "")))
    mode = str(getattr(base, "assistant_research", "off") or "off")
    audiences = research_audiences(mode)
    research = "public" in audiences
    settings = replace(
        base, chat_engine="assistant", chat_log_path="", knowledge_enabled=False,
        motorium_enabled=False, catalog_api_key="",
        automotive_cache_path=str(folder / CACHE_FILE),
        # No chat log for Messenger: it would keep every customer's words under a random
        # session id, out of reach of the dashboard's "Delete data" (the chats themselves live
        # in garage.messenger_messages, which that action erases).
        site_chat_log_path="",
        # Research that could only run for staff would build a service and a cache file for
        # nothing: Messenger is public.
        assistant_research=mode if research else "off",
        assistant_research_cache_path=str(folder / RESEARCH_CACHE_FILE))
    rules = load_rules()
    repository = AutomotiveRepository(settings.database_url)
    vin_service = VinService(repository, settings.vin_hmac_key)
    chat_service = ChatService(repository, vin_service, settings, rules=rules)
    catalog_store = CatalogStore(settings.database_url, rules)
    chat_service.catalog_store = catalog_store
    shop = MetaDatabase(getattr(meta_settings, "database_url", ""),
                        application_name="garage-messenger-shop")
    content = SiteContent(ShopFacts(shop))
    # The owner's prompt edits (dashboard > Prompts), read with the worker's own login, which
    # migration 0019 lets read garage.llm_prompts and nothing more of them.
    from automotive.prompt_library import PromptStore

    chat_service.prompt_texts = PromptStore(MetaDatabase(
        getattr(meta_settings, "database_url", ""), application_name="garage-messenger-prompts"))
    chat_service.assistant_extras = extras_for(settings, chat_service, repository,
                                               SimpleNamespace(content=content))
    public = build_public_chat(settings, chat_service, repository, vin_service, catalog_store,
                               SimpleNamespace(limit=0))
    # Only its search coordinator is used (it made no chat log, see above): the visitor store
    # it also made is closed unused.
    public.store.close()
    search_service = public.store.search_service
    store = MessengerConversations(chat_service, search_service, chat_log=public.chat_log)
    engine = MessengerEngine(store, catalog_store, content=content,
                             closers=(catalog_store, search_service, chat_service))
    catalog_store.start()
    LOGGER.info(
        "messenger_engine_ready audience=public engine=assistant data_dir=%s cache=%s "
        "chat_log=%s research=%s research_cache=%s research_audiences=%s shop_facts=%s "
        "model_key=%s", folder, settings.automotive_cache_path,
        settings.site_chat_log_path if public.chat_log is not None else "off",
        "on" if research else "off",
        settings.assistant_research_cache_path if research else "-",
        ",".join(audiences) or "none", "database" if shop.configured else "defaults",
        "set" if settings.anthropic_api_key else "missing")
    return engine


# -- the saved state -------------------------------------------------------------------------


def clean_state(state: Any) -> dict:
    """``state`` as ``restore`` takes it: every key of ``CREATED_STATE_KEYS``, of the type the
    store keeps (a missing or wrong value becomes what ``create()`` starts with), the engine's
    keys when present and of their type, nothing else, as a JSON-ready deep copy. Never
    raises."""
    source = state if isinstance(state, dict) else {}
    vin = source.get("vin")
    try:
        vin = validate_vin(vin) if isinstance(vin, str) else None
    except ValueError:
        vin = None
    language, revision = source.get("language"), source.get("vehicle_revision")
    category = source.get("category")
    clean: dict[str, Any] = {
        "vehicle": _mapping(source.get("vehicle")),
        "vin": vin,
        "category": category[:160] if isinstance(category, str) else "",
        "history": [entry for entry in _sequence(source.get("history"))
                    if isinstance(entry, dict)][-HISTORY_LIMIT:],
        "language": language if language in ("ka", "en") else None,
        "pending": source["pending"] if isinstance(source.get("pending"), dict) else None,
        "shown": _sequence(source.get("shown")),
        "vehicle_revision": revision if isinstance(revision, int)
        and not isinstance(revision, bool) and revision >= 0 else 0,
        "customer_stated": _mapping(source.get("customer_stated")),
        "resolution_candidates": _sequence(source.get("resolution_candidates")),
        "selected_configuration": _mapping(source.get("selected_configuration")),
        "unresolved_fields": _sequence(source.get("unresolved_fields")),
        "clarification_choices": _sequence(source.get("clarification_choices")),
    }
    if isinstance(source.get("memory"), dict):
        clean["memory"] = source["memory"]
    if isinstance(source.get("vin_typed"), bool):
        clean["vin_typed"] = source["vin_typed"]
    if isinstance(source.get("identity"), dict):
        clean["identity"] = source["identity"]
    return _json_safe(clean)


def state_size(state: Any) -> int:
    """Bytes of ``state`` as JSON text with ``", "`` and ``": "`` separators and UTF-8 letters:
    near enough to how PostgreSQL prints jsonb, which the ``engine_state`` CHECK measures."""
    return len(json.dumps(state, ensure_ascii=False, separators=(", ", ": ")).encode("utf-8"))


def compact(state: dict, limit: int = STATE_BYTES) -> dict:
    """``state`` brought under ``limit`` bytes, unchanged when it already is.

    It is cut where the assistant loses least, in this order: the texts of the store's history
    (the assistant only checks that an answer exists and reads 500 characters of a question),
    the oldest row handles nothing still names, the oldest turns of the transcript, the oldest
    history entries, the rows remembered for an open question, and at last everything in the
    engine's memory but the car, the notes and the open question."""
    if state_size(state) <= limit:
        return state
    result = copy.deepcopy(state)
    for step in (_shorter_history, _fewer_handles, _fewer_turns, _fewer_entries, _fewer_rows,
                 _bare_memory):
        while step(result):
            if state_size(result) <= limit:
                return result
    LOGGER.warning("messenger_state_reset bytes=%d", state_size(result))
    kept = {key: result.get(key) for key in ("vehicle", "vin", "language", "vehicle_revision",
                                             "vin_typed", "identity") if key in result}
    return clean_state(kept)


def record_shop_reply(state: Any, shop_text: Any, *, customer_text: Any = "") -> dict:
    """``state`` with an exchange the engine did not write: the shop's own reply (an echo sent
    from the page inbox or any app but the bot's) to what the customer had written.

    The assistant reads earlier turns from its memory's transcript, not from the store's
    history, so the shop's words go there as the reply that was given, after the customer's
    messages still waiting in the history and ``customer_text`` (the ones the worker holds that
    the state does not, oldest first). The history gets an answered entry, so those messages no
    longer count as unanswered. A VIN in ``customer_text`` becomes the chat's car, as it would
    have in a turn. A shop message with no customer words before it is added to the last reply
    of the transcript. VINs are kept out of all text."""
    result = clean_state(state)
    shop = _redact(_message(shop_text))[:TRANSCRIPT_TEXT]
    if not shop:
        return result
    raw = _message(customer_text)
    said = _redact(raw)
    history = result["history"]
    waiting: list[str] = []
    for entry in reversed(history):
        if _plain_text(entry.get("answer")):
            break
        if _plain_text(entry.get("question")):
            waiting.append(_plain_text(entry.get("question"))[:500])
    asked = "\n".join([*reversed(waiting), *([said] if said else [])]).strip()
    history.append({"question": said[:MESSAGE_LIMIT], "answer": shop, "note": "", "shown": []})
    result["history"] = history[-HISTORY_LIMIT:]
    memory = dict(result.get("memory") or {})
    transcript = [dict(turn) for turn in memory.get("transcript") or [] if isinstance(turn, dict)]
    if asked:
        transcript.append({"user": asked[:TRANSCRIPT_TEXT], "assistant": shop})
    elif transcript:
        last = transcript[-1]
        last["assistant"] = f"{_plain_text(last.get('assistant'))}\n{shop}".strip()[
            -TRANSCRIPT_TEXT:]
    if transcript:
        memory["transcript"] = transcript[-TRANSCRIPT_TURNS:]
        result["memory"] = memory
    vins = set()
    for found in VIN_PATTERN.findall(raw):
        with contextlib.suppress(ValueError):
            vins.add(validate_vin(found))
    if len(vins) == 1 and next(iter(vins)) != result.get("vin"):
        # What ConversationStore._submit_assistant does with a new VIN in a message.
        result.update(vin=next(iter(vins)), vin_typed=True, vehicle={},
                      vehicle_revision=result["vehicle_revision"] + 1)
    return compact(_json_safe(result))


# -- helpers ---------------------------------------------------------------------------------


class _Unprepared:
    """A chat service with everything but ``prepare`` (see ``MessengerConversations``)."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def __getattr__(self, name: str) -> Any:
        if name == "prepare":
            raise AttributeError(name)
        return getattr(self._service, name)


def _export(conversation: dict) -> dict:
    state = {key: conversation.get(key) for key in CREATED_STATE_KEYS}
    state["history"] = list(conversation.get("history") or [])
    for key in ENGINE_STATE_KEYS:
        if key in conversation:
            state[key] = conversation[key]
    return clean_state(state)


def _cause(payload: Any) -> str:
    """The error class and the engine step of a turn that ended in an error ("ValueError@model"),
    as the engine recorded them (``diagnostics``), or ''. Code names only, never a message."""
    diagnostics = payload.get("diagnostics") if isinstance(payload, dict) else None
    if not isinstance(diagnostics, dict):
        return ""
    error = re.sub(r"[^A-Za-z0-9_]", "", str(diagnostics.get("error") or ""))[:60]
    stage = re.sub(r"[^a-z]", "", str(diagnostics.get("stage") or ""))[:20]
    return f"{error}@{stage}" if error and stage else error


def _result(status: str, state: dict, unsent: dict, *, language: Any,
            error_code: str = "") -> EngineResult:
    return EngineResult(status=status, public={}, state=state, engine_status="",
                        language=language if language in ("ka", "en") else "ka", usage={},
                        error_code=error_code, unsent_state=unsent)


def _said(state: dict, message: str) -> str:
    """The customer's words in this chat, as ``public_turn`` compares a stock number with them:
    one they typed themselves stays in the reply. The history's questions are theirs."""
    words = [entry.get("question") for entry in state.get("history") or []
             if isinstance(entry, dict)]
    return " ".join(plain(text) for text in [*words, message]
                    if isinstance(text, str))[-SAID_LIMIT:]


def _message(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return _SURROGATE.sub("�", text.replace("\x00", "")).strip()[:MESSAGE_LIMIT]


def _plain_text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _language(value: Any) -> str:
    return value if value in ("ka", "en", "auto") else "auto"


def _choice(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and 1 <= value <= 99 else None


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _sequence(value: Any) -> list:
    return list(value) if isinstance(value, list | tuple | deque) else []


def _json_safe(value: Any, depth: int = 0) -> Any:
    """A deep copy PostgreSQL's jsonb takes: text without NUL or lone surrogates, finite
    numbers, string keys, lists for tuples and deques, anything else as text."""
    if depth > 40:
        return None
    if isinstance(value, str):
        return _SURROGATE.sub("�", value.replace("\x00", ""))
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {_json_safe(str(key), depth + 1): _json_safe(item, depth + 1)
                for key, item in value.items()}
    if isinstance(value, list | tuple | deque):
        return [_json_safe(item, depth + 1) for item in value]
    return _json_safe(str(value), depth + 1)


def _shorter_history(state: dict) -> bool:
    changed = False
    for entry in state.get("history") or []:
        if not isinstance(entry, dict):
            continue
        for key, limit in (("answer", 200), ("question", 500)):
            value = entry.get(key)
            if isinstance(value, str) and len(value) > limit:
                entry[key] = value[:limit]
                changed = True
        if entry.pop("note", None) is not None:
            changed = True
    return changed


def _memory(state: dict) -> dict:
    memory = state.get("memory")
    return memory if isinstance(memory, dict) else {}


def _fewer_handles(state: dict) -> bool:
    """Drop the oldest half of the row handles that the notes, the open question and the next
    steps do not name (the assistant resolves those names through them)."""
    memory = _memory(state)
    handles = memory.get("handles")
    if not isinstance(handles, dict) or not handles:
        return False
    named = set(HANDLE.findall(json.dumps(
        [memory.get("notes"), memory.get("question"), memory.get("suggestions")],
        ensure_ascii=False)))
    spare = [handle for handle in handles if handle not in named]
    if not spare:
        return False
    for handle in spare[:max(1, len(spare) // 2)]:
        del handles[handle]
    return True


def _fewer_turns(state: dict) -> bool:
    transcript = _memory(state).get("transcript")
    if isinstance(transcript, list) and len(transcript) > 2:
        del transcript[0]
        return True
    return False


def _fewer_entries(state: dict) -> bool:
    history = state.get("history")
    if isinstance(history, list) and len(history) > 1:
        del history[0]
        return True
    return False


def _fewer_rows(state: dict) -> bool:
    memory, changed = _memory(state), False
    for key in ("seen", "held", "sourced", "shown"):
        rows = memory.get(key)
        if isinstance(rows, list) and rows:
            del rows[len(rows) // 2:]
            changed = True
    return changed


def _bare_memory(state: dict) -> bool:
    memory = _memory(state)
    kept = {key: memory[key] for key in ("language", "car_label", "notes", "question", "phone",
                                        "followup", "engine_choices")
            if key in memory}
    if "memory" not in state or kept == memory:
        return False
    state["memory"] = kept
    return True
