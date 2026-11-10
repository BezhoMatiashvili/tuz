"""Daily review of the chat logs: requests that got nothing although the shop had the part.

The chat logs (``automotive.chat_log``) record every turn. This command takes the logged
turns that showed no parts and asked no question, replays the conversation each one belongs
to through the assistant engine with a larger budget, in a store that writes no log, and
reports the turns whose replay ended with a part that is in the store or at the warehouse:
the catalogue had it and the chat showed nothing.

Nothing here interprets a message. Which turns to look at follows from what the log stores
about them (status, number of cards, the engine's record of its own question), and whether
a turn is a miss follows from the cards of its replay. What the customer meant and which
part fits is judged by the engine, given more requests and more time. No message is left
out for being short or a greeting.

A VIN is never stored, so a logged message carries ``[VIN]`` and is replayed as it is. A
staff turn is also given the car the log recorded beside it, the way the staff form gives
one. A turn that had a VIN and whose replay finds nothing in stock is reported as "needs
VIN": not a miss, and not proof that nothing was missed. A replay reads the catalogue and
the free sources; it writes no chat log and nothing to PostgreSQL.

The reports are private files next to the logs and hold customer wording. A run prints
counts only; ``--dry-run`` lists the messages it would send, on the terminal.

Run: ``uv run python -m automotive.chat_review`` (``--dry-run`` calls no model).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import signal
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from automotive.chat import _redact

DEFAULT_OUT = "data/chat_logs/reviews"
LOGS = ("staff", "site")
# A turn the store finished. A cancelled one was overtaken before it reached the customer.
FINAL = ("COMPLETE", "PARTIAL", "FAILED")
ANSWERED = ("COMPLETE", "PARTIAL")
# ``availability_status`` of a card: in the store, or at the warehouse (there in 1-2 hours).
IN_STOCK = ("IN_STORE", "WITHIN_1_2_HOURS")
AVAILABILITY = {"IN_STORE": "in the store", "WITHIN_1_2_HOURS": "at the warehouse, 1-2 hours",
                "UNAVAILABLE": "not in stock", "UNKNOWN": "stock unknown"}
# The replay's budget for one turn, larger than the chat's own.
REPLAY_REQUESTS = 8
REPLAY_SECONDS = 90.0
# The store ends a turn at its own deadline, which has to outlast the engine's.
STORE_MARGIN = 15.0
POLL_SECONDS = 0.25
MAX_CARDS = 5
# How many of the customer's earlier messages a miss is reported with.
MAX_EARLIER = 5
# Replays that fail one after another mean the engine is not answering.
MAX_FAILURES = 3
TEXT_LIMIT = 2000
CLOSERS = " \t\r\n\"'»”)]"
# USD per million tokens: input, output, cache read, cache write. Haiku's are the rates for
# prompts up to 100K tokens.
PRICES = {"haiku": (0.10, 0.50, 0.01, 0.125), "sonnet": (2.0, 10.0, 0.20, 2.50)}
USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens",
         "cache_creation_input_tokens")
# What one replayed message is assumed to use, for the estimate of a dry run: four model
# requests that read the cached prompt and catalogue guide, the tool results between them
# and the replies. An assumption until runs have been measured; the budget is what holds.
ASSUMED_MESSAGE = {"input_tokens": 30_000, "output_tokens": 4_000,
                   "cache_read_input_tokens": 120_000, "cache_creation_input_tokens": 5_000}
RECORD, APPROXIMATION = "record", "approximation"
VERDICTS = ("miss", "needs_vin", "nothing_missed", "replay_failed")
UNCHECKED = {
    "limit": "over the limit of turns for one run",
    "budget": "the budget was reached",
    "engine": "the engine was not answering",
    "interrupted": "the run was stopped",
    "no_store": "the chat of this log is not installed in this configuration",
}
WINDOW_SQL = ("SELECT id, session_id, started_at, question, answer, status, engine_status,"
              " vin_hash, vehicle_json, card_total, diagnostics_json FROM turns"
              " WHERE started_at >= ? ORDER BY started_at, id")
SESSION_SQL = ("SELECT id, session_id, started_at, question, answer, status, engine_status,"
               " vin_hash, vehicle_json, card_total, diagnostics_json FROM turns"
               " WHERE session_id = ? ORDER BY started_at, id")


class ReviewError(RuntimeError):
    """The review cannot run; the message says why and holds nothing private."""


# -- what the log says about a turn ----------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class LoggedTurn:
    """One row of a log's ``turns`` table, as far as the review reads it."""

    log: str
    row_id: int
    session: str  # the stored hash of the conversation id, never the id itself
    started_at: float
    question: str
    answer: str
    status: str
    cards: int
    vin: bool  # the turn had a VIN, which the log does not hold
    car: dict  # the car the log recorded for the turn
    asked: bool
    asked_by: str  # RECORD or APPROXIMATION

    @property
    def nothing_shown(self) -> bool:
        """The turn reached the customer with no parts and no question."""
        return (self.status in FINAL and not self.cards and not self.asked
                and bool(self.question.strip()))


