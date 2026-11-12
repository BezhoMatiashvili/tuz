"""Read sources for the assistant: crawled part numbers for a car, and the shop's own facts.

``CarNumbers.find`` takes the car and part words the model chose and returns candidate
crawled vehicles (MANN, NGK, RockAuto, CatCar Toyota), the parts those catalogues list for
them in one part group, and every one-hop cross reference of those parts. ``numbers_for``
flattens that to part numbers so the Toolbox can show every shop row carrying one of them,
whatever car the row's own text names: a part the catalogues list for the customer's E400
is offered even when the shop filed it under an E63.

Matching is string mechanics only; nothing here interprets a car or a part. Which candidate
is the customer's car, and whether a part fits, is the model's judgement. Every row is a
crawled source assertion, never reviewed evidence. All reads run in one read-only
transaction with a statement timeout.

``shop_info`` gives the shop's contact facts, opening hours and services in one language,
and names what the owner has not filled in yet.
"""

from __future__ import annotations

import contextlib
import logging
import re
import string
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from automotive.codes import normalize_code
from automotive.site_content import LANGUAGES as SITE_LANGUAGES
from automotive.site_content import localized

LOGGER = logging.getLogger(__name__)

MAX_TERMS = 12  # terms per list, as for the Toolbox's other tools
TERM_LENGTH = 80
MAX_WORDS = 8  # words of one part term
MAKE_LENGTH = 60
CROSS_LIMIT = 10000  # a safety cap only: the Toolbox matches shop rows on every number
DIAGNOSED_IDS = 2000  # vehicles whose part kinds an empty-result note may name
SHOWN = 30  # models or part kinds named in a note
# Every relation the catalogues publish. RockAuto stores its OE numbers as `replaces` and
# MANN its supersessions as `replaced_by`, so all four name numbers of the same part.
RELATIONS = ("equivalent", "oe", "replaces", "replaced_by")
# A cross entry reads "fitted part <relation> number". As published, except when the source
# published it from the other part's side: then replaces and replaced_by swap, and `oe`
# becomes `oe_of` (the fitted part is the OE number the other part names).
CROSS_ORDER = ("oe", "replaces", "replaced_by", "oe_of", "equivalent")
PUNCTUATION = string.punctuation + "«»“”„‘’"
NUMERIC_ID = re.compile(r"[0-9]{1,18}")

SETUP_SQL = ("SELECT set_config('transaction_read_only', 'on', true), "
             "set_config('statement_timeout', %s, true)")

VEHICLE_COLUMNS = """v.id, v.source, v.make, v.model, v.model_code, v.engine, v.engine_code,
       v.year_from, v.year_to, v.power_kw, v.fuel, v.label"""


def _terms_cte(name: str) -> str:
    """Terms as rows: case-folded, and by their letters and digits alone. Only a term with a
    letter is compared that loosely, so a bare number cannot run across other numbers."""
    return f"""SELECT lower(term) AS raw, c.compact,
           c.compact ~ '[[:alpha:]]' AND length(c.compact) >= 2 AS loose
    FROM unnest(%({name})s::text[]) AS term
    CROSS JOIN LATERAL (SELECT regexp_replace(lower(term), '[^[:alnum:]]+', '', 'g')
                        AS compact) AS c"""  # nosec B608 - constant fragments; terms are bound


def _words_in(field: str) -> str:
    """Every word of one part term occurs in the field, case-folded."""
    return f"""EXISTS (
        SELECT 1 FROM unnest(%(words)s::text[], %(owners)s::int[]) AS w(word, term)
        GROUP BY w.term HAVING bool_and(strpos(lower({field}), lower(w.word)) > 0))"""  # nosec B608 - constant fragments; terms are bound


# A fitted part is in the requested group when a part term's words all occur in its position
# (the catalogue's part type on that car) or all in its name: CatCar names a spark plug
# "PLUG, SPARK" under a Russian group. Without part terms every fitted part counts.
POSITION_HIT = _words_in("f.position")
PART_MATCH = f"""(cardinality(%(words)s::text[]) = 0 OR {POSITION_HIT}
        OR {_words_in("p.name")})"""
