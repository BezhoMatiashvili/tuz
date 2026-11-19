"""Public face of the parts assistant: the same chat engine behind a capped facade.

Visitors get their own ``ConversationStore`` and search coordinator (no paid catalogue, no
staff capacity), every conversation id is checked here before the store is touched, and a
turn leaves this module as answer text, the answers and next steps to tap, and cards
rebuilt from a whitelist. Stock counts, code fields, warehouse names, sources, the vehicle
record and token usage stay with the staff workspace, and the shop's stock numbers are
taken out of the sentences.
"""

from __future__ import annotations

import html
import inspect
import logging
import re
import time
import weakref
from threading import Lock
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParameter
from pydantic import BaseModel, ConfigDict, Field

from automotive import chat_contract
from automotive.chat import VIN_PATTERN
from automotive.conversations import ConversationError, ConversationStore
from automotive.replies import make_name
from automotive.site_auth import ip_key, wide_key
from automotive.site_strings import STRINGS

LOGGER = logging.getLogger(__name__)
AVAILABILITY = {"IN_STORE": "in_stock", "WITHIN_1_2_HOURS": "soon",
                "UNAVAILABLE": "unavailable"}
ConversationId = Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{43}$")]
TurnId = Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{32}$")]
# 5W40 and 75W140 turn up as codes in this catalogue, and are also plain words in a sentence
# about oil.
OIL_GRADE = re.compile(r"\d{1,2}W\d{2,3}")
SAID_LIMIT = 2000
CODE_LIMIT = 400  # stock numbers taken out of one answer: more than any turn lists
CAR_LIMIT = 60
SUGGESTION_LIMIT = 4  # next steps under one reply
# Why an engine that gives no reason of its own offered a part: its ``basis``, a closed
# set, put into words (Georgian, English). The value itself stays with staff.
REASONS = {
    "fitment": ("დადასტურებულია ამ მანქანისთვის", "Confirmed for this car"),
    "staff_rule": ("დადასტურებულია ამ მანქანისთვის", "Confirmed for this car"),
    "verified_oil": ("აკმაყოფილებს ძრავის მოთხოვნას", "Meets the engine's oil requirement"),
    "code": ("ემთხვევა ამ მანქანის ნაწილის ნომერს", "Matches the part number for this car"),
    "typed_code": ("თქვენ მიერ მოწერილი ნომერია", "This is the number you sent"),
    "oil_grade_brand": ("რეკომენდებული სიბლანტე და ბრენდი", "The recommended grade and brand"),
    "oil_grade": ("იგივე სიბლანტე, სხვა ბრენდი", "Same grade, another brand"),
    "engine": ("ემთხვევა ძრავის ოჯახს", "Matches the engine family"),
    "name_year": ("კატალოგში მითითებულია ამ მოდელსა და წელზე",
                  "Listed for this model and year"),
    "name": ("კატალოგში მითითებულია ამ მოდელზე", "Listed for this model"),
}
# The confidence label a card carries already says these two: the line is not repeated.
ECHOED = {"code": "confirmed", "name_year": "listed"}
# The engine's warning that the stock figures behind its cards are getting old, and the
# interface string that says so (the one the part search shows in the same state).
STALE_WARNING, STALE_STRING = "STOCK_MAY_BE_STALE", "js.search.confirm"


def plain(text: str) -> str:
    """Letters and digits only, upper case: how a part number is compared."""
    return re.sub(r"[^0-9A-Za-z]", "", text).upper()


