"""What a finished bot turn means for the shop's staff.

The model decides what the customer means: ``finish.outcome`` and, once the engine carries it,
``finish.staff`` (intent, note, phone, missing). This module only reads what the engine has already
validated, checks it against the cards that were actually offered, and cleans the one value that
has to be literally true: a phone number must be in the customer's own words. No pattern here
reads a customer's message to decide what they want. Pure functions, no database.

When the bot found nothing (or handed over, or failed), the alarm also says why, from facts the
engine recorded and nothing judged here (``turn_detail``): each search the model ran with its
words and what the catalogue itself answered (``search_facts``: rows matching, in stock, naming
the car, words in no row, how the matching rows name their cars), the rows it found out of stock,
the rows a shop rule removed, the limits that made it answer with what it had, and for a turn that
failed, its status and the step it failed in. Whether a row fits stays the model's judgement and
is shown as such.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

KINDS = ("part_not_found", "visit_request", "service_booking", "needs_staff")
REASONS = ("", "nothing_found", "sold_out", "partial", "handoff", "failed", "phone")
# The engine's own statuses of a turn that produced an answer for the customer.
OK_STATUSES = frozenset({"OK", "CLARIFICATION"})
# What finish.staff.intent may say, and the alarm each one raises.
INTENT_KIND = {"visit": "visit_request", "service": "service_booking"}
SUMMARY_LIMIT = 300  # garage.live_alerts.summary
WORDS_LIMIT = 200  # the customer's own words, when the model wrote no note
NOTE_LIMIT = 160
CAR_LIMIT = 80
MISSING_LIMIT = 6
MISSING_WORD_LIMIT = 80
PHONE_MIN, PHONE_MAX = 7, 15
GEORGIA = "995"
LOCAL = 9  # digits of a Georgian number without the country code

# Why the bot found nothing (``turn_detail``; garage.live_alerts.detail, migration 0017): every
# list and string is cut short, and the whole stays under the column's 8192-byte CHECK, which
# counts jsonb's own spacing.
DETAIL_VERSION = 1
DETAIL_BYTES = 6000
# The read tools that look for parts (shop_info answers about the shop, not about a part).
SEARCH_TOOLS = ("search_catalog", "lookup_codes", "equivalents", "supplier_parts", "oil_guide",
                "car_numbers", "research")
MAX_SEARCHES = 10  # the engine's own cap on tool calls a turn
MAX_TERMS = 6
TERM_LIMIT = 60
LABEL_LIMIT = 80
MAX_CARS = 3
MAX_SPELLINGS = 3
MAX_NUMBERS = 8
MAX_UNAVAILABLE = 5
MAX_BARRED = 5
ERROR_LIMIT = 160
# The limits that made the model answer with what it had, as the engine records them
# (``diagnostics["limits"]``), and the turn endings that mean the same.
LIMITS = ("time", "searches", "requests")
FALLBACK_LIMITS = {"TIMEOUT": "time", "LIMIT_EXHAUSTED": "requests", "CHECKS_FAILED": "checks",
                   "UNGROUNDED_RESPONSE": "checks", "REFUSED": "checks"}
# A turn the store itself ended has no engine status: these warnings name what ended it.
STORE_FAILURES = ("TURN_DEADLINE", "CHAT_UNAVAILABLE")

# A run of digits with the separators people put inside a phone number (spaces, dots, dashes of
# every width, slashes, commas, underscores, brackets). Whitespace may split the run into groups;
# every slice of whole groups is a candidate (see ``_candidates``).
_CLUSTER = re.compile(r"[+(]?[0-9][0-9\s().,/_‐-―\-]*")


@dataclass(frozen=True)
class AlertDraft:
    """One alarm a turn raises: what staff see and, when the customer gave one, the phone.
    ``detail`` is why the bot found nothing (``turn_detail``); it is not part of what makes two
    drafts the same alarm."""

    kind: str
    reason: str
    summary: str
    car_label: str = ""
    phone: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class TurnAlerts:
    """Everything a turn tells the staff. ``phone`` is a number to attach to the chat's open
    alarms (or to raise on its own when it is new and nothing is open); ``message`` is the
    customer's own words, for an alarm that has no note of its own."""

    drafts: tuple[AlertDraft, ...] = ()
    phone: str = ""
    message: str = ""
    car_label: str = ""

    def __bool__(self) -> bool:
        return bool(self.drafts or self.phone)