# A model term occurs in the model, model code or label as written, or loosely in the model
# or model code: "E-Class" meets NGK's "E CLASS". Labels are compared as written only.
MODEL_HIT = """(strpos(lower(v.model), t.raw) > 0 OR strpos(lower(v.model_code), t.raw) > 0
        OR strpos(lower(v.label), t.raw) > 0
        OR (t.loose AND (
            strpos(regexp_replace(lower(v.model), '[^[:alnum:]]+', '', 'g'), t.compact) > 0
            OR strpos(regexp_replace(lower(v.model_code), '[^[:alnum:]]+', '', 'g'),
                      t.compact) > 0)))"""
# Engine terms only rank: as written in engine, engine code, fuel or label, or loosely in the
# engine and engine code ("X16SZR" meets MANN's "X 16 SZR").
ENGINE_HIT = """(strpos(lower(v.engine), t.raw) > 0 OR strpos(lower(v.engine_code), t.raw) > 0
        OR strpos(lower(v.fuel), t.raw) > 0 OR strpos(lower(v.label), t.raw) > 0
        OR (t.loose AND (
            strpos(regexp_replace(lower(v.engine), '[^[:alnum:]]+', '', 'g'), t.compact) > 0
            OR strpos(regexp_replace(lower(v.engine_code), '[^[:alnum:]]+', '', 'g'),
                      t.compact) > 0)))"""
# The make as the crawler normalises it, from its start ("MERCEDES" meets MERCEDESBENZ), or
# one whole word of the make as written ("ROVER" meets LAND ROVER).
MAKE_MATCH = """(%(make)s = '' OR starts_with(v.make_norm, %(make)s)
        OR %(make)s = ANY(regexp_split_to_array(upper(v.make), '[^[:alnum:]]+')))"""
# A known bound must hold; a bound the catalogue does not state does not filter.
YEAR_MATCH = """(%(year)s = 0 OR ((v.year_from IS NULL OR v.year_from <= %(year)s)
                            AND (v.year_to IS NULL OR v.year_to >= %(year)s)))"""

# Candidates: vehicles with a fitted part in the group, the make and year, and a model term.
# The exact make first, then the most terms matched; sources take turns at equal standing.
VEHICLES_SQL = f"""
WITH model_terms AS ({_terms_cte("model_terms")}),
engine_terms AS ({_terms_cte("engine_terms")}),
fitted AS (
    SELECT DISTINCT f.vehicle_id
    FROM parts_catalog.part_fitments f
    JOIN parts_catalog.parts p ON p.id = f.part_id
    WHERE {PART_MATCH}),
matched AS (
    SELECT {VEHICLE_COLUMNS}, v.make_norm = %(make)s AS exact_make,
           (SELECT count(*) FROM model_terms t WHERE {MODEL_HIT}) AS model_hits,
           (SELECT count(*) FROM engine_terms t WHERE {ENGINE_HIT}) AS engine_hits
    FROM parts_catalog.vehicles v
    WHERE v.id IN (SELECT vehicle_id FROM fitted) AND {MAKE_MATCH} AND {YEAR_MATCH}),
kept AS (
    SELECT m.*, m.model_hits + m.engine_hits AS hits
    FROM matched m
    WHERE cardinality(%(model_terms)s::text[]) = 0 OR m.model_hits > 0),
turns AS (
    SELECT k.*, row_number() OVER (
               PARTITION BY k.source
               ORDER BY k.exact_make DESC, k.hits DESC, k.year_from DESC NULLS LAST, k.id)
           AS turn
    FROM kept k)
SELECT id, source, make, model, model_code, engine, engine_code, year_from, year_to,
       power_kw, fuel, label, count(*) OVER () AS matched
FROM turns
ORDER BY exact_make DESC, hits DESC, turn, id
LIMIT %(limit)s"""  # nosec B608 - constant fragments; terms are bound

VEHICLES_BY_ID_SQL = f"""
SELECT {VEHICLE_COLUMNS}, count(*) OVER () AS matched
FROM unnest(%(ids)s::bigint[]) WITH ORDINALITY AS c(id, turn)
JOIN parts_catalog.vehicles v ON v.id = c.id
ORDER BY c.turn"""  # nosec B608 - constant fragments; terms are bound