def card_codes(cards: Any, catalog: Any) -> set[str]:
    """The shop's own stock numbers of the listed products, as written and without
    separators.

    Only the ``product_code`` field is read, and only tokens that are plainly a number:
    six to forty letters and digits, at least three of them digits, and no oil grade
    inside. The other code fields are not used. They hold manufacturers' reference
    numbers, which item names show anyway, mixed with product descriptions and oil
    approvals (``MOBIL-1 0W40 (LL01; 229.5)``), and taking those words out of a sentence
    would damage it.
    """
    found: set[str] = set()
    for card in cards or []:
        product = card.get("product") if isinstance(card, dict) else None
        if not isinstance(product, dict):
            continue
        item = catalog.by_id.get(str(product.get("source_product_id") or ""))
        for value in (product.get("product_code"), item.code if item is not None else None):
            if not isinstance(value, str):
                continue
            for piece in re.split(r"[\s,;]+", value):
                bare = plain(piece)
                if (6 <= len(bare) <= 40 and sum(letter.isdigit() for letter in bare) >= 3
                        and not OIL_GRADE.search(bare)):
                    found.update((piece, bare))
        if len(found) >= CODE_LIMIT:
            break
    return found


def without_codes(text: str, codes: Any) -> str:
    """``text`` with those stock numbers taken out, and the gaps they leave tidied."""
    wanted = sorted({code for code in codes if code}, key=len, reverse=True)
    if not text or not wanted:
        return text
    # A number goes together with the comma that only separated it from the next one.
    pattern = ("(?<![0-9A-Za-z])(?:" + "|".join(map(re.escape, wanted))
               + r")(?![0-9A-Za-z])(?:\s*[,;])?")
    cleaned, removed = re.subn(pattern, "", text, flags=re.IGNORECASE)
    if not removed:
        return text
    cleaned = re.sub(r"[(\[]\s*[)\]]", "", cleaned)  # brackets that held only the number
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    return re.sub(r"^[\s,;:]+", "", cleaned).rstrip()


def _reason(card: dict, level: str, vehicle: Any, language: str, hidden: Any) -> str:
    """Why a part is offered, in one line a visitor can read.

    The engine's own sentence when it gives one, without the stock numbers in ``hidden``.
    An engine that gives none has its ``basis`` put into words from ``REASONS``; apart
    from a number the visitor typed, those words speak of the car, so they need one.
    """
    stated = card.get("reason")
    if isinstance(stated, str) and stated.strip():
        return chat_contract.reason({"reason": without_codes(html.unescape(stated), hidden)})
    basis = card.get("basis")
    if not isinstance(basis, str) or basis not in REASONS:
        return ""
    if basis != "typed_code":
        known = isinstance(vehicle, dict) and str(vehicle.get("model") or "").strip()
        # An unreviewed fitment candidate is marked "check": it is not "confirmed" in words.
        if not known or ECHOED.get(basis) == level or (basis == "fitment"
                                                       and level != "confirmed"):
            return ""
    return REASONS[basis][1 if language == "en" else 0]


def public_card(card: dict, catalog: Any, *, vehicle: Any = None, language: str = "ka",
                hidden: Any = (), groups: Any = None) -> dict:
    """Name, vehicle text, brand, price and an availability label, then why it is offered:
    the group it is listed under, a reason and a confidence level. Nothing else.

    ``groups`` maps the engine's group ids to the ones the turn hands out, ``vehicle`` is
    the turn's car (read here, never passed on) and ``language`` the reply's.
    """
    product = card.get("product") if isinstance(card.get("product"), dict) else {}
    item = catalog.by_id.get(str(product.get("source_product_id") or ""))
    level = chat_contract.confidence(card, vehicle)
    return {
        # Some source cells arrive HTML-escaped; the widget renders text, not markup.
        "name": html.unescape(str(product.get("product_name") or ""))[:200],
        "fits": html.unescape(str(product.get("product_model") or ""))[:200],
        "brand": html.unescape(str(product.get("brand") or ""))[:80],
        "price": item.price if item and item.price else None,
        "availability": AVAILABILITY.get(card.get("availability_status"), "unknown"),
        "group": (groups or {}).get(chat_contract.group(card), ""),
        "reason": _reason(card, level, vehicle, language, hidden),
        "confidence": level,
    }


