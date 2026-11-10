"""In-memory search over the warehouse snapshot for the parts assistant.

The assistant's model decides what a customer means and which rows fit; this module only
finds rows and counts them. It holds no automotive knowledge: no makes, models, part
categories, aliases or synonyms. A term matches exactly what its letters say, and a result
that finds little reports what the catalogue has instead, so the caller can correct itself.

How a term matches the name, vehicle text and brand of a row:

- every word of the term must be in the row, in any order;
- a Georgian word matches the start of a word (`ხუნდ` finds `ხუნდი`);
- any other word equals a whole token however the row spaces it: `CR-V`, `CRV` and `CR V`
  are one word, and `E60` is not in `E600`; a trailing `*` matches the start of a token;
- a decimal is one number, written with a point or a comma: `2.5` is `2,5` and never `5`.

One ``CatalogIndex`` is built per snapshot (see ``index_for``). Nothing in it changes after
it is built, so any number of threads may read it. Only the guide's text can be printed anew:
with what the part names mean, read by a model from the rows that carry them and kept in a
file (``assistant_names``). The index is given those readings; it does not make them.
"""

from __future__ import annotations

import hashlib
import heapq
import html
import os
import re
import threading
import unicodedata
import weakref
from array import array
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from automotive.codes import normalize_code
from automotive.rules import Rules
from automotive.site_catalog import parse_price
from automotive.stock import (
    IN_STORE,
    RANKS,
    UNAVAILABLE,
    UNKNOWN,
    WITHIN_HOURS,
    classify,
    parse_count,
)

COLUMNS = ["id", "name", "vehicle", "brand", "code", "cross_codes", "price", "store",
           "warehouse"]
MAX_LIMIT = 60
MAX_TERMS = 40  # in one group
MAX_TERM_LENGTH = 80
MAX_WORDS = 8  # in one term
MAX_JOIN = 6  # adjacent catalogue tokens one typed word may span
CROSS_SHOWN = 8
MIN_NUMBER = 4  # shorter numbers are not looked up
NEAR_ROWS = 10  # rows of a result found under a spelling one letter away
# A short word of the names goes with a letter that part numbers end in when a tenth of its
# rows' numbers end in it, five times as often as in the whole catalogue.
ENDING_SHARE, ENDING_LIFT = 0.10, 5.0
# A brand's number series (the letters its numbers begin with) goes with a part name when
# it has this many rows under the name and at least half of all its rows are under it.
SERIES_ROWS, SERIES_SHOWN = 5, 3
_SERIES = re.compile(r"[A-Z]{2,}(?=[A-Z0-9/]*\d)")
GUIDE_NAMES = 175  # opening words of part names that the guide lists
# What a reader of a part name is shown of it: the names written most often under the
# opening words, and rows that carry them.
NAME_FORMS, NAME_ROWS = 8, 6
READING_CHARS = 120  # of a reading printed beside a name
NAMES_LEGEND = ("opening words rows/in stock e.g. the name written most often; the number "
                "series of brands (brand and first letters) that go with this name and "
                "with few others")
READ_LEGEND = ("opening words rows/in stock = what the part is (read from the rows that "
               "carry the name; where a neighbouring name is easily taken for it, also what "
               "it is not; no reading where the rows did not tell); e.g. the name written "
               "most often; the number series of brands (brand and first letters) that go "
               "with this name and with few others")
NEAR_WORDS = 5  # near spellings tried for one typed word
COMMON_NUMBER = 200  # a number on more rows than this says nothing about equivalence
NO_VEHICLE = "(no vehicle text)"
FIELD_ORDER = {"code": 0, "shop_code": 1, "cross": 2}
STOCKED = RANKS[WITHIN_HOURS]  # a row ranked up to here is in the shop or its warehouse

# Georgian letters in every block Unicode gives them; casefold maps the capitals back here.
_KA = "\u10a0-\u10ff\u1c90-\u1cbf\u2d00-\u2d2f"
# A token is a run of Georgian letters, a short decimal number with any letters written
# onto it (`2.5`, `2.0tdi`), or a run of other letters and digits. A decimal stays whole,
# so that a lone `5` never matches half of `2.5`.
_LEX = re.compile(
    rf"(?P<ka>[{_KA}]+)"
    rf"|(?P<dec>(?<![^\W_])(?<!\d\.)(?P<number>\d{{1,2}}\.\d{{1,2}})(?!\.?\d)"
    rf"(?P<letters>[^\W_{_KA}]*))"
    rf"|(?P<la>[^\W_{_KA}]+)")
# `2,5` may be a decimal or a list of two numbers; a row is indexed under both readings.
_COMMA_DECIMAL = re.compile(
    rf"(?<![^\W_])(?<!\d[.,])(\d{{1,2}}),(\d{{1,2}})(?![.,]?\d)([^\W_{_KA}]*)")
# One to three letters before three or more digits, and nothing after: J150, W212, OM651.
_LETTERED = re.compile(r"[^\W\d_]{1,3}(\d{3,})")
_HYPHENS = "\\-\u2010-\u2015"
_SOFT = re.compile(rf"[\s.{_HYPHENS}]*")  # between tokens that may be read as one word
_GLUE = re.compile(rf"[.{_HYPHENS}]*")  # inside one typed word
_SPACE = re.compile(r"\s+")
_LATIN_START = re.compile(rf"[^\W_{_KA}]")
_SUBNUMBER = re.compile(r"[()\[\]{}=;,]+")
_DIGIT = re.compile(r"\d")
_BRAND_WORD = re.compile(r"[\W_]+")
# A year written in a vehicle text, with the marks around it: `2006->`, `06-12`, `>2009`.
_YEAR = re.compile(
    rf"(?<![\w.,/])(?<!\w[{_HYPHENS}])(?:[<>{_HYPHENS}]{{1,3}} ?)?(?:\d{{4}}|\d{{1,2}})"
    rf"(?: ?[<>+{_HYPHENS}]{{1,3}}(?: ?(?:\d{{4}}|\d{{1,2}}))?)*(?![\w.,])")
_FOLLOWER_SPLIT = re.compile(r"[\s,;/]+")
# Later words of a vehicle text worth listing: a word, or a number of up to three digits.
# Years, ranges and engine sizes have their own section or are too many to list.
_FOLLOWER = re.compile(r"\d{1,3}|\d{0,4}[^\W\d_].*")

_LONGER = {
    "make_terms": "Use the make as the catalogue writes it; the guide lists the spellings.",
    "vehicle_terms": "Use a longer piece such as a chassis code or the model word.",
    "part_terms": "Use a longer word or word stem from the part name.",
    "exclude_terms": "Use a longer word or word stem.",
}


@dataclass(frozen=True, slots=True)
class Row:
    id: str  # source_product_id
    name: str  # product_name as written
    vehicle: str  # product_model as written
    brand: str
    code: str  # original_code as written (the brand's own number)
    shop_code: str  # product_code (the shop's stock number)
    cross_codes: tuple[str, ...]  # duplicate_code split on "/", as written
    price: float | None
    store: float | None
    warehouse: float | None  # the TUZ 11 quantity
    state: str  # stock.classify(...) result


