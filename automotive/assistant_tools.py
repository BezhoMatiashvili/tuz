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

from automotive import assistant_followup, assistant_oil_intake, oil_match
from automotive import assistant_prompt as prompt
from automotive.assistant_catalog import CROSS_SHOWN, SearchError
from automotive.assistant_oil_quote import (
    is_engine_oil,
    is_oil,
    oil_grade_conflict,
    oil_pack,
    pack_counts,
    pack_price,
)
from automotive.catalog_api import CATEGORY_TERMS
from automotive.codes import latin, normalize_code
from automotive.oil_match import normalize_grade
from automotive.parts_catalog import PartsCatalogService
from automotive.recommendations import OIL_DPF_REQUIRED, OIL_MARKET_REQUIRED, RecommendationService

ROW_COLUMNS = ("id", "name", "vehicle", "brand", "code", "cross_codes", "price", "store",
               "warehouse")
# What the outside sources can be asked for: the catalogue API's part groups plus the
# groups the oil and filter selectors answer (vin_search.VINSearchCoordinator._scope).
OIL_GROUPS = ("engine_oil", "oil_change")
FILTER_GROUPS = ("oil_filter", "air_filter", "cabin_filter", "fuel_filter")
SUPPLIER_GROUPS = tuple(dict.fromkeys((*CATEGORY_TERMS, *OIL_GROUPS, *FILTER_GROUPS)))
OIL_QUERY = "engine oil"
GENERIC_OIL_TERMS = frozenset({
    "oil", "engine oil", "motor oil", "ზეთი", "ძრავის ზეთი", "масло", "моторное масло",
})
FUELS = ("unknown", "petrol", "diesel", "hybrid", "electric")
CONFIDENCES = ("confirmed", "listed", "spec", "check")
# What the customer is owed a reason for: which specification a row has, or what to verify.
REASONED = ("spec", "check")
# What the model may say of a row. `depends` is not a card's confidence: such a row waits
# for the customer's answer and is never shown.
DEPENDS = "depends"
JUDGEMENTS = (*CONFIDENCES, DEPENDS)
OUTCOMES = ("offered", "asked", "nothing_found", "sold_out", "chat", "handoff")
# What finish.staff.intent may say: the customer wants to come to the shop, wants work done, or
# neither. The shop's staff watch the Live chats page and are alarmed by the first two.
STAFF_INTENTS = ("none", "visit", "service")
LANGUAGES = ("ka", "en", "ru")
READ_TOOLS = ("search_catalog", "lookup_codes", "equivalents", "supplier_parts", "oil_guide",
              "car_numbers", "research", "shop_info")
RESEARCH_KINDS = ("numbers", "verify", "fact")
SHOP_NUMBER = 5  # shorter numbers name parts of several brands: not matched to rows
CARRIED_ROWS = 24  # rows a car_numbers or research answer carries
CROSS_PER_PART = 8  # cross numbers shown per crawled part

SUPPLIER_SECONDS = 6.0  # how long supplier_parts and oil_guide wait for outside sources
SHOP_OILS = 24  # the shop's oils of the named grades that oil_guide returns
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
OUTSIDE_SOURCES = ("vpic", "catalog_api", "motorium", "jsfilter", "shell", "pakelo", "fuchs",
                   "brembo")
# Which Brembo products answer each brake group: drum cars take shoes where others take pads.
BRAKE_KINDS = {"brake_pads": ("brake pads", "brake shoes"),
               "brake_discs": ("brake disc", "brake drum"),
               "brakes": ("brake pads", "brake shoes", "brake disc", "brake drum")}
QUIET = ("NOT_REQUESTED", "NOT_NEEDED")
FIELD_NAMES = {"code": "own number", "cross": "cross number", "shop_code": "shop code"}
JUDGE = "Judge from the shop catalogue rows you have."
HANDLE = re.compile(r"\br\d{1,6}\b")
NUMBER_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z./-]*")
WORD = re.compile(r"[0-9a-zა-ჰа-яё]+")
_DPF_PRESENT = re.compile(
    r"(?<!\w)(?:with\s+(?:a\s+)?dpf|dpf\s+(?:present|installed)|"
    r"(?:dpf|დპფ)\s+აქვს|с\s+dpf)(?!\w)", re.IGNORECASE,
)
_DPF_ABSENT = re.compile(
    r"(?<!\w)(?:without\s+(?:a\s+)?dpf|(?:no|без)\s+dpf|dpf\s+(?:absent|removed)|"
    r"(?:dpf|დპფ)\s+(?:არ\s+აქვს|ამოღებულია))(?!\w)", re.IGNORECASE,
)


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
    verified_oils: dict[str, tuple[str, str]] = field(default_factory=dict)


def plain(value: Any) -> str:
    """Letters and digits only, upper case: how part numbers are compared."""
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def row_numbers(row: Any) -> set[str]:
    """The brand's own number and the cross numbers of a row, each as ``plain`` text."""
    if is_oil(getattr(row, "name", "")):
        return set()
    found = set()
    for value in (getattr(row, "code", ""), *(getattr(row, "cross_codes", ()) or ())):
        for piece in re.split(r"[\s/,;]+", str(value or "")):
            number = plain(piece)
            if len(number) >= 4 and any(char.isdigit() for char in number):
                found.add(number)
    return found