def visible(char: str) -> bool:
    """Whether a character shows. Format characters (text direction overrides and isolates,
    zero-width spaces, the byte order mark, soft hyphens) change what staff read without showing:
    a visitor can type them, so they are dropped."""
    return unicodedata.category(char) != "Cf"


def text(value: Any, limit: int) -> str:
    """One line of text: whitespace collapsed, no control or hidden format characters, cut to
    ``limit``."""
    if not isinstance(value, str):
        return ""
    clean = "".join(" " if unicodedata.category(char) == "Cc" else char
                    for char in value if visible(char))
    return " ".join(clean.split())[:limit].strip()


def _digits(value: Any) -> str:
    """ASCII digits only (full-width digits count as digits)."""
    return re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", str(value or "")))


def _candidates(message: str) -> set[str]:
    """Every digit string a person could have meant as one phone number in ``message``, exactly as
    they typed it: each slice of consecutive digit groups inside a run (``2014 555 12 34 56``
    holds ``555123456``), and also without a leading ``00`` (the international prefix)."""
    found: set[str] = set()
    for cluster in _CLUSTER.findall(unicodedata.normalize("NFKC", message)):
        groups = [group for group in cluster.split() if any(char.isdigit() for char in group)]
        for start in range(len(groups)):
            joined = ""
            for group in groups[start:]:
                joined += _digits(group)
                if len(joined) > PHONE_MAX + 2:
                    break
                if len(joined) >= PHONE_MIN:
                    found.add(joined)
                    if joined.startswith("00"):
                        found.add(joined[2:])
    return found


def clean_phone(value: Any, said: Iterable[str]) -> str:
    """The phone number ``value`` names, or '' when it is not one the customer wrote.

    ``said`` is the customer's own messages of this conversation. The model reads them and says
    which number is a phone; this only checks that its digits really stand in them, whole,
    whatever separators the customer used. Only Georgia's country code may differ: the customer
    may have left it out or typed it where the model did not. A number the model made up, or cut
    out of a longer number such as a part number, is dropped. Georgian numbers come back as
    ``+995`` and nine digits; any other number as its digits, with a ``+`` when it was written
    as an international number.
    """
    digits = _digits(value)
    international = digits.startswith("00") or str(value).lstrip().startswith("+")
    if digits.startswith("00"):
        digits = digits[2:]
    if not PHONE_MIN <= len(digits) <= PHONE_MAX:
        return ""
    georgian = digits.startswith(GEORGIA) and len(digits) == len(GEORGIA) + LOCAL
    wanted = {digits}
    if georgian:
        wanted.add(digits[len(GEORGIA):])  # the customer left the country code out
    elif len(digits) == LOCAL:
        wanted.add(GEORGIA + digits)  # the customer typed it and the model did not
    typed: set[str] = set()
    for message in said:
        if isinstance(message, str):
            typed |= _candidates(message)
    if not wanted & typed:
        return ""
    return ("+" if georgian or international else "") + digits


def _words(message: Any) -> str:
    return text(message, WORDS_LIMIT) or "-"


def _missing(staff: Mapping[str, Any]) -> list[str]:
    raw = staff.get("missing")
    items = raw if isinstance(raw, list | tuple) else []
    names = (text(item, MISSING_WORD_LIMIT) for item in items)
    return [name for name in names if name][:MISSING_LIMIT]


def _sellable(card: Any) -> bool:
    """A card the customer can act on: anything but a row known to be out of stock."""
    return isinstance(card, Mapping) and card.get("availability_status") != "UNAVAILABLE"


# -- why the bot found nothing -------------------------------------------------------------------

