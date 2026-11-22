"""Messenger messages laid out from the website chat's public view of a turn.

``messenger_engine`` hands over the same ``site_chat.public_turn`` dict the website's chat widget
gets, including the SAP codes on part cards. Messenger shows plain text bubbles
of at most 2,000 characters and up to 13 quick replies of at most 20 characters, under the last
bubble only. This module fits the view into those limits:

- the answer and its note, split at paragraph, then sentence, then line, then word breaks;
- up to five part lines, under their group headings when the parts are in several groups;
- the question's answers as quick replies, or as a numbered list when they do not fit;
- the channel's own fixed notices, in Georgian and English. The assistant itself follows the
  customer's language (Russian included); these texts are only the channel's.

The only parts of a customer's message read here are the channel's own mechanics: the payload of
a quick reply and a bare number typed under a numbered list. Understanding the message is the
assistant's job.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from automotive.site_strings import STRINGS

TEXT_LIMIT = 2000  # characters in one Messenger text message
QUICK_REPLY_LIMIT = 13
TITLE_LIMIT = 20  # characters in a quick reply's title
CARD_LINES = 5
CHOICE_PREFIX = "CHOICE:"  # + the answer's number from 1, the engine's own numbering
SUGGESTION_PREFIX = "SUGGEST:"
MORE_PAYLOAD = "MORE"
CURRENCY = "₾"
NBSP = " "
AVAILABILITY = ("in_stock", "soon", "unavailable", "unknown")

# The channel's own words. Georgian is the shop's language and the default. Sentences have no
# em-dashes (the site's copy rule); the part lines use them only as separators.
TEXTS: dict[str, dict[str, str]] = {
    "ka": {
        "price_on_request": "ფასი შეთანხმებით",
        "more_button": "მეტის ნახვა",
        "more_line": "კიდევ {count} ნაწილი მოიძებნა.",
        "numbered_hint": "უპასუხეთ ნომრით ან სიტყვებით.",
        "late": "ბოდიშს გიხდით დაგვიანებული პასუხისთვის.",
        "fallback": "ბოდიში, პასუხის მომზადება ახლა ვერ მოხერხდა.",
        "fallback_call": "დაგვირეკეთ: {phone}",
        "fallback_later": "გთხოვთ, ცოტა ხანში ისევ მოგვწეროთ.",
        "phone": "ტელეფონი: {phone}",
        "cap": "დღევანდელი ლიმიტი ამოიწურა. დაგვირეკეთ ან მოგვწერეთ ხვალ.",
        "cap_phone": "დღევანდელი ლიმიტი ამოიწურა. დაგვირეკეთ ნომერზე {phone} ან მოგვწერეთ ხვალ.",
        "media": ("ჩატში ჯერ მხოლოდ ტექსტს ვკითხულობთ. მოგვწერეთ, გთხოვთ, მანქანა ან VIN კოდი "
                  "და რომელი ნაწილი გჭირდებათ."),
        "vins": ("შეტყობინებაში ორი სხვადასხვა VIN კოდია. გთხოვთ, მოგვწეროთ ერთი: იმ მანქანის, "
                 "რომლისთვისაც ნაწილს ეძებთ."),
        "status_on": "ბოტი: ჩართულია",
        "status_off": "ბოტი: გამორთულია",
        "status_paused": "ბოტი: შეჩერებულია (AI პროვაიდერის ანგარიში)",
        "status_test": "ბოტი: სატესტო რეჟიმი, პასუხობს მხოლოდ ტესტერებს ({count})",
        "status_turns": "დღევანდელი პასუხები: {count} / {limit}",
        "status_turns_open": "დღევანდელი პასუხები: {count}",
        "status_comments_on": "კომენტარები: ჩართულია",
        "status_comments_off": "კომენტარები: გამორთულია",
        "switched_on": "ბოტი ჩაირთო. ამიერიდან ახალ შეტყობინებებს უპასუხებს.",
        "switched_off": "ბოტი გამოირთო. შეტყობინებებს აღარ უპასუხებს.",
        "already_on": "ბოტი უკვე ჩართულია.",
        "already_off": "ბოტი უკვე გამორთულია.",
        "switched_test": ("ჩაირთო სატესტო რეჟიმი. ბოტი პასუხობს მხოლოდ ტესტერებს ({count}); "
                          "დანარჩენ მომხმარებლებს თქვენ პასუხობთ."),
        "already_test": "სატესტო რეჟიმი უკვე ჩართულია. ტესტერები: {count}.",
        "on_refused": ("ბოტი არ ჩაირთო. ჯერ Meta Business Suite-ში გამორთეთ „Auto reply“ და "
                       "ეს პანელში დაადასტურეთ."),
        "help": "ბრძანებები: #bot on, #bot test, #bot off, #bot status",
        "command_failed": "ბრძანება ვერ შესრულდა. სცადეთ თავიდან ან გამოიყენეთ პანელი.",
    },
    "en": {
        "price_on_request": "Price on request",
        "more_button": "Show more",
        "more_line": "{count} more parts found.",
        "more_line_one": "1 more part found.",
        "numbered_hint": "Reply with a number or in your own words.",
        "late": "Sorry for the late reply.",
        "fallback": "Sorry, we could not prepare an answer just now.",
        "fallback_call": "Please call us: {phone}",
        "fallback_later": "Please write to us again in a little while.",
        "phone": "Phone: {phone}",
        "cap": "Today's limit is used up. Call us or write again tomorrow.",
        "cap_phone": "Today's limit is used up. Call us at {phone} or write again tomorrow.",
        "media": ("For now we can only read text here. Please type your car or VIN and the "
                  "part you need."),
        "vins": ("Your message has two different VINs. Please send just one, for the car you "
                 "need the part for."),
        "status_on": "Bot: on",
        "status_off": "Bot: off",
        "status_paused": "Bot: paused (AI provider account)",
        "status_test": "Bot: test mode, answers testers only ({count})",
        "status_turns": "Replies today: {count} / {limit}",
        "status_turns_open": "Replies today: {count}",
        "status_comments_on": "Comments: on",
        "status_comments_off": "Comments: off",
        "switched_on": "The bot is on. It answers new messages from now on.",
        "switched_off": "The bot is off. It no longer answers messages.",
        "already_on": "The bot is already on.",
        "already_off": "The bot is already off.",
        "switched_test": ("Test mode is on. The bot answers only the testers ({count}); you "
                          "answer everyone else."),
        "already_test": "Test mode is already on. Testers: {count}.",
        "on_refused": ("The bot was not switched on. First switch off “Auto reply” in Meta "
                       "Business Suite and confirm it in the dashboard."),
        "help": "Commands: #bot on, #bot test, #bot off, #bot status",
        "command_failed": "The command did not go through. Try again, or use the dashboard.",
    },
}
# What the website's part search and chat call the same four states.
_AVAILABILITY_FALLBACK = {
    "ka": {"in_stock": "მარაგშია", "soon": "საწყობშია, 1-2 საათში",
           "unavailable": "არ არის მარაგში", "unknown": "დააზუსტეთ მაღაზიაში"},
    "en": {"in_stock": "In stock", "soon": "At the warehouse, 1-2 hours",
           "unavailable": "Out of stock", "unknown": "Ask the shop"},
}

# Where a long text may be cut, best first. Each pattern's group is the gap that is dropped:
# a blank line, the space after a sentence's last mark (and its closing quote or bracket), a line
# break, the space between words. A no-break space is never a gap: it holds "45.50 ₾" together.
_BREAKS = (
    (re.compile(r"\n[^\S\n]*\n\s*"), 0),
    (re.compile(r"[.!?…]+[\"'»”’)\]]*(\s+)"), 1),
    (re.compile(r"\n\s*"), 0),
    (re.compile(r"[^\S\n   ]+"), 0),
)
_SURROGATE = re.compile("[\ud800-\udfff]")
_CHOICE = re.compile(r"CHOICE:([0-9]{1,3})")
# "2", " 2 ", "2." or "2)": a number typed under a numbered list, and nothing else.
_NUMBER = re.compile(r"\s*([0-9]{1,3})\s*[.)]?\s*")


@dataclass(frozen=True)
class RenderedReply:
    """What to send for one turn.

    ``messages`` are Send API ``message`` objects in order; only the last carries
    ``quick_replies``. ``options`` are the labels of the question's answers in the engine's own
    numbering (empty when the turn asks nothing) and ``numbered`` says they were sent as a
    numbered list instead of buttons: keep both with the chat, for ``parse_choice`` on the
    customer's next message. ``next_offset`` is where the next "more" page of parts starts,
    None when every part was shown."""

    messages: list[dict]
    options: list[str]
    numbered: bool
    next_offset: int | None = None


def text_language(language: Any) -> str:
    """The language of the channel's own texts: English when the engine said "en", Georgian
    for anything else (Georgian is the shop's language; the engine's "ru" turns say "en")."""
    return "en" if language == "en" else "ka"


def render(public: Any, *, language: Any, offset: int = 0, opening: str = "") -> RenderedReply:
    """The messages for one public turn view.

    ``offset`` above 0 renders the next page of parts after a "more" tap: the parts from there
    and the buttons, without the answer again. ``opening`` is put before the answer, in the
    first message (the apology for a late reply)."""
    view = public if isinstance(public, dict) else {}
    lang = text_language(language)
    start = offset if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0 else 0
    blocks = [_clean(opening)]
    if start == 0:
        blocks += [_clean(view.get("answer")), _clean(view.get("note"))]
    cards, shown, remaining = _cards(view, lang, start)
    blocks.append(cards)
    asked = view.get("question") if isinstance(view.get("question"), dict) else {}
    options = _labels(asked.get("options"))
    as_buttons = bool(options) and quick_replies_fit(options)
    numbered = bool(options) and not as_buttons
    if numbered and start == 0:
        blocks.append(_numbered(options, lang))
    chunks = split_text("\n\n".join(block for block in blocks if block))
    quick: list[dict] = []
    if as_buttons:
        quick = [_quick(label, f"{CHOICE_PREFIX}{number}")
                 for number, label in enumerate(options, 1)]
    if remaining and len(quick) < QUICK_REPLY_LIMIT:
        quick.append(_quick(TEXTS[lang]["more_button"], MORE_PAYLOAD))
    taken = {label.casefold() for label in options}
    steps = [label for label in _labels(view.get("suggestions")) if label.casefold() not in taken]
    # Unlike a question's answers, suggestions are independent next steps whose tap sends the
    # label itself as the customer's text, so a label too long for Meta's 20-character title is
    # left out on its own instead of taking the others with it (each keeps the number the
    # engine gave it). The ones that fit still go all together or not at all when the answers
    # leave too little room, so a list of next steps is never shown cut short.
    fitting = [(number, label) for number, label in enumerate(steps, 1)
               if quick_replies_fit([label])]
    if fitting and len(fitting) <= QUICK_REPLY_LIMIT - len(quick):
        quick += [_quick(label, f"{SUGGESTION_PREFIX}{number}") for number, label in fitting]
    messages: list[dict] = [{"text": chunk} for chunk in chunks]
    if messages and quick:
        messages[-1]["quick_replies"] = quick
    return RenderedReply(messages=messages, options=options, numbered=numbered,
                         next_offset=start + shown if remaining else None)


def quick_replies_fit(labels: Any, room: int = QUICK_REPLY_LIMIT) -> bool:
    """Whether every label can be a quick reply: at most ``room`` of them, each 1-20
    characters. It is all or none, so the numbers of a question's answers never shift."""
    if not isinstance(labels, list | tuple) or not 0 < len(labels) <= min(room,
                                                                          QUICK_REPLY_LIMIT):
        return False
    return all(isinstance(label, str) and label.strip() and _units(label) <= TITLE_LIMIT
               for label in labels)