@dataclasses.dataclass(slots=True)
class LogRead:
    """What was read from one log file: the window's turns and, for every conversation
    with a turn to judge, all of its turns in the order they were sent."""

    log: str
    path: str
    error: str = ""
    turns: list[LoggedTurn] = dataclasses.field(default_factory=list)
    scripts: dict[str, list[LoggedTurn]] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class Session:
    """One logged conversation to replay: its messages up to the last turn to judge."""

    log: str
    session: str
    messages: list[LoggedTurn]
    judged: set[int]


@dataclasses.dataclass(slots=True)
class Outcome:
    """What the replays gave so far. Filled in place, so a stopped run keeps its results."""

    records: list[dict] = dataclasses.field(default_factory=list)
    spent: float = 0.0
    messages: int = 0
    stopped: str = ""


def asked_question(answer: str, engine_status: Any, diagnostics: Any) -> tuple[bool, str]:
    """Whether a logged turn asked the customer something, and how that is known.

    The assistant engine records the question it asked (``asked``) and how the turn ended
    (``outcome``) in the turn's diagnostics, and every engine gives a reply that only asks
    the status ``CLARIFICATION``. A turn with neither record comes from an older engine,
    whose log does not say: its answer counts as a question when it ends with a question
    mark. That is an approximation, and the report counts the turns decided by it.
    """
    if isinstance(diagnostics, dict) and ("asked" in diagnostics or "outcome" in diagnostics):
        return bool(diagnostics.get("asked")) or diagnostics.get("outcome") == "asked", RECORD
    if engine_status == "CLARIFICATION":
        return True, RECORD
    return answer.rstrip(CLOSERS).endswith(("?", "？")), APPROXIMATION


def _loads(text: Any) -> dict:
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {}


def _car(vehicle: dict) -> dict:
    """The car a turn was logged with: plain values only, and redacted like the text."""
    return {str(key)[:40]: _redact(value)[:100] if isinstance(value, str) else value
            for key, value in list(vehicle.items())[:20]
            if isinstance(value, str | int | float) and not isinstance(value, bool) and value}


def _logged(log: str, row: sqlite3.Row) -> LoggedTurn:
    # Logged text is already redacted. Doing it again costs nothing and makes sure that no
    # replay, listing or report can carry a VIN, whatever an old log file holds.
    question = _redact(row["question"]) if isinstance(row["question"], str) else ""
    answer = _redact(row["answer"]) if isinstance(row["answer"], str) else ""
    asked, how = asked_question(answer, row["engine_status"], _loads(row["diagnostics_json"]))
    total = row["card_total"]
    return LoggedTurn(
        log=log, row_id=int(row["id"]), session=str(row["session_id"]),
        started_at=float(row["started_at"]), question=question, answer=answer,
        status=str(row["status"] or ""), cards=total if isinstance(total, int) else 0,
        vin="[VIN]" in question or bool(row["vin_hash"]),
        car=_car(_loads(row["vehicle_json"])), asked=asked, asked_by=how)


def _script(turns: list[LoggedTurn]) -> list[LoggedTurn]:
    """A conversation's turns with the VIN carried forward: a conversation keeps the car of
    a VIN it was sent, so the turns after it had that VIN as well."""
    seen, script = False, []
    for turn in turns:
        seen = seen or turn.vin
        script.append(dataclasses.replace(turn, vin=seen))
    return script


def read_log(log: str, path: str, since: float) -> LogRead:
    """The turns of one log file from ``since`` on. The file is opened read-only."""
    read = LogRead(log, str(path or ""))
    if not path:
        read.error = "logging is switched off (the path is empty)"
        return read
    if not Path(path).is_file():
        read.error = "there is no log file"
        return read
    try:
        connection = sqlite3.connect(f"{Path(path).absolute().as_uri()}?mode=ro", uri=True,
                                     timeout=5)
    except sqlite3.Error:
        read.error = "the log file could not be opened"
        return read
    connection.row_factory = sqlite3.Row
    try:
        read.turns = [_logged(log, row) for row in connection.execute(WINDOW_SQL, (since,))]
        for session in dict.fromkeys(turn.session for turn in read.turns
                                     if turn.nothing_shown):
            read.scripts[session] = _script([
                _logged(log, row) for row in connection.execute(SESSION_SQL, (session,))])
    except (sqlite3.Error, ValueError, TypeError, IndexError):
        read.error = "the log file could not be read"
        read.turns, read.scripts = [], {}
    finally:
        connection.close()
    return read