def _count(value: Any) -> int:
    """A count a tool reported, or 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, min(int(value), 10**9))


def _listed(value: Any) -> int:
    """How many rows a tool's answer lists under one key."""
    return len(value) if isinstance(value, list) else 0


def _terms(value: Any, limit: int = MAX_TERMS) -> list[str]:
    items = value if isinstance(value, list | tuple) else []
    words = (text(item, TERM_LIMIT) for item in items if isinstance(item, str))
    return [word for word in words if word][:limit]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _row_cars(got: Mapping[str, Any]) -> list[list]:
    """How the rows a result carries name their cars, the most rows first, each with how many of
    them are in stock: the catalogue's own words. The catalogue counts this itself only when it
    could not show every row (``other_vehicle_texts``)."""
    columns, rows = got.get("columns"), got.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list) or "vehicle" not in columns:
        return []
    at = {name: columns.index(name) for name in ("vehicle", "store", "warehouse")
          if name in columns}
    counted: dict[str, list[int]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) <= at["vehicle"]:
            continue
        vehicle = text(row[at["vehicle"]], TERM_LIMIT)
        if not vehicle:
            continue
        stocked = any(_count(row[at[key]]) > 0 for key in ("store", "warehouse")
                      if key in at and len(row) > at[key])
        entry = counted.setdefault(vehicle, [0, 0])
        entry[0] += 1
        entry[1] += int(stocked)
    ranked = sorted(counted.items(), key=lambda item: (-item[1][0], item[0]))
    return [[vehicle, found, stocked] for vehicle, (found, stocked) in ranked[:MAX_CARS]]


def _catalogue_facts(asked: Mapping[str, Any], got: Mapping[str, Any]) -> dict:
    facts: dict = {}
    for key, short in (("part_terms", "part"), ("vehicle_terms", "car"), ("make_terms", "make"),
                       ("exclude_terms", "exclude")):
        words = _terms(asked.get(key))
        if words:
            facts[short] = words
    if asked.get("in_stock_only") is True:
        facts["stock_only"] = True
    if not got:
        return facts
    facts.update(total=_count(got.get("total")), available=_count(got.get("available")),
                 rows=_listed(got.get("rows")))
    if facts.get("car"):  # rows that name the car: counted only when car words were given
        facts["car_rows"] = _count(got.get("matched_vehicle"))
    unmatched = _terms(got.get("unmatched_terms"))
    if unmatched:
        facts["unmatched"] = unmatched
    why = _mapping(got.get("why"))
    alone = {short: _count(why.get(f"{key}_alone")) for key, short in (
        ("part_terms", "part"), ("vehicle_terms", "car"), ("make_terms", "make"))
        if f"{key}_alone" in why}
    if alone:
        facts["alone"] = alone
    if _count(why.get("removed_by_exclude_terms")):
        facts["excluded"] = _count(why.get("removed_by_exclude_terms"))
    cars = [[text(item[0], TERM_LIMIT), _count(item[1]), _count(item[2])]
            for item in got.get("other_vehicle_texts") or []
            if isinstance(item, list | tuple) and len(item) >= 3 and isinstance(item[0], str)]
    if not cars and facts.get("car") and not facts.get("car_rows"):
        cars = _row_cars(got)  # every row was shown and none names the car: what do they name
    if cars:
        facts["cars"] = cars[:MAX_CARS]
    spellings = [[text(item.get("term"), TERM_LIMIT), text(item.get("catalogue_token"),
                                                            TERM_LIMIT), _count(item.get("rows"))]
                 for item in got.get("similar_spellings") or [] if isinstance(item, Mapping)]
    if spellings:
        facts["spellings"] = spellings[:MAX_SPELLINGS]
    for key, short in (("rows_under_similar_spellings", "near"),
                       ("rows_without_the_make_word", "no_make"),
                       ("in_stock_under_other_cars", "elsewhere")):
        if _listed(got.get(key)):
            facts[short] = _listed(got.get(key))
    return facts