def _choice(option: Any, hidden: Any = ()) -> dict | None:
    """A reply the visitor can tap. One that names a hidden stock number is left out:
    without the number it would no longer say what it stands for."""
    label = value = None
    if isinstance(option, str):
        label = value = option
    elif isinstance(option, dict):
        label = option.get("label") or option.get("value")
        value = option.get("value") or option.get("label")
    if not (isinstance(label, str) and isinstance(value, str) and label.strip()):
        return None
    if without_codes(label, hidden) != label or without_codes(value, hidden) != value:
        return None
    return {"label": label[:80], "value": value[:200]}


def _car(payload: dict, hidden: Any = ()) -> str:
    """A short label of the car a turn is about, for the line under the chat's title:
    make, model and year, and nothing else of the vehicle record.

    The engine's own ``car_label`` is used as it stands unless it carries what stays
    with staff. One that names the record's engine code, trim or VIN, like a turn with
    no label at all, gets the label put together here from make, model and year (once
    make and model are both known). Anything VIN-shaped goes in either case.
    """
    vehicle = payload.get("vehicle") if isinstance(payload.get("vehicle"), dict) else {}
    make, model = make_name(vehicle.get("make")), str(vehicle.get("model") or "").strip()
    year = str(vehicle.get("model_year") or "").strip()
    built = (" ".join((make, model, year if re.fullmatch(r"(?:19|20)\d\d", year) else ""))
             if make and model else "")
    stated = payload.get("car_label")
    label = built
    if isinstance(stated, str) and stated.strip():
        named = set(f"{make} {model}".upper().split())
        private = {form for key in ("engine_code", "vin", "trim") if vehicle.get(key)
                   for form in (str(vehicle[key]), plain(str(vehicle[key])))
                   # A trim that is a word of the model's own name ("Sport") is not taken out.
                   if key != "trim" or not set(str(vehicle[key]).upper().split()) <= named}
        stated = html.unescape(stated)
        label = without_codes(VIN_PATTERN.sub("", stated), private)
        if label != stated and built:
            label = built
    return " ".join(without_codes(label, hidden).split())[:CAR_LIMIT].strip()


def _note(payload: dict, cards: list, language: str, hidden: Any = ()) -> str:
    """The one caveat under a reply: the engine's own when it writes one.

    The assistant engine (the one that states how a turn ended, ``outcome``) writes no
    note. That the stock figures behind its cards are getting old it reports only as a
    warning code, which no screen draws, so the caveat is put into words here. The older
    engines word their notes themselves and say each once in a conversation.
    """
    stated = without_codes(html.unescape(str(payload.get("note") or "")), hidden)[:400]
    warnings = payload.get("warnings")
    if stated or not cards or not payload.get("outcome") or not isinstance(warnings, list):
        return stated
    return STRINGS.get(language, {}).get(STALE_STRING, "") if STALE_WARNING in warnings else ""