# One row per vehicle, part and position, its notes joined. A part matched by its name alone
# is left out on a vehicle where another part matches by position: MANN names hydraulic
# steering and gearbox filters "Oil Filter" and files them under their own position.
# Vehicles take turns, so one with hundreds of parts cannot fill the limit alone.
PARTS_SQL = f"""
WITH chosen AS (
    SELECT c.vehicle_id, c.turn
    FROM unnest(%(ids)s::bigint[]) WITH ORDINALITY AS c(vehicle_id, turn)),
listed AS (
    SELECT f.vehicle_id, p.id AS part_id, p.brand, p.number, p.name, f.position,
           coalesce(string_agg(DISTINCT NULLIF(f.note, ''), ' | '
                               ORDER BY NULLIF(f.note, '')), '') AS note,
           bool_or({POSITION_HIT}) AS by_position
    FROM parts_catalog.part_fitments f
    JOIN parts_catalog.parts p ON p.id = f.part_id
    WHERE f.vehicle_id = ANY(%(ids)s::bigint[]) AND {PART_MATCH}
    GROUP BY f.vehicle_id, p.id, p.brand, p.number, p.name, f.position),
weighed AS (
    SELECT l.*, bool_or(l.by_position) OVER (PARTITION BY l.vehicle_id) AS vehicle_by_position
    FROM listed l),
ranked AS (
    SELECT w.*, c.turn AS vehicle_turn,
           row_number() OVER (PARTITION BY w.vehicle_id
                              ORDER BY w.position, w.name, w.brand, w.number) AS turn,
           count(*) OVER () AS listed
    FROM weighed w
    JOIN chosen c ON c.vehicle_id = w.vehicle_id
    WHERE w.by_position OR NOT w.vehicle_by_position)
SELECT vehicle_id, part_id, brand, number, name, position, note, listed
FROM ranked
ORDER BY turn, vehicle_turn
LIMIT %(limit)s"""  # nosec B608 - constant fragments; terms are bound

# Both directions, one entry per other part (its strongest relation), OE numbers first.
# Parts take turns, so a cut at the safety limit shortens the longest lists.
CROSS_SQL = """
WITH refs AS (
    SELECT x.part_id, x.other_part_id AS other_id, x.relation
    FROM parts_catalog.part_cross_refs x
    WHERE x.part_id = ANY(%(ids)s::bigint[]) AND x.relation = ANY(%(relations)s::text[])
    UNION
    SELECT x.other_part_id, x.part_id,
           CASE x.relation WHEN 'replaces' THEN 'replaced_by'
                           WHEN 'replaced_by' THEN 'replaces'
                           WHEN 'oe' THEN 'oe_of'
                           ELSE x.relation END
    FROM parts_catalog.part_cross_refs x
    WHERE x.other_part_id = ANY(%(ids)s::bigint[]) AND x.relation = ANY(%(relations)s::text[])),
best AS (
    SELECT DISTINCT ON (r.part_id, r.other_id) r.part_id, r.other_id, r.relation
    FROM refs r
    ORDER BY r.part_id, r.other_id, array_position(%(order)s::text[], r.relation)),
ranked AS (
    SELECT b.part_id, o.brand, o.number, b.relation,
           row_number() OVER (PARTITION BY b.part_id
                              ORDER BY array_position(%(order)s::text[], b.relation),
                                       o.brand, o.number) AS turn
    FROM best b
    JOIN parts_catalog.parts o ON o.id = b.other_id)
SELECT part_id, brand, number, relation
FROM ranked
ORDER BY turn, part_id
LIMIT %(limit)s"""