def plan(reads: list[LogRead], limit: int) -> tuple[list[Session], list[LoggedTurn]]:
    """The conversations to replay, the newest first, and the turns the limit leaves out.

    A conversation is replayed from its first message, so its turns are taken oldest
    first: where the limit cuts one short, nothing is replayed that is not judged.
    """
    found = []
    for read in reads:
        window = {turn.row_id for turn in read.turns}
        for session, script in read.scripts.items():
            wanted = [turn for turn in script if turn.row_id in window and turn.nothing_shown]
            if wanted:
                found.append((wanted[-1].started_at, read.log, session, script, wanted))
    found.sort(key=lambda item: item[0], reverse=True)
    sessions: list[Session] = []
    left_out: list[LoggedTurn] = []
    room = max(0, limit)
    for _, log, session, script, wanted in found:
        taken, rest = wanted[:room], wanted[room:]
        room -= len(taken)
        left_out.extend(rest)
        if taken:
            last = max(index for index, turn in enumerate(script)
                       if turn.row_id == taken[-1].row_id)
            sessions.append(Session(log, session, script[:last + 1],
                                    {turn.row_id for turn in taken}))
    return sessions, left_out


# -- money -----------------------------------------------------------------------------------


def price_family(model: Any) -> str:
    """The row of ``PRICES`` a model id falls under. One that names neither family is
    priced as the dearest, so the budget is never flattered."""
    name = str(model or "").casefold()
    return next((family for family in PRICES if family in name),
                max(PRICES, key=lambda family: PRICES[family][0]))


def cost(model: Any, usage: Any) -> float:
    """What the token counts of one turn cost in USD."""
    counts = usage if isinstance(usage, dict) else {}
    total = 0.0
    for key, rate in zip(USAGE, PRICES[price_family(model)], strict=True):
        count = counts.get(key)
        if isinstance(count, int | float) and not isinstance(count, bool) and count > 0:
            total += count * rate
    return total / 1e6


# -- privacy ---------------------------------------------------------------------------------


def secrets_of(settings: Any) -> tuple[str, ...]:
    """The values the settings keep out of their own repr: keys, tokens, database URLs."""
    if not dataclasses.is_dataclass(settings):
        return ()
    return tuple(value for field in dataclasses.fields(settings) if not field.repr
                 and isinstance(value := getattr(settings, field.name, None), str) and value)


def scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    """``value`` with every string in it passed through the chat's own redaction once
    more: a VIN becomes ``[VIN]`` and a configured secret ``[REDACTED]``. Keys are the
    report's own names and stay as they are."""
    if isinstance(value, str):
        return _redact(value, secrets)
    if isinstance(value, dict):
        return {key: scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(item, secrets) for item in value]
    return value


# -- the replay ------------------------------------------------------------------------------


def review_settings(settings: Any, model: str | None = None) -> Any:
    """The chat's settings with the replay's budget, and nothing that writes or bills
    beyond the model: no chat log, so a replay never appears among the chats, no learning
    knowledge base (it writes to PostgreSQL) and no paid catalogue."""
    changes: dict = {
        "chat_engine": "assistant", "chat_log_path": "", "site_chat_log_path": "",
        "assistant_max_requests": REPLAY_REQUESTS, "assistant_turn_seconds": REPLAY_SECONDS,
        "chat_turn_seconds": max(float(getattr(settings, "chat_turn_seconds", 0) or 0),
                                 REPLAY_SECONDS + STORE_MARGIN),
        "knowledge_enabled": False, "catalog_api_key": "",
    }
    if model:
        changes["assistant_model"] = model
    return dataclasses.replace(settings, **changes)


def build_stores(settings: Any, wanted: Any) -> tuple[dict[str, Any], Callable[[], None]]:
    """The application as the chats run it, built by ``create_app`` from the review's
    settings. Returns the conversation store behind each wanted log (staff, site) and a
    function that closes what was opened."""
    if not getattr(settings, "anthropic_api_key", ""):
        raise ReviewError("ANTHROPIC_API_KEY is not set: nothing can be replayed.")
    from automotive.api import create_app

    try:
        app = create_app(settings=settings)
    except Exception as exc:  # Nothing is connected yet: this is the configuration's fault.
        said = scrub(f"{type(exc).__name__}: {exc}", secrets_of(settings))
        raise ReviewError(f"The application could not be built ({said[:300]}).") from None
    site = getattr(app.state, "site", None)
    staff = getattr(app.state, "conversations", None)
    public = getattr(site, "chat", None)

    def close() -> None:
        for target in (public, staff, getattr(staff, "search_service", None),
                       getattr(staff, "chat_service", None)):
            closing = getattr(target, "close", None)
            if callable(closing):
                with contextlib.suppress(Exception):
                    closing()

    try:
        # The server loads the catalogue when it starts; no server is started here.
        catalog = getattr(site, "catalog_store", None)
        if catalog is not None:
            catalog.refresh()
        if catalog is None or getattr(catalog, "loaded", True) is False:
            raise ReviewError("The catalogue could not be loaded.")
        stores = {"staff": staff, "site": getattr(public, "store", None)}
        stores = {log: store for log, store in stores.items()
                  if log in wanted and store is not None}
        for store in stores.values():
            seconds = getattr(store, "turn_seconds", None)
            if isinstance(seconds, int | float):
                store.turn_seconds = max(float(seconds), REPLAY_SECONDS + STORE_MARGIN)
    except ReviewError:
        close()
        raise
    except Exception as exc:  # Whatever it was, the message must not carry a database URL.
        close()
        raise ReviewError(
            f"The catalogue could not be loaded ({type(exc).__name__}).") from None
    return stores, close