def public_turn(payload: dict, catalog: Any, *, said: str = "", listed: Any = None) -> dict:
    """The widget's view of a turn. Built key by key so new engine fields never leak.

    The engine's sentences name a listed product by the shop's stock number, which means
    nothing to a visitor and which the cards leave out, so those numbers are taken out of
    the text and the choices too. ``listed`` is every card of the turn when ``payload``
    carries one page of them. ``said`` is what the visitor typed, as ``plain`` text: a
    number they wrote themselves is left alone.

    What the reply asks and suggests is read through ``chat_contract``, as the staff route
    reads it. ``question`` holds the answers to tap and is None when the reply asks
    nothing; the place of an answer in it is the number its tap sends back. ``suggestions``
    are the next steps, ``groups`` the headings the cards are listed under (with ids made
    here, never the engine's) and ``car`` the short label of the car. ``quick_replies``
    repeats the answers, else the suggestions, for a page loaded before those existed.
    """
    # What a running turn has to wait for: the sources an older engine reads, or the
    # searches the assistant engine runs (its entries name a ``stage``). They are counted,
    # never named: a source's name and the words of a search stay with staff.
    sources = [entry for entry in payload.get("progress") or []
               if isinstance(entry, dict) and (entry.get("source_id") or entry.get("stage"))]
    language = payload.get("language") if payload.get("language") in ("ka", "en") else "ka"
    cards = [card for card in payload.get("cards") or [] if isinstance(card, dict)]
    hidden = [code for code in card_codes(cards if listed is None else listed, catalog)
              if plain(code) not in said]

    def shown(option: dict) -> dict | None:
        return _choice(option, hidden)

    asked = chat_contract.question(payload, keep=shown)
    steps = chat_contract.suggestions(payload, SUGGESTION_LIMIT, keep=shown)
    headings: list[dict] = []
    ids: dict[str, str] = {}
    for entry in chat_contract.groups(payload):
        label = without_codes(html.unescape(entry["label"]), hidden).strip()
        if label:  # A heading that was only a stock number is no heading.
            ids[entry["id"]] = f"g{len(headings) + 1}"
            headings.append({"id": ids[entry["id"]], "label": label, "count": entry["count"]})
    vehicle = payload.get("vehicle")
    return {
        "status": str(payload.get("status") or "FAILED"),
        # The whole text, with the question it asks even where the engine kept that apart.
        "answer": without_codes(html.unescape(chat_contract.answer(payload)), hidden)[:4000],
        "note": _note(payload, cards, language, hidden),
        "language": language,
        "car": _car(payload, hidden),
        "question": asked,
        "suggestions": steps,
        "quick_replies": [dict(option) for option in (asked["options"] if asked else steps)],
        "groups": headings,
        "cards": [public_card(card, catalog, vehicle=vehicle, language=language,
                              hidden=hidden, groups=ids) for card in cards],
        "total": int(payload.get("total") or 0),
        "offset": int(payload.get("offset") or 0),
        "next_offset": payload.get("next_offset")
        if isinstance(payload.get("next_offset"), int) else None,
        "checked": sum(1 for entry in sources
                       if entry.get("status") not in ("RUNNING", "PENDING")),
        "checking": len(sources),
    }