# Why nothing matched: vehicles with listed parts after each filter, in the order applied.
DIAGNOSE_SQL = f"""
WITH model_terms AS ({_terms_cte("model_terms")}),
base AS (
    SELECT v.id, v.model, v.year_from, v.year_to,
           (cardinality(%(model_terms)s::text[]) = 0
            OR EXISTS (SELECT 1 FROM model_terms t WHERE {MODEL_HIT})) AS model_ok,
           {YEAR_MATCH} AS year_ok
    FROM parts_catalog.vehicles v
    WHERE v.id IN (SELECT f.vehicle_id FROM parts_catalog.part_fitments f) AND {MAKE_MATCH})
SELECT count(*) AS make_count,
       count(*) FILTER (WHERE model_ok) AS model_count,
       count(*) FILTER (WHERE model_ok AND year_ok) AS year_count,
       min(year_from) FILTER (WHERE model_ok) AS first_year,
       max(coalesce(year_to, 9999)) FILTER (WHERE model_ok) AS last_year,
       (array_agg(DISTINCT model ORDER BY model) FILTER (WHERE model <> ''))[1:%(shown)s]
           AS models,
       (array_agg(id ORDER BY id) FILTER (WHERE model_ok AND year_ok))[1:%(ids)s] AS year_ids
FROM base"""  # nosec B608 - constant fragments; terms are bound

# The part kinds listed for some vehicles, the most widely listed first.
KINDS_SQL = """
SELECT coalesce(NULLIF(f.position, ''), NULLIF(p.name, ''), '') AS kind,
       count(DISTINCT f.vehicle_id) AS vehicles
FROM parts_catalog.part_fitments f
JOIN parts_catalog.parts p ON p.id = f.part_id
WHERE f.vehicle_id = ANY(%(ids)s::bigint[])
GROUP BY 1
ORDER BY 2 DESC, 1
LIMIT %(limit)s"""


class BadArguments(ValueError):
    """Arguments the model can correct: what was wrong and what to send instead."""

    def __init__(self, error: str, fix: str) -> None:
        super().__init__(error)
        self.error, self.fix = error, fix

    def payload(self) -> dict:
        return {"ok": False, "error": self.error, "fix": self.fix}


class SourceUnavailable(RuntimeError):
    """The crawled catalogues cannot be read right now: no database, a timeout, no tables."""

    def __init__(self, error: str, fix: str = "Judge from the shop catalogue rows.") -> None:
        super().__init__(error)
        self.error, self.fix = error, fix

    def payload(self) -> dict:
        return {"ok": False, "error": self.error, "fix": self.fix}


def make_norm(value: str) -> str:
    """A make as ``parts_catalog.vehicles.make_norm`` stores it: accents folded, then only
    A-Z and digits ('Škoda' -> 'SKODA'). Mirrors ``parts_crawler.records.normalize_brand``."""
    folded = unicodedata.normalize("NFKD", value or "")
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]+", "", plain.upper())


@dataclass(frozen=True)
class _Query:
    make: str
    make_norm: str
    model_terms: list[str]
    year: int
    engine_terms: list[str]
    part_terms: list[str]
    words: list[str]
    owners: list[int]
    vehicle_ids: list[int]
    ids_dropped: int


def _usable(term: str) -> bool:
    return any(char.isalnum() for char in term)


def _terms(args: Mapping, name: str, filters: bool = True) -> list[str]:
    """A list of strings, whitespace collapsed and de-duplicated. A term without a letter or
    digit is dropped: it would occur in every text. A filtering list whose every term is
    dropped is an error, since dropping them all would widen the search instead."""
    value = args.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BadArguments(f"`{name}` must be a list of strings.",
                           "Send every field of the tool's schema with the stated types.")
    kept: dict[str, str] = {}
    for item in value:
        term = " ".join(item.split())[:TERM_LENGTH]
        if _usable(term):
            kept.setdefault(term.casefold(), term)
    if filters and value and not kept:
        raise BadArguments(f"`{name}` has no term with a letter or digit.",
                           f"Give {name} as words, or an empty list.")
    return list(kept.values())[:MAX_TERMS]


def _year(args: Mapping) -> int:
    value = args.get("year")
    if value is None:
        return 0
    if (isinstance(value, bool) or not isinstance(value, int | float)
            or value != int(value) or not (value == 0 or 1900 <= value <= 2100)):
        raise BadArguments("`year` must be the car's model year as four digits, or 0.",
                           "Send 0 when the year is not known.")
    return int(value)