def _number_facts(asked: Mapping[str, Any], got: Mapping[str, Any]) -> dict:
    if not got:
        return {"numbers": [{"number": number} for number in _terms(asked.get("numbers"),
                                                                     MAX_NUMBERS)]}
    numbers = []
    for entry in got.get("numbers") or []:
        if not isinstance(entry, Mapping):
            continue
        item: dict = {"number": text(entry.get("number"), TERM_LIMIT)}
        if "rows" not in entry:  # too short to look up
            item["short"] = True
        else:
            item["rows"] = _listed(entry.get("rows"))
        revisions = _listed(_mapping(entry.get("other_revisions")).get("rows"))
        if revisions:
            item["revision"] = revisions
        if _listed(entry.get("equivalents")):
            item["cross"] = _listed(entry.get("equivalents"))
        numbers.append(item)
    return {"numbers": numbers[:MAX_NUMBERS]}


def search_facts(name: str, args: Any, payload: Any) -> dict:
    """What one read tool call searched for and what it found, for the staff: the words the
    model searched with and the counts the tool itself reported (catalogue rows that hold the
    words, how many are in stock, how many name the car, words in no row at all, how the rows
    that matched name their cars, part numbers no row carries). Read from the tool's own
    answer; nothing is judged here. ``payload`` is the answer the model was given, or None
    when the call failed (then only what was asked for)."""
    asked, got = _mapping(args), _mapping(payload)
    if name == "search_catalog":
        return _catalogue_facts(asked, got)
    if name == "lookup_codes":
        return _number_facts(asked, got)
    if not got:
        return {}
    if name in ("equivalents", "car_numbers", "research", "supplier_parts"):
        facts: dict = {"rows": _listed(got.get("rows"))}
        if name == "car_numbers":
            facts.update(vehicles=_listed(got.get("vehicles")), parts=_listed(got.get("parts")))
        if name in ("research", "supplier_parts"):
            facts["status"] = text(got.get("status"), 30)
        if name == "supplier_parts":
            facts.update(numbers=_count(got.get("supplier_numbers")),
                         not_in_catalogue=_listed(got.get("numbers_not_in_catalogue")))
        return facts
    if name == "oil_guide":
        return {"status": text(got.get("status"), 30)}
    return {}