def own_number(row: Any) -> str:
    """A row's own number when it can stand for the part by itself: six or more letters
    and digits with a digit among them. A shorter one (a grade, a size) names many parts."""
    if is_oil(getattr(row, "name", "")):
        return ""
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
    # Cyrillic look-alike letters typed into a number count as the Latin ones (codes.latin).
    for token in NUMBER_TOKEN.findall(latin(message).replace("[VIN]", " ")):
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
        "oil_guide": _schema({
            **copy.deepcopy(car),
            "market": {"type": "string", "description":
                       "Original vehicle sales market from reliable evidence or the user, "
                       "such as US. Leave empty when unknown."},
        }),
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
            "oil_quote": {
                **_schema({
                    "oil_id": {"type": "string", "description": "Selected engine oil row ID."},
                    "filter_id": {"type": "string", "description": "Selected oil filter row ID."},
                    "litres": {"type": "number", "description":
                               "Litres requested by the customer, or 0 for a reviewed fill."},
                }),
                "description": "Request a calculated parts-only oil and filter total when "
                               "asked for one. Otherwise send empty IDs and 0 litres.",
            },
            "question": _field("string", "question"),
            "options": _strings("options"),
            "suggestions": _strings("suggestions"),
            "reply": _field("string", "reply"),
            "notes": _field("string", "notes"),
            "outcome": _field("string", "outcome", enum=list(OUTCOMES)),
            # Last: it depends on the reply (a phone number is asked for in it) and the outcome.
            "staff": {**_schema({
                "intent": _field("string", "staff_intent", enum=list(STAFF_INTENTS)),
                "note": _field("string", "staff_note"),
                "phone": _field("string", "staff_phone"),
                "missing": _strings("staff_missing"),
            }), "description": prompt.FIELD_DESCRIPTIONS["staff"]},
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


