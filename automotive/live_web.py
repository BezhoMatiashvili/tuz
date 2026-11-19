"""The website chat as the staff see it: a recorder behind the visitors' ``ConversationStore``.

Dashboard > Live chats shows what a website visitor wrote and what the bot answered, raises the
alarms of the turn (a part not found, a visit or service request, a phone number, a bot that
needs a human), and lets staff switch the bot off for one visitor. This module is the website's
half of that. It is built to be harmless to the chat:

- every database write happens on one background thread, behind a bounded queue; when the queue
  is full or the tables are missing the record is dropped and counted, never waited for;
- the one question a visitor's message asks, "is the bot switched off for this visitor", is
  answered from a set the writer thread refreshes every few seconds: the request never touches
  the database, and a database that cannot be reached leaves the last set in place (the bot
  answers as it always did);
- an anonymous visitor cannot bury the staff's screen: alarms a visitor or an outage can produce
  in numbers (a part not found, a failed turn) and alarms that matter most (a visit or service
  request, a phone number, a handoff) have separate room per address and on the board, an alarm
  counts against its address only when it is **created**, and a repeat or a phone number added to
  an open alarm is never refused;
- nothing here logs a message, a number or a conversation id, only an error's class name.

A visitor is named by ``live_store.web_key`` (the website chat log's own key), never by the
conversation id, which is a bearer secret. The rows live in ``garage.live_web_*`` (migration
0015) and are kept for good (owner's decision, 2026-11-20); only the alarms are pruned, after the
website chat log's retention (30 days).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from typing import Any

from automotive.live_alerts import OK_STATUSES, AlertDraft, TurnAlerts, text, turn_alerts
from automotive.live_store import LiveStore, web_key

LOGGER = logging.getLogger(__name__)

QUEUE_SIZE = 500
PRUNE_SECONDS = 6 * 3600.0
MUTE_REFRESH_SECONDS = 3.0  # how often the writer thread reads which visitors are muted
# New alarms one address may cause in an hour, and how many of each group the board holds open.
NOISY_PER_ADDRESS = 5
VALUABLE_PER_ADDRESS = 20
ALARM_WINDOW = 3600.0
MAX_OPEN_NOISY = 100
MAX_OPEN_VALUABLE = 300
ADDRESSES_KEPT = 2000
OFFERED_LIMIT = 8
LOG_EVERY = 60.0  # one line a minute about dropped records or capped addresses

# What a visitor reads while staff have switched the assistant off for them. Honest: there is no way
# to answer a visitor from the dashboard, so it says where to reach the shop. The Georgian wording
# needs a native speaker's review like the rest of the interface strings.
MUTED = {
    "ka": ("ამ საუბარში ავტომატური ასისტენტი გამორთულია. გთხოვთ, დაგვიკავშირდეთ პირდაპირ.",
           "დაგვირეკეთ: {phone}"),
    "en": ("The automatic assistant is switched off in this chat. Please contact the shop "
           "directly.", "Please call us: {phone}"),
}


def _offered(cards: Any) -> list[dict]:
    """What the visitor saw offered: name, brand, price and the availability text, nothing of the
    shop's own numbers."""
    shown: list[dict] = []
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, Mapping):
            continue
        product = card.get("product") if isinstance(card.get("product"), Mapping) else {}
        price = product.get("price")
        shown.append({
            "name": text(product.get("product_name"), 120),
            "brand": text(product.get("brand"), 60),
            "price": price if isinstance(price, int | float) and not isinstance(price, bool)
            else None,
            "availability": text(card.get("availability"), 80),
            "status": text(card.get("availability_status"), 30)})
        if len(shown) >= OFFERED_LIMIT:
            break
    return shown


def _noisy(draft: AlertDraft) -> bool:
    """An alarm a visitor or an outage can produce in numbers, as opposed to one that matters
    most (a visit or service request, a handoff, a phone number)."""
    return draft.kind == "part_not_found" or (
        draft.kind == "needs_staff" and draft.reason == "failed")