def _vehicle_ids(args: Mapping, limit: int) -> tuple[list[int], int]:
    value = args.get("vehicle_ids")
    if value is None:
        return [], 0
    error = BadArguments("`vehicle_ids` must list ids of vehicles from an earlier car_numbers "
                         "result.", "Send the `id` values you were shown, or an empty list.")
    if not isinstance(value, list):
        raise error
    ids: list[int] = []
    for item in value:
        if isinstance(item, str) and NUMERIC_ID.fullmatch(item.strip()):
            item = int(item.strip())
        elif isinstance(item, float) and item.is_integer():
            item = int(item)
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise error
        if item not in ids:
            ids.append(item)
    return ids[:limit], max(0, len(ids) - limit)


def _words(terms: list[str]) -> tuple[list[str], list[int]]:
    """Every word of every part term, with the position of the term it belongs to."""
    words: list[str] = []
    owners: list[int] = []
    for position, term in enumerate(terms):
        for word in term.split()[:MAX_WORDS]:
            word = word.strip(PUNCTUATION)
            if _usable(word):
                words.append(word)
                owners.append(position)
    return words, owners


def _parse(args: Any, vehicle_limit: int) -> _Query:
    if not isinstance(args, Mapping):
        raise BadArguments("car_numbers needs an object with the fields of its schema.",
                           "Send every field of the tool's schema.")
    make = args.get("make")
    if make is None:
        make = ""
    if not isinstance(make, str):
        raise BadArguments("`make` must be a string.", "Send the make, or an empty string.")
    make = " ".join(make.split())[:MAKE_LENGTH]
    norm = make_norm(make)
    if make and not norm:
        raise BadArguments(f"make {make!r} has no Latin letter or digit.",
                           "Give the make in English, as parts catalogues write it.")
    model_terms = _terms(args, "model_terms")
    part_terms = _terms(args, "part_terms")
    words, owners = _words(part_terms)
    ids, dropped = _vehicle_ids(args, vehicle_limit)
    if not (norm or model_terms or ids):
        raise BadArguments("car_numbers needs the car: make or model_terms, or vehicle_ids.",
                           "Send make and model_terms as catalogues spell them in English, "
                           "or the ids of vehicles an earlier result showed.")
    return _Query(make=make, make_norm=norm, model_terms=model_terms, year=_year(args),
                  engine_terms=_terms(args, "engine_terms", filters=False),
                  part_terms=part_terms,
                  words=words, owners=owners, vehicle_ids=ids, ids_dropped=dropped)


def _rows(conn: Any, statement: str, params: Mapping) -> list[dict]:
    """Rows as dicts, whether the connection hands out dict rows or tuples."""
    cursor = conn.execute(statement, params)
    rows = cursor.fetchall()
    names: list[str] | None = None
    found: list[dict] = []
    for row in rows:
        if isinstance(row, Mapping):
            found.append(dict(row))
            continue
        if names is None:
            names = [getattr(column, "name", None) or column[0]
                     for column in cursor.description]
        found.append(dict(zip(names, row, strict=True)))
    return found