_TAKES_CHOICE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _takes_choice(submit: Any) -> bool:
    """Whether a store's ``submit`` knows the ``choice`` keyword, asked once per function.

    The store is gaining that keyword. One that does not have it yet is sent the
    message alone, which carries the tapped answer as its text anyway.
    """
    function = getattr(submit, "__func__", submit)
    try:
        return _TAKES_CHOICE[function]
    except (KeyError, TypeError):
        pass
    try:
        known = any(
            parameter.kind is parameter.VAR_KEYWORD
            or (parameter.name == "choice" and parameter.kind in (
                parameter.KEYWORD_ONLY, parameter.POSITIONAL_OR_KEYWORD))
            for parameter in inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        known = False
    try:
        _TAKES_CHOICE[function] = known
    except TypeError:  # Not something a weak reference can point at: asked again next time.
        pass
    return known


class PublicChat:
    """Budgets and ownership in front of the visitor ConversationStore."""

    def __init__(self, store: Any, catalog_store: Any, daily: Any, *,
                 per_address_daily: int = 40, live_per_address: int = 3, inflight: int = 4,
                 inflight_per_address: int = 2, per_wide_daily: int | None = None,
                 clock: Any = time.monotonic, wall: Any = time.time) -> None:
        self.store, self.catalog_store, self.daily = store, catalog_store, daily
        self.per_address_daily, self.live_per_address = per_address_daily, live_per_address
        # The /64s of one IPv6 /48 (one end site) share twice one address's day, so changing
        # address inside it buys nothing more (site_auth.wide_key).
        self.per_wide_daily = (2 * per_address_daily if per_wide_daily is None
                               else per_wide_daily)
        self.inflight, self.inflight_per_address = inflight, inflight_per_address
        self.clock, self.wall = clock, wall
        self._lock = Lock()
        self._owned: dict[str, str] = {}
        self._idle: dict[str, float] = {}  # conversation id -> when the store lets it go
        self._said: dict[str, str] = {}  # conversation id -> its visitor's words, plain()
        self._running: dict[Any, tuple[float, str]] = {}  # turn id -> (deadline, address)
        self._sent: dict[tuple[int, str], int] = {}

    def _forget_finished(self) -> None:
        now = self.clock()
        for turn_id in [key for key, (deadline, _) in self._running.items()
                        if deadline <= now]:
            del self._running[turn_id]

    def _drop(self, identifier: str) -> None:
        """Forget a conversation. Call with the lock held."""
        for table in (self._owned, self._idle, self._said):
            table.pop(identifier, None)

    def _forget_expired(self) -> None:
        """Drop ids the store has let go of, so an abandoned chat stops counting against
        its address. The store's ttl is idle time. Call with the lock held."""
        now = self.clock()
        for identifier in [key for key, deadline in self._idle.items() if deadline <= now]:
            self._drop(identifier)

    def _renew(self, identifier: str) -> None:
        # A message or a poll restarts the store's idle timeout, so it restarts here too.
        self._idle[identifier] = self.clock() + getattr(self.store, "ttl_seconds", 600)

    def _keep_order(self, identifier: str) -> tuple[bool, float]:
        # First to go: a conversation nobody has written in, then the longest idle.
        return identifier in self._said, self._idle.get(identifier, 0.0)

    def create(self, address: str) -> dict:
        """A new conversation. Nobody is refused for lack of room: at its own limit an
        address gives up its least useful conversation, and a full store gives up the
        least useful one overall. Opening conversations and leaving them idle therefore
        shuts nobody out, and several people behind one address can all chat."""
        with self._lock:
            self._forget_expired()
            mine = sorted((identifier for identifier, owner in self._owned.items()
                           if owner == address), key=self._keep_order)
            dropped = mine[:max(0, len(mine) - self.live_per_address + 1)]
            for identifier in dropped:
                self._drop(identifier)
        for identifier in dropped:
            self.store.delete(identifier)
        try:
            created = self.store.create()
        except ConversationError as exc:
            if exc.code != "CONVERSATION_CAPACITY":
                raise
            with self._lock:
                oldest = min(self._owned, key=self._keep_order, default=None)
                if oldest is not None:
                    self._drop(oldest)
            if oldest is None:
                raise
            self.store.delete(oldest)
            created = self.store.create()
        with self._lock:
            self._owned[created["conversation_id"]] = address
            self._renew(created["conversation_id"])
            while len(self._owned) > 4 * max(1, getattr(self.store, "maximum", 40)):
                self._drop(next(iter(self._owned)))  # Ids the store already expired.
        return created

    def submit(self, address: str, identifier: str, message: str, language: str,
               choice: int | None = None) -> dict:
        """Start a turn. ``choice`` is the number of the answer the visitor tapped, when
        the message came from one of the buttons under a question."""
        day = int(self.wall() // 86400)
        with self._lock:
            # An id this facade did not issue never reaches the store: submit() would start
            # VIN look-ups before it noticed the conversation does not exist.
            self._forget_expired()
            if identifier not in self._owned:
                raise ConversationError("CONVERSATION_EXPIRED", 404)
            self._forget_finished()
            mine = sum(1 for _, owner in self._running.values() if owner == address)
            if len(self._running) >= self.inflight or mine >= self.inflight_per_address:
                raise ConversationError("CHAT_WORKER_CAPACITY", 429)
            self._sent = {key: count for key, count in self._sent.items() if key[0] == day}
            counted = [((day, address), self.per_address_daily)]
            wide = wide_key(address)
            if wide is not None:
                counted.append(((day, wide), self.per_wide_daily))
            if any(self._sent.get(key, 0) >= limit for key, limit in counted):
                raise ConversationError("CHAT_DAILY_LIMIT", 429)
            if not self.daily.take():
                raise ConversationError("CHAT_DAILY_LIMIT", 429)
            for key, _ in counted:
                self._sent[key] = self._sent.get(key, 0) + 1
            self._renew(identifier)
            self._said[identifier] = (self._said.get(identifier, "") + " "
                                      + plain(message))[-SAID_LIMIT:]
            # The slot is taken now, under the lock, so two messages arriving together
            # cannot both slip past the limits above.
            slot = (self.clock() + getattr(self.store, "turn_seconds", 75) + 5, address)
            reservation = object()
            self._running[reservation] = slot
        live = getattr(self.store, "live", None)
        if live is not None:
            try:
                live.seen(identifier, address)  # for the hourly cap on one address's alarms
            except Exception:
                LOGGER.debug("live_address_skipped")
        try:
            tapped = ({"choice": choice}
                      if choice is not None and _takes_choice(self.store.submit) else {})
            started = self.store.submit(identifier, message, language=language, **tapped)
        except BaseException as exc:
            with self._lock:
                self._running.pop(reservation, None)
                # No turn was started, so nothing is charged for it.
                for key, _ in counted:
                    if self._sent.get(key, 0) > 0:
                        self._sent[key] -= 1
                if isinstance(exc, ConversationError) and exc.code == "CONVERSATION_EXPIRED":
                    self._drop(identifier)
            refund = getattr(self.daily, "refund", None)
            if refund is not None:
                refund()
            raise
        with self._lock:
            self._running.pop(reservation, None)
            self._running[started["turn_id"]] = slot
        if started.get("canned"):
            # No model ran (staff switched the bot off), so the day's model budget is given
            # back. The address keeps the message on its own daily count: a muted visitor
            # cannot write without end, and every message is a row on the staff's screen.
            refund = getattr(self.daily, "refund", None)
            if refund is not None:
                refund()
        return {"turn_id": started["turn_id"], "status": started["status"],
                "language": started["language"]}

    def turn(self, identifier: str, turn_id: str, offset: int, limit: int) -> dict:
        with self._lock:
            self._forget_expired()
            if identifier not in self._owned:
                raise ConversationError("CONVERSATION_EXPIRED", 404)
            self._renew(identifier)
            said = self._said.get(identifier, "")
        payload = self.store.get(identifier, turn_id, offset, limit)
        if payload.get("status") != "RUNNING":
            with self._lock:
                self._running.pop(turn_id, None)
        listed, total = payload.get("cards") or [], payload.get("total")
        if ((payload.get("answer") or payload.get("note"))
                and isinstance(total, int) and total > len(listed)):
            # The sentence may name a product that is not on this page of cards.
            listed = self.store.get(identifier, turn_id, 0, 100).get("cards") or []
        return public_turn(payload, self.catalog_store.current(), said=said, listed=listed)

    def delete(self, identifier: str) -> None:
        with self._lock:
            owned = identifier in self._owned
            self._drop(identifier)
        if owned:
            self.store.delete(identifier)

    def close(self) -> None:
        self.store.close()
        search = getattr(self.store, "search_service", None)
        if getattr(self, "owns_search", False) and hasattr(search, "close"):
            search.close()
        chat_log = getattr(self, "chat_log", None)
        if chat_log is not None:
            chat_log.close()  # Drains queued records before exit, as the staff log does.
        live = getattr(self.store, "live", None)
        if live is not None and hasattr(live, "close"):
            live.close()  # Writes what is queued for the Live chats page, then stops.


def build_public_chat(settings: Any, chat_service: Any, repository: Any, vin_service: Any,
                      catalog_store: Any, daily: Any, search_service: Any = None,
                      live: Any = None) -> PublicChat:
    """A visitor store with its own coordinator: no paid catalogue, no Motorium, no learning.
    ``live`` is the Live chats recorder (``live_web.LiveRecorder``) or None."""
    owns_search = search_service is None
    if search_service is None:
        from automotive.fuchs import FuchsClient
        from automotive.jsfilter import JsFilterClient
        from automotive.pakelo import PakeloClient
        from automotive.shell_oil import ShellOilClient
        from automotive.ttl_cache import SQLiteTTLCache
        from automotive.vin_search import VINSearchCoordinator

        cache = (SQLiteTTLCache(settings.automotive_cache_path)
                 if settings.automotive_cache_path else None)
        search_service = VINSearchCoordinator(
            repository, vin_service, None, settings.vin_hmac_key, motorium_enabled=False,
            fuchs_service=FuchsClient() if settings.fuchs_enabled else None,
            shell_service=ShellOilClient() if settings.shell_enabled else None,
            pakelo_service=PakeloClient() if settings.pakelo_enabled else None,
            jsfilter_service=JsFilterClient() if settings.jsfilter_enabled else None,
            catalog_service=None, rules=getattr(chat_service, "rules", None), cache=cache,
            fuchs_enabled=settings.fuchs_enabled, shell_enabled=settings.shell_enabled,
            pakelo_enabled=settings.pakelo_enabled, jsfilter_enabled=settings.jsfilter_enabled)
    chat_log = None
    if getattr(settings, "site_chat_log_path", ""):
        from automotive.chat_log import ChatLogger

        try:
            chat_log = ChatLogger(settings.site_chat_log_path,
                                  retention_days=settings.site_chat_log_retention_days,
                                  hmac_key=settings.vin_hmac_key)
        except Exception:  # A log that cannot open must not take the chat down.
            LOGGER.warning("site_chat_log_disabled")
    store = ConversationStore(
        chat_service, search_service, ttl_seconds=600,
        turn_seconds=min(float(getattr(settings, "chat_turn_seconds", 60)), 60.0),
        maximum=40, chat_log=chat_log, live=live)
    # A visitor's conversation: the assistant engine gives it none of the shop's own numbers.
    store.audience = "public"
    # One address may use an eighth of the day's allowance, so emptying it takes several.
    chat = PublicChat(store, catalog_store, daily,
                      per_address_daily=max(10, int(getattr(daily, "limit", 320)) // 8))
    chat.owns_search = owns_search
    chat.chat_log = chat_log
    return chat


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=500)
    language: Literal["ka", "en", "auto"] = "auto"
    # The answer button the visitor tapped, counted from 1 as the question listed them.
    choice: int | None = Field(default=None, ge=1, le=chat_contract.OPTION_LIMIT, strict=True)


def build_chat_router(chat: PublicChat | None, enabled: Any) -> APIRouter:
    """Routes under /api/site/chat. ``enabled()`` reflects the dashboard's chat switch."""
    router = APIRouter(prefix="/api/site/chat", include_in_schema=False)

    def service() -> PublicChat:
        if chat is None or not enabled():
            raise ConversationError("CHAT_DISABLED", 503)
        return chat

    def address(request: Request) -> str:
        return ip_key(request.client.host if request.client else None)

    @router.post("/conversations", status_code=201)
    def create(request: Request) -> dict:
        return service().create(address(request))

    @router.post("/conversations/{conversation_id}/messages", status_code=202)
    def message(conversation_id: ConversationId, payload: MessageBody,
                request: Request) -> dict:
        return service().submit(address(request), conversation_id, payload.message,
                                payload.language, payload.choice)

    @router.get("/conversations/{conversation_id}/turns/{turn_id}")
    def turn(conversation_id: ConversationId, turn_id: TurnId,
             offset: Annotated[int, Query(ge=0, le=500)] = 0,
             limit: Annotated[int, Query(ge=1, le=20)] = 5) -> dict:
        return service().turn(conversation_id, turn_id, offset, limit)

    @router.delete("/conversations/{conversation_id}", status_code=204)
    def delete(conversation_id: ConversationId) -> None:
        if chat is not None:
            chat.delete(conversation_id)

    return router