def parse_choice(text: Any, quick_reply_payload: Any, options: Any, numbered: bool) -> int | None:
    """The number of the answer a customer chose, counted from 1, or None.

    A ``CHOICE:n`` payload comes from one of our own quick replies; when ``options`` is
    unknown (the worker restarted) it is still passed on, and the engine checks it against the
    question it asked. A bare number counts only under a numbered list, and only within it."""
    count = len(options) if isinstance(options, list | tuple) else 0
    if isinstance(quick_reply_payload, str):
        match = _CHOICE.fullmatch(quick_reply_payload.strip())
        if match:
            number = int(match.group(1))
            return number if 1 <= number <= (count or QUICK_REPLY_LIMIT) else None
    if numbered and count and isinstance(text, str):
        match = _NUMBER.fullmatch(text)
        if match and 1 <= int(match.group(1)) <= count:
            return int(match.group(1))
    return None


def split_text(text: Any, limit: int = TEXT_LIMIT) -> list[str]:
    """``text`` in pieces of at most ``limit`` characters, none empty.

    Each piece ends at the last paragraph break that fits, else the last sentence end, else the
    last line break, else the last space; only a single word longer than ``limit`` is cut
    inside. A break in the second half of the room is preferred to a better kind of break in
    the first half, so a short first paragraph does not become a message of its own. Length is
    counted in UTF-16 units, as Messenger counts: an emoji is two."""
    rest = text.strip() if isinstance(text, str) else ""
    pieces: list[str] = []
    while rest:
        if _units(rest) <= limit:
            pieces.append(rest)
            break
        end, resume = _cut(rest, limit)
        piece = rest[:end].rstrip()
        if piece:
            pieces.append(piece)
        rest = rest[resume:].lstrip()
    return pieces