def _years(start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return ""
    if start is not None and start == end:
        return str(start)
    return f"{start or ''}-{end or ''}"


def _vehicle(row: Mapping) -> dict:
    return {"id": row["id"], "source": row["source"], "make": row["make"],
            "model": row["model"], "model_code": row["model_code"] or "",
            "engine": row["engine"] or "", "engine_code": row["engine_code"] or "",
            "years": _years(row["year_from"], row["year_to"]), "kw": row["power_kw"],
            "fuel": row["fuel"] or "", "label": row["label"] or ""}


def _id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and NUMERIC_ID.fullmatch(value.strip()):
        return int(value.strip())
    return None


def numbers_for(result: Mapping, vehicle_ids: Iterable[Any] | None = None) -> list[str]:
    """Every distinct part number of a ``find`` result for the given vehicles (all of them when
    None): the fitted parts' own numbers first, then their cross numbers in result order.
    Numbers differing only in spacing or punctuation count once, as first written."""
    wanted = None if vehicle_ids is None else {_id(item) for item in vehicle_ids}
    parts = [part for part in result.get("parts") or [] if isinstance(part, Mapping)
             and (wanted is None or _id(part.get("vehicle_id")) in wanted)]
    seen: dict[str, str] = {}

    def add(number: Any) -> None:
        if isinstance(number, str) and number.strip():
            seen.setdefault(normalize_code(number) or number.strip(), number.strip())

    for part in parts:
        add(part.get("number"))
    for part in parts:
        for entry in part.get("cross") or []:
            if isinstance(entry, Mapping):
                add(entry.get("number"))
    return list(seen.values())


class CarNumbers:
    """Crawled fitment numbers for the customer's car, read-only and bounded in time."""

    def __init__(self, repository_or_connection_factory: Any, *, statement_ms: int = 3000,
                 vehicle_limit: int = 8, part_limit: int = 40, cross_limit: int = CROSS_LIMIT,
                 relations: Sequence[str] = RELATIONS) -> None:
        source = repository_or_connection_factory
        connect = getattr(source, "_connect", None)
        if not callable(connect):
            connect = source if callable(source) else None
        if connect is None:
            raise TypeError("CarNumbers needs a repository with _connect() or a callable that "
                            "returns a database connection.")
        unknown = [name for name in relations if name not in RELATIONS]
        if unknown or not relations:
            raise ValueError(f"relations must be some of {', '.join(RELATIONS)}.")
        self._connect = connect
        self.statement_ms = max(1, int(statement_ms))
        self.vehicle_limit = max(1, int(vehicle_limit))
        self.part_limit = max(1, int(part_limit))
        self.cross_limit = max(1, int(cross_limit))
        self.relations = list(dict.fromkeys(relations))

    @contextlib.contextmanager
    def _session(self) -> Iterator[Any]:
        """One read-only transaction whose statements stop after ``statement_ms``. Any
        failure to reach or read the catalogues becomes SourceUnavailable; a failed
        statement aborts the transaction, so nothing more is read after it."""
        try:
            connection = self._connect()
        except (psycopg.Error, OSError, RuntimeError) as exc:
            raise SourceUnavailable("The crawled catalogues cannot be reached right now.") \
                from exc
        try:
            with connection as conn, conn.transaction():
                conn.execute(SETUP_SQL, (str(self.statement_ms),))
                yield conn
        except psycopg.errors.QueryCanceled as exc:
            raise SourceUnavailable(
                "The crawled catalogues did not answer in time.",
                "Judge from the shop catalogue rows, or call again with fewer or more "
                "specific terms.") from exc
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn,
                psycopg.errors.InvalidSchemaName) as exc:
            raise SourceUnavailable("The crawled catalogues are not installed here.") from exc
        except (psycopg.Error, OSError) as exc:
            raise SourceUnavailable("The crawled catalogues cannot be read right now.") from exc

    def find(self, args: Any) -> dict:
        """Candidate crawled vehicles for the car words in ``args`` and the parts their
        catalogues list in the part group, each with its cross numbers.

        ``args``: ``make`` (str), ``model_terms`` (list), ``year`` (int, 0 when unknown),
        ``engine_terms`` (list, ranking only), ``part_terms`` (list, English as the
        catalogues name parts) and ``vehicle_ids`` (list; when given, only those vehicles).
        Raises BadArguments for arguments the model can fix and SourceUnavailable when the
        catalogues cannot be read.
        """
        query = _parse(args, self.vehicle_limit)
        part_filter = {"words": query.words, "owners": query.owners}
        diagnosis: dict | None = None
        kinds: list[str] = []
        with self._session() as conn:
            if query.vehicle_ids:
                rows = _rows(conn, VEHICLES_BY_ID_SQL, {"ids": query.vehicle_ids})
            else:
                rows = _rows(conn, VEHICLES_SQL, {
                    "model_terms": query.model_terms, "engine_terms": query.engine_terms,
                    **part_filter, "make": query.make_norm, "year": query.year,
                    "limit": self.vehicle_limit})
            vehicles = [_vehicle(row) for row in rows]
            ids = [vehicle["id"] for vehicle in vehicles]
            part_rows = _rows(conn, PARTS_SQL, {"ids": ids, **part_filter,
                                                "limit": self.part_limit}) if ids else []
            part_ids = list(dict.fromkeys(row["part_id"] for row in part_rows))
            cross_rows = _rows(conn, CROSS_SQL, {
                "ids": part_ids, "relations": self.relations, "order": list(CROSS_ORDER),
                "limit": self.cross_limit + 1}) if part_ids else []
            if not vehicles and not query.vehicle_ids:
                found = _rows(conn, DIAGNOSE_SQL, {
                    "model_terms": query.model_terms, "make": query.make_norm,
                    "year": query.year, "shown": SHOWN, "ids": DIAGNOSED_IDS})
                diagnosis = found[0] if found else {}
                if diagnosis.get("year_count"):
                    kinds = self._kinds(conn, diagnosis["year_ids"] or [])
            elif ids and not part_rows:
                kinds = self._kinds(conn, ids)
        truncated = len(cross_rows) > self.cross_limit
        cross: dict[Any, list[dict]] = {}
        for row in cross_rows[:self.cross_limit]:
            cross.setdefault(row["part_id"], []).append(
                {"brand": row["brand"], "number": row["number"], "relation": row["relation"]})
        order = {vehicle_id: position for position, vehicle_id in enumerate(ids)}
        part_rows.sort(key=lambda row: (order[row["vehicle_id"]], row["position"],
                                        row["name"], row["brand"], row["number"]))
        parts = [{"vehicle_id": row["vehicle_id"], "brand": row["brand"],
                  "number": row["number"], "part": row["name"] or row["position"],
                  "position": row["position"], "note": row["note"] or "",
                  "cross": [dict(entry) for entry in cross.get(row["part_id"], [])]}
                 for row in part_rows]
        matched = int(rows[0]["matched"]) if rows else 0
        listed = int(part_rows[0]["listed"]) if part_rows else 0
        notes = self._notes(query, vehicles, matched, parts, listed, truncated, diagnosis,
                            kinds)
        return {"vehicles": vehicles, "parts": parts, "vehicles_matched": matched,
                "parts_listed": listed, "cross_truncated": truncated, "note": " ".join(notes)}

    def _kinds(self, conn: Any, ids: list[int]) -> list[str]:
        if not ids:
            return []
        rows = _rows(conn, KINDS_SQL, {"ids": list(ids)[:DIAGNOSED_IDS], "limit": SHOWN})
        return [row["kind"] for row in rows if row["kind"]]

    def _notes(self, query: _Query, vehicles: list[dict], matched: int, parts: list[dict],
               listed: int, truncated: bool, diagnosis: dict | None,
               kinds: list[str]) -> list[str]:
        notes: list[str] = []
        named = _quoted(query.part_terms)
        if query.vehicle_ids:
            missing = [str(item) for item in query.vehicle_ids
                       if item not in {vehicle["id"] for vehicle in vehicles}]
            if missing:
                notes.append(f"Unknown vehicle_ids: {', '.join(missing)}.")
            if query.ids_dropped:
                notes.append(f"Only the first {self.vehicle_limit} vehicle_ids were read.")
            if vehicles and not parts:
                notes.append(_kinds_note(f"No part listed for these vehicles is named like "
                                         f"{named}." if named else
                                         "These vehicles have no listed parts.", kinds))
        elif diagnosis is not None:
            notes.append(_empty_note(query, diagnosis, kinds) if diagnosis else
                         "No crawled vehicle matched these terms.")
        if matched > len(vehicles):
            notes.append(f"Showing {len(vehicles)} of {matched} matching vehicles, the best "
                         "matches first; narrow with year or engine_terms, or send "
                         "vehicle_ids.")
        if listed > len(parts):
            notes.append(f"Showing {len(parts)} of {listed} listed parts; narrow with "
                         "part_terms or vehicle_ids.")
        if truncated:
            notes.append(f"Cross numbers stop at {self.cross_limit}.")
        return notes


