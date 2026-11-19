"""What a finished bot turn means for the shop's staff.

The model decides what the customer means: ``finish.outcome`` and, once the engine carries it,
``finish.staff`` (intent, note, phone, missing). This module only reads what the engine has already
validated, checks it against the cards that were actually offered, and cleans the one value that
has to be literally true: a phone number must be in the customer's own words. No pattern here
reads a customer's message to decide what they want. Pure functions, no database.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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

# A run of digits with the separators people put inside a phone number (spaces, dots, dashes of
# every width, slashes, commas, underscores, brackets). Whitespace may split the run into groups;
# every slice of whole groups is a candidate (see ``_candidates``).
_CLUSTER = re.compile(r"[+(]?[0-9][0-9\s().,/_‐-―\-]*")


@dataclass(frozen=True)
class AlertDraft:
    """One alarm a turn raises: what staff see and, when the customer gave one, the phone."""

    kind: str
    reason: str
    summary: str
    car_label: str = ""
    phone: str = ""


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
        return TurnAlerts((AlertDraft("needs_staff", reason, words[:SUMMARY_LIMIT], car),),
                          "", words, car)
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
    if reason:
        wanted = ", ".join(missing) or note or words
        drafts.append(AlertDraft("part_not_found", reason, wanted[:SUMMARY_LIMIT], car, phone))
    kind = INTENT_KIND.get(str(staff.get("intent") or ""))
    if kind:
        drafts.append(AlertDraft(kind, "", (note or words)[:SUMMARY_LIMIT], car, phone))
    if outcome == "handoff":
        drafts.append(AlertDraft("needs_staff", "handoff", (note or words)[:SUMMARY_LIMIT],
                                 car, phone))
    return TurnAlerts(tuple(drafts), phone, words, car)