# -- the channel's own texts ---------------------------------------------------------------


def late_apology(language: Any) -> str:
    """Opens a reply to a message more than an hour old (the laptop slept)."""
    return TEXTS[text_language(language)]["late"]


def fallback_text(language: Any, phone: Any = "", custom: Any = "") -> str:
    """The one message sent when no answer could be made. It points to the phone and never
    promises a call back. ``custom`` is the dashboard's own text (``fallback_ka`` /
    ``fallback_en``); the phone is added under it."""
    texts = TEXTS[text_language(language)]
    number = _one_line(phone)[:40]
    own = _clean(custom)[:500]
    if own:
        return f"{own}\n{texts['phone'].replace('{phone}', number)}" if number else own
    follow = texts["fallback_call"].replace("{phone}", number) if number \
        else texts["fallback_later"]
    return f"{texts['fallback']} {follow}"


def cap_notice(language: Any, phone: Any = "") -> str:
    """Sent once when a daily turn cap is reached (the shop's or this customer's)."""
    texts = TEXTS[text_language(language)]
    number = _one_line(phone)[:40]
    return texts["cap_phone"].replace("{phone}", number) if number else texts["cap"]


def media_only_text(language: Any) -> str:
    """For a photo, voice note, sticker or other attachment without text."""
    return TEXTS[text_language(language)]["media"]