def _quoted(terms: list[str]) -> str:
    return ", ".join(f'"{term}"' for term in terms)


def _kinds_note(lead: str, kinds: list[str]) -> str:
    # Joined by semicolons: catalogue texts carry commas of their own.
    if not kinds:
        return lead
    return (f"{lead} Part kinds they list: {'; '.join(kinds)}. Name the part as these "
            "catalogues do, or they do not cover it for this car.")


def _empty_note(query: _Query, diagnosis: Mapping, kinds: list[str]) -> str:
    """Which filter left no vehicle: the counts of crawled vehicles with listed parts after
    each filter, with what the catalogues do hold at the step that emptied the result."""
    make_count, model_count = diagnosis["make_count"], diagnosis["model_count"]
    year_count = diagnosis["year_count"]
    if not make_count:
        if query.make:
            return (f"No crawled vehicle with listed parts has the make {query.make!r}. Give "
                    "the make in English as parts catalogues spell it.")
        return "The crawled catalogues list no parts for any vehicle."
    lead = (f"{make_count} crawled vehicles with listed parts match the make {query.make!r}"
            if query.make else f"{make_count} crawled vehicles have listed parts")
    if query.model_terms and not model_count:
        text = f"{lead}; none matches a model term ({_quoted(query.model_terms)})."
        models = [model for model in diagnosis.get("models") or [] if model]
        if query.make and models:
            text += f" Models listed for this make: {'; '.join(models)}."
        if query.make:
            text += " If the make has a longer or other spelling, call again with it."
        return text
    if query.model_terms:
        lead += f"; {model_count} of them match a model term"
    if query.year and not year_count:
        first, last = diagnosis.get("first_year"), diagnosis.get("last_year")
        span = _years(first, None if last == 9999 else last)
        return f"{lead}; none covers {query.year}" + (f" (they cover {span})." if span
                                                     else ".")
    if query.year:
        lead += f"; {year_count} cover {query.year}"
    if not query.part_terms:
        return f"{lead}, but none could be read. Call again."
    return _kinds_note(f"{lead}; none lists a part named like {_quoted(query.part_terms)}.",
                       kinds)


