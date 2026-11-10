"""One contract for both chats: the shapes a reply is drawn from.

The staff route and the public facade both read a turn through this module, so the two
screens are handed the same things: the reply's whole ``answer``, a ``question`` with
the answers to tap, the ``suggestions`` for what to do next, the ``groups`` the parts are
listed under, and a group, a reason and one of four confidence levels for each part.
Pure functions over the dict ``ConversationStore.get`` returns: nothing here reads
settings, the catalogue or the clock.

Engine fields win. An engine that states ``question``, ``suggestions``, ``groups`` and a
card's ``group``, ``reason`` and ``confidence`` is taken at its word. For an engine that
does not, the same shapes are read from what it does send: ``act``, ``engine_status``,
``pending``, ``clarification_choices`` and ``quick_replies``.

Every list and string is clamped here. A response model that refuses one oversized value
fails the whole turn, so nothing leaves this module longer than its limit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# How sure an offer is. The first three speak of a part's fit to the car; ``spec`` is for a
# product its catalogue text ties to no car (an engine oil, a fluid, a universal item) that
# was chosen because its specification is what the car needs.
LEVELS = ("confirmed", "listed", "check", "spec")
LABEL_LIMIT, VALUE_LIMIT = 80, 200
OPTION_LIMIT, SUGGESTION_LIMIT, GROUP_LIMIT = 8, 6, 6
ID_LIMIT, REASON_LIMIT, COUNT_LIMIT, ANSWER_LIMIT = 40, 160, 999, 4000
# The words an older engine already understands as the answer to its own offer
# ("Need the rear ones too?"), in the language of the reply.
YES_NO = {"ka": ("დიახ", "არა"), "en": ("Yes", "No")}
# Why an older engine offered a part, as one of the levels that speak of fit. An oil it
# chose by its grade and a number the customer typed stay unrated, as they always were:
# only an engine that says ``spec`` itself, with its reason, is taken to mean it.
_LEGACY_LEVELS = {"staff_rule": "confirmed", "code": "confirmed", "name_year": "listed",
                  "name": "check", "engine": "check"}
_UNRATED = frozenset({"verified_oil", "oil_grade_brand", "oil_grade", "typed_code"})

# A caller's own rule for one cleaned option: return it (changed or not) to keep it, or
# None to leave it out. The public side uses this to drop options that name a stock number.
Keep = Callable[[dict], Any]


def _line(value: Any, limit: int) -> str:
    """One line of text of at most ``limit`` characters; anything that is not text is empty."""
    return " ".join(value.split())[:limit].rstrip() if isinstance(value, str) else ""


def _option(entry: Any) -> dict | None:
    label = value = None
    if isinstance(entry, str):
        label = value = entry
    elif isinstance(entry, dict):
        label = entry.get("label") or entry.get("value")
        value = entry.get("value") or entry.get("label")
    label, value = _line(label, LABEL_LIMIT), _line(value, VALUE_LIMIT)
    return {"label": label, "value": value or label} if label else None


def _numbered(values: Any, limit: int, keep: Keep | None = None) -> list[tuple[int, dict]]:
    """The usable options, each with the place (from 0) it had in ``values``."""
    found: list[tuple[int, dict]] = []
    for place, entry in enumerate(values if isinstance(values, list | tuple) else ()):
        if len(found) >= limit:
            break
        option = _option(entry)
        if option is not None and keep is not None:
            option = _option(keep(option))
        if option is not None:
            found.append((place, option))
    return found


def _distinct(found: list[dict]) -> list[dict]:
    """``found`` without a second option that reads the same as an earlier one."""
    seen: set[str] = set()
    kept: list[dict] = []
    for option in found:
        key = option["label"].casefold()
        if key not in seen:
            seen.add(key)
            kept.append(option)
    return kept


def options(values: Any, limit: int = OPTION_LIMIT) -> list[dict]:
    """Replies a person can tap, as ``{"label", "value"}`` pairs.

    A string stands for both; a mapping may give either. The label is what the button
    says (80 characters at most) and the value is what is sent (200). Anything else, and
    an option without a label, is left out.
    """
    return [option for _, option in _numbered(values, max(0, limit))]


def _legacy(payload: dict) -> tuple[Any, Any]:
    """What an engine without these fields asked and suggested, from what it does send.

    Its reply is a question when ``act`` starts with ``clarify``, ``engine_status`` is
    ``CLARIFICATION``, it lists ``clarification_choices`` or it left something
    ``pending``. The answers are the choices, else the quick replies; an offer it is
    waiting on is answered with yes or no. Otherwise the quick replies are next steps.
    """
    pending = payload.get("pending") if isinstance(payload.get("pending"), dict) else {}
    choices = payload.get("clarification_choices") or []
    quick = payload.get("quick_replies") or []
    if choices:
        return choices, []
    if pending.get("offer"):
        return list(YES_NO["en" if payload.get("language") == "en" else "ka"]), quick
    asking = (str(payload.get("act") or "").startswith("clarify")
              or payload.get("engine_status") == "CLARIFICATION" or bool(payload.get("pending")))
    return (quick, []) if asking else ([], quick)


def _split(payload: Any, keep: Keep | None = None) -> tuple[list[dict], list[dict]]:
    """The answers to the question a reply asks, and the next steps it suggests."""
    if not isinstance(payload, dict) or payload.get("status") == "RUNNING":
        return [], []  # A turn that is still running has asked nothing yet.
    stated = "question" in payload or "suggestions" in payload
    if stated:
        asked = payload.get("question")
        answers = asked.get("options") if isinstance(asked, dict) else None
        steps = payload.get("suggestions")
    else:
        answers, steps = _legacy(payload)
    kept = _numbered(answers, OPTION_LIMIT, keep)
    chosen = [option for _, option in kept]
    following = [option for _, option
                 in _numbered(steps, SUGGESTION_LIMIT + OPTION_LIMIT, keep)]
    in_place = all(place == index for index, (place, _) in enumerate(kept))
    if len(chosen) >= 2 and (in_place or not stated):
        return chosen, _distinct(following)
    # Fewer than two answers is not a choice. And an engine's answers are numbered by
    # their place (a tap sends that number), so once one before another is left out the
    # numbers no longer hold. What is left can still be tapped, as a next step that
    # sends its text only.
    return [], _distinct(chosen + following)


def question(payload: Any, keep: Keep | None = None) -> dict | None:
    """``{"options": [...]}`` with two to eight answers when the reply asks, else None.

    The place of an answer in the list (from 1) is the number a tap sends back as
    ``choice``, and for an engine that states its question it is that engine's own
    numbering. The question's sentence is not repeated here: the reply's text carries
    it (see ``answer``).
    """
    answers, _ = _split(payload, keep)
    return {"options": answers} if answers else None


def answer(payload: Any, limit: int = ANSWER_LIMIT) -> str:
    """The whole text of a reply, the question it asks included.

    An engine writes its question into ``answer`` itself. Should one state the question
    apart from it, in a reply that neither repeats that sentence nor asks anything at
    all, the sentence is added here: answer buttons under a text that asks nothing would
    be a riddle.
    """
    turn = payload if isinstance(payload, dict) else {}
    text = turn.get("answer") if isinstance(turn.get("answer"), str) else ""
    asked = turn.get("question")
    sentence = _line(asked.get("text"), LABEL_LIMIT * 5) if isinstance(asked, dict) else ""
    if sentence and "?" not in text and sentence.casefold() not in _line(text, limit).casefold():
        text = f"{text.rstrip()} {sentence}".strip()
    return text[:max(0, limit)]


def suggestions(payload: Any, limit: int = SUGGESTION_LIMIT,
                keep: Keep | None = None) -> list[dict]:
    """Next steps that describe themselves ("rear pads too"), as ``options`` returns them."""
    _, steps = _split(payload, keep)
    return steps[:max(0, limit)]


def _identifier(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return _line(value, ID_LIMIT)


def groups(payload: Any) -> list[dict]:
    """The headings a reply's parts are listed under, in the engine's order: at most six
    of ``{"id", "label", "count"}``. An older engine has none."""
    stated = payload.get("groups") if isinstance(payload, dict) else None
    found: list[dict] = []
    for entry in stated if isinstance(stated, list | tuple) else ():
        if not isinstance(entry, dict):
            continue
        identifier, label = _identifier(entry.get("id")), _line(entry.get("label"), LABEL_LIMIT)
        if not identifier or not label or any(identifier == known["id"] for known in found):
            continue
        count = entry.get("count")
        count = count if isinstance(count, int) and not isinstance(count, bool) else 0
        found.append({"id": identifier, "label": label,
                      "count": min(max(count, 0), COUNT_LIMIT)})
        if len(found) >= GROUP_LIMIT:
            break
    return found


def group(card: Any) -> str:
    """The id of the group a card is listed under, or empty."""
    return _identifier(card.get("group")) if isinstance(card, dict) else ""


def reason(card: Any) -> str:
    """The engine's own sentence on why it offers this part, on one line of 160 characters."""
    return _line(card.get("reason"), REASON_LIMIT) if isinstance(card, dict) else ""