def conflicting_vins_text(language: Any) -> str:
    """For a message with two different VINs, which the engine refuses."""
    return TEXTS[text_language(language)]["vins"]


def bot_status_text(language: Any = "ka", *, enabled: bool, paused: Any = "",
                    turns_today: int | None = None, turn_limit: int | None = None,
                    comments_enabled: bool | None = None, mode: str | None = None,
                    testers: int | None = None) -> str:
    """The answer to ``#bot status`` from an admin's chat: the switch (or the pause and its
    reason; ``mode`` 'test' with the number of ``testers``), today's replies against the cap,
    and the comment switch, one per line."""
    texts = TEXTS[text_language(language)]
    if paused:
        lines = [texts["status_paused"]]
    elif mode == "test":
        lines = [texts["status_test"].replace("{count}", str(_count(testers) or 0))]
    else:
        lines = [texts["status_on"] if enabled else texts["status_off"]]
    if _count(turns_today) is not None:
        limit = _count(turn_limit)
        line = texts["status_turns"] if limit is not None else texts["status_turns_open"]
        lines.append(line.replace("{count}", str(turns_today)).replace("{limit}", str(limit)))
    if isinstance(comments_enabled, bool):
        lines.append(texts["status_comments_on"] if comments_enabled
                     else texts["status_comments_off"])
    return "\n".join(lines)


def bot_switched_text(language: Any, enabled: bool, changed: bool = True) -> str:
    """The answer to ``#bot on`` / ``#bot off``; ``changed`` False when it already was."""
    texts = TEXTS[text_language(language)]
    if not changed:
        return texts["already_on"] if enabled else texts["already_off"]
    return texts["switched_on"] if enabled else texts["switched_off"]


def bot_test_text(language: Any, *, changed: bool = True, testers: int = 0) -> str:
    """The answer to ``#bot test``: test mode is on (or already was), with the testers' count."""
    texts = TEXTS[text_language(language)]
    return texts["switched_test" if changed else "already_test"].replace(
        "{count}", str(_count(testers) or 0))


def bot_on_refused_text(language: Any) -> str:
    """``#bot on`` while Meta's "Auto reply" is not confirmed off in the dashboard."""
    return TEXTS[text_language(language)]["on_refused"]


def bot_help_text(language: Any) -> str:
    """The answer to a ``#bot`` command that is none of the four."""
    return TEXTS[text_language(language)]["help"]


def bot_failed_text(language: Any) -> str:
    """A ``#bot`` command the worker could not carry out (the database refused): the switch is
    as it was."""
    return TEXTS[text_language(language)]["command_failed"]


# -- parts ---------------------------------------------------------------------------------


def card_line(card: Any, language: Any) -> str:
    """Format one public card with its brand, SAP code, price and availability."""
    if not isinstance(card, dict):
        return ""
    lang = text_language(language)
    name = _one_line(card.get("name")) or _one_line(card.get("fits"))
    if not name:
        return ""
    parts = [name]
    brand = _one_line(card.get("brand"))
    if brand:
        parts.append(brand)
    sap_code = _one_line(card.get("sap_code"))
    if sap_code:
        parts.append(STRINGS[lang]["js.chat.sap_code"].format(code=sap_code))
    parts += [price_text(card.get("price"), lang), availability_text(card.get("availability"),
                                                                      lang)]
    return "• " + " — ".join(parts)