# -- the shop's own facts ---------------------------------------------------------------------

CONTACTS = ("phone", "whatsapp", "email", "address", "map_url")
LINKS = ("facebook", "instagram", "tiktok")
SHOP_FACTS = (*CONTACTS, "hours", *LINKS, "services", "service_prices")


def _text_of(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def shop_info(site_content: Any, language: str) -> dict:
    """The shop's contact facts, opening hours, links and services in ``language`` (a
    language the site has no text in reads English), empty values left out, and ``missing``
    naming every fact the owner has not filled in. Never raises: when the site content
    cannot be read the answer is ``{"missing": ["all"], "note": ...}``."""
    chosen = language if language in SITE_LANGUAGES else "en"
    try:
        view = localized(site_content.snapshot(), chosen)
        brand = view.get("brand") or {}
        info: dict[str, Any] = {}
        for key in CONTACTS:
            if _text_of(brand.get(key)):
                info[key] = _text_of(brand.get(key))
        hours = [{"day": _text_of(row.get("days")), "time": _text_of(row.get("time"))}
                 for row in brand.get("hours") or [] if isinstance(row, Mapping)]
        hours = [{key: value for key, value in row.items() if value} for row in hours
                 if row["day"] or row["time"]]
        if hours:
            info["hours"] = hours
        for key in LINKS:
            if _text_of(brand.get(key)):
                info[key] = _text_of(brand.get(key))
        if _text_of(brand.get("currency")):
            info["currency"] = _text_of(brand.get("currency"))
        services = []
        for row in view.get("services") or []:
            if not isinstance(row, Mapping) or not _text_of(row.get("title")):
                continue
            service: dict[str, Any] = {"title": _text_of(row.get("title"))}
            if _text_of(row.get("summary")):
                service["summary"] = _text_of(row.get("summary"))
            if row.get("price_from") is not None:
                service["price_from"] = row["price_from"]
            services.append(service)
        if services:
            info["services"] = services
        priced = any("price_from" in service for service in services)
        info["missing"] = [name for name in SHOP_FACTS if name not in info
                           and not (name == "service_prices" and priced)]
        return info
    except Exception:  # A question about the shop must never fail the turn.
        LOGGER.warning("shop_info_unavailable")
        return {"missing": ["all"],
                "note": "The shop's details cannot be read right now; do not guess them."}