class SearchError(ValueError):
    """A call the caller can correct: what was wrong, and what to do instead."""

    def __init__(self, message: str, fix: str) -> None:
        super().__init__(f"{message} {fix}")
        self.message, self.fix = message, fix


@dataclass(frozen=True, slots=True)
class _Word:
    key: str
    georgian: bool = False
    prefix: bool = False  # typed with a trailing `*`
    joins: bool = False  # only spaces separate it from the Latin word before it


_Term = tuple[str, list[_Word], set[int]]  # a term as typed, its words, the rows it matches


def _text(value: Any) -> str:
    # The same reading of a source cell as site_catalog._label: entities unescaped
    # (`2005&gt;`), whitespace collapsed.
    return " ".join(html.unescape(str(value or "")).split())


def _fold(text: str) -> str:
    # casefold, never upper(): upper() moves Georgian letters to another Unicode block.
    return unicodedata.normalize("NFKC", text).casefold()


def _split(cell: str) -> tuple[str, ...]:
    if "/" not in cell:  # the usual case keeps the one string instead of copying it
        return (cell,) if cell else ()
    return tuple(part for part in (piece.strip() for piece in cell.split("/")) if part)


def _unchanged(text: str, source: Any) -> str:
    """The source object itself when cleaning changed nothing, so it is stored once."""
    return source if isinstance(source, str) and source == text else text


def _clip(text: str, size: int) -> str:
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def _latin_tail(name: str) -> str:
    """The name from its first Latin letter or digit on: where a car is written, if at all."""
    match = _LATIN_START.search(name)
    return name[match.start():] if match else ""


def _name_form(name: str) -> str:
    """A part name without the car, size or number written after it: the words it begins
    with. Names that differ only in what follows are one form of the name."""
    tail = _latin_tail(name)
    return " ".join(name[:len(name) - len(tail)].split()).rstrip(" .,/(-") or name


def _compact(value: float | None) -> float | int | None:
    return int(value) if value is not None and float(value).is_integer() else value


def _id_key(value: str) -> tuple[int, int, str]:
    return (0, int(value), "") if value.isascii() and value.isdigit() else (1, 0, value)


def _round2(value: int) -> int:
    """Two significant digits, so that a count moving a little leaves the text unchanged."""
    if value < 100:
        return value
    scale = 10 ** (len(str(value)) - 2)
    return (value + scale // 2) // scale * scale


def _pack(rows: list[int]) -> int | tuple[int, ...]:
    return rows[0] if len(rows) == 1 else tuple(rows)


def _unpack(rows: int | tuple[int, ...] | None) -> tuple[int, ...]:
    return () if rows is None else (rows,) if isinstance(rows, int) else rows


def _one_edit(left: str, right: str) -> bool:
    """One insertion, deletion, substitution or swap of neighbours apart, and not equal."""
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    at = 0
    while at < len(left) and left[at] == right[at]:
        at += 1
    if len(left) < len(right):
        return left[at:] == right[at + 1:]
    if left[at + 1:] == right[at + 1:]:
        return True
    return (at + 1 < len(left) and left[at] == right[at + 1] and left[at + 1] == right[at]
            and left[at + 2:] == right[at + 2:])


def _scan(text: str) -> tuple[set[str], set[str], str]:
    """Georgian tokens, other tokens, and the other tokens in order as one "chain" string.

    In the chain `~` joins two tokens that only spaces, dots or hyphens separate and a space
    separates the rest: `cr-v iii, 2.4` gives ` cr~v~iii 2.4 `. A typed word is read against
    the chain to decide whether it equals one token or several neighbours written apart.
    A row that writes `2,5` is read twice, as two numbers and as the decimal a term means.
    """
    georgian: set[str] = set()
    latin: set[str] = set()
    chain: list[str] = []  # every token follows the mark that separates it from the last
    decimal = _COMMA_DECIMAL.sub(r"\1.\2\3", text)
    for reading in (text, decimal) if decimal != text else (text,):
        # Other readings of a number with letters written onto it: `2.0` and `tdi`.
        extra: set[str] = set()
        end, joinable = 0, False
        for match in _LEX.finditer(reading):
            token = match.group()
            if match.lastgroup == "ka":
                georgian.add(token)
                joinable = False
            else:
                chain.append("~" if joinable and _SOFT.fullmatch(reading, end, match.start())
                             else " ")
                latin.add(token)
                chain.append(token)
                joinable = True
                if match["letters"]:
                    extra.update((match["number"], match["letters"]))
            end = match.end()
        for token in sorted(extra - latin):
            latin.add(token)
            chain.append(f" {token}")
    chain.append(" ")
    return georgian, latin, "".join(chain)


def _in_chain(chains: bytes, needle: bytes, start: int, end: int) -> bool:
    """Is the needle in this row's chain as whole tokens, not as a piece of longer ones?"""
    at = chains.find(needle, start, end)
    while at >= 0:
        if chains[at - 1] in b" ~" and chains[at + len(needle)] in b" ~":
            return True
        at = chains.find(needle, at + 1, end)
    return False


def _words(term: str) -> list[_Word]:
    """The words of one typed term. `cr-v` is one word, `cr v` two that may be read as one."""
    text = _COMMA_DECIMAL.sub(r"\1.\2\3", _fold(term))
    matches = list(_LEX.finditer(text))
    words: list[_Word] = []
    index, last_end = 0, 0
    while index < len(matches):
        match = matches[index]
        start, end = match.span()
        if match.lastgroup == "ka":
            words.append(_Word(match.group(), georgian=True))
        else:
            key = match.group()
            while index + 1 < len(matches):
                following = matches[index + 1]
                if (following.lastgroup == "ka"
                        or not _GLUE.fullmatch(text, end, following.start())):
                    break
                key += following.group()
                end = following.end()
                index += 1
            previous = words[-1] if words else None
            joins = (previous is not None and not previous.georgian and not previous.prefix
                     and _SPACE.fullmatch(text, last_end, start) is not None)
            words.append(_Word(key, prefix=text.startswith("*", end), joins=joins))
        last_end = end
        index += 1
    return words


def _opening(name: str) -> str:
    """The first two words of a folded name; three when the first is an abbreviation.

    Georgian words, plus a Latin word when the name opens with one. Numbers and codes in
    front are skipped, and the first Latin word or number after the opening ends it.
    """
    words: list[str] = []
    for match in _LEX.finditer(name):
        token = match.group()
        if match.lastgroup == "ka" or (not words and token.isalpha()):
            words.append(token)
            if len(words) == (3 if len(words[0]) < 4 else 2):
                break
        elif words:
            break
    return " ".join(words)


def first_word(vehicle: str) -> str:
    """The first word of a vehicle text, folded: how the row spells its make, as the guide
    counts it. Empty for a text without a letter or digit."""
    for word in vehicle.split():
        if any(char.isalnum() for char in word):
            return word.strip(".,;:").casefold()
    return ""


def opening_of(name: str) -> str:
    """The opening words of a part name, folded: the part type as the guide counts it."""
    return _opening(_fold(name))


def _top(counter: Counter, size: int) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))[:size]