def brembo_answer(snapshot: dict, group: str) -> dict | None:
    """What Brembo said for this car: its products for the group (axle, size, OE numbers),
    or the Brembo models or engines to choose between. Quoted, never a stock claim."""
    for report in snapshot.get("fluids") or []:
        if not isinstance(report, dict) or report.get("provider") != "Brembo":
            continue
        answer: dict = {"car": report.get("vehicle_label") or "",
                        "status": str(report.get("status") or "").lower()}
        kinds = BRAKE_KINDS.get(group, ())
        products = [{
            "part": item.get("component"), "code": item.get("product_code"),
            "line": item.get("line"), "details": item.get("details"),
            "oe_numbers": (item.get("oem_codes") or [])[:8],
            **({"note": item["note"]} if item.get("note") else {}),
        } for item in report.get("items") or []
            if isinstance(item, dict) and any(
                str(item.get("component") or "").endswith(kind) for kind in kinds)]
        if products:
            answer["products"] = products[:8]
        if report.get("candidates"):
            answer["choose_from"] = report["candidates"]
        if report.get("clarification"):
            answer["note"] = report["clarification"]
        return answer
    return None


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
        self.repository = _bounded(repository)
        self.crawled = crawled or PartsCatalogService(self.repository)
        self.handles: dict[str, str] = {
            str(key): str(value) for key, value in (handles or {}).items()}
        self._by_product = {product: handle for handle, product in self.handles.items()}
        numbers = [int(handle[1:]) for handle in self.handles if HANDLE.fullmatch(handle)]
        self._next = max(numbers, default=0) + 1
        self.sourced: set[str] = set()
        self.verified_oils: dict[str, tuple[str, str]] = {}
        self.oil_capacities: dict[str, float | None] = {}
        self.decoded_vehicle: dict = {}
        self.stated_vehicle: dict = {}
        self.followup_vehicle: dict = {}
        self.requested_oil_grades: tuple[str, ...] = ()
        self._engine_choices: set[tuple[str, ...]] = set()
        self.vin_engine_conflict = False
        # Which car's engine the customer was already asked about, and which car this turn's
        # oil look-up left depending on the engine.
        self.engine_asked_car = ""
        self.engine_split_car = ""
        self.customer_vehicle_text = ""
        self.customer_message = ""
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

    def confirmed_part(self, row: Any, sourced: set[str]) -> bool:
        return not is_oil(row.name) and bool(self.index.article_numbers(row) & sourced)

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
        packs: dict[str, dict] = {}
        conflicts: list[str] = []

        def convert(value: Any) -> Any:
            nonlocal count
            if isinstance(value, Table):
                rendered = []
                for position, row in enumerate(value.rows):
                    extra = value.extras[position] if position < len(value.extras) else []
                    rendered.append([*self.row_values(row), *extra])
                    if oil_grade_conflict(row.name, row.code):
                        conflicts.append(rendered[-1][0])
                    if litres := oil_pack(row.name):
                        packs[rendered[-1][0]] = {
                            "litres": litres, "price": pack_price(row.price, litres),
                            "stock": pack_counts({"store": row.store, "tuz11": row.warehouse},
                                                 litres)}
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
            if packs:
                data["oil_packs"] = packs
            if conflicts:
                data["oil_grade_conflicts"] = list(dict.fromkeys(conflicts))
            for key, field_name in (("vehicle_readings", "vehicle"), ("name_readings", "name")):
                readings = self._readings(output, key, field_name)
                if readings:
                    data[key] = readings
        self.sourced.update(output.sourced)
        self.verified_oils.update(output.verified_oils)
        if output.verified_oils:
            capacity = output.data["needs"].get("capacity_l")
            for product_id in output.verified_oils:
                previous = self.oil_capacities.get(product_id, capacity)
                self.oil_capacities[product_id] = capacity if capacity == previous else None
        variants = output.data.get("engine_variants")
        brembo = output.data.get("brembo") or {}
        if not variants and str(brembo.get("note", "")).startswith("Several Brembo engines fit"):
            variants = brembo.get("choose_from")
        if variants:
            titles = {_text(title, 160) for title in variants} - {""}
            self._engine_choices.add(tuple(sorted(titles)))
        return data, count

    @property
    def engine_variants(self) -> tuple[str, ...]:
        return next(iter(self._engine_choices)) if len(self._engine_choices) == 1 else ()

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

    def typed_numbers(self, tokens: list[str], grouped: list[str],
                      quiet: bool = False) -> dict | None:
        """Exact index hits for tokens of the customer's message. Tokens without rows are
        named too (not when ``quiet``); numbers assembled from spaced groups only when they
        hit. A token without rows that ends in a maker's revision letters also brings the
        rows that carry the same number in another revision, apart from the exact hits."""
        candidates = list({plain(token): token for token in (*grouped, *tokens)
                           if len(plain(token)) >= 5
                           and not self._requested_oil_grade(token)}.values())[:MAX_TERMS]
        if not candidates:
            return None
        hits = self.index.by_codes(candidates, limit_per_number=8)
        found, missing, revised = [], [], []
        revisions = getattr(self.index, "revisions", None)
        for token in candidates:
            matches = _matches(hits, token)
            rows = self._rows(_row_id(item) for item in matches)
            if rows:
                found.append({"typed": token, "rows": Table(rows)})
            else:
                # A number joined from spaced groups ('1K0 698 151 A') is not named as
                # missing, but it brings its other revisions as a typed one does; not when it
                # is only a found number with a word after it ('0986494520 a filter').
                if token in tokens:
                    missing.append(token)
                elif any(plain(token).startswith(plain(item["typed"])) for item in found):
                    continue
                other = revisions(token) if callable(revisions) else []
                rows = self._rows(row_id for row_id, _ in other)
                if rows:
                    revised.append({"typed": token,
                                    "carried_as": list(dict.fromkeys(n for _, n in other)),
                                    "rows": Table(rows)})
        if not found and not revised and (quiet or not missing):
            return None
        data: dict = {}
        if found:
            data["found"] = found
        if revised:
            data["other_revisions"] = revised
        if missing and not quiet:
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
                    "oil_guide": "can answer" if self._local_oil_configured() else
                    "cannot answer in this turn", **self._extra_sources()}
        has_vin = self.vin is not None
        every = [group for group in SUPPLIER_GROUPS if group not in OIL_GROUPS]
        groups = [group for group in every if self._able(group, has_vin) != []]
        oil = self._local_oil_configured() or self._able(OIL_GROUPS[0], has_vin) != []
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

    def _oil_search_term(self, term: str) -> str:
        if not self.requested_oil_grades:
            return term
        return assistant_followup.normalize_oil_shorthand(
            term, oil_context=True, allowed=self.requested_oil_grades)

    def _requested_oil_grade(self, token: str) -> str | None:
        if not self.requested_oil_grades:
            return None
        term = self._oil_search_term(token)
        grade = assistant_followup.oil_grade(term)
        return grade if grade in self.requested_oil_grades and plain(term) == plain(grade) else None

    def _search(self, args: dict) -> Output:
        groups = {name: _strings_of(args, name)
                  for name in ("make_terms", "vehicle_terms", "part_terms", "exclude_terms")}
        if self.requested_oil_grades:
            terms = [self._oil_search_term(term) for term in groups["part_terms"]]
            if any(set(assistant_followup.oil_grades(term)).intersection(self.requested_oil_grades)
                   for term in terms):
                # Part terms are OR alternatives. A generic oil term would admit other grades.
                terms = [term for term in terms if term.casefold() not in GENERIC_OIL_TERMS]
            groups["part_terms"] = list(dict.fromkeys(terms))
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
        visible = [*rows, *near, *unmade]
        hidden = self._hidden(groups, visible)
        original_hidden = self._hidden_first_page(groups, limit) \
            if args.get("in_stock_only") is True else []
        # New bridges fill spare candidate slots. They must not evict the stocked
        # alternatives the original search already reached when the cap is full.
        existing = self._elsewhere([*visible, *original_hidden])
        additional = self._elsewhere([*visible, *hidden]) \
            if hidden and len(existing) < ELSEWHERE_ROWS else []
        elsewhere = list({str(row.id): row for row in [*existing, *additional]}.values())[
            :ELSEWHERE_ROWS]
        behind: list[Any] = []
        if hidden and elsewhere:
            # One direct bridge per extra candidate keeps the explanation bounded too.
            # A transitive same_part group alone is not a bridge to this candidate.
            for other in elsewhere:
                bridge = next((row for row in hidden if same_part(row, other)), None)
                if bridge is not None and bridge not in behind:
                    behind.append(bridge)
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

    def _hidden_first_page(self, groups: dict, limit: int) -> list[Any]:
        """The bridges the original stock-only search already reached, before expansion."""
        if not groups["vehicle_terms"]:
            return []
        found = self.index.search(**groups, in_stock_only=False, limit=limit)
        columns = list(found.get("columns") or ROW_COLUMNS)
        position = columns.index("id") if "id" in columns else 0
        matched = int(found.get("matched_vehicle") or 0)
        rows = self._rows(item[position] for item in (found.get("rows") or [])[:matched])
        return [row for row in rows if not ((row.store or 0) > 0 or (row.warehouse or 0) > 0)]

    def _hidden(self, groups: dict, shown: list[Any]) -> list[Any]:
        """Unavailable matching rows omitted by stock filtering or the visible row limit.
        Select without stocked rows first, so they cannot crowd out a number bridge.
        The separate missing-make search follows the same terms as _without_make.
        """
        if not groups["vehicle_terms"]:
            return []
        inside = {str(row.id) for row in shown}
        try:
            found = self.index.out_of_stock_matches(**groups, limit=SEARCH_LIMIT)
            if groups["make_terms"] and groups["part_terms"]:
                found += self.index.out_of_stock_matches(
                    **{**groups, "make_terms": []}, limit=SEARCH_LIMIT)
        except SearchError:
            return []
        return list({str(row.id): row for row in found if str(row.id) not in inside
                     and not ((row.store or 0) > 0 or (row.warehouse or 0) > 0)}.values())

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
        grades = {number: grade for number in numbers
                  if (grade := self._requested_oil_grade(number))}
        usable = [number for number in numbers if number not in grades and len(plain(number)) >= 4]
        hits = self.index.by_codes(usable, limit_per_number=20) if usable else {}
        entries, sourced = [], []
        shown: set[str] = set()  # rows this result already carries in full
        for number in numbers:
            entry: dict = {"number": number}
            entries.append(entry)
            if number in grades:
                grade = grades[number]
                found = self.index.search(part_terms=[grade], limit=20)
                rows = [row for row in self._rows(item[0] for item in found["rows"])
                        if assistant_oil_intake.oil_candidate(row.name, row.code)
                        and assistant_followup.oil_grade(row.name) == grade]
                entry.update(grade=grade, rows=Table(rows, [["oil grade"] for _ in rows]))
                shown.update(str(row.id) for row in rows)
                continue
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
            if not rows:
                # The same number in another revision of the maker ('...151D' asked, the
                # shop carries '...151B'), as the customer's typed numbers get them.
                revisions = getattr(self.index, "revisions", None)
                other = revisions(number) if callable(revisions) else []
                revised = self._rows(row_id for row_id, _ in other
                                     if str(row_id) not in shown)
                if revised:
                    entry["other_revisions"] = {
                        "carried_as": list(dict.fromkeys(carried for _, carried in other)),
                        "rows": Table(revised, [["other revision"] for _ in revised])}
                    shown.update(str(row.id) for row in revised)
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
        source = self.index.row(product_id)
        found = [item for item in self.index.equivalents(product_id, limit=30) or []
                 if isinstance(item, dict)]
        rows, extras = [], []
        for item in found:
            row = self.index.row(_row_id(item))
            if row is not None and source is not None and not (
                    is_oil(source.name) or is_oil(row.name)):
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
        hits = self.index.by_codes(usable, limit_per_number=12, article_only=True) if usable else {}
        rows, via, seen = [], [], set()
        for number in usable:
            for item in _matches(hits, number):
                row = self.index.row(_row_id(item))
                if row is None or str(row.id) in seen or is_oil(row.name):
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
        return Output(data, ("via",),
                      tuple(normalize_code(number) for number in numbers) if ids else ())

    def _research(self, args: dict) -> Output:
        from automotive.assistant_research import for_the_bot, numbers_of

        if not self._research_on():
            raise ToolError("Web research is not available in this chat.", JUDGE)
        seconds = float(self.extras.get("research_seconds") or 8.0)
        # Leave the finish request its time: research waits at most what the turn can spare.
        wait = max(0.5, min(seconds, self.deadline - self.clock() - 8.0))
        raw = self.extras["research"].ask(args, wait=wait)
        # Web text, cleaned of links and contacts a page could plant for the bot to pass on.
        # A phone-shaped token stays only as a code the shop's own rows write or a number the
        # request asked about; the customers' chat sees no other phone-shaped finding number.
        codes = [code for row in self._carriers(numbers_of(raw)).rows
                 for code in row_numbers(row)]
        asked = args.get("numbers") if isinstance(args.get("numbers"), list) else []
        result = for_the_bot(raw, known=[*codes, *asked], strict=self.audience != "staff")
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
            if self.vin_engine_conflict:
                raise ToolError("The stated engine differs from or is unconfirmed by the VIN.",
                                "Use the stated configuration or hand it to staff.")
            return self.vin, details
        make, model = _text(args.get("make"), 60), _text(args.get("model"), 60)
        year = _number(args.get("year"))
        year = int(year) if year is not None and 1950 <= year <= 2100 else None
        known = self.followup_vehicle
        requested = {"make": make, "model": model, "model_year": year}
        if all(known.get(key) for key in assistant_followup.IDENTITY) and not (
                assistant_followup.conflicts(known, requested)):
            make, model, year = make or known["make"], model or known["model"], \
                year or known["model_year"]
            if not assistant_followup.conflicts(known, details, assistant_followup.DETAILS):
                details = {**{key: known[key] for key in ("engine_code", "trim", "body", "fuel",
                                                         "displacement_l") if key in known},
                           **details}
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
        if group in BRAKE_KINDS and on("brembo"):
            able.append("brembo")
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

    def _supplier_numbers(self, snapshot: dict, group: str = "") -> list[dict]:
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
                elif (isinstance(item, dict) and item.get("kind") == "brake"
                      and report.get("status") == "PROVISIONAL"
                      and any(str(item.get("component") or "").endswith(kind)
                              for kind in BRAKE_KINDS.get(group, ()))):
                    part = item.get("component")
                    for code in [item.get("product_code"), *(item.get("variants") or [])]:
                        add(code, "Brembo", "Brembo", part)
                    for code in item.get("oem_codes") or []:
                        add(code, "OE", "Brembo", part)
        unique: dict[str, dict] = {}
        for item in found:
            unique.setdefault(normalize_code(item["number"]), item)
        return list(unique.values())

    def _supplier(self, args: dict) -> Output:
        group = args.get("part_group")
        if group not in SUPPLIER_GROUPS:
            raise ToolError(f"part_group {group!r} is not a group the outside sources answer.",
                            "Use one of: " + ", ".join(SUPPLIER_GROUPS) + ". " + JUDGE)
        snapshot, running = self._federation(group, supplier_query(group), args,
                                             "supplier_parts")
        numbers = self._supplier_numbers(snapshot, group)
        hits = (self.index.by_codes([item["number"] for item in numbers], limit_per_number=8,
                                    article_only=True)
                if numbers else {})
        matched: dict[str, tuple[Any, list[str]]] = {}
        unmatched = []
        for item in numbers:
            name = " ".join(part for part in (item["brand"], item["number"]) if part)
            found = [match for match in _matches(hits, item["number"])
                     if (row := self.index.row(_row_id(match))) is not None
                     and not is_oil(row.name)]
            for match in found:
                row = self.index.row(_row_id(match))
                if row is not None:
                    via = FIELD_NAMES.get(match.get("field"), "number")
                    matched.setdefault(str(row.id), (row, []))[1].append(f"{name} = {via}")
            if not found:
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
        chosen = sorted(matched.values(), key=lambda match: not (
            (match[0].store or 0) > 0 or (match[0].warehouse or 0) > 0))[:SEARCH_LIMIT]
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
        brembo = brembo_answer(snapshot, group)
        if brembo:
            data["brembo"] = brembo
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
        return Output(data, ("via",), tuple(normalize_code(item["number"]) for item in numbers))

    def _local_oil_configured(self) -> bool:
        from automotive.repository import AutomotiveRepository

        return (isinstance(self.repository, AutomotiveRepository)
                and bool(self.repository.database_url.strip()))

    def _verified_oil(self, vehicle: dict) -> Output | None:
        if not all(vehicle.get(key) for key in ("make", "model", "model_year", "engine_code")):
            return None
        try:
            result = RecommendationService(self.repository).recommend_oil(
                vehicle, deadline=self.deadline - 1.0)
        except (AttributeError, OSError, RuntimeError, psycopg.Error, TypeError):
            return None
        if OIL_DPF_REQUIRED in result.get("warnings", []):
            return Output({"status": "needs_detail", "detail": "dpf",
                           "note": "Ask whether the vehicle has a diesel particulate filter."})
        if OIL_MARKET_REQUIRED in result.get("warnings", []):
            return Output({"status": "needs_detail", "detail": "market",
                           "note": "Ask the vehicle's original sales market."})
        if result["status"] == "CONFLICTED":
            return Output({"status": "sources_disagree", "note":
                           "Reviewed engine-oil requirements conflict for this configuration."})
        requirements = result.get("requirements")
        if not requirements:
            return None
        needs = {"engine_code": vehicle["engine_code"], "grades": requirements["grades"],
                 "approval_options": [[code.upper() for code in option]
                                      for option in requirements["approval_options"]],
                 "sources": ["Reviewed manufacturer evidence"]}
        if result.get("capacity_liters"):
            needs["capacity_l"] = result["capacity_liters"]
        rows, approvals = [], []
        verified = {}
        for item in result["items"][:SEARCH_LIMIT]:
            product = item["product"]
            row = self.index.row(product["source_product_id"])
            if row is None:
                continue
            key = ("approval_content_fingerprint" if all(
                "approval_content_fingerprint" in evidence for evidence in item["evidence"])
                   else "source_fingerprint")
            verified[str(row.id)] = (key, product[key])
            rows.append(row)
            approvals.append([", ".join(item["required_approvals"]).upper()])
        if not rows:
            return Output({"status": "specification", "needs": needs})
        needs["sources"] = ["Reviewed manufacturer and product evidence"]
        return Output({
            "status": "verified",
            "needs": needs,
            "rows": Table(rows, approvals),
            "note": "These exact oil rows have verified approvals for the stated engine. "
                    "Use these rows for vehicle-specific offers.",
        }, ("verified_approvals",), verified_oils=verified)

    def _confirmed_oil_vehicle(self, vin: str | None, configuration: dict,
                               reference_engine: str | None = None) -> dict:
        vehicle = dict(self.decoded_vehicle if vin else configuration)
        form = self.stated_vehicle
        same_form = all(vehicle.get(key) and plain(vehicle[key]) == plain(form.get(key))
                        for key in ("make", "model", "model_year"))
        known_engine = (reference_engine or vehicle.get("engine_code")) if vin else (
            form.get("engine_code") if same_form else None)
        requested_engine = configuration.get("engine_code")
        remembered = self.followup_vehicle
        same_remembered = bool(remembered) and all(
            vehicle.get(key) and plain(vehicle[key]) == plain(remembered.get(key))
            for key in assistant_followup.IDENTITY)
        if not known_engine and same_remembered:
            known_engine = remembered.get("engine_code")
        if not known_engine and requested_engine:
            parts = re.findall(r"[a-z]+|[0-9]+", requested_engine, re.IGNORECASE)
            pattern = (r"(?<![a-z0-9])" + r"[ .\-/]*".join(parts) + r"(?![a-z0-9])")
            if parts and re.search(pattern, self.customer_vehicle_text, re.IGNORECASE):
                known_engine = requested_engine
        if requested_engine and plain(known_engine) != plain(requested_engine):
            return {}
        if not known_engine:
            return {key: vehicle.get(key) for key in assistant_followup.IDENTITY}
        vehicle["engine_code"] = known_engine
        if same_form and plain(form.get("engine_code")) == plain(known_engine):
            vehicle = {**form, **vehicle} if vin else dict(form)
        elif not vin:
            vehicle = {key: vehicle[key] for key in ("make", "model", "model_year", "engine_code")}
        if same_remembered:
            for key in ("dpf", "market"):
                if vehicle.get(key) in (None, "") and remembered.get(key) not in (None, ""):
                    vehicle[key] = remembered[key]
        statement = self.customer_message if "?" not in self.customer_message else ""
        present = bool(_DPF_PRESENT.search(statement))
        absent = bool(_DPF_ABSENT.search(statement))
        if present and absent:
            return {}
        if present or absent:
            known = vehicle.get("dpf")
            if isinstance(known, bool) and known != present:
                return {}
            vehicle["dpf"] = present
        return vehicle

    def oil_evidence(self, vin: str | None, configuration: dict) -> Output | None:
        """Reviewed and manufacturer requirements, read before any oil maker's selector."""
        reference = assistant_oil_intake.manufacturer_reference(
            vin or (self.vin if self._decoded_car(configuration) else None), self.decoded_vehicle,
            {**self.followup_vehicle, **self.stated_vehicle})
        if reference:
            vin = vin or self.vin
            if assistant_oil_intake.reference_engine_matches(
                    configuration.get("engine_code"), reference["engine_code"]):
                configuration = {**configuration, "engine_code": reference["engine_code"]}
        vehicle = self._confirmed_oil_vehicle(
            vin, configuration, reference["engine_code"] if reference else None)
        verified = self._verified_oil(vehicle)
        if verified is None and vin and not vehicle.get("engine_code"):
            # vPIC's descriptive label is a lookup key. Only independently reviewed
            # requirements for the exact configuration can turn it into an oil answer.
            if engine_model := self.decoded_vehicle.get("engine_model"):
                candidate = self._confirmed_oil_vehicle(vin, configuration, engine_model)
                verified = self._verified_oil(candidate)
        if verified is not None:
            return verified
        if reference:
            return Output({"status": "specification", "needs": reference})
        return None

    def _decoded_car(self, configuration: dict) -> bool:
        return all(self.decoded_vehicle.get(key)
                   and plain(configuration.get(key)) == plain(self.decoded_vehicle[key])
                   for key in assistant_followup.IDENTITY)

    def _with_spec_oils(self, evidence: Output) -> Output:
        """A requirement without verified products also gets the shop's oils that document it."""
        if evidence.data.get("status") != "specification":
            return evidence
        rows = self._spec_oils(evidence.data["needs"])
        data = {**evidence.data, "note": (
            "needs states what this engine requires. Offer rows as check: the approval must be "
            "confirmed on the bottle." if rows else
            "needs states what this engine requires, and no shop oil documents it: state it and "
            "hand over.")}
        if rows:
            data["rows"] = Table(rows, [[normalize_grade(row.name)] for row in rows])
        return Output(data, ("grade",) if rows else ())

    def _spec_oils(self, needs: dict) -> list[Any]:
        """Shop engine oils of these grades whose bottle name states the approval, or whose
        package research, still matching the product, documents it. Stocked first."""
        options = needs.get("approval_options") or []
        grades = set(needs.get("grades") or [])
        if not options or not grades:
            return []
        research = self._package_research(grades, options)
        rows = [row for row in self.index.rows()
                if normalize_grade(row.name) in grades
                and assistant_oil_intake.oil_candidate(row.name, row.code)
                and (assistant_oil_intake.stock_candidate(row.name, row.brand, needs)
                     or self._researched(row, research.get(str(row.id))))]
        rows.sort(key=lambda row: ((row.store or 0) <= 0, (row.warehouse or 0) <= 0, row.name))
        return rows[:SHOP_OILS]

    def _package_research(self, grades: set[str], options: list) -> dict[str, dict]:
        read = getattr(self.repository, "oil_package_research", None)
        if not callable(read):
            return {}
        try:
            entries = read(sorted(grades))
        except (OSError, RuntimeError, psycopg.Error):
            return {}
        matching = {}
        for entry in entries:
            # Industry classifications describe performance, not manufacturer approval.
            standards = [value for value in entry["specifications"]
                         if value.upper().startswith(("API ", "ILSAC "))]
            approvals = assistant_oil_intake.documented_standards(
                [*entry["approvals"], *standards])
            if any(option and all(normalize_code(code) in approvals for code in option)
                   for option in options):
                matching[str(entry["source_product_id"])] = entry
        return matching

    def _researched(self, row: Any, document: dict | None) -> bool:
        if not document:
            return False
        try:
            current = self.repository.get_product(str(row.id))
        except (OSError, RuntimeError, psycopg.Error):
            return False
        return bool(current
                    and document["catalog_fingerprint"] == current.get(
                        "approval_content_fingerprint")
                    and document["sae_grade"] == normalize_grade(current.get("product_name") or ""))

    def _oil(self, args: dict) -> Output:
        vin, configuration = self._car(args, "oil_guide")
        # The customer says the engine is not the VIN's: no factory fact may choose its oil,
        # however the car is asked about.
        if self.vin_engine_conflict and self._decoded_car(configuration):
            return Output({"status": "vehicle_conflict", "note": (
                "The VIN describes an engine the customer says is not installed. Offer no oil: "
                "say the shop's team will check which fits, with outcome handoff.")})
        # A code the customer never gave verifies nothing, and the catalogues are asked without
        # it: it would hide the other engines they list.
        if configuration.get("engine_code") and not self._confirmed_oil_vehicle(
                vin, configuration).get("engine_code"):
            configuration = {key: value for key, value in configuration.items()
                             if key != "engine_code"}
            args = {**args, "engine_code": ""}
        if evidence := self.oil_evidence(vin, configuration):
            return self._with_spec_oils(evidence)
        if self._local_oil_configured() and (
                self.search_service is None or self._able("engine_oil", vin is not None) == []):
            return Output({"status": "no_answer",
                           "note": "No reviewed oil match for this exact configuration. If "
                                   "one engine or market detail is missing, ask it once; "
                                   "otherwise hand over to the shop's team."})
        snapshot, running = self._federation("engine_oil", OIL_QUERY, args, "oil_guide")
        reports = [report for report in snapshot.get("fluids") or []
                   if isinstance(report, dict)]
        engine = configuration.get("engine_code") or (
            self.followup_vehicle.get("engine_code") if vin else None)
        # Shell is the first catalogue and the others only back it up: once it has read the
        # grades of every engine, what they say adds nothing and must not outvote it.
        grouped_shell = [report for report in reports
                         if report.get("provider") == "Shell" and report.get("grade_groups")]
        reports = grouped_shell or reports
        # A report that names another engine is dropped; one that names none is no disagreement.
        answered = [report for report in reports
                    if report.get("status") == "PROVISIONAL" and report.get("items")
                    and (not engine or not report.get("engine_code")
                         or plain(report.get("engine_code")) == plain(engine))]
        variants, matches = [], set()
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
            for item in oils:
                grade = oil_match.normalize_grade(item.get("viscosity") or item.get("product_name"))
                code = normalize_code(str(item.get("product_code") or ""))
                brand = oil_match.BRANDS.get(str(report.get("provider") or "").casefold())
                if grade and len(code) >= 5 and brand:
                    matches.add((grade, code, brand))
            variants.append({
                "source": report.get("provider"), "label": report.get("vehicle_label"),
                **summary, **({"approvals": approvals} if approvals else {}),
                "products": list(dict.fromkeys(
                    item["name"] for item in need["recommended"]))[:4]})
        # Catalogues agree when they share a grade. One that reports no fill quantity or engine
        # code, or more grades than another, does not disagree.
        # A catalogue that lists several engines still answers when all of them take a grade
        # in common; otherwise the engine decides the grade.
        split: list[dict] = []
        by_fuel = False  # one grade, but a diesel's oil must also state its standard
        for report in reports:
            groups = report.get("grade_groups") if report.get("status") == "CONFLICTED" else None
            if not groups:
                continue
            common = set.intersection(*(set(group["grades"]) for group in groups))
            fuels = {fuel for group in groups for fuel in group.get("fuels") or []}
            if common and {"diesel", "petrol"} <= fuels:
                by_fuel = True
                split.append({"grades": sorted(common),
                              "engines": [name for group in groups for name in group["engines"]]})
                continue
            if not common:
                split.extend({key: group[key] for key in ("grades", "engines")}
                             for group in groups)
                continue
            filled = {value for group in groups for value in group.get("capacities") or [None]}
            try:
                litres = float(str(filled.pop()).replace(",", ".")) if len(filled) == 1 else None
            except ValueError:
                litres = None
            variants.append({"source": report.get("provider"),
                             "label": report.get("vehicle_label"), "grades": sorted(common),
                             **({"capacity_l": litres} if litres else {})})
        shared = set.intersection(*(set(item["grades"]) for item in variants)) \
            if variants else set()
        agreed = not variants or bool(shared)
        open_engines = [
            str(label) for report in reports
            if report.get("status") == "CONFLICTED" and not report.get("grade_groups")
            for label in report.get("candidates") or []]
        undecided = list(dict.fromkeys(
            [engine for group in split for engine in group["engines"]] + open_engines))[:12]
        data: dict = {"status": "sources_disagree" if variants and not agreed else
                      "engine_ambiguous" if undecided else
                      "answered" if variants and agreed else
                      "still_running" if running else "no_answer"}
        rows: list[Any] = []
        if variants and agreed and not undecided:
            needs: dict = {"grades": [grade for grade in dict.fromkeys(
                grade for item in variants for grade in item["grades"]) if grade in shared]}
            for key in ("capacity_l", "engine_code"):
                values = {item[key] for item in variants if item.get(key)}
                if len(values) == 1:
                    needs[key] = values.pop()
            needs["sources"] = list(dict.fromkeys(item["source"] for item in variants))
            approvals = sorted({value for item in variants
                                for value in item.get("approvals", [])})
            data["needs"] = {**needs, **({"approvals": approvals} if approvals else {}),
                             "products": list(dict.fromkeys(
                                 name for item in variants
                                 for name in item.get("products", [])))[:6]}
            data["catalogue_codes"] = sorted({code for _, code, _ in matches})
            rows = self._shop_oils(needs["grades"])
            if rows:
                data["rows"] = Table(rows, [[normalize_grade(row.name)] for row in rows])
                note = ("The oil makers' catalogues name these grades for this car. Offer the "
                        "shop's engine oils of these grades, listed in rows, as spec; no other "
                        "search is needed. A diesel with a particle filter takes only rows "
                        "whose own name states the standard.")
            else:
                note = ("The oil makers' catalogues name these grades for this car, and the "
                        "shop lists no engine oil of them: say so and hand over.")
        elif split:
            kinds = [_engine_kinds(group["engines"]) for group in split]
            data["engine_groups"] = [{**group, "kinds": held}
                                     for group, held in zip(split, kinds, strict=True)]
            decides = ("Petrol and diesel engines are listed, and a diesel's oil must also "
                       "state its standard: ask once petrol or diesel" if by_fuel else
                       "The engine decides the grade (engine_groups). Ask once, with one option "
                       "for each entry of engine_groups: each option covers every engine of its "
                       "own entry and none of another's, worded for a customer (new or old "
                       "body and its years, hybrid, power, litres)")
            # Entries that hold the same kind of engine cannot be told apart by that word.
            twice = [kind for kind in ("petrol, not hybrid", "diesel")
                     if sum(kind in held for held in kinds) > 1]
            if twice and not by_fuel:
                decides += (f". Several entries hold {twice[0]} engines (see kinds): give each "
                            "of them its own option for those, named by what differs (new or "
                            "old body and its years, power); a hybrid option never stands for "
                            "the others")
            note = (f"{decides}, unless the conversation already says; then call oil_guide "
                    "again with it.")
        elif variants:
            data["variants"] = variants
            note = ("The selectors disagree. Decide by the engine the customer has, or ask "
                    "which it is.")
        else:
            note = ("The selectors gave no oil for this car. If you did not use the European "
                    "model name, call again with it; otherwise hand over to the shop's team, "
                    "or go on with the grade the customer names.")
        asked_before = False
        if undecided:
            data["engine_variants"] = undecided
            car = configuration if configuration.get("make") else self.decoded_vehicle
            self.engine_split_car = "|".join(
                plain(car.get(key)) for key in assistant_followup.IDENTITY)
            asked_before = self.engine_split_car == self.engine_asked_car
        else:
            self.engine_split_car = ""
        if asked_before and self.audience == "public":
            # One question is all a customer gets: nothing is left to guess an oil from.
            data.pop("engine_groups", None)
            data.pop("engine_variants", None)
            note = ("You already asked which engine this car has, and the grade still depends "
                    "on it. Do not ask again: offer no oil and say the shop's team will check "
                    "which fits, with outcome handoff and a staff note naming the car.")
        elif undecided and not split:
            note += (" The selectors list several engines. If the conversation decides the "
                     "engine, call oil_guide again with trim, engine_code, engine_litres "
                     "and fuel filled; otherwise ask once which engine it is.")
        if running and data["status"] != "answered":
            note += (" Some selectors were still working after "
                     f"{SUPPLIER_SECONDS:g} seconds; the same call next turn returns the "
                     "finished answer.")
        data["sources"] = _source_lines(snapshot, ("shell", "pakelo", "fuchs"))
        data["note"] = note
        return Output(data, ("grade",) if rows else (),
                      sourced=tuple(data.get("catalogue_codes", ())))

    def _shop_oils(self, grades: list[str]) -> list[Any]:
        """The shop's engine oils whose name states one of these grades, stocked first."""
        wanted = set(grades)
        rows = [row for row in self.index.rows()
                if normalize_grade(row.name) in wanted and is_engine_oil(row.name)]
        rows.sort(key=lambda row: ((row.store or 0) <= 0, (row.warehouse or 0) <= 0, row.name))
        return rows[:SHOP_OILS]


def _engine_kinds(titles: list[str]) -> list[str]:
    """What a customer would call the engines that a chooser's titles name."""
    kinds = set()
    for title in titles:
        if "hybrid" in title.casefold():
            kinds.add("hybrid")
        elif "(D)" in title:
            kinds.add("diesel")
        elif "(P)" in title:
            kinds.add("petrol, not hybrid")
    return sorted(kinds)


def _bounded(repository: Any) -> Any:
    """The same database with a statement timeout, so a slow read cannot hold a turn."""
    from automotive.repository import AutomotiveRepository

    if isinstance(repository, AutomotiveRepository):
        return AutomotiveRepository(repository.database_url, statement_timeout_ms=4000)
    return repository