def _plain(value: Any, depth: int = 0) -> Any:
    """``value`` as JSON-ready data of bounded size: short single-line strings, counts, flags and
    short lists and objects, three levels deep at most."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return _count(value)
    if isinstance(value, str):
        return text(value, TERM_LIMIT)
    if depth >= 3:
        return None
    if isinstance(value, Mapping):
        return {text(key, 20): _plain(item, depth + 1)
                for key, item in list(value.items())[:16] if isinstance(key, str)}
    if isinstance(value, list | tuple):
        return [_plain(item, depth + 1) for item in list(value)[:MAX_NUMBERS]]
    return None


def _searches(diagnostics: Mapping[str, Any]) -> tuple[int, list[dict]]:
    """How many part searches the turn ran, and each one as the staff are shown it."""
    searches: list[dict] = []
    for record in diagnostics.get("tools") or []:
        if not isinstance(record, Mapping) or record.get("name") not in SEARCH_TOOLS:
            continue
        entry: dict = {"tool": record["name"]}
        label = text(record.get("label"), LABEL_LIMIT)
        if label and record["name"] != "search_catalog":  # its own words say more
            entry["label"] = label
        found = _plain(record.get("found"))
        if isinstance(found, dict):
            entry.update({key: item for key, item in found.items()
                          if key not in ("tool", "label", "error")})
        error = text(record.get("error"), ERROR_LIMIT)
        if error:
            entry["error"] = error
        searches.append(entry)
    return len(searches), searches[:MAX_SEARCHES]


def _limits(data: Mapping[str, Any], diagnostics: Mapping[str, Any]) -> list[str]:
    """The limits that made the model answer with what it had, and stock figures too old to
    trust, in the order the staff read them."""
    found: list[str] = []

    def add(code: str) -> None:
        if code not in found:
            found.append(code)

    for code in diagnostics.get("limits") or []:
        if code in LIMITS:
            add(code)
    if _count(diagnostics.get("refused")):
        add("searches")
    fallback = FALLBACK_LIMITS.get(str(diagnostics.get("fallback") or ""))
    if fallback:
        add(fallback)
    warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
    if "TURN_DEADLINE" in warnings or data.get("engine_status") == "TIMEOUT":
        add("time")
    if "ANSWER_CHECKS_FAILED" in warnings:
        add("checks")
    if "STOCK_MAY_BE_STALE" in warnings:
        add("stale_stock")
    return found


def _unavailable(cards: Any) -> list[dict]:
    """The rows the reply showed as out of stock: the part exists, the shop has none."""
    shown: list[dict] = []
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, Mapping) or card.get("availability_status") != "UNAVAILABLE":
            continue
        product = _mapping(card.get("product"))
        counts = _mapping(product.get("stock"))
        item = {"brand": text(product.get("brand"), 40),
                "code": text(product.get("original_code"), 40),
                "name": text(product.get("product_name"), LABEL_LIMIT),
                "store": _count(counts.get("store")), "warehouse": _count(counts.get("tuz11"))}
        shown.append({key: value for key, value in item.items() if value != ""})
    return shown[:MAX_UNAVAILABLE]


def _size(detail: Mapping[str, Any]) -> int:
    return len(json.dumps(detail, ensure_ascii=False, default=str).encode())


def _bounded(detail: dict) -> dict:
    """``detail`` cut down until it fits ``DETAIL_BYTES``: first how the rows name their cars
    and the near spellings, then the last searches, then the out-of-stock rows."""
    if _size(detail) <= DETAIL_BYTES:
        return detail
    for search in detail.get("searches") or []:
        search.pop("cars", None)
        search.pop("spellings", None)
    searches = detail.get("searches") or []
    while len(searches) > 1 and _size(detail) > DETAIL_BYTES:
        searches.pop()
    for key in ("unavailable", "barred", "searches", "note"):
        if _size(detail) <= DETAIL_BYTES:
            break
        detail.pop(key, None)
    return detail


def turn_detail(payload: Mapping[str, Any] | None) -> dict:
    """Why a turn found nothing (or handed over, or failed), as the staff are shown it:

    - ``outcome``, and ``chosen`` when the engine changed what the model chose (rows it chose
      that could not be shown make an "offered" a "nothing_found");
    - ``note``: the line the model wrote for the staff; ``missing``: the parts its reply says it
      could not offer (no search is tied to a part: the model searched for all of them);
    - ``searched``: how many part searches ran (0: the model searched nothing), and
      ``searches``: each with its words and what the tool reported (``search_facts``), or the
      error it ended in;
    - ``unavailable``: the rows shown as out of stock; ``barred``: why a shop rule took rows out;
    - ``limits``: what cut the turn short (time, the search budget, the model's last step, an
      answer that failed its checks) and ``stale_stock``;
    - for a turn that produced no answer: ``status``, the error's class and the step it failed
      in (``stage``), as the engine recorded them.

    Facts only, read from the engine's own record of the turn (``diagnostics``), bounded in
    size. Of the customer's words it holds only those the model searched with."""
    data = _mapping(payload)
    diagnostics = _mapping(data.get("diagnostics"))
    staff = _mapping(data.get("staff"))
    detail: dict = {"v": DETAIL_VERSION}
    outcome = text(data.get("outcome"), 20)
    if outcome:
        detail["outcome"] = outcome
    chosen = text(diagnostics.get("chosen_outcome"), 20)
    if chosen and chosen != outcome:
        detail["chosen"] = chosen
    note = text(staff.get("note"), NOTE_LIMIT)
    if note:
        detail["note"] = note
    missing = _missing(staff)  # the parts the reply itself says it could not offer
    if missing:
        detail["missing"] = missing
    detail["searched"], searches = _searches(diagnostics)
    if searches:
        detail["searches"] = searches
    if oil_lookup := text(diagnostics.get("oil_lookup"), 30):
        detail["oil_lookup"] = oil_lookup
    unavailable = _unavailable(data.get("cards"))
    if unavailable:
        detail["unavailable"] = unavailable
    barred = list(dict.fromkeys(
        reason for item in diagnostics.get("removed") or [] if isinstance(item, Mapping)
        if (reason := text(item.get("reason"), ERROR_LIMIT))))[:MAX_BARRED]
    if barred:
        detail["barred"] = barred
    limits = _limits(data, diagnostics)
    if limits:
        detail["limits"] = limits
    engine_status = text(data.get("engine_status"), 40)
    if engine_status not in OK_STATUSES:
        warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
        detail["status"] = engine_status or next(
            (warning for warning in STORE_FAILURES if warning in warnings),
            text(data.get("status"), 40)) or "FAILED"
        for key, limit in (("error", 60), ("stage", 20)):
            value = text(diagnostics.get(key), limit)
            if value:
                detail[key] = value
    return _bounded(detail)