def _by_rounded(pairs: Iterable[tuple[str, int]]) -> list[tuple[str, int]]:
    return sorted(pairs, key=lambda pair: (-_round2(pair[1]), pair[0]))


def _ending(code: str, brand: str = "") -> str:
    """The letter a part number ends in; empty for one that ends in a digit or is none. A
    brand's name written after the number (`26096 FEBI`) is not part of it."""
    key, maker = normalize_code(code), normalize_code(brand)
    if maker and key.endswith(maker):
        key = key[:-len(maker)]
    return key[-1] if (len(key) >= MIN_NUMBER and key[-1].isalpha()
                       and _DIGIT.search(key)) else ""


def _series(row: Row) -> str:
    """The brand of a row with the two letters its own number begins with (`JIKIU LS`):
    makers give each kind of part its own series. Empty for a number that begins with a
    digit, or a row without a brand."""
    brand = " ".join(row.brand.upper().split())
    letters = _SERIES.match(normalize_code(row.code))
    return f"{brand} {letters.group()[:2]}" if brand and letters else ""


def _cell_codes(pieces: Sequence[str], brand: frozenset[str] = frozenset(),
                as_typed: bool = False) -> list[tuple[str, str, bool]]:
    """(normalised, as written, whole number?) for each way to look one code cell up.

    A cell is one number or several separated by "/". A piece too short to be a number is
    a suffix of the piece before it (`AP104/2`), so the stem alone finds the row but is not
    a number the row shares with `AP104/3`. Numbers in brackets (`CL0275(CLKH30)`), a number
    written next to the row's own brand name, and a number before a short spaced suffix
    (`44994 01`) are whole numbers too. ``as_typed`` adds the cell in one piece, for a caller
    who copies all of it. Words without a digit (a brand, a description) are not numbers.
    """
    pieces = [piece for piece in (piece.strip() for piece in pieces) if piece]
    keys = [normalize_code(piece) for piece in pieces]
    groups: list[list[int]] = []
    lead: list[int] = []
    for index, key in enumerate(keys):
        if len(key) >= MIN_NUMBER:
            groups.append([*lead, index])
            lead = []
        elif groups:
            groups[-1].append(index)
        else:
            lead.append(index)
    if lead:
        groups.append(lead)
    found: dict[str, tuple[str, bool]] = {}

    def add(written: str, key: str | None = None, whole: bool = True) -> None:
        key = normalize_code(written) if key is None else key
        if len(key) >= MIN_NUMBER and _DIGIT.search(key):
            # A number written without punctuation is its own key: one string, not two.
            found.setdefault(written if key == written else key, (written, whole))

    stems: list[tuple[str, str]] = []
    for group in groups:
        written = "/".join(pieces[index] for index in group)
        add(written, "/".join(keys[index] for index in group if keys[index]))
        if _SUBNUMBER.search(written):
            for part in _SUBNUMBER.split(written):
                add(part.strip())
        if " " in written:
            words = written.split()
            kept = [word for word in words if _fold(word.strip(".,")) not in brand]
            if kept and len(kept) < len(words):
                add(" ".join(kept))
            if (any(char.isdigit() for char in words[0])
                    and all(len(normalize_code(word)) <= 2 for word in words[1:])):
                add(words[0])
        if len(group) > 1:
            stems.extend((keys[index], written) for index in group)
    for key, written in stems:
        add(written, key, whole=False)
    if as_typed and len(groups) > 1:
        add("/".join(pieces), "/".join(key for key in keys if key), whole=False)
    return [(key, written, whole) for key, (written, whole) in found.items()]


def _row_codes(row: Row) -> list[tuple[str, str, str, bool]]:
    """(normalised, as written, field, whole number?) for every number of a row."""
    brand = frozenset(word for word in _BRAND_WORD.split(_fold(row.brand))
                      if len(word) > 1 and word.isalpha())
    found = [(key, written, "code", whole)
             for key, written, whole in _cell_codes(_split(row.code), brand, as_typed=True)]
    found += [(key, written, "shop_code", whole)
              for key, written, whole in _cell_codes(_split(row.shop_code), as_typed=True)]
    found += [(key, written, "cross", whole)
              for key, written, whole in _cell_codes(row.cross_codes)]
    return found


def _note(total: int, pool: int, hits: int, shown: int, rest: int, limit: int,
          partition: bool, has_part: bool, has_vehicle: bool) -> str:
    """One sentence about a result that is empty or cut; nothing when all of it is shown."""
    if has_part and has_vehicle:
        narrow = "add more words to a term or exclude_terms to narrow the result"
    elif has_part:
        narrow = "add vehicle_terms, more words to a term or exclude_terms to narrow the result"
    else:
        narrow = "other_part_names counts what they are, add part_terms to narrow the result"
    if not total:
        return "No row matches every group; `why` counts what each group matches alone."
    if not pool:
        return (f"None of the matching rows ({total}) is in stock; set in_stock_only to false "
                "to see them.")
    if pool <= limit:
        return ""
    if not partition:
        return f"Showing {shown} of {pool} rows; {narrow}."
    if not hits:
        return ("No row matches a vehicle term; other_vehicle_texts shows how the "
                f"{pool} rows that match the other terms name their cars.")
    if hits > limit:
        return f"Showing {shown} of {hits} rows that match a vehicle term; {narrow}."
    return (f"Rows that match the other terms but no vehicle term: {rest}; "
            "other_vehicle_texts counts how they name their cars.")


def _row(source: dict, rules: Rules) -> Row:
    store = parse_count(source.get("stock_store"))
    warehouse = parse_count(source.get("stock_tuz11"))
    return Row(
        id=str(source.get("source_product_id") or ""),
        name=_text(source.get("product_name")), vehicle=_text(source.get("product_model")),
        brand=_text(source.get("brand")), code=_text(source.get("original_code")),
        shop_code=_text(source.get("product_code")),
        cross_codes=_split(_text(source.get("duplicate_code"))),
        price=parse_price(source.get("price_raw")), store=store, warehouse=warehouse,
        state=classify({"store": store, "tuz11": warehouse}, rules.min_store, rules.min_tuz11))