def _text(value: Any, limit: int = TEXT_LIMIT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")


def _stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M")


def _car_name(car: dict) -> str:
    return " ".join(str(car[key]) for key in ("make", "model", "model_year") if car.get(key))


def card_summary(card: dict) -> dict:
    """What the report keeps of a part the replay showed."""
    product = card.get("product") if isinstance(card.get("product"), dict) else {}
    return {"name": _text(product.get("product_name"), 200),
            "brand": _text(product.get("brand"), 80),
            "number": _text(product.get("original_code"), 120),
            "vehicle": _text(product.get("product_model"), 300),
            "availability": _text(card.get("availability_status"), 40) or "UNKNOWN",
            "confidence": _text(card.get("confidence"), 40),
            "reason": _text(card.get("reason"), 300)}


def _failure(payload: dict) -> str:
    """Why a replay gave no answer, as a status code. Never text of the turn."""
    if payload.get("status") == "RUNNING":
        return "TIMED_OUT"
    code = payload.get("engine_status")
    return code[:40] if isinstance(code, str) and code else "FAILED"


def judge(turn: LoggedTurn, payload: dict, *, car_given: bool = False,
          earlier: list[LoggedTurn] | None = None) -> dict:
    """What the replay of a logged turn says about it.

    A miss is a replay that ends with at least one part to be had today: in the store
    or at the warehouse. Without one, a replay that did not answer says nothing, a turn
    that had a VIN cannot be judged (the replay never has the VIN), and otherwise the
    replay agrees with the log: nothing was missed. ``earlier`` are the conversation's
    messages before this one; a miss is reported with the last few of them.
    """
    cards = payload.get("cards") if isinstance(payload.get("cards"), list) else []
    cards = [card for card in cards if isinstance(card, dict)]
    stocked = [card for card in cards if card.get("availability_status") in IN_STOCK]
    answered = payload.get("status") in ANSWERED
    verdict = ("miss" if stocked else "replay_failed" if not answered
               else "needs_vin" if turn.vin else "nothing_missed")
    outcome = payload.get("outcome")
    record = {"log": turn.log, "session": turn.session, "turn": turn.row_id,
              "time": _iso(turn.started_at), "verdict": verdict, "had_vin": turn.vin,
              "question_decided_by": turn.asked_by,
              "replay_outcome": outcome[:40] if isinstance(outcome, str) else "",
              "detail": "" if answered else _failure(payload)}
    if verdict == "miss":
        ranked = sorted(cards, key=lambda card: IN_STOCK.index(card["availability_status"])
                        if card.get("availability_status") in IN_STOCK else len(IN_STOCK))
        record.update(
            earlier_messages=[_text(before.question, 300)
                              for before in (earlier or [])[-MAX_EARLIER:]],
            customer=_text(turn.question), logged_answer=_text(turn.answer),
            logged_status=turn.status, replay_reply=_text(payload.get("answer")),
            car_label=_text(payload.get("car_label"), 200),
            car_from_log=_car_name(turn.car) if car_given else "",
            cards_in_stock=len(stocked), cards_total=len(cards),
            cards=[card_summary(card) for card in ranked[:MAX_CARDS]])
    return record


def ask(store: Any, conversation: str, turn: LoggedTurn, *, car: dict, timeout: float,
        sleep: Callable[[float], None], monotonic: Callable[[], float]) -> tuple[dict, float]:
    """Send one logged message exactly as it was logged and wait for the turn to end.

    ``car`` is what the log recorded beside the message: the staff form's car and what
    the VIN decoded to at the time. It goes to the store as the form's car does.
    """
    started = monotonic()
    extra = {"vehicle": dict(car)} if car else {}
    ticket = store.submit(conversation, turn.question, language="auto", **extra)
    payload = store.get(conversation, ticket["turn_id"], 0, 100)
    while (isinstance(payload, dict) and payload.get("status") == "RUNNING"
           and monotonic() - started < timeout):
        sleep(POLL_SECONDS)
        payload = store.get(conversation, ticket["turn_id"], 0, 100)
    return (payload if isinstance(payload, dict) else {}), monotonic() - started


def _model_of(payload: dict, fallback: str) -> str:
    diagnostics = payload.get("diagnostics")
    named = diagnostics.get("model") if isinstance(diagnostics, dict) else None
    return named if isinstance(named, str) and named else fallback


def replay(sessions: list[Session], stores: dict[str, Any], outcome: Outcome, *, model: str,
           budget: float, progress: Callable[[str], None] = lambda line: None,
           timeout: float = REPLAY_SECONDS + 2 * STORE_MARGIN,
           sleep: Callable[[float], None] = time.sleep,
           monotonic: Callable[[], float] = time.monotonic) -> None:
    """Replay each conversation in a fresh one of its log's store and judge its turns.

    Stops when the budget is spent or when replays keep failing; ``outcome.stopped``
    says which. Only the store's ``create``, ``submit``, ``get`` and ``delete`` are used.
    """
    total = sum(len(session.judged) for session in sessions if session.log in stores)
    failures = 0
    for session in sessions:
        store = stores.get(session.log)
        if store is None:
            continue
        if outcome.spent >= budget:
            outcome.stopped = "budget"
            break
        conversation = None
        try:
            conversation = store.create()["conversation_id"]
            for number, turn in enumerate(session.messages, 1):
                if outcome.spent >= budget:
                    outcome.stopped = "budget"
                    break
                # The public chat has no form: its engine is told a car by the visitor only.
                car = turn.car if session.log == "staff" else {}
                payload, seconds = ask(store, conversation, turn, car=car, timeout=timeout,
                                       sleep=sleep, monotonic=monotonic)
                spent = cost(_model_of(payload, model), payload.get("usage"))
                outcome.spent += spent
                outcome.messages += 1
                failures = 0 if payload.get("status") in ANSWERED else failures + 1
                verdict = "context"
                if turn.row_id in session.judged:
                    record = judge(turn, payload, car_given=bool(car),
                                   earlier=session.messages[:number - 1])
                    record.update(cost_usd=round(spent, 5), seconds=round(seconds, 1))
                    outcome.records.append(record)
                    verdict = record["verdict"]
                progress(f"[{len(outcome.records)}/{total}] {session.log} "
                         f"{_stamp(turn.started_at)} message {number} of "
                         f"{len(session.messages)}: {verdict} (${spent:.4f}, {seconds:.0f} s)")
                if failures >= MAX_FAILURES:
                    outcome.stopped = "engine"
                    break
        except Exception as exc:  # One conversation that cannot be replayed is not the run's.
            failures += 1
            done = {record["turn"] for record in outcome.records
                    if (record["log"], record["session"]) == (session.log, session.session)}
            for turn in session.messages:
                if turn.row_id in session.judged and turn.row_id not in done:
                    record = judge(turn, {})
                    record.update(detail=type(exc).__name__[:60], cost_usd=0.0, seconds=0.0)
                    outcome.records.append(record)
            progress(f"[{len(outcome.records)}/{total}] {session.log}: the conversation "
                     f"could not be replayed ({type(exc).__name__})")
            if failures >= MAX_FAILURES:
                outcome.stopped = "engine"
        finally:
            delete = getattr(store, "delete", None)
            if conversation is not None and callable(delete):
                with contextlib.suppress(Exception):
                    delete(conversation)
        if outcome.stopped:
            break


# -- the report ------------------------------------------------------------------------------


def build_report(*, reads: list[LogRead], sessions: list[Session],
                 left_out: list[LoggedTurn], stores: dict[str, Any] | None,
                 outcome: Outcome, now: float, since: float, hours: float, model: str,
                 budget: float, limit: int, error: str = "") -> dict:
    """Everything a run found, as plain data. Nothing in it is redacted yet. ``stores``
    is None when the application could not be built, and ``error`` then says why."""
    judged = {(record["log"], record["turn"]) for record in outcome.records}
    unchecked = [{"log": turn.log, "session": turn.session, "turn": turn.row_id,
                  "time": _iso(turn.started_at), "reason": "limit"} for turn in left_out]
    for session in sessions:
        reason = ("no_store" if stores is not None and session.log not in stores
                  else outcome.stopped or "interrupted")
        unchecked.extend(
            {"log": turn.log, "session": turn.session, "turn": turn.row_id,
             "time": _iso(turn.started_at), "reason": reason}
            for turn in session.messages
            if turn.row_id in session.judged and (turn.log, turn.row_id) not in judged)
    counts: dict[str, dict] = {}
    rule = {RECORD: 0, APPROXIMATION: 0, "left_out_by_approximation": 0}
    for read in reads:
        shown = [turn for turn in read.turns if turn.nothing_shown]
        for turn in shown:
            rule[turn.asked_by] += 1
        rule["left_out_by_approximation"] += sum(
            1 for turn in read.turns if turn.status in FINAL and not turn.cards
            and turn.asked and turn.asked_by == APPROXIMATION)
        mine = [record for record in outcome.records if record["log"] == read.log]
        row = {"in_window": len(read.turns), "nothing_shown": len(shown),
               **{verdict: sum(1 for record in mine if record["verdict"] == verdict)
                  for verdict in VERDICTS},
               "unchecked": sum(1 for item in unchecked if item["log"] == read.log)}
        row["replayed"] = row["miss"] + row["needs_vin"] + row["nothing_missed"]
        counts[read.log] = row
    counts["all"] = {key: sum(row[key] for row in counts.values())
                     for key in ("in_window", "nothing_shown", "replayed", *VERDICTS,
                                 "unchecked")}
    records = sorted(outcome.records, key=lambda record: record["time"])
    return {
        "date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
        "generated_at": _iso(now),
        "window": {"hours": hours, "from": _iso(since), "to": _iso(now)},
        "model": model, "priced_as": price_family(model),
        "replay": {"model_requests": REPLAY_REQUESTS, "turn_seconds": REPLAY_SECONDS},
        "limit": limit, "budget_usd": budget, "cost_usd": round(outcome.spent, 4),
        "replayed_messages": outcome.messages, "stopped": outcome.stopped, "error": error,
        "logs": {read.log: read.error or "read" for read in reads},
        "counts": counts, "question_rule": rule,
        "misses": [record for record in records if record["verdict"] == "miss"],
        "other_turns": [record for record in records if record["verdict"] != "miss"],
        "unchecked": sorted(unchecked, key=lambda item: item["time"]),
    }


def _line(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _minute(moment: Any) -> str:
    """An ISO time of the report as a person reads it: local, to the minute."""
    return str(moment)[:16].replace("T", " ")


def _cell(value: Any, limit: int = 300) -> str:
    """One line for a table cell: nothing in it ends the cell or starts markup."""
    return (_line(value, limit).replace("\\", "\\\\").replace("|", "\\|")
            .replace("<", "&lt;").replace("`", "'"))


def _quote(value: Any) -> str:
    """Text as a quoted block, line by line."""
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    return "\n".join("> " + line.replace("<", "&lt;") for line in lines or ["(nothing)"])


def render_markdown(report: dict) -> str:
    """The report for a person: a summary table, then one section for every miss."""
    counts, window, rule = report["counts"], report["window"], report["question_rule"]
    names = list(counts)
    lines = [
        f"# Chat review {report['date']}", "",
        f"Logged turns from {_minute(window['from'])} to {_minute(window['to'])} that showed "
        "no parts and asked no question were asked again: the conversation of each was "
        f"replayed through the assistant engine (model `{report['model']}`, up to "
        f"{report['replay']['model_requests']} model requests and "
        f"{report['replay']['turn_seconds']:.0f} seconds a turn), with no chat log. A "
        "**miss** is a turn whose replay ended with at least one part that is in the "
        "store or at the warehouse.", "",
        "| | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    for label, key in (("Logged turns in the window", "in_window"),
                       ("No parts and no question", "nothing_shown"),
                       ("Replayed", "replayed"),
                       ("**Misses**", "miss"), ("Needs VIN", "needs_vin"),
                       ("Nothing missed", "nothing_missed"),
                       ("Replay failed", "replay_failed"), ("Left unchecked", "unchecked")):
        lines.append(f"| {label} | " + " | ".join(str(counts[name][key]) for name in names)
                     + " |")
    priced = "" if report["priced_as"] in str(report["model"]).casefold() else (
        f" The model is not in the price table and was counted at the {report['priced_as']} "
        "rates.")
    lines += ["",
              f"- Cost: ${report['cost_usd']:.4f} of the ${report['budget_usd']:.2f} budget, "
              f"for {_plural(report['replayed_messages'], 'replayed message')}.{priced}",
              f"- No question: for {_plural(rule[RECORD], 'turn')} the log holds the engine's "
              f"own record of whether it asked. For {_plural(rule[APPROXIMATION], 'turn')} "
              "of older engines it does not, and an answer that does not end with a question "
              "mark was taken as no question. The same approximation left out "
              f"{_plural(rule['left_out_by_approximation'], 'turn')} whose answer ends with "
              "one.",
              "- Needs VIN: the turn had a VIN, which is never stored, and its replay "
              "found nothing in stock. That is not a miss and says nothing either way."]
    reasons = {item["reason"] for item in report["unchecked"]}
    if reasons:
        lines.append(f"- Left unchecked: {_plural(len(report['unchecked']), 'turn')} ("
                     + "; ".join(UNCHECKED[reason] for reason in sorted(reasons)) + ").")
    lines += [f"- The {name} log was not read: {state}."
              for name, state in report["logs"].items() if state != "read"]
    if report["error"]:
        lines.append(f"- Nothing was replayed: {_line(report['error'], 400)}")
    for number, miss in enumerate(report["misses"], 1):
        given = (f" The replay was given the car the log recorded: "
                 f"{_line(miss['car_from_log'], 80)}." if miss["car_from_log"] else "")
        lines += ["", f"## Miss {number}: {miss['log']} log, {_minute(miss['time'])}", "",
                  f"Session `{_cell(miss['session'], 64)}`, turn {miss['turn']}."
                  + (" The turn had a VIN." if miss["had_vin"] else "") + given, "",
                  *(["**Earlier in the conversation**", "",
                     _quote("\n".join(miss["earlier_messages"])), ""]
                    if miss["earlier_messages"] else []),
                  "**Customer**", "", _quote(miss["customer"]), "",
                  f"**Logged answer** ({_line(miss['logged_status'], 20)})", "",
                  _quote(miss["logged_answer"]), "",
                  f"**Replay** (car: {_line(miss['car_label'], 80) or 'not named'}; "
                  f"{miss['cards_in_stock']} of {_plural(miss['cards_total'], 'part')} in "
                  "stock)", "",
                  _quote(miss["replay_reply"]), "",
                  "| Part | Brand | Number | Catalogue vehicle text | Availability | "
                  "Confidence | Reason |", "|---|---|---|---|---|---|---|"]
        lines += ["| " + " | ".join((
            _cell(card["name"], 120), _cell(card["brand"], 60), _cell(card["number"], 80),
            _cell(card["vehicle"], 160),
            AVAILABILITY.get(card["availability"], _cell(card["availability"], 40)),
            _cell(card["confidence"], 20), _cell(card["reason"], 200))) + " |"
            for card in miss["cards"]]
    rest = [{**record, "result": record["verdict"].replace("_", " "),
             "why": record["detail"] or record["replay_outcome"]}
            for record in report["other_turns"] if record["verdict"] != "nothing_missed"]
    rest += [{**item, "result": "left unchecked", "why": UNCHECKED[item["reason"]]}
             for item in report["unchecked"]]
    if rest:
        lines += ["", "## Not judged", "", "| Log | Time | Session | Turn | Result | Detail |",
                  "|---|---|---|---:|---|---|"]
        lines += [f"| {item['log']} | {_minute(item['time'])} | "
                  f"`{_cell(item['session'], 64)}` | "
                  f"{item['turn']} | {item['result']} | {_cell(item['why'], 80)} |"
                  for item in sorted(rest, key=lambda item: item["time"])]
    return "\n".join(lines) + "\n"


def write_private(path: Path, text: str) -> None:
    """The whole file or none of it, readable by its owner only."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def private_directory(path: Path) -> None:
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)  # mkdir's mode passes through the umask


def write_reports(directory: Path, report: dict) -> tuple[Path, Path]:
    private_directory(directory)
    markdown = directory / f"chat-review-{report['date']}.md"
    data = directory / f"chat-review-{report['date']}.json"
    write_private(markdown, render_markdown(report))
    write_private(data, json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n")
    return markdown, data


# -- the command -----------------------------------------------------------------------------


def _positive(kind: Callable[[str], Any]) -> Callable[[str], Any]:
    def parse(text: str) -> Any:
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError("a number is expected") from None
        if not 0 < value < float("inf"):
            raise argparse.ArgumentTypeError("the value must be above zero")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m automotive.chat_review",
        description="Replay the logged chat turns that showed no parts and asked nothing, "
                    "and report those where the catalogue had the part in stock.")
    parser.add_argument("--since-hours", type=_positive(float), default=24.0, metavar="HOURS",
                        help="how far back to read the logs (default 24)")
    parser.add_argument("--log", choices=(*LOGS, "both"), default="both",
                        help="which chat log to review (default both)")
    parser.add_argument("--limit", type=_positive(int), default=40, metavar="TURNS",
                        help="judge at most this many turns, the newest conversations first "
                             "(default 40)")
    parser.add_argument("--budget-usd", type=_positive(float), default=1.0, metavar="USD",
                        help="stop when the replays have cost this much (default 1.0)")
    parser.add_argument("--model", metavar="ID",
                        help="model for the replays (default: the chat's own)")
    parser.add_argument("--out", default=DEFAULT_OUT, metavar="DIR",
                        help=f"directory for the reports (default {DEFAULT_OUT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be replayed and an estimate of the cost; "
                             "calls no model and writes nothing")
    return parser


def _dry_run(sessions: list[Session], left_out: list[LoggedTurn], *, model: str,
             budget: float, limit: int, secrets: tuple[str, ...], out: TextIO) -> None:
    for session in sessions:
        out.write(f"{session.log:5s} {_stamp(session.messages[-1].started_at)}  session "
                  f"{session.session}  {_plural(len(session.messages), 'message')}, "
                  f"{len(session.judged)} to judge\n")
        for number, turn in enumerate(session.messages, 1):
            mark = "*" if turn.row_id in session.judged else " "
            out.write(f"    {number:2d}{mark} {scrub(_line(turn.question, 160), secrets)}\n")
    judged = sum(len(session.judged) for session in sessions)
    messages = sum(len(session.messages) for session in sessions)
    each = cost(model, ASSUMED_MESSAGE)
    out.write(
        f"dry run: {judged} of {_plural(judged + len(left_out), 'logged turn')} with no parts "
        f"and no question would be judged (limit {limit}), in "
        f"{_plural(len(sessions), 'conversation')} and "
        f"{_plural(messages, 'replayed message')} (* marks a turn to judge)\n"
        f"estimated cost: about ${messages * each:.2f} on {model} "
        f"({_plural(messages, 'message')} at an assumed ${each:.4f} each, "
        f"{price_family(model)} rates); the budget of ${budget:.2f} covers about "
        f"{_plural(int(budget // each), 'message')}\n"
        "nothing was sent to the model and no report was written\n")


def run(args: argparse.Namespace, settings: Any, *, build: Callable = build_stores,
        clock: Callable[[], float] = time.time, out: TextIO | None = None,
        err: TextIO | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """One review. Returns the exit status: 0 also when misses were found, because a
    miss is a finding and not a failure of the command."""
    out, err = out or sys.stdout, err or sys.stderr
    now = clock()
    since = now - args.since_hours * 3600
    wanted = LOGS if args.log == "both" else (args.log,)
    paths = {"staff": getattr(settings, "chat_log_path", ""),
             "site": getattr(settings, "site_chat_log_path", "")}
    reads = [read_log(log, paths[log], since) for log in wanted]
    for read in reads:
        if read.error:
            err.write(f"{read.log} log: {read.error}\n")
    if all(read.error for read in reads):
        return 1
    sessions, left_out = plan(reads, args.limit)
    secrets = secrets_of(settings)
    review = review_settings(settings, args.model)
    model = str(getattr(review, "assistant_model", "")
                or getattr(review, "anthropic_model", "") or "")
    if args.dry_run:
        _dry_run(sessions, left_out, model=model, budget=args.budget_usd, limit=args.limit,
                 secrets=secrets, out=out)
        return 0
    directory = Path(args.out)
    try:
        private_directory(directory)
    except OSError:
        err.write(f"The report directory {directory} could not be created.\n")
        return 1
    stores: dict[str, Any] | None = {}
    close: Callable[[], None] | None = None
    outcome = Outcome()
    failed = ""
    try:
        if sessions:
            stores = None
            stores, close = build(review, {session.log for session in sessions})
            replay(sessions, stores, outcome, model=model, budget=args.budget_usd,
                   progress=lambda line: err.write(line + "\n"), sleep=sleep)
    except ReviewError as exc:
        failed = str(exc)
        outcome.stopped = "engine"
    except KeyboardInterrupt:
        outcome.stopped = "interrupted"
    finally:
        if close is not None:
            close()
    report = scrub(build_report(
        reads=reads, sessions=sessions, left_out=left_out, stores=stores, outcome=outcome,
        now=now, since=since, hours=args.since_hours, model=model, budget=args.budget_usd,
        limit=args.limit, error=failed), secrets)
    try:
        markdown, _ = write_reports(directory, report)
    except OSError:
        err.write(f"The report could not be written to {directory}.\n")
        return 1
    if failed:
        err.write(failed + "\n")
    total = report["counts"]["all"]
    each = ", ".join(f"{read.log} {report['counts'][read.log]['nothing_shown']}"
                     for read in reads)
    stopped = f" ({UNCHECKED[outcome.stopped]})" if outcome.stopped else ""
    out.write(
        f"chat review {report['date']}: {total['nothing_shown']} of "
        f"{_plural(total['in_window'], 'logged turn')} in the last {args.since_hours:g} hours "
        f"showed no parts and asked no question ({each})\n"
        f"replayed {total['replayed']}: misses {total['miss']}, needs VIN "
        f"{total['needs_vin']}, nothing missed {total['nothing_missed']}; replay failed "
        f"{total['replay_failed']}, left unchecked {total['unchecked']}{stopped}; cost "
        f"${report['cost_usd']:.4f} of ${args.budget_usd:.2f} on {model}\n"
        f"report: {markdown}\n")
    if outcome.stopped == "interrupted":
        return 130
    return 1 if outcome.stopped == "engine" else 0


def _terminate(signum: int, frame: Any) -> None:
    """``kill`` and a stopped unit end a run the way Ctrl+C does: the report of what was
    judged so far is still written."""
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    from automotive.config import Settings

    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    with contextlib.suppress(ValueError):  # Only the main thread may set a handler.
        signal.signal(signal.SIGTERM, _terminate)
    return run(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