def turn_alerts(payload: Mapping[str, Any] | None, message: Any, *,
                alarm_failures: bool = False) -> TurnAlerts:
    """The alarms and the phone number of one finished turn.

    ``payload`` is the engine's answer as the store keeps it (``engine_status``, ``outcome``,
    ``cards``, ``car_label``, and ``staff`` once the engine carries it); ``message`` is what the
    customer wrote, already redacted by the store. ``alarm_failures``: a turn that ended without
    an answer also alarms (the website has no flag like Messenger's ``needs_attention``).

    - part_not_found: outcome nothing_found with no card, outcome sold_out with no card that is
      not out of stock, or ``staff.missing`` names parts this reply says it could not offer;
    - visit_request / service_booking: ``staff.intent`` says the customer wants to come in, or
      to have work done;
    - needs_staff: outcome handoff (the model asks staff to take over), or, when
      ``alarm_failures`` is set, a turn that failed.

    Those two kinds carry why (``turn_detail``); a visit or service request needs no reason.
    """
    data = payload if isinstance(payload, Mapping) else {}
    staff = data.get("staff") if isinstance(data.get("staff"), Mapping) else {}
    engine_status = str(data.get("engine_status") or "")
    car = text(data.get("car_label"), CAR_LIMIT)
    note = text(staff.get("note"), NOTE_LIMIT)
    words = _words(message)
    phone = text(staff.get("phone"), PHONE_MAX + 2) if engine_status in OK_STATUSES else ""
    if not re.fullmatch(r"\+?[0-9]{7,15}", phone):
        phone = ""
    if engine_status not in OK_STATUSES:
        if not alarm_failures or engine_status == "CANCELLED":
            return TurnAlerts()
        reason = "failed"
        return TurnAlerts((AlertDraft("needs_staff", reason, words[:SUMMARY_LIMIT], car,
                                      detail=turn_detail(data)),), "", words, car)
    cards = data.get("cards") if isinstance(data.get("cards"), list) else []
    outcome = data.get("outcome")
    missing = _missing(staff)
    drafts: list[AlertDraft] = []
    reason = ""
    if outcome == "nothing_found" and not cards:
        reason = "nothing_found"
    elif outcome == "sold_out" and not any(_sellable(card) for card in cards):
        reason = "sold_out"
    elif missing:
        reason = "partial"
    detail = turn_detail(data) if reason or outcome == "handoff" else {}
    if reason:
        wanted = ", ".join(missing) or note or words
        drafts.append(AlertDraft("part_not_found", reason, wanted[:SUMMARY_LIMIT], car, phone,
                                 detail))
    kind = INTENT_KIND.get(str(staff.get("intent") or ""))
    if kind:
        drafts.append(AlertDraft(kind, "", (note or words)[:SUMMARY_LIMIT], car, phone))
    if outcome == "handoff":
        drafts.append(AlertDraft("needs_staff", "handoff", (note or words)[:SUMMARY_LIMIT],
                                 car, phone, detail))
    return TurnAlerts(tuple(drafts), phone, words, car)