class LiveRecorder:
    """What ``ConversationStore`` calls: ``customer``, ``turn``, ``canned``, ``muted_reply``, and
    ``PublicChat`` calls ``seen``. ``store`` is a ``LiveStore`` on the site's own login;
    ``shop_phone`` returns the shop's phone for the message a muted visitor reads."""

    def __init__(self, store: LiveStore, *, retention_days: int = 30,
                 shop_phone: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.monotonic, start: bool = True) -> None:
        self.store = store
        self.retention_days = max(1, int(retention_days))
        self.shop_phone = shop_phone or (lambda: "")
        self._clock = clock
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._lock = threading.Lock()
        self._addresses: OrderedDict[str, str] = OrderedDict()
        self._spent: dict[tuple[str, bool], deque] = {}
        self._muted: frozenset[str] = frozenset()
        self._logged: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped = 0
        if start:
            self.start()

    # -- the writer ------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="live-web-recorder",
                                            daemon=True)
            self._thread.start()

    def close(self) -> None:
        """Stop the writer; what is still queued is written first (a couple of seconds at most)."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)

    def _run(self) -> None:
        next_prune = next_refresh = 0.0  # both on start
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                item = None
            if item is None and self._stop.is_set():
                return
            now = self._clock()
            if now >= next_refresh:
                next_refresh = now + MUTE_REFRESH_SECONDS
                self.refresh()
            if now >= next_prune:
                next_prune = now + PRUNE_SECONDS
                self._prune()
            if item is not None:
                self._write(item)

    def drain(self) -> int:
        """Write everything queued, in the calling thread (the writer does not need to run). The
        number of records written. For tests and for a clean shutdown."""
        written = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return written
            self._write(item)
            written += 1

    def _put(self, item: tuple) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            self._note("live_web_queue_full")

    def _note(self, line: str, *values: Any) -> None:
        """At most one log line a minute per kind: a problem repeats on every message."""
        now = self._clock()
        with self._lock:
            if now - self._logged.get(line, -LOG_EVERY) < LOG_EVERY:
                return
            self._logged[line] = now
        LOGGER.warning(line, *values)

    def _attempt(self, action: Callable[[], Any]) -> bool:
        """One write that may fail without taking the others of the same turn with it."""
        try:
            action()
            return True
        except Exception as error:
            self.dropped += 1
            self._note("live_web_write_failed error=%s", type(error).__name__)
            return False

    def _write(self, item: tuple) -> None:
        try:
            if not self.store.usable():
                self.dropped += 1
                self._note("live_web_not_ready")
                return
        except Exception as error:
            self.dropped += 1
            self._note("live_web_write_failed error=%s", type(error).__name__)
            return
        if item[0] == "customer":
            _, key, body, language = item
            self._attempt(lambda: self.store.web_customer(key, body, language))
            return
        _, key, body, offered, outcome, car, language, alerts, address, recovered = item
        if body or offered:
            self._attempt(lambda: self.store.web_bot(
                key, body, offered=offered, outcome=outcome, car_label=car, language=language))
        self._alarms(key, alerts, address, recovered)

    def _prune(self) -> None:
        try:
            if self.store.usable():
                self.store.web_prune(self.retention_days)
        except Exception as error:
            self._note("live_web_prune_failed error=%s", type(error).__name__)

    # -- who is muted ----------------------------------------------------------------------------

    def refresh(self) -> None:
        """Read which visitors staff have switched the bot off for. A database that cannot be
        read leaves the last set in place."""
        try:
            if not self.store.usable():
                return
            keys = frozenset(self.store.web_muted_keys())
        except Exception as error:
            self._note("live_web_mute_refresh_failed error=%s", type(error).__name__)
            return
        with self._lock:
            self._muted = keys

    def muted_reply(self, identifier: str, language: str) -> str | None:
        """The text a muted visitor reads in place of an answer, or None when the bot answers.
        Asked before each message of a visitor; answered from memory, never from the database."""
        with self._lock:
            muted = web_key(identifier) in self._muted
        if not muted:
            return None
        lang = language if language in MUTED else "ka"
        sentence, call = MUTED[lang]
        try:
            phone = " ".join(str(self.shop_phone() or "").split())
        except Exception:
            phone = ""
        return f"{sentence} {call.replace('{phone}', phone)}" if phone else sentence

    # -- alarms: who may create one ----------------------------------------------------------------

    def _room(self, address: str, noisy: bool, counts: Mapping[str, int]) -> bool:
        """Whether this address may cause one more new alarm of this group now, and the board has
        room for it. Nothing is spent here: that happens when the alarm is created."""
        with self._lock:
            times = self._spent.setdefault((address, noisy), deque())
            now = self._clock()
            while times and now - times[0] > ALARM_WINDOW:
                times.popleft()
            used = len(times)
        limit, cap = ((NOISY_PER_ADDRESS, MAX_OPEN_NOISY) if noisy
                      else (VALUABLE_PER_ADDRESS, MAX_OPEN_VALUABLE))
        return used < limit and counts["noisy" if noisy else "valuable"] < cap

    def _spend(self, address: str, noisy: bool) -> None:
        with self._lock:
            self._spent.setdefault((address, noisy), deque()).append(self._clock())
            if len(self._spent) > ADDRESSES_KEPT:
                for stale in [key for key, seen in self._spent.items() if not seen]:
                    del self._spent[stale]

    def _alarms(self, key: str, alerts: TurnAlerts, address: str, recovered: bool) -> None:
        """Record what the turn tells the staff. A repeat of an open alarm, and a phone number for
        one, always go through; only a NEW alarm needs room (the address's and the board's)."""
        if recovered:
            self._attempt(lambda: self.store.close_failed("web", key))
        if not alerts:
            return
        counts = {"noisy": 0, "valuable": 0}
        if not self._attempt(lambda: counts.update(self.store.open_web_alert_counts())):
            return  # the board's state is unknown: no new alarm this time
        # Each draft and the phone are attempted on their own: one failing must not take the
        # visit request and the number of the same turn down with it.
        for draft in alerts.drafts:
            self._attempt(lambda draft=draft: self._one(key, draft, address, counts))
        if alerts.phone:
            self._attempt(lambda: self._phone(key, alerts, address, counts))

    def _one(self, key: str, draft: AlertDraft, address: str, counts: dict) -> None:
        noisy = _noisy(draft)
        raised = self.store.raise_alert("web", key, draft,
                                        allow_create=self._room(address, noisy, counts))
        if raised is None:
            self._note("live_web_alarm_refused")
        elif raised.created:
            self._spend(address, noisy)
            counts["noisy" if noisy else "valuable"] += 1

    def _phone(self, key: str, alerts: TurnAlerts, address: str, counts: dict) -> None:
        made = self.store.attach_phone("web", key, alerts.phone, alerts.message, alerts.car_label,
                                       allow_create=self._room(address, False, counts))
        if any(item.created for item in made):
            self._spend(address, False)

    # -- what the visitors' store calls ----------------------------------------------------------

    def seen(self, identifier: str, address: str) -> None:
        """The address a conversation's messages come from, for the hourly limits on its alarms."""
        with self._lock:
            self._addresses[identifier] = address
            self._addresses.move_to_end(identifier)
            while len(self._addresses) > ADDRESSES_KEPT:
                self._addresses.popitem(last=False)

    def customer(self, identifier: str, message: str, language: str) -> None:
        """A visitor's message, queued the moment it is accepted."""
        self._put(("customer", web_key(identifier), message, language))

    def canned(self, identifier: str, answer: str, language: str) -> None:
        """What a muted visitor was told instead of an answer."""
        self._put(("bot", web_key(identifier), answer, [], "", "", language, TurnAlerts(), "",
                   False))

    def turn(self, identifier: str, message: str, payload: Mapping[str, Any],
             language: str) -> None:
        """A finished turn: the bot's answer as the visitor saw it, and the alarms it raises."""
        alerts = turn_alerts(payload, message, alarm_failures=True)
        outcome = payload.get("outcome") if isinstance(payload.get("outcome"), str) else ""
        with self._lock:
            address = self._addresses.get(identifier, "")
        # A turn that answered closes the same visitor's open "failed" alarm (the outage is over).
        recovered = str(payload.get("engine_status") or "") in OK_STATUSES
        self._put(("bot", web_key(identifier), str(payload.get("answer") or ""),
                   _offered(payload.get("cards")), text(outcome, 20),
                   text(payload.get("car_label"), 80), language, alerts, address, recovered))
