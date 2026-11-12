"""Read tools of the assistant engine: strict definitions and their executors.

The tools hand the model data and nothing else. Which rows fit, what the customer meant
and what to ask is the model's judgement; no tool filters by part type, make or year.
Every failure is returned as ``{ok: false, error, fix}`` so the model can correct itself.

Rows go out as a header plus arrays. The id column holds a per-conversation handle
(``r1``, ``r2`` ...) that only this process can map to a product, and the shop's own stock
number is a column for the staff audience only.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg

from automotive import assistant_prompt as prompt
from automotive import oil_match
from automotive.assistant_catalog import CROSS_SHOWN, SearchError
from automotive.catalog_api import CATEGORY_TERMS
from automotive.parts_catalog import PartsCatalogService

ROW_COLUMNS = ("id", "name", "vehicle", "brand", "code", "cross_codes", "price", "store",
               "warehouse")
# What the outside sources can be asked for: the catalogue API's part groups plus the
# groups the oil and filter selectors answer (vin_search.VINSearchCoordinator._scope).
OIL_GROUPS = ("engine_oil", "oil_change")
FILTER_GROUPS = ("oil_filter", "air_filter", "cabin_filter", "fuel_filter")
SUPPLIER_GROUPS = tuple(dict.fromkeys((*CATEGORY_TERMS, *OIL_GROUPS, *FILTER_GROUPS)))
OIL_QUERY = "engine oil"
FUELS = ("unknown", "petrol", "diesel", "hybrid", "electric")
CONFIDENCES = ("confirmed", "listed", "spec", "check")
# What the customer is owed a reason for: which specification a row has, or what to verify.
REASONED = ("spec", "check")
# What the model may say of a row. `depends` is not a card's confidence: such a row waits
# for the customer's answer and is never shown.
DEPENDS = "depends"
JUDGEMENTS = (*CONFIDENCES, DEPENDS)
OUTCOMES = ("offered", "asked", "nothing_found", "sold_out", "chat", "handoff")
LANGUAGES = ("ka", "en", "ru")
READ_TOOLS = ("search_catalog", "lookup_codes", "equivalents", "supplier_parts", "oil_guide",
              "car_numbers", "research", "shop_info")
RESEARCH_KINDS = ("numbers", "verify", "fact")
SHOP_NUMBER = 5  # shorter numbers name parts of several brands: not matched to rows
CARRIED_ROWS = 24  # rows a car_numbers or research answer carries
CROSS_PER_PART = 8  # cross numbers shown per crawled part

SUPPLIER_SECONDS = 6.0  # how long supplier_parts and oil_guide wait for outside sources
SETTLE_SECONDS = 0.6  # no outside source running for this long: nothing more is coming
POLL_SECONDS = 0.1
SEARCH_LIMIT = 40
SEARCH_ROWS = 30  # rows of one search_catalog result
ELSEWHERE_ROWS = 10  # rows in stock that carry the numbers of a result's out-of-stock rows
OWN_NUMBER = 6  # a row's own number of this length names one part
MAX_TERMS = 12
MAX_NUMBERS = 8
CRAWLED_VEHICLES = 120
SHOWN_VEHICLES = 12
OUTSIDE_SOURCES = ("vpic", "catalog_api", "motorium", "jsfilter", "shell", "pakelo", "fuchs")
QUIET = ("NOT_REQUESTED", "NOT_NEEDED")
FIELD_NAMES = {"code": "own number", "cross": "cross number", "shop_code": "shop code"}
JUDGE = "Judge from the shop catalogue rows you have."
HANDLE = re.compile(r"\br\d{1,6}\b")
NUMBER_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z./-]*")
WORD = re.compile(r"[0-9a-zა-ჰа-яё]+")


class ToolError(Exception):
    """A failure the model can act on: what was wrong and what to do instead."""

    def __init__(self, error: str, fix: str) -> None:
        super().__init__(error)
        self.error, self.fix = error, fix

    def payload(self) -> dict:
        return {"ok": False, "error": self.error, "fix": self.fix}


@dataclass
class Table:
    """Catalogue rows on their way to the model, with per-row values of extra columns."""

    rows: list[Any]
    extras: list[list] = field(default_factory=list)


@dataclass
class Listed:
    """Rows the same result already carries in full: only their handles are repeated."""

    rows: list[Any]


@dataclass
class Output:
    """A tool's answer before handles are assigned. ``sourced`` are part numbers a
    car-specific outside source returned: they are what ``confirmed`` may rest on."""

    data: dict
    extra_columns: tuple[str, ...] = ()
    sourced: tuple[str, ...] = ()


def plain(value: Any) -> str:
    """Letters and digits only, upper case: how part numbers are compared."""
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def row_numbers(row: Any) -> set[str]:
    """The brand's own number and the cross numbers of a row, each as ``plain`` text."""
    found = set()
    for value in (getattr(row, "code", ""), *(getattr(row, "cross_codes", ()) or ())):
        for piece in re.split(r"[\s/,;]+", str(value or "")):
            if len(plain(piece)) >= 4:
                found.add(plain(piece))
    return found


def own_number(row: Any) -> str:
    """A row's own number when it can stand for the part by itself: six or more letters
    and digits with a digit among them. A shorter one (a grade, a size) names many parts."""
    number = plain(getattr(row, "code", ""))
    return number if len(number) >= OWN_NUMBER and any(char.isdigit() for char in number) \
        else ""


def same_part(left: Any, right: Any) -> bool:
    """Do two rows carry the same part by their numbers: two or more numbers in common, or
    the own number of one among the numbers of the other."""
    ours, theirs = row_numbers(left), row_numbers(right)
    return len(ours & theirs) >= 2 or own_number(left) in theirs or own_number(right) in ours