def price_text(value: Any, language: Any) -> str:
    """A price as the website writes it: "1 234.50 ₾" in Georgian, "₾1 234.50" in English;
    an unknown or zero price is "on request"."""
    lang = text_language(language)
    if (isinstance(value, bool) or not isinstance(value, int | float)
            or not math.isfinite(value) or not 0 < value < 1e12):
        return TEXTS[lang]["price_on_request"]
    whole, cents = divmod(round(value * 100), 100)
    amount = f"{whole:,}".replace(",", NBSP) + (f".{cents:02d}" if cents else "")
    return f"{amount}{NBSP}{CURRENCY}" if lang == "ka" else f"{CURRENCY}{amount}"


def availability_text(value: Any, language: Any) -> str:
    """The website's label for a card's availability (in stock, soon, out, ask the shop)."""
    lang = text_language(language)
    key = value if value in AVAILABILITY else "unknown"
    return STRINGS.get(lang, {}).get(f"js.avail.{key}") or _AVAILABILITY_FALLBACK[lang][key]


def _cards(view: dict, lang: str, start: int) -> tuple[str, int, int]:
    """The part lines from ``start``: text, how many parts it shows, how many are left."""
    cards = [card for card in view.get("cards") or [] if isinstance(card, dict)]
    page = cards[start:start + CARD_LINES]
    if not page:
        return "", 0, 0
    total = view.get("total")
    total = total if isinstance(total, int) and not isinstance(total, bool) else 0
    total = max(total, len(cards))
    groups = {entry["id"]: _one_line(entry.get("label"))
              for entry in view.get("groups") or []
              if isinstance(entry, dict) and isinstance(entry.get("id"), str)
              and _one_line(entry.get("label"))}
    headed = len(groups) > 1
    lines: list[str] = []
    current = None
    for card in page:
        group = card.get("group")
        if headed and group in groups and group != current:
            heading = groups[group]
            heading = heading if heading[-1:] in ".:!?" else heading + ":"
            lines.append(("\n" if lines else "") + heading)
            current = group
        line = card_line(card, lang)
        if line:
            lines.append(line)
    remaining = max(0, total - start - len(page))
    if remaining and lines:
        texts = TEXTS[lang]
        line = texts.get("more_line_one") if remaining == 1 else None
        lines.append(line or texts["more_line"].replace("{count}", str(remaining)))
    return "\n".join(lines), len(page), remaining


def _numbered(options: list[str], lang: str) -> str:
    lines = [f"{number}. {_one_line(label)}" for number, label in enumerate(options, 1)]
    return "\n".join([*lines, TEXTS[lang]["numbered_hint"]])


def _labels(values: Any) -> list[str]:
    """Option labels in their given order. None is dropped or moved: an answer's place is the
    number its tap sends."""
    labels: list[str] = []
    for entry in values if isinstance(values, list | tuple) else ():
        if isinstance(entry, dict):
            entry = entry.get("label") or entry.get("value")
        labels.append(_one_line(entry))
    return labels


def _quick(label: str, payload: str) -> dict:
    return {"content_type": "text", "title": _one_line(label), "payload": payload}


# -- text ----------------------------------------------------------------------------------


def _cut(text: str, limit: int) -> tuple[int, int]:
    """Where the first piece of ``text`` ends, and where the rest starts."""
    room = max(1, _room(text, limit))
    window = text[:room + 1]  # a gap right after the room still ends the piece in time
    for low in (max(1, room // 2), 1):
        for pattern, group in _BREAKS:
            best = None
            for match in pattern.finditer(window):
                if low <= match.start(group) <= room:
                    best = (match.start(group), match.end(group))
            if best:
                return best
    return room, room


def _units(text: str) -> int:
    """Length in UTF-16 code units, the way Messenger counts its limits."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _room(text: str, limit: int) -> int:
    """How many characters of ``text`` fit in ``limit`` UTF-16 units."""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return index
    return len(text)


def _clean(value: Any) -> str:
    """Text for a message: lines kept, spaces at their ends dropped, no NUL or lone
    surrogate (the Send API's JSON could not carry one)."""
    if not isinstance(value, str):
        return ""
    value = _SURROGATE.sub("�", value.replace("\x00", ""))
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n")
             .split("\n")]
    return "\n".join(lines).strip()


def _one_line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_SURROGATE.sub("�", value.replace("\x00", "")).split())


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 \
        else None