def confidence(card: Any, vehicle: Any = None) -> str:
    """How sure an offer is: one of ``LEVELS``, or empty when nothing can be said.

    ``confirmed``: a part number from a source specific to this car is on the row.
    ``listed``: the row's own text names this model with a year range that includes it.
    ``check``: the fit rests on the model's name alone or on indirect evidence.
    ``spec``: the row's text ties it to no car (an engine oil, a fluid, a universal
    item) and it was chosen because its specification is what the car needs.

    An engine's own level is used as it stands, and a value that is none of these is not
    passed on: a screen shows no label then, never a wrong one. For an older engine the
    level is read from ``basis`` and ``fitment_status``, and only when the car's model is
    known: without a car there is nothing for a part to fit.
    """
    if not isinstance(card, dict):
        return ""
    stated = card.get("confidence")
    if isinstance(stated, str) and stated.strip():
        # The engine speaks for itself here: a level this module does not know is no
        # level, and nothing is read out of the older fields in its place.
        return stated if stated in LEVELS else ""
    if not (isinstance(vehicle, dict) and str(vehicle.get("model") or "").strip()):
        return ""
    basis = card.get("basis") if isinstance(card.get("basis"), str) else ""
    if basis in _UNRATED:
        return ""
    if basis in _LEGACY_LEVELS:
        return _LEGACY_LEVELS[basis]
    verified = card.get("fitment_status") == "VERIFIED"
    if basis == "fitment":  # Reviewed fitment data, or only a candidate from it.
        return "confirmed" if verified else "check"
    return "confirmed" if verified else ""


def view(payload: Any) -> dict:
    """Everything above for one turn: what the staff route adds to the turn it already
    sends. ``cards`` runs parallel to the turn's own cards."""
    turn = payload if isinstance(payload, dict) else {}
    cards = turn.get("cards") if isinstance(turn.get("cards"), list | tuple) else ()
    vehicle = turn.get("vehicle")
    return {
        "question": question(turn), "suggestions": suggestions(turn), "groups": groups(turn),
        "cards": [{"group": group(card), "reason": reason(card),
                   "confidence": confidence(card, vehicle)} for card in cards],
    }