def same_parts(rows: list[Any]) -> list[str]:
    """A letter for every row that is the same part as another row of the list by its
    numbers (see ``same_part``), the same letter for rows linked that way; empty for a row
    that stands alone. Counted from the rows' own and cross numbers, nothing else."""
    numbers = [row_numbers(row) for row in rows]
    own = [own_number(row) for row in rows]
    group = list(range(len(rows)))

    def root(index: int) -> int:
        while group[index] != index:
            group[index] = group[group[index]]
            index = group[index]
        return index

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if (len(numbers[left] & numbers[right]) >= 2 or own[left] in numbers[right]
                    or own[right] in numbers[left]):
                group[root(right)] = root(left)
    roots = [root(index) for index in range(len(rows))]
    letters: dict[int, str] = {}
    for item in roots:
        if roots.count(item) > 1 and item not in letters:
            count = len(letters)
            letters[item] = chr(65 + count % 26) * (1 + count // 26)
    return [letters.get(item, "") for item in roots]


def typed_tokens(message: str) -> list[str]:
    """Tokens of a message that could be part numbers: five or more characters with a
    digit. Whether one is a part number is only known once the index has it."""
    found: list[str] = []
    for token in NUMBER_TOKEN.findall(message.replace("[VIN]", " ")):
        token = token.strip("./-")
        if len(token) >= 5 and any(char.isdigit() for char in token) and token not in found:
            found.append(token)
    return found


def handles_in(text: Any) -> list[str]:
    return list(dict.fromkeys(HANDLE.findall(str(text or ""))))


def handle_of(value: Any) -> str:
    """A handle as the model wrote it: `R12 ` names `r12`."""
    return value.strip().casefold()[:20] if isinstance(value, str) else ""


def supplier_query(group: str) -> str:
    """The text the source federation classifies as this group."""
    return group.replace("_", " ")


def _schema(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(properties),
            "properties": properties}


def _field(kind: str, name: str, **extra: Any) -> dict:
    return {"type": kind, "description": prompt.FIELD_DESCRIPTIONS[name], **extra}


def _strings(name: str) -> dict:
    return _field("array", name, items={"type": "string"})


def tool_definitions(ttl: str = "") -> list[dict]:
    """Strict tools: every property required, no unions, no length or range keywords.
    Caps are stated in the descriptions and enforced by the executors. ``ttl`` is how long
    the provider keeps the list ready (see ``assistant_prompt.cache_control``)."""
    car = {
        "use_vin": {"type": "boolean"}, "make": {"type": "string"}, "model": {"type": "string"},
        "year": {"type": "integer"}, "trim": {"type": "string"},
        "engine_code": {"type": "string"}, "engine_litres": {"type": "number"},
        "fuel": {"type": "string", "enum": list(FUELS)}, "body": {"type": "string"},
    }
    for name, definition in car.items():
        definition["description"] = prompt.CAR_FIELDS[name]
    product = _schema({
        "ids": _strings("ids"),
        "confidence": _field("string", "confidence", enum=list(JUDGEMENTS)),
        "evidence": _field("string", "evidence"), "reason": _field("string", "reason"),
        "group": _field("string", "group"),
    })
    groups = ", ".join(
        f"{group} ({', '.join(CATEGORY_TERMS[group])})" if group in CATEGORY_TERMS else group
        for group in SUPPLIER_GROUPS)
    schemas = {
        "search_catalog": _schema({
            "make_terms": _strings("make_terms"), "vehicle_terms": _strings("vehicle_terms"),
            "part_terms": _strings("part_terms"), "exclude_terms": _strings("exclude_terms"),
            "in_stock_only": _field("boolean", "in_stock_only"),
        }),
        "lookup_codes": _schema({"numbers": _strings("numbers"),
                                 "vehicle_hint": _field("string", "vehicle_hint")}),
        "equivalents": _schema({"id": _field("string", "id")}),
        "supplier_parts": _schema({
            "part_group": _field("string", "part_group", enum=list(SUPPLIER_GROUPS)), **car}),
        "oil_guide": _schema(copy.deepcopy(car)),
        "car_numbers": _schema({
            "make": _field("string", "cn_make"), "model_terms": _strings("model_terms"),
            "year": _field("integer", "cn_year"), "engine_terms": _strings("engine_terms"),
            "part_terms": _strings("cn_part_terms"), "vehicle_ids": _strings("vehicle_ids"),
        }),
        "research": _schema({
            "kind": _field("string", "research_kind", enum=list(RESEARCH_KINDS)),
            **{name: copy.deepcopy(car[name]) for name in (
                "make", "model", "year", "trim", "engine_code", "body")},
            "part": _field("string", "research_part"),
            "position": _field("string", "research_position"),
            "numbers": _strings("research_numbers"),
            "question": _field("string", "research_question"),
        }),
        "shop_info": _schema({}),
        # In the order the answer is decided: the language and the car, the rows, what to
        # ask, and only then the words for the customer.
        "finish": _schema({
            "language": _field("string", "language", enum=list(LANGUAGES)),
            "car_label": _field("string", "car_label"),
            "products": _field("array", "products", items=product),
            "question": _field("string", "question"),
            "options": _strings("options"),
            "suggestions": _strings("suggestions"),
            "reply": _field("string", "reply"),
            "notes": _field("string", "notes"),
            "outcome": _field("string", "outcome", enum=list(OUTCOMES)),
        }),
    }
    definitions = []
    for name, schema in schemas.items():
        description = prompt.TOOL_DESCRIPTIONS[name]
        if name == "supplier_parts":
            description += f" Part groups and what each asks for: {groups}."
        definitions.append({"name": name, "description": description, "strict": True,
                            "input_schema": schema})
    # One breakpoint after the last tool caches the whole tool list.
    definitions[-1]["cache_control"] = prompt.cache_control(ttl)
    return definitions


def _strings_of(args: dict, name: str, limit: int = MAX_TERMS) -> list[str]:
    value = args.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ToolError(f"`{name}` must be a list of strings.",
                        "Send every field of the tool's schema with the stated types.")
    cleaned = [" ".join(item.split())[:80] for item in value]
    return list(dict.fromkeys(item for item in cleaned if item))[:limit]


def _text(value: Any, limit: int = 120) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _trim_number(value: Any) -> Any:
    """`85.0` reads as `85`; anything that is not a whole number is left alone."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _matches(hits: Any, number: str) -> list[dict]:
    """The index's hits for one number, whether it keys them as typed or normalised."""
    if not isinstance(hits, dict):
        return []
    found = hits.get(number)
    if found is None:
        wanted = plain(number)
        found = next((value for key, value in hits.items() if plain(key) == wanted), None)
    return [item for item in found or [] if isinstance(item, dict)]


def _row_id(item: dict) -> Any:
    row = item.get("row")
    return row[0] if isinstance(row, list | tuple) and row else None


def _vehicle_label(vehicle: dict) -> str:
    start, end = vehicle.get("year_from"), vehicle.get("year_to")
    years = f"{start or ''}-{end or ''}" if start or end else ""
    parts = (vehicle.get("make"), vehicle.get("model"), years, vehicle.get("engine"),
             vehicle.get("model_code"), vehicle.get("engine_code"), vehicle.get("fuel"),
             vehicle.get("position"), vehicle.get("note"))
    return " ".join(str(part) for part in parts if part)[:160]


def _settled(snapshot: dict) -> bool:
    """True once the job has started and none of its outside sources is still working.
    The warehouse stages that follow them are not waited for: the rows are matched here."""
    sources = {entry.get("source_id"): entry for entry in snapshot.get("sources") or []
               if isinstance(entry, dict)}
    watched = [sources[name] for name in OUTSIDE_SOURCES if name in sources]
    if not watched or (sources.get("vpic") or {}).get("status") == "PENDING":
        return False
    return not any(entry.get("status") == "RUNNING" for entry in watched)


def _source_lines(snapshot: dict, names: tuple[str, ...]) -> list[dict]:
    lines = []
    for entry in snapshot.get("sources") or []:
        if not isinstance(entry, dict) or entry.get("source_id") not in names:
            continue
        status, code = entry.get("status"), entry.get("error_code")
        if status in ("DISABLED", "PENDING") or code in QUIET:
            continue
        line = {"source": entry["source_id"], "status": status}
        if entry.get("count"):
            line["count"] = entry["count"]
        if code:
            line["error"] = code
        lines.append(line)
    return lines


class Toolbox:
    """The read tools of one turn. ``run`` may be called from several threads at once;
    ``render`` assigns handles and must be called on one thread, in the model's order."""

    def __init__(self, *, index: Any, repository: Any, search_service: Any, vin: str | None,
                 audience: str, handles: dict | None, deadline: float,
                 crawled: Any = None, clock: Any = time.monotonic,
                 extras: dict | None = None) -> None:
        self.index, self.search_service, self.vin = index, search_service, vin
        # Services of the newer tools (see assistant_extras): each may be missing.
        self.extras: dict = dict(extras) if isinstance(extras, dict) else {}
        self.audience, self.deadline, self.clock = audience, deadline, clock
        self.crawled = crawled or PartsCatalogService(_bounded(repository))
        self.handles: dict[str, str] = {
            str(key): str(value) for key, value in (handles or {}).items()}
        self._by_product = {product: handle for handle, product in self.handles.items()}
        numbers = [int(handle[1:]) for handle in self.handles if HANDLE.fullmatch(handle)]
        self._next = max(numbers, default=0) + 1
        self.sourced: set[str] = set()
        self._crawled_down = False

    # -- rows ----------------------------------------------------------------------------

    @property
    def columns(self) -> list[str]:
        return [*ROW_COLUMNS, "shop_code"] if self.audience == "staff" else list(ROW_COLUMNS)

    def handle(self, product_id: Any) -> str:
        product_id = str(product_id)
        known = self._by_product.get(product_id)
        if known is None:
            known = f"r{self._next}"
            self._next += 1
            self.handles[known] = product_id
            self._by_product[product_id] = known
        return known

    def row(self, handle: Any) -> Any:
        """The catalogue row behind a handle the model was shown, or None."""
        product_id = self.handles.get(handle_of(handle))
        return self.index.row(product_id) if product_id is not None else None

    def row_values(self, row: Any) -> list:
        cross = "/".join(row.cross_codes[:CROSS_SHOWN])
        if len(row.cross_codes) > CROSS_SHOWN:
            cross += f" (+{len(row.cross_codes) - CROSS_SHOWN} more)"
        values = [self.handle(row.id), row.name, row.vehicle, row.brand, row.code, cross,
                  _trim_number(row.price), _trim_number(row.store),
                  _trim_number(row.warehouse)]
        if self.audience == "staff":
            values.append(row.shop_code)
        return values

    def render(self, output: Output) -> tuple[dict, int]:
        """The JSON-ready result and how many rows it carries."""
        count = 0

        def convert(value: Any) -> Any:
            nonlocal count
            if isinstance(value, Table):
                rendered = []
                for position, row in enumerate(value.rows):
                    extra = value.extras[position] if position < len(value.extras) else []
                    rendered.append([*self.row_values(row), *extra])
                count += len(rendered)
                return rendered
            if isinstance(value, Listed):
                return [self.handle(row.id) for row in value.rows]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, list):
                return [convert(item) for item in value]
            return value

        data = convert(output.data)
        if count:
            data = {"columns": [*self.columns, *output.extra_columns], **data}
            for key, field_name in (("vehicle_readings", "vehicle"), ("name_readings", "name")):
                readings = self._readings(output, key, field_name)
                if readings:
                    data[key] = readings
        self.sourced.update(output.sourced)
        return data, count

    def _readings(self, output: Output, key: str, field_name: str) -> dict[str, str]:
        """What the vehicle texts (or part names) of a result's rows mean, as a model read
        them once (``assistant_vehicles``): advice beside the rows; the row decides."""
        known = self.extras.get(key) or {}
        found: dict[str, str] = {}
        if known:
            for row in self._rows(self.rows_in(output)):
                text = (getattr(row, field_name, "") or "").strip()
                if text in known and text not in found:
                    found[text] = known[text]
        return found

    @staticmethod
    def rows_in(output: Output) -> list[str]:
        """The product ids of the rows a tool's answer carries, in its order."""
        found: dict[str, None] = {}

        def walk(value: Any) -> None:
            if isinstance(value, Table):
                found.update(dict.fromkeys(str(row.id) for row in value.rows))
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(output.data)
        return list(found)

    def _rows(self, product_ids: Any) -> list[Any]:
        rows = []
        for product_id in dict.fromkeys(product_ids):
            row = self.index.row(product_id) if product_id is not None else None
            if row is not None:
                rows.append(row)
        return rows

    def table(self, product_ids: Any, extra_columns: tuple[str, ...] = ()) -> dict | None:
        """Rows by product id as a rendered table, for the turn's own context."""
        rows = self._rows(product_ids)
        if not rows:
            return None
        return self.render(Output({"rows": Table(rows)}, extra_columns))[0]

    def typed_numbers(self, tokens: list[str], grouped: list[str]) -> dict | None:
        """Exact index hits for tokens of the customer's message. Tokens without rows are
        named too; numbers assembled from spaced groups only when they hit."""
        candidates = list({plain(token): token for token in (*grouped, *tokens)
                           if len(plain(token)) >= 5}.values())[:MAX_TERMS]
        if not candidates:
            return None
        hits = self.index.by_codes(candidates, limit_per_number=8)
        found, missing = [], []
        for token in candidates:
            matches = _matches(hits, token)
            rows = self._rows(_row_id(item) for item in matches)
            if rows:
                found.append({"typed": token, "rows": Table(rows)})
            elif token in tokens:
                missing.append(token)
        if not found and not missing:
            return None
        data: dict = {}
        if found:
            data["found"] = found
        if missing:
            data["without_rows"] = missing
        return self.render(Output(data))[0]

    # -- dispatch ------------------------------------------------------------------------

    def run(self, name: str, args: Any) -> Output:
        if not isinstance(args, dict):
            raise ToolError(f"{name} needs an object with the fields of its schema.",
                            "Send every field of the tool's schema.")
        tools = {"search_catalog": self._search, "lookup_codes": self._lookup,
                 "equivalents": self._equivalents, "supplier_parts": self._supplier,
                 "oil_guide": self._oil, "car_numbers": self._car_numbers,
                 "research": self._research, "shop_info": self._shop_info}
        tool = tools.get(name)
        if tool is None:
            raise ToolError(f"There is no tool named {name!r}.",
                            "Use " + ", ".join(tools) + " or finish.")
        try:
            return tool(args)
        except SearchError as exc:  # The index says what was wrong with the terms.
            raise ToolError(exc.message, exc.fix) from None

    def sources(self) -> dict:
        """What the outside sources of this installation can answer in this turn: the part
        groups for supplier_parts and whether oil_guide has a selector, given whether the
        conversation holds a VIN. A source whose state is unknown counts as able."""
        if self.search_service is None:
            return {"supplier_parts": "cannot answer in this turn",
                    "oil_guide": "cannot answer in this turn", **self._extra_sources()}
        has_vin = self.vin is not None
        every = [group for group in SUPPLIER_GROUPS if group not in OIL_GROUPS]
        groups = [group for group in every if self._able(group, has_vin) != []]
        oil = self._able(OIL_GROUPS[0], has_vin) != []
        return {"supplier_parts": "any part group" if groups == every else
                groups or "cannot answer in this turn",
                "oil_guide": "can answer" if oil else "cannot answer in this turn",
                **self._extra_sources()}

    def label(self, name: str, args: Any) -> str:
        """A short text for the progress line, built from the call's own terms."""
        args = args if isinstance(args, dict) else {}

        def words(key: str, limit: int) -> str:
            value = args.get(key)
            items = value if isinstance(value, list) else []
            return ", ".join(str(item) for item in items[:limit] if isinstance(item, str))

        if name == "search_catalog":
            text = " · ".join(part for part in (
                words("vehicle_terms", 2) or words("make_terms", 2), words("part_terms", 2))
                if part)
        elif name == "lookup_codes":
            text = words("numbers", 3)
        elif name == "equivalents":
            row = self.row(args.get("id"))
            text = f"{row.brand} {row.code}".strip() if row is not None else ""
        elif name == "car_numbers":
            text = " · ".join(part for part in (
                " ".join([str(args.get("make") or ""), words("model_terms", 2)]).strip(),
                words("part_terms", 2)) if part)
        elif name == "research":
            car = " ".join(str(args.get(key)) for key in ("make", "model", "year")
                           if args.get(key))
            text = " · ".join(part for part in (
                car, str(args.get("part") or "") or words("numbers", 2)) if part)
        elif name == "shop_info":
            text = "shop"
        else:
            car = " ".join(str(args.get(key)) for key in ("make", "model", "year")
                           if args.get(key))
            group = supplier_query(str(args.get("part_group") or "")) or "oil"
            text = " · ".join(part for part in (group, car) if part)
        return " ".join(text.split())[:80]

    # -- the shop's catalogue --------------------------------------------------------------

    def _search(self, args: dict) -> Output:
        groups = {name: _strings_of(args, name)
                  for name in ("make_terms", "vehicle_terms", "part_terms", "exclude_terms")}
        if not (groups["make_terms"] or groups["vehicle_terms"] or groups["part_terms"]):
            raise ToolError("make_terms, vehicle_terms and part_terms are all empty.",
                            "Give at least one term: the part in the catalogue's wording, "
                            "the car in the catalogue's spelling, or both.")
        limit = _number(args.get("limit"))
        limit = SEARCH_ROWS if limit is None else int(max(1, min(SEARCH_LIMIT, limit)))
        found = self.index.search(**groups, in_stock_only=args.get("in_stock_only") is True,
                                  limit=limit)
        columns = list(found.get("columns") or ROW_COLUMNS)
        position = columns.index("id") if "id" in columns else 0
        rows = self._rows(item[position] for item in found.get("rows") or [])
        near = self._rows(item[position] for item in found.get("similar_spelling_rows") or [])
        unmade = self._without_make(groups, args, [*rows, *near])
        hidden = self._hidden(groups, limit) if args.get("in_stock_only") is True else []
        visible = [*rows, *near, *unmade]
        elsewhere = self._elsewhere([*visible, *hidden])
        behind: list[Any] = []
        if hidden and elsewhere:
            # The hidden rows that explain a twin: the out-of-stock rows that name the car.
            marks = same_parts([*elsewhere, *hidden])
            twins = set(marks[:len(elsewhere)]) - {""}
            behind = [row for row, mark in zip(hidden, marks[len(elsewhere):], strict=True)
                      if mark in twins]
        letters = same_parts([*visible, *elsewhere, *behind])
        end = len(visible) + len(elsewhere)
        listed = Table(rows, [[letter] for letter in letters[:len(rows)]])
        data: dict = {}
        for key, value in found.items():
            if key == "similar_spelling_rows":
                continue  # Added below under handles: no raw row leaves this process.
            if key == "rows":
                data["rows"] = listed
            elif key in ("other_vehicle_texts", "other_part_names") and value:
                # Counted wordings, as [text, rows, in stock]: shorter than an object each.
                data[key] = [[item.get("text"), item.get("rows"), item.get("available")]
                             for item in value if isinstance(item, dict)]
            elif key != "columns" and value not in (None, "", [], {}):
                data[key] = value
        data.setdefault("rows", listed)
        if near:
            data["rows_under_similar_spellings"] = Table(
                near, [[letter] for letter in letters[len(rows):len(rows) + len(near)]])
        if unmade:
            start = len(rows) + len(near)
            data["rows_without_the_make_word"] = Table(
                unmade, [[letter] for letter in letters[start:start + len(unmade)]])
        if elsewhere:
            data["in_stock_under_other_cars"] = Table(
                elsewhere, [[letter] for letter in letters[len(visible):end]])
        if behind:
            data["out_of_stock_rows_with_a_twin_in_stock"] = Table(
                behind, [[letter] for letter in letters[end:]])
        return Output(data, ("same_part",))

    def _hidden(self, groups: dict, limit: int) -> list[Any]:
        """The out-of-stock rows that match the car words and the part, which in_stock_only
        hid: an in-stock row that is the same part by its numbers, filed under another car,
        is reached only through them."""
        if not groups["vehicle_terms"]:
            return []
        try:
            loose = self.index.search(**groups, in_stock_only=False, limit=limit)
        except SearchError:
            return []
        columns = list(loose.get("columns") or ROW_COLUMNS)
        position = columns.index("id") if "id" in columns else 0
        matched = int(loose.get("matched_vehicle") or 0)
        found = self._rows(item[position] for item in (loose.get("rows") or [])[:matched])
        return [row for row in found if not ((row.store or 0) > 0 or (row.warehouse or 0) > 0)]

    def _without_make(self, groups: dict, args: dict, shown: list[Any]) -> list[Any]:
        """Rows that match the car words and the part but write no make word: the make terms
        left them out of the result, and the catalogue omits the make on some rows."""
        if not (groups["make_terms"] and groups["vehicle_terms"] and groups["part_terms"]):
            return []
        try:
            loose = self.index.search(
                make_terms=[], vehicle_terms=groups["vehicle_terms"],
                part_terms=groups["part_terms"], exclude_terms=groups["exclude_terms"],
                in_stock_only=args.get("in_stock_only") is True, limit=SEARCH_LIMIT)
        except SearchError:
            return []
        columns = list(loose.get("columns") or ROW_COLUMNS)
        position = columns.index("id") if "id" in columns else 0
        matched = int(loose.get("matched_vehicle") or 0)
        inside = {str(row.id) for row in shown}
        found = self._rows(item[position] for item in (loose.get("rows") or [])[:matched])
        return [row for row in found if str(row.id) not in inside][:ELSEWHERE_ROWS]

    def _elsewhere(self, rows: list[Any]) -> list[Any]:
        """Rows in stock, outside a result, that are the same part by their numbers as a
        row of the result that is out of stock: the part filed under another car, or
        under none. Those sharing the most numbers first."""
        inside = {str(row.id) for row in rows}
        found: dict[str, tuple[int, Any]] = {}
        for row in rows:
            if (row.store or 0) > 0 or (row.warehouse or 0) > 0:
                continue
            for item in self.index.equivalents(row.id, limit=30) or []:
                other = self.index.row(_row_id(item)) if isinstance(item, dict) else None
                if other is None or str(other.id) in inside or not (
                        (other.store or 0) > 0 or (other.warehouse or 0) > 0):
                    continue
                if same_part(row, other):
                    shared = len(item.get("shared_numbers") or [])
                    if shared > found.get(str(other.id), (0, None))[0]:
                        found[str(other.id)] = (shared, other)
        ranked = sorted(found.values(), key=lambda pair: -pair[0])
        return [other for _, other in ranked[:ELSEWHERE_ROWS]]

    def _lookup(self, args: dict) -> Output:
        numbers = _strings_of(args, "numbers", MAX_NUMBERS)
        if not numbers:
            raise ToolError("`numbers` is empty.", "Give one to eight part numbers.")
        hint = [word for word in WORD.findall(_text(args.get("vehicle_hint"), 200).casefold())
                if len(word) >= 2]
        usable = [number for number in numbers if len(plain(number)) >= 4]
        hits = self.index.by_codes(usable, limit_per_number=20) if usable else {}
        entries, sourced = [], []
        shown: set[str] = set()  # rows this result already carries in full
        for number in numbers:
            entry: dict = {"number": number}
            entries.append(entry)
            if number not in usable:
                entry["note"] = "Too short to look up: a part number has four or more " \
                                "letters and digits."
                continue
            matches = _matches(hits, number)
            rows = self._rows(_row_id(item) for item in matches)
            fields = {str(_row_id(item)): item.get("field") for item in matches}
            entry["rows"] = Table(rows, [[FIELD_NAMES.get(fields.get(str(row.id)), "")]
                                         for row in rows])
            shown.update(str(row.id) for row in rows)
            # What the crawled catalogues list for a number is shown, but a list of cars
            # is not a source for this car: `confirmed` rests on car-specific tools only.
            self._crawled(number, hint, entry, shown)
            if not rows and len(entry) == 2:
                entry["note"] = "No catalogue row carries this number."
        data: dict = {"numbers": entries}
        if self._crawled_down:
            data["note"] = ("The crawled manufacturer catalogues could not be read right "
                            "now; only the shop catalogue was checked.")
        return Output(data, ("matched_in",), tuple(sourced))

    def _crawled(self, number: str, hint: list[str], entry: dict, shown: set[str]) -> bool:
        """What crawled catalogues say about a number, added to its entry. True when they
        list vehicles for it, which makes it a number from a car-specific source."""
        if self._crawled_down:
            return False
        try:
            report = self.crawled.lookup(number, limit=CRAWLED_VEHICLES)
        except (psycopg.Error, RuntimeError, OSError, AttributeError, TypeError, ValueError):
            self._crawled_down = True
            return False
        if not isinstance(report, dict) or report.get("status") == "CATALOG_NOT_INSTALLED":
            self._crawled_down = True
            return False
        parts = [part for part in report.get("parts") or [] if isinstance(part, dict)]
        if parts:
            entry["catalogue_part"] = [
                " ".join(str(part.get(key)) for key in ("brand", "number", "name")
                         if part.get(key))[:160] for part in parts[:5]]
        vehicles = [item for item in report.get("vehicles") or [] if isinstance(item, dict)]
        labels = list(dict.fromkeys(label for label in map(_vehicle_label, vehicles) if label))
        if labels:
            def score(label: str) -> int:
                folded = label.casefold()
                return sum(1 for word in hint if word in folded)

            ranked = sorted(labels, key=score, reverse=True)
            entry["listed_for"] = {
                "total": len(labels), "more_exist": len(vehicles) >= CRAWLED_VEHICLES,
                "naming_the_hint": sum(1 for label in labels if score(label)),
                "shown": ranked[:SHOWN_VEHICLES]}
        references = [item for item in report.get("cross_references") or []
                      if isinstance(item, dict) and item.get("number")]
        if references:
            unique = list({plain(item["number"]): item for item in references}.values())[:40]
            ours = self.index.by_codes([item["number"] for item in unique], limit_per_number=5)
            stocked, others = [], []
            for item in unique:
                rows = self._rows(_row_id(match) for match in _matches(ours, item["number"]))
                name = " ".join(str(item.get(key)) for key in ("brand", "number")
                                if item.get(key))
                if rows:
                    fresh = [row for row in rows if str(row.id) not in shown]
                    shown.update(str(row.id) for row in fresh)
                    found: dict = {"number": name, "relation": item.get("relation"),
                                   "source": item.get("source")}
                    if fresh:
                        found["rows"] = Table(fresh, [["cross reference"]] * len(fresh))
                    if len(fresh) < len(rows):
                        found["rows_listed_above"] = Listed(
                            [row for row in rows if row not in fresh])
                    stocked.append(found)
                else:
                    others.append(name)
            if stocked:
                entry["equivalents"] = stocked[:12]
            if others:
                entry["equivalents_not_in_catalogue"] = others[:15]
        return bool(labels)

    def _equivalents(self, args: dict) -> Output:
        handle = handle_of(args.get("id"))
        product_id = self.handles.get(handle)
        if product_id is None:
            raise ToolError(f"equivalents names {handle or 'an empty id'}, which you were "
                            "never shown.",
                            "Use the handle of a row from a result in this conversation.")
        found = [item for item in self.index.equivalents(product_id, limit=30) or []
                 if isinstance(item, dict)]
        rows, extras = [], []
        for item in found:
            row = self.index.row(_row_id(item))
            if row is not None:
                rows.append(row)
                extras.append(["/".join(str(code) for code in item.get("shared_numbers") or []),
                               bool(item.get("same_brand"))])
        data: dict = {"of": handle, "rows": Table(rows, extras)}
        data["note"] = ("Two or more shared numbers are a strong hint that it is the same "
                        "part, one is a weak hint. Position and car must still agree."
                        if rows else f"No other row shares a number with {handle}.")
        return Output(data, ("shared_numbers", "same_brand"))

    # -- data beyond the shop's catalogue: crawled fitments, the web, the shop's facts ------

    def _extra_sources(self) -> dict:
        return {"car_numbers": "can answer" if self.extras.get("car_numbers") is not None
                else "cannot answer in this turn",
                "research": "can answer" if self._research_on()
                else "cannot answer in this turn",
                "shop_info": "can answer" if self.extras.get("site_content") is not None
                else "cannot answer in this turn"}

    def _research_on(self) -> bool:
        return self.extras.get("research") is not None and \
            self.audience in tuple(self.extras.get("research_audiences") or ())

    def _carriers(self, numbers: list[str]) -> Table:
        """The shop rows that carry any of the numbers, in stock first, each with the number
        it carries. Numbers shorter than five letters and digits are not matched: they
        name parts of several brands."""
        usable = [number for number in dict.fromkeys(numbers)
                  if len(plain(number)) >= SHOP_NUMBER]
        hits = self.index.by_codes(usable, limit_per_number=12) if usable else {}
        rows, via, seen = [], [], set()
        for number in usable:
            for item in _matches(hits, number):
                row = self.index.row(_row_id(item))
                if row is None or str(row.id) in seen or item.get("field") == "shop_code":
                    continue
                seen.add(str(row.id))
                rows.append(row)
                via.append([number])
        order = sorted(range(len(rows)), key=lambda position: not (
            (rows[position].store or 0) > 0 or (rows[position].warehouse or 0) > 0))
        return Table([rows[position] for position in order][:CARRIED_ROWS],
                     [via[position] for position in order][:CARRIED_ROWS])

    def _car_numbers(self, args: dict) -> Output:
        from automotive.assistant_sources import BadArguments, SourceUnavailable, numbers_for

        service = self.extras.get("car_numbers")
        if service is None:
            raise ToolError("The crawled manufacturer catalogues are not connected here.",
                            JUDGE)
        try:
            found = service.find(args)
        except (BadArguments, SourceUnavailable) as exc:
            raise ToolError(exc.error, exc.fix) from None
        vehicles = found.get("vehicles") or []
        chosen = [str(item) for item in args.get("vehicle_ids") or [] if str(item).strip()]
        ids = chosen or ([str(vehicles[0].get("id"))] if len(vehicles) == 1 else [])
        numbers = numbers_for(found, ids or None)
        parts = []
        for part in (found.get("parts") or [])[:30]:
            cross = part.get("cross") or []
            entry = {key: part.get(key) for key in ("vehicle_id", "brand", "number", "part",
                                                    "position", "note") if part.get(key)}
            if cross:
                entry["cross"] = [f"{item.get('brand') or ''} {item.get('number')}".strip()
                                  for item in cross[:CROSS_PER_PART]]
                if len(cross) > CROSS_PER_PART:
                    entry["more_cross"] = len(cross) - CROSS_PER_PART
            parts.append(entry)
        data: dict = {"vehicles": vehicles, "parts": parts, "rows": self._carriers(numbers)}
        if found.get("note"):
            data["note"] = found["note"]
        if not ids and len(vehicles) > 1:
            data["choose"] = ("Several crawled vehicles match: call car_numbers again with "
                              "vehicle_ids of the one that is the customer's car. Only the "
                              "numbers of a vehicle you chose count for confirmed.")
        # The numbers of a vehicle the model chose (or the only match) are this car's.
        return Output(data, ("via",), tuple(plain(number) for number in numbers) if ids else ())

    def _research(self, args: dict) -> Output:
        from automotive.assistant_research import for_the_bot, numbers_of

        if not self._research_on():
            raise ToolError("Web research is not available in this chat.", JUDGE)
        seconds = float(self.extras.get("research_seconds") or 8.0)
        # Leave the finish request its time: research waits at most what the turn can spare.
        wait = max(0.5, min(seconds, self.deadline - self.clock() - 8.0))
        # Web text, cleaned of links and contacts a page could plant for the bot to pass on.
        result = for_the_bot(self.extras["research"].ask(args, wait=wait))
        status = result.get("status")
        data: dict = {key: result.get(key) for key in ("status", "findings", "answer", "note")
                      if result.get(key)}
        if status == "answered":
            data["rows"] = self._carriers(numbers_of(result))
        # Numbers found on the web support check, never confirmed: nothing is sourced.
        return Output(data, ("via",))

    def _shop_info(self, args: dict) -> Output:
        from automotive.assistant_sources import shop_info

        site = self.extras.get("site_content")
        if site is None:
            raise ToolError("The shop's details are not available here.",
                            "Say that the shop staff will tell, and offer to look for parts.")
        return Output({"georgian": shop_info(site, "ka"), "english": shop_info(site, "en")})

    # -- outside sources -------------------------------------------------------------------

    def _car(self, args: dict, tool: str) -> tuple[str | None, dict]:
        """The VIN (never shown to the model) or a typed car, as the federation takes it."""
        details: dict = {}
        for key in ("trim", "engine_code", "body"):
            if _text(args.get(key), 60):
                details[key] = _text(args.get(key), 60)
        litres = _number(args.get("engine_litres"))
        if litres and 0 < litres <= 20:
            details["displacement_l"] = litres
        if args.get("fuel") in FUELS[1:]:
            details["fuel"] = args["fuel"]
        if args.get("use_vin") is True and self.vin:
            return self.vin, details
        make, model = _text(args.get("make"), 60), _text(args.get("model"), 60)
        year = _number(args.get("year"))
        year = int(year) if year is not None and 1950 <= year <= 2100 else None
        missing = [name for name, value in (("make", make), ("model", model), ("year", year))
                   if not value]
        if missing:
            lead = ("This conversation has no VIN, and " if args.get("use_vin") is True
                    else f"{tool} needs the car: ")
            raise ToolError(
                f"{lead}{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} missing.",
                "Fill make, model and year from what you know about the car, ask the "
                "customer for them, or judge from the shop catalogue rows.")
        return None, {"make": make, "model": model, "model_year": year, **details}

    def _able(self, group: str, has_vin: bool) -> list[str] | None:
        """Outside sources of this installation that can answer a group; None if unknown."""
        search = self.search_service
        external = getattr(search, "_external", None)
        if not isinstance(external, dict):
            return None

        def on(name: str) -> bool:
            entry = external.get(name)
            return bool(isinstance(entry, tuple) and len(entry) == 2 and entry[1])

        able = []
        if getattr(search, "_catalog", None) is not None and group in CATEGORY_TERMS:
            able.append("catalog_api")
        if has_vin and getattr(search, "motorium_enabled", False) is True:
            able.append("motorium")
        if has_vin and group in FILTER_GROUPS and on("jsfilter"):
            able.append("jsfilter")
        if group in OIL_GROUPS:
            able.extend(name for name in ("shell", "pakelo", "fuchs") if on(name))
        return able

    def _federation(self, group: str, query: str, args: dict, tool: str) -> tuple[dict, bool]:
        """Start or rejoin the federation job for this car and wait, at most six seconds,
        for its outside sources. The job keeps running and serves the same call later."""
        search = self.search_service
        if search is None:
            raise ToolError("No outside source is connected in this installation.", JUDGE)
        vin, configuration = self._car(args, tool)
        if self._able(group, vin is not None) == []:
            raise ToolError(
                f"No outside source of this installation answers {group} for "
                f"{'this car' if vin else 'a car without a VIN'}.", JUDGE)
        try:
            snapshot = search.start(vin, query, configuration.get("model_year"),
                                    configuration or None)
        except ValueError:
            raise ToolError("The outside sources did not accept this car.",
                            "Check make, model and year, or judge from the shop catalogue "
                            "rows.") from None
        except RuntimeError:
            raise ToolError("The outside sources are busy right now.", JUDGE) from None
        until = min(self.clock() + SUPPLIER_SECONDS, self.deadline - 1.0)
        quiet: float | None = None
        while isinstance(snapshot, dict) and snapshot.get("status") == "RUNNING":
            now = self.clock()
            if _settled(snapshot):
                quiet = now if quiet is None else quiet
                if now - quiet >= SETTLE_SECONDS:
                    return snapshot, False
            else:
                quiet = None
            if now >= until:
                return snapshot, True
            time.sleep(POLL_SECONDS)
            snapshot = search.get(snapshot.get("search_id"), 0, 100)
        if not isinstance(snapshot, dict):
            raise ToolError("The outside sources dropped the request.", JUDGE)
        return snapshot, False

    def _catalog_articles(self, snapshot: dict) -> list[dict]:
        """Articles the catalogue API named for the car. The federation's public snapshot
        lists only their warehouse matches, so the job itself is read."""
        jobs, lock = (getattr(self.search_service, name, None) for name in ("_jobs", "_lock"))
        if not isinstance(jobs, dict) or lock is None:
            return []
        with lock:
            job = jobs.get(snapshot.get("search_id"))
            items = job.get("catalog_items") if isinstance(job, dict) else None
            return copy.deepcopy([item for item in items or [] if isinstance(item, dict)])

    def _supplier_numbers(self, snapshot: dict) -> list[dict]:
        found: list[dict] = []

        def add(number: Any, brand: Any, source: str, part: Any = None) -> None:
            if isinstance(number, str) and len(plain(number)) >= 5:
                found.append({"number": number.strip(), "brand": _text(brand, 40),
                              "source": source, "part": _text(part, 60)})

        for item in self._catalog_articles(snapshot):
            add(item.get("code"), item.get("supplier"), "catalogue API", item.get("product"))
            for code in item.get("oem_codes") or []:
                add(code, "OE", "catalogue API", item.get("product"))
        for item in snapshot.get("items") or []:
            if not isinstance(item, dict):
                continue
            for record in item.get("supplier_records") or []:
                if isinstance(record, dict) and record.get("status") != "CONFLICTED":
                    add(record.get("mpn"), record.get("manufacturer"), "Motorium",
                        record.get("name"))
                    for code in record.get("oem_codes") or []:
                        add(code, "OE", "Motorium", record.get("name"))
            for record in item.get("warehouse_records") or []:
                if isinstance(record, dict) and record.get("match_type") == "CODE_CANDIDATE":
                    for via in record.get("provenance") or []:
                        source, _, number = str(via).rpartition(": ")
                        add(number, "", source or "supplier")
        for report in snapshot.get("fluids") or []:
            if not isinstance(report, dict):
                continue
            for item in report.get("items") or []:
                if isinstance(item, dict) and item.get("kind") == "filter":
                    add(item.get("product_code"), report.get("provider"),
                        str(report.get("provider") or "selector"),
                        f"{item.get('component') or ''} filter")
        unique: dict[str, dict] = {}
        for item in found:
            unique.setdefault(plain(item["number"]), item)
        return list(unique.values())

    def _supplier(self, args: dict) -> Output:
        group = args.get("part_group")
        if group not in SUPPLIER_GROUPS:
            raise ToolError(f"part_group {group!r} is not a group the outside sources answer.",
                            "Use one of: " + ", ".join(SUPPLIER_GROUPS) + ". " + JUDGE)
        snapshot, running = self._federation(group, supplier_query(group), args,
                                             "supplier_parts")
        numbers = self._supplier_numbers(snapshot)
        hits = (self.index.by_codes([item["number"] for item in numbers], limit_per_number=8)
                if numbers else {})
        matched: dict[str, tuple[Any, list[str]]] = {}
        unmatched = []
        for item in numbers:
            name = " ".join(part for part in (item["brand"], item["number"]) if part)
            found = _matches(hits, item["number"])
            for match in found:
                row = self.index.row(_row_id(match))
                if row is not None:
                    via = FIELD_NAMES.get(match.get("field"), "number")
                    matched.setdefault(str(row.id), (row, []))[1].append(f"{name} = {via}")
            if not any(self.index.row(_row_id(match)) is not None for match in found):
                unmatched.append(f"{name} ({item['part']})" if item["part"] else name)
        for item in snapshot.get("items") or []:
            for record in (item.get("warehouse_records") or []) if isinstance(item, dict) else []:
                product = record.get("product") if isinstance(record, dict) else None
                if not isinstance(product, dict) or record.get("match_type") not in (
                        "CODE_CANDIDATE", "VERIFIED_FITMENT", "FITMENT_CANDIDATE"):
                    continue
                row = self.index.row(product.get("source_product_id"))
                if row is not None and str(row.id) not in matched:
                    via = [str(value) for value in record.get("provenance") or []][:3]
                    matched[str(row.id)] = (row, via or ["fitment record"])
        chosen = list(matched.values())[:SEARCH_LIMIT]
        vehicle = snapshot.get("vehicle") if isinstance(snapshot.get("vehicle"), dict) else {}
        state = str(snapshot.get("status") or "").lower()
        data: dict = {
            # A job whose outside sources are done is complete here, whatever its own
            # warehouse stages are still doing.
            "status": "still_running" if running else
            "complete" if state in ("running", "complete") else state,
            "car": " ".join(str(vehicle.get(key)) for key in ("make", "model", "model_year")
                            if vehicle.get(key)),
            "sources": _source_lines(snapshot, OUTSIDE_SOURCES),
            "supplier_numbers": len(numbers),
            "rows": Table([row for row, _ in chosen],
                          [["; ".join(dict.fromkeys(via))[:200]] for _, via in chosen]),
        }
        if unmatched:
            data["numbers_not_in_catalogue"] = unmatched[:30]
        if not numbers:
            note = ("No outside source returned part numbers for this car and part. That "
                    "says nothing about fit: judge from the shop catalogue rows.")
        elif chosen:
            note = ("These rows carry a number the outside sources give for this car. Check "
                    "that the row's name is the requested part and position.")
        else:
            note = ("The outside sources named part numbers for this car, but no catalogue "
                    "row carries one of them. Rows found by name may still fit.")
        if running:
            note += (" Some sources were still working after "
                     f"{SUPPLIER_SECONDS:g} seconds; the same call next turn returns the "
                     "finished answer.")
        data["note"] = note
        return Output(data, ("via",), tuple(plain(item["number"]) for item in numbers))

    def _oil(self, args: dict) -> Output:
        snapshot, running = self._federation("engine_oil", OIL_QUERY, args, "oil_guide")
        reports = [report for report in snapshot.get("fluids") or []
                   if isinstance(report, dict)]
        answered = [report for report in reports
                    if report.get("status") == "PROVISIONAL" and report.get("items")]
        variants, codes = [], []
        for report in answered:
            oils = [item for item in report["items"]
                    if isinstance(item, dict) and item.get("kind") == "oil"]
            need = oil_match.requirements([report])
            summary = {key: value for key, value in oil_match.summary(need).items()
                       if key != "sources"}
            if not summary.get("grades"):
                continue
            approvals = sorted({str(value) for item in oils
                                for value in item.get("approvals") or []})
            codes.extend(plain(item.get("product_code")) for item in oils
                         if len(plain(item.get("product_code"))) >= 5)
            variants.append({
                "source": report.get("provider"), "label": report.get("vehicle_label"),
                **summary, **({"approvals": approvals} if approvals else {}),
                "products": list(dict.fromkeys(
                    item["name"] for item in need["recommended"]))[:4]})
        agreed = len({(tuple(sorted(item["grades"])), item.get("capacity_l"),
                       item.get("engine_code")) for item in variants}) <= 1
        undecided = list(dict.fromkeys(
            str(label) for report in reports if report.get("status") == "CONFLICTED"
            for label in report.get("candidates") or []))[:12]
        data: dict = {"status": "answered" if variants and agreed else
                      "sources_disagree" if variants else
                      "engine_ambiguous" if undecided else
                      "still_running" if running else "no_answer"}
        if variants and agreed:
            merged = oil_match.summary(oil_match.requirements(answered))
            approvals = sorted({value for item in variants
                                for value in item.get("approvals", [])})
            data["needs"] = {**merged, **({"approvals": approvals} if approvals else {}),
                             "products": list(dict.fromkeys(
                                 name for item in variants for name in item["products"]))[:6]}
            note = ("Supplier-asserted. Find oils of this grade with search_catalog, with "
                    "the grade as the catalogue writes it.")
        elif variants:
            data["variants"] = variants
            note = ("The selectors disagree. Decide by the engine the customer has, or ask "
                    "which it is.")
        else:
            note = ("The selectors gave no oil for this car. That says nothing about the "
                    "car: ask for the engine or go on with the grade the customer names.")
        if undecided:
            data["engine_variants"] = undecided
            note += (" The selectors list several engines. If the conversation decides the "
                     "engine, call oil_guide again with trim, engine_code, engine_litres "
                     "and fuel filled; otherwise ask which engine it is.")
        if running:
            note += (" Some selectors were still working after "
                     f"{SUPPLIER_SECONDS:g} seconds; the same call next turn returns the "
                     "finished answer.")
        data["sources"] = _source_lines(snapshot, ("shell", "pakelo", "fuchs"))
        data["note"] = note
        return Output(data, sourced=tuple(dict.fromkeys(codes)))


def _bounded(repository: Any) -> Any:
    """The same database with a statement timeout, so a slow read cannot hold a turn."""
    from automotive.repository import AutomotiveRepository

    if isinstance(repository, AutomotiveRepository):
        return AutomotiveRepository(repository.database_url, statement_timeout_ms=4000)
    return repository