class CatalogIndex:
    """Every row of one snapshot, searchable by words and by numbers."""

    def __init__(self, rows: Iterable[Row], rules: Rules | None = None,
                 readings: Mapping[str, str] | None = None) -> None:
        self._rules = rules or Rules()
        self._rows: tuple[Row, ...] = tuple(rows)
        count = len(self._rows)
        self._rank = bytes(RANKS[row.state] for row in self._rows)
        # The rows of each stock state that can be offered or might be: in the shop, only in
        # the warehouse, stock unknown. They are few, so sorting by stock starts from them.
        self._tiers = tuple(frozenset(position for position in range(count)
                                      if self._rank[position] == rank)
                            for rank in sorted(RANKS.values())[:-1])
        self._stocked = frozenset().union(*self._tiers[:STOCKED + 1])
        self._tiered = frozenset().union(*self._tiers)
        # Rows in id order find a row by its id, and give each row its place in that order:
        # the last, stable sort key.
        self._by_id = array("I", sorted(range(count), key=self._id_at))
        self._order = array("I", [0]) * count
        for place, position in enumerate(self._by_id):
            self._order[position] = place

        georgian: dict[str, list[int]] = defaultdict(list)
        latin: dict[str, list[int]] = defaultdict(list)
        shared: dict[str, str] = {}
        chains: list[bytes] = []
        self._chain_at = array("I", [0])  # where each row's chain starts in self._chains
        self._facets: list[str] = []  # the vehicle text, or where the name writes a car
        self._openings: list[str] = []  # the first words of the name
        for position, row in enumerate(self._rows):
            name = _fold(row.name)
            ka, la, chain = _scan(f"{name} | {_fold(row.vehicle)} | {_fold(row.brand)}")
            for token in ka:
                georgian[token].append(position)
            for token in la:
                latin[token].append(position)
            chains.append(chain.encode("utf-8"))
            self._chain_at.append(self._chain_at[-1] + len(chains[-1]))
            facet = _clip(row.vehicle or _latin_tail(row.name) or NO_VEHICLE, 60)
            self._facets.append(shared.setdefault(facet, facet))
            opening = _opening(name)
            self._openings.append(shared.setdefault(opening, opening))
        self._chains = b"".join(chains)
        self._georgian = {token: array("I", rows) for token, rows in georgian.items()}
        self._latin = {token: array("I", rows) for token, rows in latin.items()}
        self._georgian_words = sorted(self._georgian)
        self._latin_words = sorted(self._latin)
        # A chassis or engine code is letters before a number (J150, W212, OM651) and is
        # asked for by the number alone: such a word is also found under its digits.
        lettered: dict[str, list[str]] = defaultdict(list)
        for token in self._latin_words:
            code = _LETTERED.fullmatch(token)
            if code:
                lettered[code.group(1)].append(token)
        self._lettered = dict(lettered)

        # Words one deleted letter away, to find catalogue spellings near a typed word.
        deletions: dict[str, list[str]] = defaultdict(list)
        for token in self._latin_words:
            if len(token) >= 5 and token.isalpha():
                for cut in range(len(token)):
                    deletions[token[:cut] + token[cut + 1:]].append(token)
        self._deletions = {variant: tuple(dict.fromkeys(tokens))
                           for variant, tokens in deletions.items()}

        numbers: dict[str, list[int]] = defaultdict(list)  # own and cross numbers, whole
        partial: dict[str, list[int]] = defaultdict(list)  # shop codes, stems, whole cells
        for position, row in enumerate(self._rows):
            whole: set[str] = set()
            other: set[str] = set()
            for key, _, field, is_whole in _row_codes(row):
                (whole if is_whole and field != "shop_code" else other).add(key)
            for key in whole:
                numbers[key].append(position)
            for key in other - whole:
                partial[key].append(position)
        self._numbers = {key: _pack(rows) for key, rows in numbers.items()}
        self._partial = {key: _pack(rows) for key, rows in partial.items()}

        self._guide_parts = self._build_guide()
        self.names_stamp: tuple | None = None  # of the file the readings came from
        self._guide = self.guide_version = ""
        self.read_as(readings or {})

    @classmethod
    def from_rows(cls, rows: list[dict], rules: Rules | None = None,
                  readings: Mapping[str, str] | None = None) -> CatalogIndex:
        """Build from the dict rows that ``site_catalog.SNAPSHOT_SQL`` returns."""
        rules = rules or Rules()
        return cls((_row(row, rules) for row in rows), rules, readings)

    @classmethod
    def from_items(cls, items: Iterable[Any], rules: Rules | None = None,
                   readings: Mapping[str, str] | None = None) -> CatalogIndex:
        """Build from the ``site_catalog.Item`` objects of an already loaded ``Catalog``."""
        return cls((Row(
            id=item.sid, name=item.name, vehicle=item.model, brand=item.brand,
            code=_unchanged(_text(item.original_code), item.original_code),
            shop_code=_unchanged(_text(item.code), item.code),
            cross_codes=_split(_unchanged(_text(item.duplicate_code), item.duplicate_code)),
            price=item.price, store=item.store, warehouse=item.tuz11, state=item.state)
            for item in items), rules, readings)

    def __len__(self) -> int:
        return len(self._rows)

    def _id_at(self, position: int) -> tuple[int, int, str]:
        return _id_key(self._rows[position].id)

    def _find(self, product_id: Any) -> int | None:
        wanted = str(product_id)
        at = bisect_left(self._by_id, _id_key(wanted), key=self._id_at)
        if at < len(self._by_id) and self._rows[self._by_id[at]].id == wanted:
            return self._by_id[at]
        return None

    def row(self, product_id: str) -> Row | None:
        position = self._find(product_id)
        return None if position is None else self._rows[position]

    def rows(self) -> tuple[Row, ...]:
        """Every row of the snapshot, in the order it was loaded."""
        return self._rows

    def guide(self) -> str:
        return self._guide

    def read_as(self, readings: Mapping[str, str]) -> None:
        """Print the guide with what its part names mean. ``readings`` are by opening
        words, each read by a model from the rows that carry the name; a name without one
        is printed as before, and with no reading at all the guide is the counted one.
        The search does not use them: only the guide's text and its version follow."""
        before, names, after = self._guide_parts
        shown = {}
        for opening, *_ in names:
            reading = _clip(" ".join(str(readings.get(opening) or "").replace(";", ",")
                                     .split()), READING_CHARS)
            if reading:
                shown[opening] = reading
        lines = [*before, READ_LEGEND if shown else NAMES_LEGEND]
        for opening, counts, written, series in names:
            line = f"{opening} {counts}"
            if opening in shown:
                line += f" = {shown[opening]};"
            lines.append(f"{line} e.g. {written}" + (f"; {series}" if series else ""))
        guide = "\n".join([*lines, *after]) + "\n"
        self._guide, self.guide_version = (
            guide, hashlib.sha256(guide.encode("utf-8")).hexdigest()[:12])

    def part_names(self) -> list[dict]:
        """The part names the guide lists, by their opening words and in the guide's order,
        each with what a reader needs to say what the part is. ``names``: the names written
        most often under the opening, without the car, size or number some end in, each
        with its number of rows; an opening can hold more than one part. ``examples``: rows
        that carry them, one for each of those names in turn and the most written first; of
        a name's rows, one with cross numbers before one without, a brand not shown yet
        before one that is, and a row in stock before one that is not."""
        found: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for position, opening in enumerate(self._openings):
            if opening:
                found[opening][_name_form(self._rows[position].name)].append(position)
        listed = []
        for opening, *_ in self._guide_parts[1]:
            forms = found[opening]
            written = _top(Counter({form: len(rows) for form, rows in forms.items()}),
                           NAME_FORMS)
            chosen: dict[int, None] = {}
            brands: set[str] = set()
            left = sum(count for _, count in written)
            while left and len(chosen) < NAME_ROWS:
                for form, _ in written:
                    rest = [position for position in forms[form] if position not in chosen]
                    if not rest or len(chosen) == NAME_ROWS:
                        continue
                    position = min(rest, key=lambda position: (
                        not self._rows[position].cross_codes, not self._rows[position].code,
                        self._rows[position].brand in brands,
                        self._rank[position] > STOCKED, self._order[position]))
                    chosen[position] = None
                    brands.add(self._rows[position].brand)
                    left -= 1
            listed.append({"opening": opening,
                           "names": [{"name": form, "rows": count} for form, count in written],
                           "examples": [self._rows[position] for position in chosen]})
        return listed

    # -- words -------------------------------------------------------------------------

    def _georgian_rows(self, word: str, exact: bool) -> set[int]:
        """Rows with a Georgian token that starts with the word."""
        if exact:
            return set(self._georgian.get(word, ()))
        found: set[int] = set()
        words = self._georgian_words
        for at in range(bisect_left(words, word), len(words)):
            if not words[at].startswith(word):
                break
            found.update(self._georgian[words[at]])
        return found

    def _splits(self, key: str) -> list[tuple[str, ...]]:
        """Every way to cut the key into two to MAX_JOIN catalogue tokens."""
        tokens, size = self._latin, len(key)
        found: list[tuple[str, ...]] = []

        def cut(start: int, pieces: tuple[str, ...]) -> None:
            for at in range(start + 1, size):
                head = key[start:at]
                if head not in tokens:
                    continue
                if key[at:] in tokens:
                    found.append((*pieces, head, key[at:]))
                if len(pieces) + 2 < MAX_JOIN:
                    cut(at, (*pieces, head))

        cut(0, ())
        return found

    def _latin_rows(self, key: str, prefix: bool = False) -> set[int]:
        """Rows where the key is one whole token, or neighbours that spell it written apart.
        A number of three or more digits also finds the words that write it behind one to
        three letters: `150` finds `J150` and `LC150`, as customers say "Prado 150"."""
        found = set(self._latin.get(key, ()))
        if key.isascii() and key.isdigit():
            for token in self._lettered.get(key, ()):
                found.update(self._latin[token])
        for pieces in self._splits(key):
            postings = sorted((self._latin[piece] for piece in set(pieces)), key=len)
            candidates = set(postings[0])
            for more in postings[1:]:
                candidates.intersection_update(more)
            needle = "~".join(pieces).encode("utf-8")
            chains, at = self._chains, self._chain_at
            found.update(position for position in candidates - found
                         if _in_chain(chains, needle, at[position], at[position + 1]))
        if prefix:
            words = self._latin_words
            for at in range(bisect_left(words, key), len(words)):
                if not words[at].startswith(key):
                    break
                found.update(self._latin[words[at]])
        return found

    def _run_rows(self, run: list[_Word]) -> set[int]:
        """Rows with every word of a run of Latin words, or with neighbours joined into one.

        `cr v` finds `CR-V` and `CRV`; `5 series` finds the two words anywhere in the row.
        """
        if len(run) == 1:
            return self._latin_rows(run[0].key, run[0].prefix)
        cached: dict[tuple[str, bool], set[int]] = {}
        gaps = len(run) - 1
        masks = range(1 << gaps) if len(run) <= MAX_JOIN else (0, (1 << gaps) - 1)
        found: set[int] = set()
        for mask in masks:  # each bit: the two words around that gap are read as one
            rows: set[int] | None = None
            start = 0
            for at in range(1, len(run) + 1):
                if at < len(run) and mask >> (at - 1) & 1:
                    continue
                group = ("".join(word.key for word in run[start:at]), run[at - 1].prefix)
                if group not in cached:
                    cached[group] = self._latin_rows(*group)
                rows = cached[group] if rows is None else rows & cached[group]
                start = at
                if not rows:
                    break
            found |= rows or set()
        return found

    def _term_rows(self, words: list[_Word]) -> set[int]:
        """Rows that have every word of the term, in any order."""
        sets: list[set[int]] = []
        run: list[_Word] = []  # Latin words that only spaces separate
        for word in words:
            if run and word.joins:
                run.append(word)
                continue
            if run:
                sets.append(self._run_rows(run))
                run = []
            if word.georgian:
                # One Georgian letter among other words is an abbreviation, not a stem.
                sets.append(self._georgian_rows(word.key, len(word.key) == 1))
            else:
                run = [word]
        if run:
            sets.append(self._run_rows(run))
        sets.sort(key=len)
        found = sets[0]
        for more in sets[1:]:
            found = found & more
        return found

    def _terms(self, group: str, value: Any) -> list[tuple[str, list[_Word]]]:
        """The usable terms of one group with their words; refuses what cannot be searched."""
        if isinstance(value, str):
            value = [value]
        try:
            items = list(value or ())
        except TypeError:
            items = [value]
        if any(not isinstance(item, str) for item in items):
            raise SearchError(f"`{group}` must be a list of strings.",
                              "Pass each term as its own string in the list.")
        if len(items) > MAX_TERMS:
            raise SearchError(
                f"`{group}` has {len(items)} terms; at most {MAX_TERMS} are read.",
                "Keep the most specific spellings and search again for the rest.")
        terms: list[tuple[str, list[_Word]]] = []
        seen: set[tuple[_Word, ...]] = set()
        for item in items:
            term = " ".join(item.split())
            if not term:
                continue
            if len(term) > MAX_TERM_LENGTH:
                raise SearchError(
                    f"`{group}` contains a term of {len(term)} characters "
                    f"({term[:30]!r}...); at most {MAX_TERM_LENGTH} are read.",
                    "Use the few words that identify the row, one alternative per term.")
            words = _words(term)
            if not words:
                raise SearchError(f"`{group}` contains {term!r}, which has no letters or digits.",
                                  "Remove it, or write a word or a number from the catalogue.")
            if len(words) > MAX_WORDS:
                raise SearchError(
                    f"`{group}` contains {term!r} ({len(words)} words); every word of a term "
                    f"must be in the row, and at most {MAX_WORDS} are read.",
                    "Put alternative spellings into separate terms.")
            if len(words) == 1 and len(words[0].key) == 1 and words[0].key.isalpha():
                raise SearchError(
                    f"`{group}` contains {term!r} (1 letter), which matches almost every row.",
                    _LONGER[group])
            if any(word.prefix and len(word.key) < 2 for word in words):
                raise SearchError(
                    f"`{group}` contains {term!r}, where `*` follows 1 character and matches "
                    "almost every row.", "Write at least two characters before the `*`.")
            if tuple(words) not in seen:
                seen.add(tuple(words))
                terms.append((term, words))
        return terms

    def _similar(self, key: str) -> list[tuple[str, int]]:
        """Catalogue words one edit away from the key, most used first, with row counts."""
        candidates = set(self._deletions.get(key, ()))
        for cut in range(len(key)):
            shorter = key[:cut] + key[cut + 1:]
            if shorter.isalpha() and shorter in self._latin:
                candidates.add(shorter)
            candidates.update(self._deletions.get(shorter, ()))
        # A word typed as one (`e-class`) already matches its own pieces written apart.
        candidates.difference_update(piece for pieces in self._splits(key) for piece in pieces)
        near = [(token, len(self._latin[token])) for token in candidates
                if _one_edit(key, token)]
        return sorted(near, key=lambda pair: (-pair[1], pair[0]))

    def _counts(self, positions: set[int], labels: list[str], cap: int,
                first: frozenset[str] = frozenset()) -> list[dict]:
        """How many of the rows carry each label, and how many of those are in stock.

        The most frequent labels, after any of the labels in ``first``."""
        rows = Counter(map(labels.__getitem__, positions))
        rows.pop("", None)
        pairs = list(rows.items())
        if len(pairs) > cap:  # only the labels that can reach the list are sorted
            least = rows.most_common(cap)[-1][1]
            pairs = [pair for pair in pairs if pair[1] >= least or pair[0] in first]
        pairs.sort(key=lambda pair: (pair[0] not in first, -pair[1], pair[0]))
        available = Counter(map(labels.__getitem__, positions & self._stocked))
        return [{"text": label, "rows": count, "available": available[label]}
                for label, count in pairs[:cap]]

    def _best(self, candidates: set[int], limit: int, scoring: list[set[int]]) -> list[int]:
        """The first rows in result order: in the shop, only in the warehouse, stock unknown,
        unavailable; inside each, rows matching more of the terms first, then by id."""
        order = self._order
        best: list[int] = []
        for tier in (*self._tiers, None):
            if len(best) >= limit:
                break
            bucket = candidates - self._tiered if tier is None else candidates & tier
            if bucket:
                score: Counter = Counter()
                for rows in scoring:
                    score.update(bucket & rows)
                best += heapq.nsmallest(limit - len(best), bucket,
                                        key=lambda position: (-score[position], order[position]))
        return best

    def _array(self, position: int) -> list:
        row = self._rows[position]
        cross = "/".join(row.cross_codes[:CROSS_SHOWN])
        if len(row.cross_codes) > CROSS_SHOWN:
            cross += f" (+{len(row.cross_codes) - CROSS_SHOWN} more)"
        return [row.id, row.name, row.vehicle, row.brand, row.code, cross,
                _compact(row.price), _compact(row.store), _compact(row.warehouse)]

    def search(self, *, make_terms: Sequence[str] = (), vehicle_terms: Sequence[str] = (),
               part_terms: Sequence[str] = (), exclude_terms: Sequence[str] = (),
               in_stock_only: bool = False, limit: int = 40) -> dict:
        """Rows whose name, vehicle text and brand hold the given words.

        A row must match a make term (if any are given) and a part term (if any) and no
        exclude term; ``total`` and ``available`` count those rows. Vehicle terms do not
        filter: when the rows fit in the limit all are returned, those matching a vehicle
        term first; otherwise only those are returned and ``other_vehicle_texts`` counts
        how the others name their cars. With vehicle terms alone, they are the filter.
        ``in_stock_only`` hides the rows that are not in stock from everything but
        ``total``. Raises ``SearchError`` for a call that cannot be searched.
        """
        names = ("make_terms", "vehicle_terms", "part_terms", "exclude_terms")
        given = dict(zip(names, (make_terms, vehicle_terms, part_terms, exclude_terms),
                         strict=True))
        matched = {name: [(term, words, self._term_rows(words))
                          for term, words in self._terms(name, given[name])]
                   for name in names}
        if not (matched["make_terms"] or matched["vehicle_terms"] or matched["part_terms"]):
            raise SearchError(
                "No search terms were given.",
                "Put at least one word into make_terms, vehicle_terms or part_terms.")
        try:
            limit = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = 40
        union = {name: set().union(*(rows for _, _, rows in matched[name]))
                 if matched[name] else None for name in names}
        make, vehicle, part = (union[name] for name in names[:3])
        excluded = union["exclude_terms"] or set()
        required = [rows for rows in (make, part) if rows is not None]
        if required:
            base = set(required[0]) if len(required) == 1 else required[0] & required[1]
        else:  # only vehicle terms: they are the filter, and nothing is left to partition
            base = set(vehicle or ())
        unfiltered = len(base)
        base -= excluded
        partition = bool(required) and vehicle is not None
        in_stock = base & self._stocked
        pool = in_stock if in_stock_only else base
        hits = pool & vehicle if partition else pool
        scoring = [rows for name in names[:3] for _, _, rows in matched[name]]
        shown = self._best(hits, limit, scoring)
        if len(pool) <= limit:  # everything fits: the rows without a vehicle match follow
            shown += self._best(pool - hits, limit, scoring)
            rest: set[int] = set()
        else:
            rest = pool - hits if partition else pool.difference(shown)
        near = self._near(matched, part, vehicle, base, bool(required))
        near = (near & self._stocked if in_stock_only else near) - excluded - set(shown)

        # What the parts are called, when the caller named none or named none that exists.
        other_part_names: list[dict] = []
        if part is None:
            if len(hits) > limit:
                other_part_names = self._counts(hits, self._openings, 30)
        elif not (base & vehicle if partition else base):
            both = make & vehicle if make is not None and vehicle is not None else None
            for car in (both, make, vehicle):
                car = car - excluded if car else None
                if car:  # the narrowest reading of the car that has rows at all
                    if not car & part:
                        other_part_names = self._counts(car, self._openings, 30)
                    break
        why: dict[str, int] = {}
        if not base:
            for name in names[:3]:
                if union[name] is not None:
                    why[f"{name}_alone"] = len(union[name])
            if unfiltered:
                why["removed_by_exclude_terms"] = unfiltered
        positive = [term for name in names[:3] for term in matched[name]]
        return {
            "total": len(base),
            "available": len(in_stock),
            "matched_vehicle": len(hits) if vehicle is not None else 0,
            "columns": list(COLUMNS),
            "rows": [self._array(position) for position in shown],
            "other_vehicle_texts": self._other_texts(
                rest, matched["vehicle_terms"] if partition else ()),
            "other_part_names": other_part_names,
            "similar_spellings": self._spellings(positive),
            # Rows the search itself did not match, found under a make or model word that
            # is one letter away from a typed one: reported apart, never mixed in.
            "similar_spelling_rows": [self._array(position) for position in self._best(
                near, NEAR_ROWS, scoring)],
            "unmatched_terms": list(dict.fromkeys(
                term for name in names for term, _, rows in matched[name] if not rows)),
            "why": why,
            "note": _note(len(base), len(pool), len(hits), len(shown), len(rest), limit,
                          partition, part is not None, vehicle is not None),
        }

    def _variants(self, terms: Sequence[_Term]) -> set[int]:
        """Rows that match one of the terms once a word of it is replaced by a catalogue
        word one edit away: `TOYOA` for `TOYOTA`, `HIGLANDER` for `HIGHLANDER`."""
        found: set[int] = set()
        for _, words, _ in terms:
            for at, word in enumerate(words):
                if word.georgian or word.prefix or len(word.key) < 5 or not word.key.isalpha():
                    continue  # a number one digit away is another number, not a spelling
                for token, _ in self._similar(word.key)[:NEAR_WORDS]:
                    found |= self._term_rows(
                        [*words[:at], _Word(token, joins=word.joins), *words[at + 1:]])
        return found

    def _near(self, matched: dict, part: set[int] | None, vehicle: set[int] | None,
              base: set[int], filtered: bool) -> set[int]:
        """Rows outside a result that would be in it had the catalogue spelt a make or a
        model word as the caller did: the make under a near spelling with the other groups
        matching, or the make and part matching with a model under a near spelling."""
        near_make = self._variants(matched["make_terms"])
        near_vehicle = self._variants(matched["vehicle_terms"])
        if part is not None:
            near_make &= part
        if vehicle is not None:
            near_make &= vehicle | near_vehicle
        named = near_vehicle - (vehicle or set())
        return (near_make - base) | (named & base if filtered else named)

    def _other_texts(self, rest: set[int], vehicle_terms: Sequence[_Term]) -> list[dict]:
        """How the rows that are not shown name their cars, the most frequent texts first.

        A text with a word that begins like a vehicle term (`GOLF5` for `GOLF`) is the
        likeliest other spelling of the car, so such texts come before the others.
        """
        begins: set[int] = set()
        if rest:
            for _, words, _ in vehicle_terms:
                for word in words:
                    if not word.georgian and len(word.key) > 1:
                        begins |= self._latin_rows(word.key, prefix=True)
        leading = frozenset(self._facets[position] for position in begins & rest)
        return self._counts(rest, self._facets, 40, leading)

    def _spellings(self, terms: Sequence[_Term]) -> list[dict]:
        """Catalogue words one edit away from the typed ones: reported, never matched."""
        found: list[dict] = []
        for term, words, _ in terms:
            # A number one digit away is another number, not a spelling: letters only.
            close = {token: count for word in words
                     if not (word.georgian or word.prefix) and len(word.key) >= 5
                     and word.key.isalpha() for token, count in self._similar(word.key)}
            ranked = sorted(close.items(), key=lambda pair: (-pair[1], pair[0]))
            found += [{"term": term, "catalogue_token": token.upper(), "rows": count}
                      for token, count in ranked[:5]]
        return found

    # -- numbers -----------------------------------------------------------------------

    def by_codes(self, numbers: Sequence[str],
                 limit_per_number: int = 20) -> dict[str, list[dict]]:
        """Rows that carry each number as their own code, shop code or a cross code.

        Punctuation and spacing are ignored. `matched` is the number as the row writes it,
        which is longer than the number asked for when only its stem was given.
        """
        if isinstance(numbers, str):
            numbers = [numbers]
        try:
            limit = max(1, min(int(limit_per_number), 50))
        except (TypeError, ValueError):
            limit = 20
        found: dict[str, list[dict]] = {}
        for number in numbers:
            given = str(number)
            key = normalize_code(given)
            hits: list[tuple[tuple, int, str, str]] = []
            positions = (*_unpack(self._numbers.get(key)), *_unpack(self._partial.get(key)))
            for position in positions if len(key) >= MIN_NUMBER else ():
                # The best place the row writes this number: whole before stem, own first.
                partial, _, written, field = min(
                    (not whole, FIELD_ORDER[field], written, field)
                    for code, written, field, whole in _row_codes(self._rows[position])
                    if code == key)
                hits.append(((partial, field == "cross", self._rank[position],
                              self._order[position]), position, written, field))
            hits.sort()
            found[given] = [{"row": self._array(position), "matched": written, "field": field}
                            for _, position, written, field in hits[:limit]]
        return found

    def equivalents(self, product_id: str, limit: int = 30) -> list[dict]:
        """Other rows that share a number with this row, the most shared numbers first.

        The row's own code and its cross codes count; the shop's stock number does not.
        """
        position = self._find(product_id)
        if position is None:
            return []
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 30
        source = self._rows[position]
        shared: dict[int, list[str]] = defaultdict(list)
        for key in dict.fromkeys(key for key, _, field, whole in _row_codes(source)
                                 if whole and field != "shop_code"):
            rows = _unpack(self._numbers.get(key))
            if len(rows) > COMMON_NUMBER:
                continue
            for other in rows:
                if other != position:
                    shared[other].append(key)
        ranked = sorted(shared, key=lambda other: (-len(shared[other]), self._rank[other],
                                                   self._order[other]))
        found: list[dict] = []
        for other in ranked[:limit]:
            row = self._rows[other]
            written = {key: text for key, text, field, whole in reversed(_row_codes(row))
                       if whole and field != "shop_code"}
            found.append({
                "row": self._array(other),
                "shared_numbers": [written[key] for key in shared[other]],
                "same_brand": self._rules.brands_match(source.brand, row.brand)})
        return found

    # -- guide -------------------------------------------------------------------------

    def _build_guide(self) -> tuple[list[str], list[tuple[str, str, str, str]], list[str]]:
        """How this catalogue is written, found by counting it and nothing else: the lines
        before the part names, the part names (opening words, rows/in stock, the name
        written most often, the brands' series) and the lines after them."""
        rows, rank = self._rows, self._rank
        states = Counter(rank)
        with_vehicle = name_only = with_code = with_cross = cross_numbers = 0
        head_rows: Counter = Counter()
        head_stock: Counter = Counter()
        written: dict[str, Counter] = defaultdict(Counter)  # a folded word as it is typed
        followers: dict[str, Counter] = defaultdict(Counter)
        examples: dict[str, Counter] = defaultdict(Counter)
        year_rows: Counter = Counter()
        year_examples: dict[str, Counter] = defaultdict(Counter)
        opening_names: dict[str, Counter] = defaultdict(Counter)
        opening_stock: Counter = Counter()
        opening_series: dict[str, Counter] = defaultdict(Counter)
        series_rows: Counter = Counter()
        brands: Counter = Counter()
        for position, row in enumerate(rows):
            stocked = rank[position] <= STOCKED
            with_code += bool(row.code)
            with_cross += bool(row.cross_codes)
            cross_numbers += len(row.cross_codes)
            if row.brand:
                brands[row.brand] += 1
            opening = self._openings[position]
            series = _series(row)
            series_rows[series] += bool(series)
            if opening:
                opening_names[opening][row.name] += 1
                opening_stock[opening] += stocked
                if series:
                    opening_series[opening][series] += 1
            text = row.vehicle
            if not text:
                name_only += bool(_latin_tail(row.name))
                continue
            with_vehicle += 1
            notations: set[str] = set()
            for match in _YEAR.finditer(text):
                found = match.group()
                if found.isdigit() and not (len(found) == 4 and 1950 <= int(found) <= 2049):
                    continue  # a bare number that does not read as a year
                found = re.sub(rf"[{_HYPHENS}]", "-", found).replace(" ", "")
                notations.add(re.sub(r"\d", "9", found))
            for notation in notations:
                year_rows[notation] += 1
                year_examples[notation][text] += 1
            words = text.split()
            lead = next((at for at, word in enumerate(words)
                         if any(char.isalnum() for char in word)), None)
            if lead is None:
                continue
            first = words[lead].strip(".,;:")
            head = first.casefold()
            written[head][first] += 1
            head_rows[head] += 1
            head_stock[head] += stocked
            examples[head][" ".join(words[lead:])] += 1
            later: set[str] = set()
            for word in _FOLLOWER_SPLIT.split(" ".join(words[lead + 1:])):
                word = word.strip(".()[]:'\"*?")
                if _FOLLOWER.fullmatch(word):
                    later.add(word.casefold())
                    written[word.casefold()][word] += 1
            followers[head].update(later)

        def show(word: str) -> str:
            return min(written[word].items(), key=lambda pair: (-pair[1], pair[0]))[0]

        top, by_rounded = _top, _by_rounded

        lines = [
            "CATALOGUE GUIDE, counted from the shop's own catalogue. Counts are rounded.",
            f"ROWS {_round2(len(rows))}: {_round2(states[RANKS[IN_STORE]])} in the shop, "
            f"{_round2(states[RANKS[WITHIN_HOURS]])} only in the warehouse, "
            f"{_round2(states[RANKS[UNAVAILABLE]])} not in stock"
            + (f", {_round2(states[RANKS[UNKNOWN]])} with stock unknown."
               if states[RANKS[UNKNOWN]] else "."),
            f"{_round2(with_vehicle)} rows have a vehicle text. {_round2(name_only)} have "
            "none but write Latin words or numbers in the name. "
            f"{_round2(len(rows) - with_vehicle - name_only)} have neither.",
            "",
            "VEHICLE TEXTS BY FIRST WORD",
            "first word rows/in stock: the words written after it most often, with rows "
            "~ rarer words one letter away from a more frequent one (in brackets) "
            "|| two full texts",
        ]
        covered = 0
        for head, count in top(head_rows, 130):
            if covered >= 0.9 * with_vehicle or count < 8:
                break
            covered += count
            ranked = top(followers[head], len(followers[head]))
            counts = dict(ranked)
            size = max(4, min(16, count // 100))
            line = f"{show(head)} {_round2(count)}/{_round2(head_stock[head])}:"
            usual = [pair for pair in ranked[:size] if pair[1] > 1]
            if usual:
                line += " " + ", ".join(f"{show(word)} {_round2(seen)}"
                                        for word, seen in by_rounded(usual))
            near: list[tuple[str, int]] = []
            for word, seen in ranked[size:]:
                if len(near) == 4 or seen < 2:
                    break
                if len(word) >= 5 and word.isalpha():
                    most, partner = max(((counts.get(token, 0), token)
                                         for token, _ in self._similar(word)), default=(0, ""))
                    if most > seen:
                        near.append((f"{show(word)} {_round2(seen)} "
                                     f"({show(partner)} {_round2(most)})", seen))
            if near:
                line += " ~ " + ", ".join(text for text, _ in by_rounded(near))
            texts = [text for text, _ in top(examples[head], len(examples[head]))]
            # A text that says more than the first word shows how the rest is written.
            texts = [text for text in texts if len(text.split()) > 2] or texts
            texts = list({text.casefold(): text for text in reversed(texts)}.values())[::-1]
            lines.append(line + " || " + " | ".join(_clip(text, 44) for text in texts[:2]))

        lines += ["", "YEAR NOTATIONS IN VEHICLE TEXTS, digits shown as 9",
                  "notation rows: two full texts"]
        for notation, count in by_rounded(top(year_rows, 24)):
            if count >= 10:
                lines.append(f"{notation} {_round2(count)}: " + " ; ".join(
                    _clip(text, 44) for text, _ in top(year_examples[notation], 2)))

        lines += ["", "PART NAMES BY OPENING WORDS"]
        before, lines = lines, []
        listed = []
        opening_rows = Counter({opening: sum(names.values())
                                for opening, names in opening_names.items()})
        for opening, count in by_rounded(top(opening_rows, GUIDE_NAMES)):
            # Counted, not known: which series of which brand is filed under this name.
            own = [series for series, seen in top(opening_series[opening], 12)
                   if seen >= SERIES_ROWS and 2 * seen >= series_rows[series]]
            listed.append((opening, f"{_round2(count)}/{_round2(opening_stock[opening])}",
                          _clip(top(opening_names[opening], 1)[0][0], 48),
                          ", ".join(own[:SERIES_SHOWN])))

        lines += ["", "SHORT WORDS IN NAMES (one to three Georgian letters)",
                  "word rows (a name that has it; the letter its rows' own part numbers "
                  "end in far more often than the catalogue's do, when one stands out)"]
        short = Counter({token: len(found) for token, found in self._georgian.items()
                         if len(token) <= 3})
        endings = [_ending(row.code, row.brand) for row in rows]
        usual = Counter(endings)
        entries = []
        for token, count in by_rounded(top(short, 60)):
            if count >= 10:
                found = self._georgian[token]
                names = Counter(rows[position].name for position in found)
                entry = f"{token} {_round2(count)} ({_clip(top(names, 1)[0][0], 40)}"
                # Counted, not known: makers end the numbers of a pair's two sides, and of
                # other paired parts, in different letters.
                letter, seen = max(Counter(endings[position] for position in found
                                           if endings[position]).items(),
                                   key=lambda pair: (pair[1], pair[0]), default=("", 0))
                if seen >= ENDING_SHARE * count \
                        and seen * len(rows) >= ENDING_LIFT * usual[letter] * count:
                    entry += f"; numbers end in {letter}"
                entries.append(entry + ")")
        lines.append("; ".join(entries))

        lines += ["", "BRANDS, the 60 with most rows",
                  ", ".join(f"{brand} {_round2(count)}"
                            for brand, count in by_rounded(top(brands, 60)))]
        lines += ["", "NUMBERS",
                  f"{_round2(with_code)} rows carry the maker's own number. "
                  f"{_round2(with_cross)} rows carry cross numbers, "
                  f"{_round2(cross_numbers)} in all, written with / between them."]
        return before, listed, lines


_INDEXES: weakref.WeakKeyDictionary[Any, CatalogIndex] = weakref.WeakKeyDictionary()
_INDEXES_LOCK = threading.Lock()


def _names_stamp(path: Path | str) -> tuple:
    """What tells one state of the readings file from another: its place, time and size."""
    try:
        status = os.stat(path) if path else None
    except OSError:
        status = None
    return (str(path), 0, 0) if status is None else (
        str(path), status.st_mtime_ns, status.st_size)


def index_for(catalog_store: Any, names_path: Path | str | None = None) -> CatalogIndex:
    """The index of the store's current catalogue: built once per snapshot, then shared.

    A new snapshot is a new ``Catalog`` object, so the index follows the store's refresh
    without a second database read; the index of a replaced catalogue is dropped with it.

    ``names_path`` is the file that holds what the part names mean (``assistant_names``).
    It is read when the index is built and again when the file has changed; an empty path
    or a missing file gives the counted guide alone. Without a path the guide stays as the
    last caller that named one left it.
    """
    catalog = catalog_store.current()
    with _INDEXES_LOCK:
        index = _INDEXES.get(catalog)
        stamp = None if names_path is None else _names_stamp(names_path)
        if index is not None and (stamp is None or stamp == index.names_stamp):
            return index
        readings: dict[str, str] = {}
        if stamp is not None and names_path:
            from automotive.assistant_names import load

            readings = load(names_path)
        if index is None:
            index = CatalogIndex.from_items(catalog.items, catalog.rules, readings)
            _INDEXES[catalog] = index
        else:
            index.read_as(readings)
        index.names_stamp = stamp
        return index
