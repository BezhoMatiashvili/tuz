"""The assistant engine: one chat turn in which the model searches the catalogue through
read tools, judges the rows and writes the reply.

Code here does three things and no more. It hands the model data (the decoded car, exact
part-number hits, the rows of the last turn, tool results). It keeps facts honest: cards
are built from catalogue rows by id with stock re-read, a row the model names must be one
it was shown, quoted evidence must be in the row, ``confirmed`` needs a number from a
car-specific source, and staff vetoes hold. And it keeps privacy and limits: the VIN never
leaves this process, and a turn is bounded in time, model requests and tool calls.

Nothing here decides what the customer meant, which rows fit or what to ask. The model's
own answer is held to its form: a reply that asks carries answer buttons, a row the model
says depends on a fact the customer has not given is kept back until the reply asks for
it, and a message in Georgian or Russian letters is answered in that language. A failed
check goes back to the model with the reason; after two corrections the valid part of its
answer is used, so the customer always gets a reply.

A reply is read as it is written (streamed), so a slow one has the whole turn's time and a
stalled one is noticed and asked for again. Opening a conversation has the provider read
the fixed part of the prompt ahead of the first message (``prepare``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

import anthropic
import psycopg

from automotive import assistant_followup as followup
from automotive import assistant_oil_intake as oil_intake
from automotive import assistant_prompt as prompt
from automotive import prompt_library, shop_hours, stock
from automotive.assistant_oil_quote import (
    is_engine_oil,
    is_oil,
    is_oil_filter,
    oil_filter_total,
    oil_grade_conflict,
    oil_pack,
    pack_counts,
    pack_price,
    requested_litres,
)
from automotive.assistant_tools import (
    CONFIDENCES,
    DEPENDS,
    JUDGEMENTS,
    LANGUAGES,
    OUTCOMES,
    REASONED,
    STAFF_INTENTS,
    WORD,
    Toolbox,
    ToolError,
    handle_of,
    handles_in,
    plain,
    row_numbers,
    tool_definitions,
    typed_tokens,
)
from automotive.codes import codes_in_text, normalize_code
from automotive.live_alerts import clean_phone, search_facts
from automotive.model_client import out_of_credit
from automotive.oil_match import normalize_grade
from automotive.rules import Rules

LOGGER = logging.getLogger(__name__)
MAX_OUTPUT_TOKENS = 2400
MESSAGE_LIMIT = 2000
REPLY_LIMIT = 600
# finish.staff: what the shop's staff are told (see ``_staff``); nothing for them by default.
NO_STAFF = {"intent": "none", "note": "", "phone": "", "missing": []}
STAFF_NOTE_LIMIT = 160
STAFF_WORD_LIMIT = 80
STAFF_MISSING_LIMIT = 6
LABEL_LIMIT = 40
NOTES_LIMIT = 800
MAX_CORRECTIONS = 2
MAX_PRODUCTS = 20
MAX_OPTIONS = 8
MAX_SUGGESTIONS = 4
MAX_GROUPS = 6
TRANSCRIPT_TURNS = 8
KNOWN_ROWS = 60
HANDLE_LIMIT = 3000
SOURCED_LIMIT = 800
RESULT_LIMIT = 24_000
MIN_REQUEST_SECONDS = 2.0
VIN_SECONDS = 6.0
# A reply that is being written sends something several times a second. Silence this long
# is a stalled connection, and the request is worth sending again.
STALL_SECONDS = 10.0
CONNECT_SECONDS = 5.0
# The provider keeps the instructions, the guide and the tools ready for five minutes after
# they were last read. A conversation that opens later than this has them read again first.
WARM_SECONDS = 240.0  # the five-minute cache: warm again after four quiet minutes
WARM_SECONDS_1H = 3300.0  # the one-hour cache: after 55 quiet minutes
WARM_ATTEMPTS = 4  # each refusal teaches one setting the model will not take
WARM_TIMEOUT = 20.0
DASHES = re.compile("[\u2010-\u2015\u2212]")
EPHEMERAL = {"type": "ephemeral"}
ACTS = {"offered": "results", "asked": "clarify", "nothing_found": "no_results",
        "sold_out": "none_in_stock", "chat": "help", "handoff": "help"}
SECRET_SETTINGS = ("anthropic_api_key", "openrouter_api_key", "operator_token", "vin_hmac_key",
                   "database_url",
                   "enrichment_database_url", "catalog_api_key", "site_database_url",
                   "knowledge_validator_database_url")
# What the staff form and the VIN decoder may state about a car.
VEHICLE_KEYS = ("make", "model", "model_year", "trim", "engine_code", "displacement_l",
                "engine_cylinders", "fuel", "fuel_type", "transmission", "drive", "body",
                "market", "generation", "aspiration", "dpf")
VIN_FACTS = (*VEHICLE_KEYS, "engine_model")
PRODUCT_KEYS = ("source_product_id", "source_fingerprint", "product_code", "product_name",
                "product_model", "original_code", "duplicate_code", "brand",
                "last_observed_at")
# An amount next to a currency mark. `2015 ლარგუსი` is a car, not a price.
PRICE = re.compile(
    r"\d[\d.,]*\s?(?:₾|\$|€|(?:gel|lari|usd|eur)\b|ლარ(?:ი|ის|ად|ით|ში|ზე)?(?![ა-ჰ])"
    r"|лар(?:и|ей|я|ов)?(?![а-яё])|руб(?:лей|ля|ль|\.)?(?![а-яё])|доллар|евро)"
    r"|(?:₾|\$|€|\bgel\b)\s?\d", re.IGNORECASE)
FAILED = {
    "ka": "ბოდიში, პასუხის მომზადება ახლა ვერ მოხერხდა. გთხოვთ, სცადოთ ხელახლა.",
    "en": "Sorry, I could not prepare an answer just now. Please try again.",
    "ru": "Извините, сейчас не получилось подготовить ответ. Пожалуйста, попробуйте ещё раз.",
}
RETRY = {"ka": "ხელახლა ცდა", "en": "Try again", "ru": "Попробовать снова"}
OIL_TOTAL = {
    "ka": "ზეთი ({packs} შეფუთვა, {size:g} ლ) და ფილტრი: ჯამში {total:.2f} ₾, სამუშაოს გარეშე.",
    "en": "Oil ({packs} × {size:g} L) and filter: ₾{total:.2f} total, excluding labour.",
    "ru": "Масло ({packs} × {size:g} л) и фильтр: {total:.2f} ₾ без стоимости работы.",
}
OIL_GRADE_CONFLICT = "Catalog oil grades disagree. Check the package label before offering."
OIL_LABEL_CHECK = {
    "ka": ("კატალოგში ამ ზეთს ორი განსხვავებული სიბლანტე აქვს მითითებული. "
           "შეფუთვა უნდა გადავამოწმოთ."),
    "en": "The catalog lists two different grades for this oil. The shop needs to check the label.",
    "ru": "В каталоге указаны две разные вязкости этого масла. Нужно проверить этикетку.",
}
# The provider refuses every request until its account is put right (no working key, no
# access, no credit): sending the message again does not help, the part search and the
# phone do. The status names the cause for the shop; the customer is not told it.
ACCOUNT_STATUSES = frozenset({"AUTHENTICATION_ERROR", "ACCESS_DENIED", "PROVIDER_BILLING"})
UNAVAILABLE = {
    "ka": "ბოდიში, ასისტენტი ახლა მიუწვდომელია. ნაწილი შეგიძლიათ მოძებნოთ საიტზე ან "
          "დაგვირეკოთ.",
    "en": "Sorry, the assistant is not available right now. You can search for the part on "
          "the site or call the shop.",
    "ru": "Извините, ассистент сейчас недоступен. Деталь можно найти на сайте или "
          "позвонить в магазин.",
}
NOT_CONFIGURED = {
    "ka": "ჩატი ჯერ არ არის გააქტიურებული: საჭიროა Anthropic-ის API გასაღების კონფიგურაცია.",
    "en": "The assistant is not configured yet: it needs an Anthropic API key.",
    "ru": "Ассистент ещё не настроен: нужен ключ Anthropic API.",
}
LOADING = {
    "ka": "კატალოგი ჯერ იტვირთება. გთხოვთ, სცადოთ ცოტა ხანში.",
    "en": "The catalogue is still loading. Please try again in a moment.",
    "ru": "Каталог ещё загружается. Пожалуйста, попробуйте чуть позже.",
}
# What each model id refused, for services that keep no state of their own.
_MODEL_STATE: dict[str, dict] = {}
_PROCESS_KEY = secrets.token_bytes(32)
_UNREAD = object()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _text(value: Any, limit: int = 4000) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _block(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    return block.model_dump() if hasattr(block, "model_dump") else {}


def _fold(value: Any) -> str:
    """Case-insensitive text with whitespace collapsed: how labels are compared."""
    return " ".join(str(value or "").casefold().split())


def _bare(value: Any) -> str:
    """How a quote is compared to a row: case, spacing and the kind of dash do not count,
    so `2005 ->` quotes `2005–>`."""
    return "".join(DASHES.sub("-", str(value or "").casefold()).split())


def _letters(text: str) -> dict[str, int]:
    """How many letters of a text are Georgian, Cyrillic and Latin."""
    counts = {"ka": 0, "ru": 0, "en": 0}
    for char in text:
        if "\u10a0" <= char <= "\u10ff" or "\u1c90" <= char <= "\u1cbf":
            counts["ka"] += 1
        elif "\u0400" <= char <= "\u04ff":
            counts["ru"] += 1
        elif char.isascii() and char.isalpha():
            counts["en"] += 1
    return counts


def written_in(message: str) -> str:
    """The language the letters of a message settle: Georgian or Russian when it is written
    in that script. A message in Latin letters only settles nothing: it may be English,
    Georgian typed in Latin letters, or just the name of a car."""
    counts = _letters(message)
    if max(counts["ka"], counts["ru"]) < 2:
        return ""
    return "ka" if counts["ka"] >= counts["ru"] else "ru"


def _named(item: dict) -> list[str]:
    """The handles one products entry names: its `ids`, or a single `id`."""
    ids = item.get("ids")
    if not isinstance(ids, list):
        ids = [item.get("id")]
    return list(dict.fromkeys(handle_of(value) for value in ids))


def _options(value: Any) -> list[dict]:
    """Buttons as the model wrote them, nothing checked yet. A button is its text; one
    stored with a note of what it means keeps that note."""
    found = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            found.append({"label": _text(item, 200), "meaning": ""})
        elif isinstance(item, dict):
            found.append({"label": _text(item.get("label"), 200),
                          "meaning": _text(item.get("meaning"), 400)})
    return found


def _asked(raw: dict) -> tuple[str, list[dict]]:
    """The question of a finish call and its answer buttons."""
    asked = raw.get("question")
    if isinstance(asked, dict):  # {text, options}: how a stored question is shaped
        return _text(asked.get("text"), 600), _options(asked.get("options"))
    return _text(asked, 600), _options(raw.get("options"))


def _brief(options: list[dict]) -> list:
    """Buttons as the model reads them back: the text, with its note where one exists."""
    return [option if option.get("meaning") else option["label"] for option in options]


def _question(value: Any) -> dict | None:
    """A stored question: its text and at least two options."""
    if not isinstance(value, dict):
        return None
    text = _text(value.get("question") or value.get("text"), 400)
    options = [option for option in _options(value.get("options")) if option["label"]]
    return {"text": text, "options": options[:MAX_OPTIONS]} if text and options else None


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?…])\s+", text) if part]


# The step of a turn each engine method belongs to: where an unexpected error came from, for the
# staff (dashboard > Live chats) and the log. The innermost of them on the traceback decides.
STAGES = {"_converse": "model", "_create": "model", "_send": "model", "_request": "model",
          "_read": "tools", "_execute": "tools", "_one": "tools",
          "_check": "checks", "_products_of": "checks", "_staff": "checks",
          "_salvage": "answer", "_respond": "answer", "_card": "answer", "_memory": "answer",
          "_failure": "answer", "_context": "context", "_vin_facts": "context",
          "_transcript": "context"}


def _stage_of(error: BaseException) -> tuple[str, str]:
    """The step of the turn ``error`` came from (``STAGES``; 'other' outside them) and the
    innermost code location as ``file:function:line``. Only the code's own frames are read,
    never a value of the turn."""
    frames = traceback.extract_tb(error.__traceback__)
    stage = "other"
    for frame in frames:
        if frame.filename == __file__ and frame.name in STAGES:
            stage = STAGES[frame.name]
    last = frames[-1] if frames else None
    where = (f"{last.filename.rsplit('/', 1)[-1]}:{last.name}:{last.lineno}"
             if last is not None else "")
    return stage, where


def prepare(service: Any) -> None:
    """Called when a conversation opens, off the request path: build the catalogue index
    and have the provider read the fixed part of the prompt, so that the first message
    waits for neither."""
    index = catalogue_index(service)
    if index is None or service.client is not None \
            or not getattr(service.settings, "anthropic_api_key", ""):
        return  # An injected client is a test double or somebody else's to keep warm.
    AssistantTurn(service, "", None, "ka", vin=None, history=None, category="",
                  search_service=None, progress=None, deadline=None, cancelled=None).warm(index)


def catalogue_index(service: Any) -> Any:
    """The index over the snapshot the site already holds, or None while there is none.
    Its guide carries what the part names mean when the settings name a file of readings."""
    store = getattr(service, "catalog_store", None)
    if store is None or getattr(store, "loaded", True) is False:
        return None
    from automotive.assistant_catalog import index_for

    return index_for(store, str(getattr(service.settings, "assistant_names_path", "") or ""))


def token_counts(usage: Any) -> dict[str, int]:
    """The token counts of one model response, as a turn's usage keeps them."""
    counts = {key: int(getattr(usage, key, 0) or 0) for key in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens")}
    counts["cache_creation_1h_input_tokens"] = int(getattr(
        getattr(usage, "cache_creation", None), "ephemeral_1h_input_tokens", 0) or 0)
    return counts


def warm_usage(service: Any, model: str) -> dict[str, int] | None:
    """What the last request that had the provider read the fixed prompt for this model
    used, in tokens; None when there was none or it failed."""
    states = getattr(service, "assistant_state", None)
    usage = (states if isinstance(states, dict) else _MODEL_STATE).get(model, {}).get(
        "warm_usage")
    return dict(usage) if isinstance(usage, dict) else None


class AssistantTurn:
    """Same constructor as ``ConversationTurn``; ``run`` returns the turn's response."""

    ai_led = True

    def __init__(self, service: Any, message: str, vehicle: dict | None, language: str,
                 *, vin: str | None, history: Any, category: str, search_service: Any,
                 progress: Any, deadline: float | None, cancelled: Any,
                 dialogue: dict | None = None) -> None:
        from automotive.chat import VIN_PATTERN, _redact

        del category  # The older engines' part category: this one keeps no such thing.
        settings = service.settings
        self.service, self.settings = service, settings
        self.repository = service.repository
        rules = getattr(service, "rules", None)
        self.rules = rules if isinstance(rules, Rules) else Rules()
        self.secrets = tuple(value for key in SECRET_SETTINGS
                             if isinstance(value := getattr(settings, key, ""), str) and value)
        self.redact = lambda value: _redact(value, self.secrets)
        found = VIN_PATTERN.search(message)
        self.vin = found.group().upper() if found else (vin or None)
        self.message = self.redact(message)[:MESSAGE_LIMIT]
        self.script = written_in(self.message)
        state = dialogue or {}
        # A VIN that stood in a message is the customer's car. One that only the staff
        # form's VIN field holds is sent with every message, wanted or left over: it
        # reaches the model as what the form says, which a message can override.
        self.vin_typed = bool(found) or state.get("vin_source") != "form"
        memory = state.get("memory")
        self.memory: dict = dict(memory) if isinstance(memory, dict) else {}
        self.audience = "public" if state.get("audience") == "public" else "staff"
        choice = state.get("choice")
        self.choice = choice if isinstance(choice, int) and not isinstance(choice, bool) else None
        self.question = _question(state.get("pending")) or _question(self.memory.get("question"))
        remembered = self.memory.get("language")
        self.replied_in = remembered if remembered in LANGUAGES else ""
        self.language = self.replied_in or (language if language in LANGUAGES else "ka")
        self.form = {key: value for key, value in (vehicle or {}).items()
                     if key in VEHICLE_KEYS and value not in (None, "")}
        saved = self.memory.get("followup")
        saved = saved if isinstance(saved, dict) else {}
        if not saved:
            saved = followup.from_transcript(self.memory.get("transcript"))
        previous = saved.get("vehicle") or self.memory.get("vin_facts")
        previous = previous if isinstance(previous, dict) else {}
        stated = followup.stated_vehicle(self.message)
        changed = followup.conflicts(previous, stated)
        old_vin = self.memory.get("vin_key")
        new_vin = bool(self.vin and old_vin and old_vin != self._vin_key())
        if changed and not found and not new_vin and self.vin_typed:
            self.vin = None
        reset = changed or new_vin or state.get("reset_fitment") is True
        self.followup = self._followup_state(previous, stated, saved, reset)
        self.facts: dict = {}
        self.history = [entry for entry in history or [] if isinstance(entry, dict)][-12:]
        self.search_service = search_service
        self.progress = progress or (lambda _: None)
        self.cancelled = cancelled or (lambda: False)
        self.started = time.monotonic()
        self.turn_seconds = float(getattr(settings, "assistant_turn_seconds", 30) or 30)
        self.deadline = min(deadline if deadline is not None else float("inf"),
                            self.started + self.turn_seconds)
        self.model = str(getattr(settings, "assistant_model", "") or settings.anthropic_model)
        self.effort = str(getattr(settings, "assistant_effort", "low") or "")
        self.thinking = str(getattr(settings, "assistant_thinking", "") or "")
        self.max_requests = int(getattr(settings, "assistant_max_requests", 4) or 4)
        self.max_tools = int(getattr(settings, "assistant_max_tool_calls", 10) or 10)
        self.cache_ttl = str(getattr(settings, "assistant_cache_ttl", "") or "")
        # The 1h count is the part of cache_creation_input_tokens written for an hour.
        self.usage = {"model_requests": 0, "tool_executions": 0, "input_tokens": 0,
                      "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_creation_1h_input_tokens": 0}
        self.warnings: list[str] = []
        self.entries: list[dict] = []  # one progress entry per tool call of the turn
        self.seen: dict[str, None] = {}  # the rows this turn's read tools returned
        self.results: list[dict] = []
        self.requirements: dict = {}
        self.toolbox: Toolbox | None = None
        self.last_finish: Any = None
        self.last_text = ""
        self.unanswered: list[str] = []
        self.retried = False
        self._products: dict[str, Any] = {}
        self.diagnostics: dict = {"engine": "assistant", "model": self.model,
                                  "audience": self.audience, "requests": [], "tools": [],
                                  "corrections": [], "removed": []}

    # -- the turn --------------------------------------------------------------------------

    def run(self) -> dict:
        try:
            response = self._run()
        except Exception as exc:  # Whatever went wrong, the customer still gets a reply.
            stage, where = _stage_of(exc)
            LOGGER.warning("assistant_turn_failed %s stage=%s at=%s", type(exc).__name__,
                           stage, where)
            self.diagnostics["error"] = type(exc).__name__
            self.diagnostics["stage"] = stage
            response = self._failure("DATA_UNAVAILABLE")
        LOGGER.info("automotive_assistant %s", _dump({
            "status": response["status"], "outcome": response["outcome"],
            "elapsed_ms": self.diagnostics.get("elapsed_ms"), "usage": self.usage,
            "tools": [tool["name"] for tool in self.diagnostics["tools"]],
            "warnings": response["warnings"]}))
        return response

    def _run(self) -> dict:
        if self.cancelled():
            return self._failure("CANCELLED")
        client = self._client()
        if client is None:
            return self._failure("NOT_CONFIGURED")
        index = catalogue_index(self.service)
        if index is None:
            return self._failure("DATA_UNAVAILABLE", LOADING)
        self.toolbox = Toolbox(
            index=index, repository=self.repository, search_service=self.search_service,
            vin=self.vin, audience=self.audience, handles=self.memory.get("handles"),
            deadline=self.deadline, crawled=getattr(self.service, "crawled_catalog", None),
            extras=getattr(self.service, "assistant_extras", None))
        context = self._context()
        request = {
            "system": prompt.system_blocks(self.redact(str(index.guide() or "")),
                                           self.cache_ttl, self._prompt("system")),
            "tools": tool_definitions(self.cache_ttl),
            "messages": [*self._transcript(), {"role": "user", "content": [{
                "type": "text", "text": self.redact(_dump(context)),
                "cache_control": EPHEMERAL}]}],
        }
        try:
            decision, reason = self._converse(client, request)
        except anthropic.AuthenticationError:
            return self._failure("AUTHENTICATION_ERROR")
        except anthropic.PermissionDeniedError:
            return self._failure("ACCESS_DENIED")
        except anthropic.RateLimitError as exc:
            # The usage tier's monthly spend cap is a 429 too; asking again fails until
            # the month ends.
            return self._failure("PROVIDER_BILLING" if out_of_credit(exc)
                                 else "PROVIDER_RATE_LIMITED")
        except (anthropic.APITimeoutError, TimeoutError):
            return self._failure("TIMEOUT")
        except anthropic.BadRequestError as exc:
            # A request the provider refuses for the account's sake says so in its words
            # ("Your credit balance is too low ..."): that is no fault of this request.
            said = str(getattr(exc, "message", "") or exc).casefold()
            billing = out_of_credit(exc) or "credit balance" in said or "billing" in said
            return self._failure("PROVIDER_BILLING" if billing else "PROVIDER_UNAVAILABLE")
        except anthropic.APIStatusError as exc:
            # A 402: no money on the account, the backup's included when neither can pay.
            return self._failure("PROVIDER_BILLING" if out_of_credit(exc)
                                 else "PROVIDER_UNAVAILABLE")
        except anthropic.APIError:
            return self._failure("PROVIDER_UNAVAILABLE")
        except (RuntimeError, OSError, psycopg.Error):
            return self._failure("CANCELLED" if self.cancelled() else "DATA_UNAVAILABLE")
        if decision is not None:
            return self._respond(decision)
        if reason == "CANCELLED":
            return self._failure("CANCELLED")
        # No accepted finish: use what is valid in the model's last attempt, or its text.
        self.diagnostics["fallback"] = reason
        salvaged = self._salvage()
        if salvaged is not None:
            self.warnings.append("ANSWER_CHECKS_FAILED")
            return self._respond(salvaged)
        return self._failure(reason if reason in {"TIMEOUT", "REFUSED", "LIMIT_EXHAUSTED"}
                             else "UNGROUNDED_RESPONSE")

    def _converse(self, client: Any, request: dict) -> tuple[dict | None, str]:
        messages = request["messages"]
        corrections, nudged = 0, False
        while self.usage["model_requests"] < self.max_requests:
            if self.cancelled():
                return None, "CANCELLED"
            if self._remaining() < MIN_REQUEST_SECONDS:
                return None, "TIMEOUT"
            response = self._create(client, request)
            last = self.usage["model_requests"] >= self.max_requests
            if getattr(response, "stop_reason", None) == "refusal":
                return None, "REFUSED"
            content = list(getattr(response, "content", None) or [])
            blocks = [_block(block) for block in content]
            if content:
                # Passed back exactly as received: the turn's message list only grows.
                messages.append({"role": "assistant", "content": content})
            calls = [block for block in blocks
                     if block.get("type") == "tool_use" and block.get("id")]
            if not calls:
                text = _text(" ".join(str(block.get("text") or "") for block in blocks
                                      if block.get("type") == "text"))
                self.last_text = text or self.last_text
                if nudged or last:
                    return None, "UNGROUNDED_RESPONSE"
                nudged = True
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": self._prompt("nudge")}]})
                continue
            finishes = [call for call in calls if call.get("name") == "finish"]
            reads = [call for call in calls if call.get("name") != "finish"]
            results: dict[str, dict] = {}
            if reads and finishes and last:
                for call in reads:
                    results[call["id"]] = self._result(call["id"], ToolError(
                        "Not run: finish came in the same response and no round was left.",
                        "Nothing to do.").payload(), True)
            elif reads:
                results.update(self._read(reads))
            if finishes:
                for call in finishes[1:]:
                    results[call["id"]] = self._result(call["id"], ToolError(
                        "finish was called more than once in one response.",
                        "Only the first one is read.").payload(), True)
                call = finishes[0]
                if reads and not last:
                    results[call["id"]] = self._result(call["id"], ToolError(
                        "finish was called together with read tools.",
                        "Read the results in this message, then call finish alone.").payload(),
                        True)
                else:
                    self.last_finish = call.get("input")
                    errors, decision = self._check(self.last_finish)
                    if not errors:
                        return decision, "OK"
                    if getattr(response, "stop_reason", None) == "max_tokens":
                        errors.insert(0, {
                            "error": "Your output was cut off at the length limit.",
                            "fix": "Send a shorter finish: fewer products, shorter reasons "
                                   "and notes."})
                    corrections += 1
                    self.diagnostics["corrections"].append(
                        [error["error"] for error in errors])
                    if corrections > MAX_CORRECTIONS or last:
                        return None, "CHECKS_FAILED"
                    results[call["id"]] = self._result(call["id"], {
                        "ok": False, "errors": errors,
                        "fix": "Correct these and call finish again, alone."}, True)
            reply: list[dict] = [results[call["id"]] for call in calls if call["id"] in results]
            reminder = self._reminder() if not finishes else ""
            if reminder:
                reply.append({"type": "text", "text": reminder})
            messages.append({"role": "user", "content": reply})
        return None, "LIMIT_EXHAUSTED"

    def _prompt(self, name: str) -> str:
        """The text of ``assistant.<name>`` this turn sends: the owner's edit on the
        dashboard (``service.prompt_texts``, a ``prompt_library.PromptStore``), else the
        packaged constant (``SYSTEM_PROMPT``, ``NUDGE``, ``LAST_ROUND``, ``OUT_OF_TIME``,
        ``SEARCH_BUDGET``)."""
        default = getattr(prompt, "SYSTEM_PROMPT" if name == "system" else name.upper())
        return prompt_library.resolve(getattr(self.service, "prompt_texts", None),
                                      f"assistant.{name}", default)

    def _reminder(self) -> str:
        if self.usage["model_requests"] >= self.max_requests - 1:
            return self._told("requests", self._prompt("last_round"))
        if self.usage["tool_executions"] >= self.max_tools:
            return self._told("searches", self._prompt("search_budget").replace(
                "{limit}", str(self.max_tools)))
        if self._remaining() < max(6.0, self.turn_seconds / 4):
            return self._told("time", self._prompt("out_of_time"))
        return ""

    def _told(self, limit: str, reminder: str) -> str:
        """``reminder``, which makes the model answer with what it has; the staff are told which
        limit cut the turn short (``diagnostics["limits"]``, dashboard > Live chats)."""
        limits = self.diagnostics.setdefault("limits", [])
        if limit not in limits:
            limits.append(limit)
        return reminder

    # -- the model ---------------------------------------------------------------------------

    def _client(self) -> Any:
        if self.service.client is not None:
            return self.service.client
        if not getattr(self.settings, "anthropic_api_key", ""):
            return None
        shared = getattr(self.service, "assistant_client", None)
        return shared() if callable(shared) else self.service._client()

    def _remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def _model_state(self) -> dict:
        states = getattr(self.service, "assistant_state", None)
        if not isinstance(states, dict):
            states = _MODEL_STATE
        return states.setdefault(self.model, {})

    def _create(self, client: Any, request: dict) -> Any:
        """One model request. What a model refuses (forced tool choice, the effort setting,
        strict tools, switching its thinking off) is remembered and it is asked again
        without it."""
        state = self._model_state()
        first = self.usage["model_requests"] == 0
        started = time.monotonic()
        while True:
            arguments, forced, quiet, effort = self._arguments(request, state, first)
            try:
                response, timing = self._send(client, arguments)
                break
            except anthropic.BadRequestError as exc:
                said = str(getattr(exc, "message", "") or exc).casefold()
                if not self._learn(said, state, forced, quiet, effort):
                    raise
        self.usage["model_requests"] += 1
        state["used_at"] = time.monotonic()
        counts = token_counts(getattr(response, "usage", None))
        for key, value in counts.items():
            self.usage[key] += value
        self.diagnostics["requests"].append({
            "ms": round((time.monotonic() - started) * 1000),
            "stop": getattr(response, "stop_reason", None),
            "tool_choice": "any" if forced else "auto", "effort": effort or None,
            "thinking": quiet or None, **timing, **counts})
        return response

    def _learn(self, said: str, state: dict, forced: bool, quiet: str, effort: str) -> bool:
        """What a refused request says this model will not take, remembered for every later
        request of the process, the cache warm-up included. True when it is worth asking
        again without it."""
        if forced and "tool_choice" in said:
            state["auto_only"] = True
        elif quiet and ("thinking" in said or "reasoning" in said):
            # First the lowest setting the model names, then its own default.
            # OpenRouter says "Reasoning is mandatory" where Anthropic says thinking.
            state["thinks" if quiet == "between_tools" else "no_disabled"] = True
        elif effort and ("output_config" in said or "effort" in said):
            state["no_effort"] = True
        elif ("strict" in said or "grammar" in said) and not state.get("no_strict"):
            # The executors check every argument anyway. OpenRouter's backend refuses the
            # strict tools as "the compiled grammar is too large ... strict tools".
            state["no_strict"] = True
        elif "ttl" in said and "cache" in said and self.cache_ttl == "1h" \
                and not state.get("no_ttl"):
            state["no_ttl"] = True  # Kept the default five minutes from now on.
        else:
            return False
        return True

    def _arguments(self, request: dict, state: dict,
                   first: bool) -> tuple[dict, bool, str, str]:
        """The request as this model takes it, and what was chosen: whether a tool call is
        forced, the thinking setting sent (if any) and the effort."""
        quiet = "" if self.thinking != "off" or state.get("thinks") else \
            "between_tools" if state.get("no_disabled") else "disabled"
        # Every response of a turn is a tool call. Forcing one also skips the model's
        # thinking, so it is forced on the opening request, which only chooses what to
        # search, and on every request when thinking is switched off.
        forced = (first or bool(quiet)) and not state.get("auto_only")
        tools = request["tools"] if not state.get("no_strict") else [
            {key: value for key, value in tool.items() if key != "strict"}
            for tool in request["tools"]]
        system = request["system"]
        if state.get("no_ttl"):  # The provider refused the longer lifetime once.
            tools, system = ([{**block, "cache_control": EPHEMERAL} if "cache_control" in block
                              else block for block in blocks] for blocks in (tools, system))
        arguments: dict = {
            "model": self.model, "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system, "tools": tools, "messages": request["messages"],
            "tool_choice": {"type": "any" if forced else "auto"},
        }
        if quiet:
            arguments["thinking"] = {"type": quiet}
        effort = self.effort if not state.get("no_effort") else ""
        if effort:
            arguments["extra_body"] = {"output_config": {"effort": effort}}
        return arguments, forced, quiet, effort

    def warm(self, index: Any) -> bool:
        """Ask the provider to read the instructions, the guide and the tools into its
        cache without writing anything. Skipped while they were read within the cache's
        lifetime. Best effort: a failure costs the first message a few seconds, no more."""
        state = self._model_state()
        # The provider keeps the prompt for the cache's lifetime: warm again only near its end.
        lifetime = WARM_SECONDS_1H if self.cache_ttl == "1h" and not state.get("no_ttl") \
            else WARM_SECONDS
        if time.monotonic() - state.get("used_at", -lifetime) < lifetime:
            return False
        client = self._client()
        if client is None:
            return False
        state["used_at"] = time.monotonic()
        # A refusal teaches what this model will not take (strict tools on OpenRouter, a
        # thinking setting), exactly as a turn learns it: the warm-up is then asked again,
        # and it caches the very tools and settings the turns send.
        for _ in range(WARM_ATTEMPTS):
            arguments, _, quiet, effort = self._arguments({
                "system": prompt.system_blocks(self.redact(str(index.guide() or "")),
                                               self.cache_ttl, self._prompt("system")),
                "tools": tool_definitions(self.cache_ttl),
                "messages": [{"role": "user", "content": "warm"}]}, state, False)
            # Nothing is written, so nothing can be forced; the settings that are part of
            # the cached prompt (thinking, effort) stay as the turns send them.
            arguments.update(max_tokens=0, tool_choice={"type": "auto"})
            state.pop("warm_usage", None)
            try:
                response = client.messages.create(**arguments, timeout=WARM_TIMEOUT)
            except anthropic.BadRequestError as exc:
                said = str(getattr(exc, "message", "") or exc)
                if self._learn(said.casefold(), state, False, quiet, effort):
                    continue
                LOGGER.info("assistant_warm_skipped %s status=%s said=%s", type(exc).__name__,
                            exc.status_code, self.redact(" ".join(said.split()))[:200])
                return False
            except (anthropic.APIError, RuntimeError, OSError) as exc:
                LOGGER.info("assistant_warm_skipped %s status=%s", type(exc).__name__,
                            getattr(exc, "status_code", None))
                return False
            state["warm_usage"] = token_counts(getattr(response, "usage", None))
            return True
        return False

    def _send(self, client: Any, arguments: dict) -> tuple[Any, dict]:
        """A kept connection the server closed while idle fails at once, and so do an
        overloaded reply, a reply that stalls while it is streamed and one that breaks
        off with an error after it began: one fresh attempt per turn. A reply that was
        waited for in one piece and never came is not asked for again: its time is gone.
        A transient rate limit gets one retry when its requested wait fits this turn.
        Other refused requests, including spend caps, are not repeated. The retried request
        goes through the same checks, so its own failure is recorded and never retried."""
        while True:
            try:
                return self._request(client, arguments)
            except anthropic.RateLimitError as exc:
                if out_of_credit(exc) or self.retried:
                    self._retry_record(429, "rate_limit", None, exhausted=True)
                    raise
                delay = self._rate_limit_wait(exc)
                if delay is None:
                    self._retry_record(429, "rate_limit", None, exhausted=True)
                    raise
                if not self._retry_sleep(delay, "rate_limit", 429):
                    raise
            except anthropic.APIError as exc:
                # An error event inside a stream arrives on a response that began with 200.
                broke_off = isinstance(exc, anthropic.APIStatusError) and exc.status_code < 400
                if not (broke_off or isinstance(
                        exc, anthropic.APIConnectionError | anthropic.InternalServerError)):
                    raise
                waited = isinstance(exc, anthropic.APITimeoutError) and not callable(
                    getattr(client.messages, "stream", None))
                status = exc.status_code if isinstance(exc, anthropic.APIStatusError) else None
                if waited:
                    raise
                if self.retried:
                    self._retry_record(status, "connection_or_server", None, exhausted=True)
                    raise
                if self._remaining() < MIN_REQUEST_SECONDS + 1:
                    self._retry_record(status, "connection_or_server", 0.4, exhausted=True)
                    raise
                if not self._retry_sleep(0.4, "connection_or_server", status):
                    raise

    @staticmethod
    def _rate_limit_wait(exc: anthropic.RateLimitError) -> float | None:
        """The provider's Retry-After delay, or one second when it sent no header.

        An invalid header is not guessed at. HTTP-date values use UTC, as required by
        the HTTP date format. The response and its message never enter diagnostics.
        """
        import math
        from datetime import UTC, datetime
        from email.utils import parsedate_to_datetime

        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        raw = headers.get("retry-after") if headers is not None else None
        if raw is None:
            return 1.0
        value = str(raw).strip()
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = (retry_at - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        if not math.isfinite(delay) or delay < 0:
            return None
        return delay

    def _retry_record(self, status: int | None, cause: str, wait_seconds: float | None,
                      *, exhausted: bool) -> None:
        self.diagnostics.setdefault("retries", []).append({
            "status": status, "cause": cause,
            "wait_seconds": round(wait_seconds, 3) if wait_seconds is not None else None,
            "exhausted": exhausted,
        })

    def _retry_sleep(self, delay: float, cause: str, status: int | None) -> bool:
        """Wait the full delay interruptibly, leaving time for another provider request."""
        if self.cancelled():
            self._retry_record(status, cause, delay, exhausted=True)
            raise RuntimeError("The turn was cancelled before a retry.")
        if delay + MIN_REQUEST_SECONDS > self._remaining():
            self._retry_record(status, cause, delay, exhausted=True)
            return False
        self.retried = True
        deadline = time.monotonic() + delay
        while True:
            if self.cancelled():
                self._retry_record(status, cause, delay, exhausted=True)
                raise RuntimeError("The turn was cancelled before a retry.")
            if self._remaining() < MIN_REQUEST_SECONDS:
                self._retry_record(status, cause, delay, exhausted=True)
                raise TimeoutError("The turn has no time left for a retry.")
            left = deadline - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, 0.1))
        if self.cancelled():
            self._retry_record(status, cause, delay, exhausted=True)
            raise RuntimeError("The turn was cancelled before a retry.")
        if self._remaining() < MIN_REQUEST_SECONDS:
            self._retry_record(status, cause, delay, exhausted=True)
            raise TimeoutError("The turn has no time left for a retry.")
        self._retry_record(status, cause, delay, exhausted=False)
        return True

    def _request(self, client: Any, arguments: dict) -> tuple[Any, dict]:
        """The response and when each of its blocks began. A client that can stream is read
        as the reply is written: a slow reply then has the whole turn's time, a stalled one
        is noticed after a few seconds, and the turn's own deadline ends the wait."""
        budget = max(1.0, self._remaining())
        stream = getattr(client.messages, "stream", None)
        if not callable(stream):
            return client.messages.create(**arguments, timeout=budget), {}
        started = time.monotonic()
        timing: dict = {"blocks": []}
        limits = anthropic.Timeout(budget, read=min(budget, STALL_SECONDS),
                                   connect=min(budget, CONNECT_SECONDS))
        with stream(**arguments, timeout=limits) as events:
            for event in events:
                if getattr(event, "type", "") == "content_block_start":
                    block = event.content_block
                    at = round((time.monotonic() - started) * 1000)
                    timing.setdefault("first_ms", at)
                    timing["blocks"].append(
                        [getattr(block, "name", None) or getattr(block, "type", ""), at])
                if self.cancelled():
                    raise RuntimeError("The turn was cancelled while the reply was written.")
                if self._remaining() <= 0:
                    raise TimeoutError("The turn ran out of time while the reply was written.")
            return events.get_final_message(), timing

    # -- read tools ----------------------------------------------------------------------------

    def _read(self, calls: list[dict]) -> dict[str, dict]:
        """Run the read tools of one response side by side; every call gets its result."""
        allowed = max(0, self.max_tools - self.usage["tool_executions"])
        run, refused = calls[:allowed], calls[allowed:]
        entries = [{"stage": str(call.get("name")), "status": "RUNNING",
                    "label": self.toolbox.label(str(call.get("name")), call.get("input"))}
                   for call in run]
        self.entries.extend(entries)
        self._publish()
        results: dict[str, dict] = {}
        for call, entry, (value, elapsed) in zip(run, entries, self._execute(run), strict=True):
            self.usage["tool_executions"] += 1
            failed = isinstance(value, ToolError)
            payload, rows = (value.payload(), 0) if failed else self.toolbox.render(value)
            if not failed:
                self.seen.update(dict.fromkeys(self.toolbox.rows_in(value)))
            if not failed and call.get("name") == "oil_guide":
                self.toolbox.oil_guide_result = payload
                needs = payload.get("needs") or {}
                self.requirements = {key: needs[key] for key in (
                    "grades", "capacity_l", "engine_code", "sources") if key in needs}
            entry["status"] = "ERROR" if failed else "OK"
            results[call["id"]] = self._result(call["id"], payload, failed)
            record = {"name": call.get("name"), "args": call.get("input"), "rows": rows,
                      "ms": elapsed, "label": entry["label"]}
            if failed:
                record["error"] = value.error
            try:  # What the staff are told this search found; it never fails a turn.
                record["found"] = search_facts(str(call.get("name")), call.get("input"),
                                               None if failed else payload)
            except Exception as error:
                LOGGER.warning("assistant_search_facts_failed %s", type(error).__name__)
            self.diagnostics["tools"].append(record)
            self.results.append({"result_id": f"t{len(self.results) + 1}",
                                 "tool": call.get("name"),
                                 "data": {"status": entry["status"], "rows": rows}})
        for call in refused:
            results[call["id"]] = self._result(call["id"], ToolError(
                f"Search budget used ({self.max_tools} of {self.max_tools}); this call was "
                "not run.", "Call finish now with what you have.").payload(), True)
        if refused:  # searches the model wanted and the budget cut off (dashboard > Live chats)
            self.diagnostics["refused"] = self.diagnostics.get("refused", 0) + len(refused)
        self._publish()
        return results

    def _execute(self, calls: list[dict]) -> list[tuple[Any, int]]:
        if not calls:
            return []
        pool = ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="assistant-tool")
        try:
            futures = [pool.submit(self._one, str(call.get("name")), call.get("input"))
                       for call in calls]
            wait(futures, timeout=max(0.5, self._remaining() - 1.0))
            late = (ToolError("The tool did not answer in time.",
                              "Go on with the results you have."),
                    round(self.turn_seconds * 1000))
            return [future.result() if future.done() else late for future in futures]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _one(self, name: str, args: Any) -> tuple[Any, int]:
        started = time.monotonic()
        try:
            value: Any = self.toolbox.run(name, args)
        except ToolError as exc:
            value = exc
        except (psycopg.Error, RuntimeError, OSError):
            value = ToolError("The data behind this tool could not be read right now.",
                              "Go on with the results you have.")
        except Exception as exc:  # A tool fault is the model's to work around, not the turn's.
            LOGGER.warning("assistant_tool_failed %s %s", name, type(exc).__name__)
            value = ToolError("The tool failed on these arguments.",
                              "Check them against the tool's schema, or go on with the "
                              "results you have.")
        return value, round((time.monotonic() - started) * 1000)

    def _result(self, identifier: str, payload: dict, failed: bool) -> dict:
        content = self.redact(_dump(payload))
        rows = payload.get("rows")
        while len(content) > RESULT_LIMIT and isinstance(rows, list) and len(rows) > 5:
            rows = rows[:len(rows) * 2 // 3]
            payload = {**payload, "rows": rows,
                       "shown_rows": f"Only the first {len(rows)} rows are shown; narrow "
                                     "the terms to see the rest."}
            content = self.redact(_dump(payload))
        if len(content) > RESULT_LIMIT:
            failed, content = True, _dump(ToolError(
                "The result was too large to return.",
                "Ask for fewer numbers or narrower terms.").payload())
        result = {"type": "tool_result", "tool_use_id": identifier, "content": content}
        if failed:
            result["is_error"] = True
        return result

    def _publish(self) -> None:
        self.progress({"progress": [dict(entry) for entry in self.entries]})

    # -- what the model is given before it starts ------------------------------------------

    def _transcript(self) -> list[dict]:
        """The conversation so far as plain text turns. Model output of earlier turns is
        never replayed as content blocks."""
        messages = []
        for turn in self.memory.get("transcript") or []:
            said = _text(turn.get("user")) if isinstance(turn, dict) else ""
            if said:
                messages.append({"role": "user", "content": said})
                messages.append({"role": "assistant",
                                 "content": _text(turn.get("assistant")) or "(no reply)"})
        return messages

    def _clear_fitment(self) -> None:
        for key in ("notes", "car_label", "shown", "held", "seen", "sourced", "handles",
                    "vin_key", "vin_facts", "transcript", "engine_choices", "question",
                    "suggestions", "oil_question"):
            self.memory.pop(key, None)
        self.question = self.choice = None

    def _followup_state(self, previous: dict, stated: dict, saved: dict, reset: bool) -> dict:
        engine_asked = bool(self.question and followup.engine_question(self.question["text"]))
        engine = followup.stated_engine(self.message, stated or previous,
                                        answering_engine=engine_asked and not reset)
        choices = self.memory.get("engine_choices")
        if engine_asked and not reset and isinstance(choices, dict):
            options = self.question["options"]
            label = (options[self.choice - 1]["label"]
                     if self.choice and 0 < self.choice <= len(options) else self.message.strip())
            chosen = _text(choices.get(label), 160)
            if chosen:
                engine.setdefault("trim", chosen)
        engine_changed = followup.conflicts(previous, {**stated, **engine}, followup.DETAILS)
        if reset or engine_changed:
            self._clear_fitment()
        vehicle = {} if reset else dict(previous)
        if engine_changed:
            for key in followup.DETAILS:
                vehicle.pop(key, None)
        vehicle.update(stated)
        vehicle.update(engine)
        grade = followup.oil_grade(self.message)
        context = {} if reset or engine_changed else {
            key: saved[key] for key in ("oil_grade", "oil_litres", "oil_product", "oil_spec")
            if key in saved}
        pending = self.memory.get("oil_question")
        if pending == "year" and re.fullmatch(r"(?:19[89]|20)\d{2}", self.message.strip()):
            vehicle["model_year"] = int(self.message)
        if pending == "dpf":
            answer = self.message.strip().casefold()
            if answer in {"with dpf", "dpf აქვს", "с dpf", "yes", "კი", "да"}:
                vehicle["dpf"] = True
            elif answer in {"no dpf", "dpf არ აქვს", "без dpf", "no", "არა", "нет"}:
                vehicle["dpf"] = False
        if pending == "spec" and (spec := oil_intake.stated_spec(self.message)):
            context["oil_spec"] = spec
        if pending == "market" and (market := oil_intake.stated_market(self.message)):
            vehicle["market"] = market
        if grade and grade != context.get("oil_grade"):
            context.pop("oil_product", None)
            context.pop("oil_litres", None)
            context["oil_grade"] = grade
        if litres := followup.oil_litres(self.message, grade):
            context["oil_litres"] = litres
        context["vehicle"] = vehicle
        return context

    def _context(self) -> dict:
        # Messages a newer one overtook before they were answered: the store keeps them
        # in its history with an empty answer.
        unanswered: list[str] = []
        for entry in reversed(self.history):
            if _text(entry.get("answer")):
                break
            if _text(entry.get("question")):
                unanswered.append(_text(entry.get("question"), 500))
        self.unanswered = unanswered[::-1]
        tapped = None
        options = self.question["options"] if self.question else []
        if self.choice is not None and 1 <= self.choice <= len(options):
            tapped = {"index": self.choice, **{key: value for key, value in options[
                self.choice - 1].items() if value}}
        vin = self._vin_facts()
        self.toolbox.decoded_vehicle = self.facts
        self.toolbox.stated_vehicle = self.form
        vehicle = self.followup["vehicle"]
        if self.facts and self.vin_typed:
            vehicle = self.followup["vehicle"] = {**vehicle, **self.facts}
        self.toolbox.followup_vehicle = vehicle
        self.toolbox.customer_message = self.message
        self.toolbox.customer_vehicle_text = "\n".join([
            self.message,
            *(_text(turn.get("user")) for turn in self.memory.get("transcript") or []
              if isinstance(turn, dict)),
        ])
        # What the staff page's form holds, the fields its VIN already settles left out.
        staff_form: dict = {}
        if self.audience != "public":
            car = {key: value for key, value in self.form.items()
                   if _fold(self.facts.get(key)) != _fold(value)}
            if car:
                staff_form["car"] = car
            if vin is not None and not self.vin_typed:
                staff_form["vin_field"], vin = vin, None
        known = [*(self.memory.get("shown") or []), *(self.memory.get("held") or [])]
        focus = dict(self.followup)
        product_id = focus.pop("oil_product", None)
        if (product_id and followup.refers_to_oil(self.message)
                and self.toolbox.index.row(product_id) is not None):
            known.insert(0, product_id)
            focus["previous_oil"] = self.toolbox.handle(product_id)
        mentioned = [self.memory.get("notes"), *(
            option.get("meaning") for option in [*options, *self._suggestions()])]
        known.extend(self.toolbox.handles.get(handle) for text in mentioned
                     for handle in handles_in(text))
        # After a question of the model's: what its searches returned, so that the answer
        # is judged on every candidate and not only on the rows its notes happened to name.
        known.extend(self.memory.get("seen") or [])
        return prompt.turn_context(
            message=self.message, audience=self.audience, language=self.replied_in,
            sources=self.toolbox.sources(), reply_language=self.script,
            limits={"model_requests": self.max_requests, "tool_calls": self.max_tools},
            unanswered=self.unanswered, notes=_text(self.memory.get("notes"), NOTES_LIMIT),
            followup={key: value for key, value in focus.items() if value},
            car_label=_text(self.memory.get("car_label"), 80),
            question={"text": self.question["text"], "options": _brief(options)}
            if self.question else None,
            tapped=tapped, suggestions=_brief(self._suggestions()), vin=vin,
            staff_form=staff_form, phone=_text(self.memory.get("phone"), 10),
            typed_numbers=self.toolbox.typed_numbers(
                typed_tokens(self.message),
                codes_in_text(self.message.replace("[VIN]", " "), limit=8)),
            earlier_typed_numbers=self._earlier_numbers(),
            stock_note=self._stock_note(),
            shop_closed=self._shop_closed(),
            rows=self.toolbox.table(
                [product for product in dict.fromkeys(known) if product][:KNOWN_ROWS]))

    def _shop_closed(self) -> bool:
        """Whether a customer writes while the shop's staff are off work (``shop_hours``): only
        a customer is told, and a turn while the shop is open carries nothing new. Read once per
        turn, so the turn's context and the phone state it leaves agree."""
        if getattr(self, "closed_now", None) is None:
            self.closed_now = self.audience == "public" and not shop_hours.is_open()
        return self.closed_now

    def _snapshot_time(self) -> Any:
        """When the sync last looked at the catalogue snapshot the search reads."""
        store = getattr(self.service, "catalog_store", None)
        current = getattr(store, "current", None)
        return getattr(current(), "observed_at", None) if callable(current) else None

    def _stock_note(self) -> str:
        """The stock figures' age, for the model, once they are older than the rules allow
        to call them current: the rows still show the last known quantities, and the cards
        then ask the customer to check with the shop. Nothing while they are current."""
        age = stock.age_hours(self._snapshot_time())
        if age is None or age <= self.rules.stale_hours:
            return ""
        return (f"stock figures last checked {int(age)} hours ago; they may have changed "
                "since, so availability needs the shop's confirmation")

    def _earlier_numbers(self) -> dict | None:
        """The rows of part numbers the customer typed in earlier messages of this chat: in a
        chat the shop's staff answered by hand, no turn of the engine looked them up."""
        earlier = " ".join(str(turn.get("user") or "") for turn in
                           (self.memory.get("transcript") or [])[-TRANSCRIPT_TURNS:]
                           if isinstance(turn, dict)).replace("[VIN]", " ")
        now = set(typed_tokens(self.message))
        tokens = [token for token in typed_tokens(earlier) if token not in now]
        return self.toolbox.typed_numbers(tokens, [], quiet=True) if tokens else None

    def _suggestions(self) -> list[dict]:
        return [option for option in _options(self.memory.get("suggestions"))
                if option["label"]][:MAX_SUGGESTIONS]

    def _vin_key(self) -> str:
        key = str(getattr(self.settings, "vin_hmac_key", "") or "").encode() or _PROCESS_KEY
        return hmac.new(key, f"assistant:{self.vin}".encode(), hashlib.sha256).hexdigest()[:16]

    def _vin_facts(self) -> dict | None:
        """What the decoder says about the customer's VIN. The VIN itself stays here."""
        if not self.vin:
            return None
        key = self._vin_key()
        if self.memory.get("vin_key") != key:
            # Another car: numbers that outside sources gave for the last one do not carry over.
            self.memory.update(vin_key=key, vin_facts=None, sourced=[])
        facts = self.memory.get("vin_facts")
        if not (isinstance(facts, dict) and facts.get("make")):
            try:
                decoded = self.service.vin_service.decode(
                    self.vin, deadline=min(self.deadline, time.monotonic() + VIN_SECONDS))
                vehicle = decoded.get("vehicle") if isinstance(decoded, dict) else None
                facts = {key: value for key, value in (vehicle or {}).items()
                         if key in VIN_FACTS and value not in (None, "")}
            except (ValueError, RuntimeError, OSError, psycopg.Error, AttributeError, TypeError):
                facts = {}
        self.diagnostics["vin"] = "decoded" if facts.get("make") else "not_decoded"
        if not facts.get("make"):
            self.warnings.append("VIN_UNAVAILABLE")
            holder = "The customer sent a VIN" if self.vin_typed else \
                "The staff form holds a VIN"
            return {"decoded": False,
                    "note": f"{holder} but it could not be decoded right now. "
                            "Ask for make, model and year, or go on with what they said."}
        self.memory["vin_facts"] = self.facts = facts
        return {"decoded": True, "facts": facts}

    # -- checking finish ---------------------------------------------------------------------

    def _sourced(self, car_label: str | None = None) -> set[str]:
        """Part numbers that car-specific sources returned in this conversation. Numbers
        gathered while the model named another car do not count for this one."""
        found = set(self.toolbox.sourced) if self.toolbox else set()
        before = plain(self.memory.get("car_label"))
        if car_label is None or not before or not plain(car_label) \
                or plain(car_label) == before:
            found.update(self.memory.get("sourced") or [])
        return found

    def _current(self, product_id: str) -> Any:
        """The product re-read from the database: a dict, None when it is no longer
        active, or ``_UNREAD`` when the database could not say."""
        if product_id not in self._products:
            try:
                found = self.repository.get_product(product_id)
            except (RuntimeError, OSError, psycopg.Error, AttributeError):
                found = _UNREAD
            if found is not None and not (
                    isinstance(found, dict)
                    and str(found.get("source_product_id")) == str(product_id)):
                found = _UNREAD
            self._products[product_id] = found
        return self._products[product_id]

    def _verified_oil(self, product_id: str, current: Any) -> bool:
        proof = self.toolbox.verified_oils.get(product_id) if self.toolbox else None
        return bool(proof and isinstance(current, dict) and current.get(proof[0]) == proof[1])

    def _refresh(self, product_ids: list[str]) -> None:
        missing = [product for product in dict.fromkeys(product_ids)
                   if product not in self._products]
        if len(missing) < 2:
            return
        pool = ThreadPoolExecutor(max_workers=min(6, len(missing)),
                                  thread_name_prefix="assistant-stock")
        try:
            wait([pool.submit(self._current, product) for product in missing],
                 timeout=max(0.5, self._remaining()))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _barred(self, row: Any, car_label: str) -> str | None:
        """A staff veto in rules.toml for this row and this car. The car is what the VIN
        and the staff form state, unless the model's own name for the car shares no word
        with their make and model: then a message named another car than the form holds,
        and the veto is looked for in that name alone."""
        current = self._current(str(row.id))
        if oil_grade_conflict(row.name, row.code) or (isinstance(current, dict) and
                oil_grade_conflict(current.get("product_name") or "",
                                   current.get("original_code") or "")):
            return OIL_GRADE_CONFLICT
        stated = {**self.form, **self.followup["vehicle"], **self.facts}
        named = set(WORD.findall(_fold(car_label)))
        known = {word for key in ("make", "model")
                 for word in WORD.findall(_fold(stated.get(key))) if len(word) >= 3}
        if not named or not known or named & known:
            reason = self.rules.never_reason({"product_code": row.shop_code}, stated)
            if reason:
                return reason
        # The rule's make and model are looked for in the name the model gave the car.
        label = plain(car_label)
        for rule in self.rules.never_offer:
            if plain(rule.product_code) != plain(row.shop_code) or not label:
                continue
            make = plain(rule.make)
            names = {make, *(name.upper() for group in self.rules.brand_groups
                             if make.casefold() in group for name in group)}
            if (any(len(name) >= 3 and name in label for name in names)
                    and plain(rule.model) in label):
                return rule.reason or "never_offer"
        return None

    def _check(self, raw: Any) -> tuple[list[dict], dict | None]:
        """Every failed check, each with what to do; or the decision when there is none."""
        errors: list[dict] = []

        def bad(error: str, fix: str) -> None:
            if all(known["error"] != error for known in errors):
                errors.append({"error": error, "fix": fix})

        if not isinstance(raw, dict):
            bad("finish needs an object with every field of its schema.",
                "Send language, car_label, products, question, options, suggestions, "
                "reply, notes, outcome and staff.")
            return errors, None
        reply = _text(raw.get("reply"))
        question, options = _asked(raw)
        engine_choices = {}
        if (question and followup.engine_question(question) and raw.get("language") in LANGUAGES
                and self.toolbox.engine_variants):
            choices = followup.engine_options(self.toolbox.engine_variants, raw["language"])
            if choices:
                question, options = followup.ENGINE_QUESTION[raw["language"]], choices
                engine_choices = {item["label"]: item["meaning"] for item in choices}
        suggestions = [option for option in _options(raw.get("suggestions")) if option["label"]]
        chosen = raw.get("products") if isinstance(raw.get("products"), list) else []
        named = [item for item in chosen if isinstance(item, dict)]
        if raw.get("language") not in LANGUAGES:
            bad("language must be ka, en or ru.", "Name the language the reply is written in.")
        elif self.script:
            # The customer wrote in Georgian or Russian letters: so does the reply.
            words = " ".join([reply, question, *(option["label"] for option in options),
                              *(option["label"] for option in suggestions)])
            counts = _letters(words)
            if raw["language"] != self.script or (
                    sum(counts.values()) and counts[self.script] < 0.2 * sum(counts.values())):
                name = {"ka": "Georgian", "ru": "Russian"}[self.script]
                bad(f"The customer wrote in {name}, but the reply is not in {name}.",
                    f"Write everything the customer reads in {name}: language "
                    f"{self.script}, the reply, the question, the options, the suggestions, "
                    "the reasons and the groups.")
        if raw.get("outcome") not in OUTCOMES:
            bad("outcome must be one of " + ", ".join(OUTCOMES) + ".", "Pick the one that "
                "describes this reply.")
        if not reply and not question:
            bad("reply is empty.", "Write one to three short sentences for the customer.")
        if len(reply) > REPLY_LIMIT:
            bad(f"reply has {len(reply)} characters; the limit is {REPLY_LIMIT}.",
                "Shorten it to one to three short sentences. The cards show the products.")
        priced = PRICE.search(" ".join([reply, question]))
        if priced:
            bad(f"The reply states an amount of money (\"{priced.group().strip()}\").",
                "Do not state prices; the cards show them.")
        if ("?" in reply or "？" in reply) and not question:
            bad("reply asks a question, but `question` is empty.",
                "Move the question into `question` and give 2 to 8 options. The reply "
                "text itself has no question mark.")
        if raw.get("outcome") == "asked" and not question:
            bad("outcome is asked, but `question` is empty.",
                "Put what you ask into `question` with 2 to 8 options, or choose the "
                "outcome that describes this reply.")
        if question and not 2 <= len(options) <= MAX_OPTIONS:
            bad(f"question has {len(options)} option(s); it needs 2 to {MAX_OPTIONS}.",
                "Keep the question and put the likely answers into `options` as button "
                "texts, with one for not being sure.")
        if options and not question:
            bad("question has options but no text.",
                "Write the question in `question`, or send options [].")
        labels = [_fold(option["label"]) for option in options]
        for option in options:
            label = option["label"]
            if not label:
                bad("An option has an empty label.", "Give every option a button text.")
            elif len(label) > LABEL_LIMIT:
                bad(f"Option label \"{label}\" has {len(label)} characters; the limit is "
                    f"{LABEL_LIMIT}.", "Shorten it to at most four words.")
            elif labels.count(_fold(label)) > 1:
                bad(f"More than one option is labelled \"{label}\".",
                    "Give every option a different label.")
        for option in suggestions[:MAX_SUGGESTIONS]:
            if len(option["label"]) > LABEL_LIMIT:
                bad(f"Suggestion label \"{option['label']}\" has {len(option['label'])} "
                    f"characters; the limit is {LABEL_LIMIT}.", "Shorten it.")
        leaked = self._handles(reply, question, *(option["label"] for option in options),
                               *(option["label"] for option in suggestions),
                               *(item.get(key) for item in named for key in ("reason", "group")))
        if leaked:
            bad(f"What the customer reads names the row handle{'s' * (len(leaked) > 1)} "
                f"{', '.join(leaked)}.",
                "Handles are private to you: they belong in `ids` and `notes` only. Say what "
                "the row is in its own words (the brand, the part, the side), or leave it out.")
        car_label = _text(raw.get("car_label"), 80)
        products, removed = self._products_of(named, car_label, bad)
        held = [item for item in products if item["held"]]
        products = [item for item in products if not item["held"]]
        if held and not question:
            open_facts = "; ".join(dict.fromkeys(item["held"] for item in held))
            bad(f"Rows are marked depends ({open_facts}), but the reply asks nothing.",
                "Ask for that fact in `question` with the values as options, or, if the "
                "conversation settles it, mark the rows that fit as listed or check.")
        if not named and not question and not suggestions:
            bad("finish has no products, no question and no suggestions.",
                "Give at least one: rows to show, a question with options, or next-step "
                "suggestions.")
        elif named and not products and not held and not errors and not question \
                and not suggestions:
            bad("Every row you chose is barred for this car by a staff rule ("
                + "; ".join(f"{item['handle']}: {item['reason']}" for item in removed) + ").",
                "Choose other rows, or say that nothing suitable was found and give "
                "suggestions.")
        staff = self._staff(raw.get("staff"), bad)
        if errors:
            return errors, None
        return [], {
            "reply": reply, "language": raw["language"], "car_label": car_label,
            "products": products, "removed": removed,
            "held": [item["product"] for item in held],
            "question": {"text": question, "options": options} if question else None,
            "suggestions": suggestions[:MAX_SUGGESTIONS],
            "notes": _text(raw.get("notes"), NOTES_LIMIT), "outcome": raw["outcome"],
            "staff": staff, "oil_quote": raw.get("oil_quote"), "engine_choices": engine_choices}

    def _staff(self, value: Any, bad: Any) -> dict:
        """``finish.staff``: what the shop's staff are told about this turn (dashboard > Live
        chats). The model decides what the customer means: that they want to come in (visit),
        want work done (service), which parts it could not offer, which of their words is a phone
        number. Code only checks the shape and that the phone stands in the customer's own
        messages (``live_alerts.clean_phone``), and calls nobody for the staff chat: only a
        visitor's conversation (audience public) reaches the staff this way. An absent object
        (an older caller) means nothing for the staff."""
        none = dict(NO_STAFF)
        if value is None:
            return none
        if not isinstance(value, dict):
            bad("staff must be an object with intent, note, phone and missing.",
                "Send staff as {intent, note, phone, missing}: intent none and the rest empty "
                "when there is nothing for the staff.")
            return none
        intent = value.get("intent")
        if intent not in STAFF_INTENTS:
            bad("staff.intent must be one of " + ", ".join(STAFF_INTENTS) + ".",
                "Say visit when the customer wants to come to the shop, service when they want "
                "work done, otherwise none.")
            return none
        if self.audience != "public":
            return none
        said = [self.message, *self.unanswered, *(
            turn.get("user") for turn in self.memory.get("transcript") or []
            if isinstance(turn, dict))]
        missing = value.get("missing")
        names = [_text(name, STAFF_WORD_LIMIT)
                 for name in (missing if isinstance(missing, list) else [])]
        phone = clean_phone(value.get("phone"), said)
        # A number the model offered that is not in the customer's words is dropped. The next
        # turn is then not told that one was asked for or given, so it may ask again.
        self.phone_rejected = bool(_text(value.get("phone"), 40)) and not phone
        return {"intent": intent, "note": _text(value.get("note"), STAFF_NOTE_LIMIT),
                "phone": phone, "missing": [name for name in names if name][:STAFF_MISSING_LIMIT]}

    def _products_of(self, named: list[dict], car_label: str,
                     bad: Any) -> tuple[list[dict], list[dict]]:
        """The rows the model chose, each checked against the catalogue. An entry names
        one or more rows that share its confidence, evidence, reason and group. ``bad``
        records a failed check; a row a staff rule bars is taken out without one."""
        toolbox = self.toolbox
        pairs = [(item, handle) for item in named for handle in _named(item)][:MAX_PRODUCTS]
        for item in named:
            if not any(_named(item)):
                bad("A products entry names no rows.",
                    "Put the handles of its rows into `ids`, or remove the entry.")
        known = [toolbox.handles.get(handle) for _, handle in pairs]
        self._refresh([product for product in known if product])
        sourced = self._sourced(car_label)
        products: list[dict] = []
        removed: list[dict] = []
        seen: set[str] = set()
        for (item, handle), product_id in zip(pairs, known, strict=True):
            if product_id is None:
                if handle or len(_named(item)) > 1:
                    bad(f"finish names {handle or 'an empty id'}, which you were never "
                        "shown.",
                        "Use only handles from the id column of results in this "
                        "conversation.")
                continue
            if product_id in seen:
                continue
            seen.add(product_id)
            row, current = toolbox.index.row(product_id), self._current(product_id)
            if row is None or current is None:
                bad(f"{handle} is no longer in the catalogue.",
                    "Remove it or choose another row.")
                continue
            confidence = item.get("confidence")
            if confidence not in JUDGEMENTS:
                bad(f"{handle} has confidence {confidence!r}.",
                    "Use confirmed, listed, spec, check or depends.")
            evidence = _text(item.get("evidence"), 300).strip("\"'`„“”«»")
            numbers = " ".join([str(row.code), *map(str, row.cross_codes or ())])
            fields = [row.name, row.vehicle, row.brand, numbers,
                      f"{row.name} {row.vehicle} {row.brand} {numbers}"]
            if self.audience == "staff":
                fields.append(str(row.shop_code))
            if not evidence:
                bad(f"{handle} has no evidence.",
                    "Quote one fragment of that row (name, vehicle text, brand or a number) "
                    "that supports offering it.")
            elif not any(_bare(evidence) in _bare(value) for value in fields) and not (
                    len(plain(evidence)) >= 4 and plain(evidence) in row_numbers(row)):
                bad(f"The evidence for {handle} (\"{evidence[:80]}\") is not text of that "
                    "row.",
                    f"Quote a fragment that every row of the entry contains, or give "
                    f"{handle} an entry of its own: name \"{row.name}\", vehicle "
                    f"\"{row.vehicle}\", brand \"{row.brand}\", numbers {numbers}.")
            if (confidence == "confirmed" and not toolbox.confirmed_part(row, sourced)
                    and not self._verified_oil(product_id, current)):
                bad(f"{handle} is marked confirmed, but no supplier_parts, oil_guide or "
                    "crawled catalogue result of this conversation holds one of its numbers.",
                    "Use listed when the row's own text names this car and year, otherwise "
                    "check.")
            reason = _text(item.get("reason"), 300)
            if PRICE.search(reason):
                bad(f"The reason for {handle} states an amount of money.",
                    "Do not state prices; the cards show them.")
            if confidence in REASONED and not reason:
                bad(f"{handle} is marked {confidence} without a reason.",
                    "Say in `reason`, in a few words of the customer's language, "
                    + ("which specification of the row is the one this car needs or the "
                       "customer asked for (the grade, the approval, the size)."
                       if confidence == "spec" else "what the shop must verify."))
            veto = self._barred(row, car_label)
            if veto:
                removed.append({"handle": handle, "product": product_id, "reason": veto})
                continue
            products.append({"handle": handle, "product": product_id, "row": row,
                             "confidence": confidence, "evidence": evidence, "reason": reason,
                             "group": _text(item.get("group"), 60),
                             "held": (reason or "an open fact")[:120]
                             if confidence == DEPENDS else ""})
        return products, removed

    def _handles(self, *texts: Any) -> list[str]:
        """The row handles of this conversation that the given texts name. A handle is the
        model's own name for a row and means nothing to a customer."""
        known = self.toolbox.handles if self.toolbox else {}
        return list(dict.fromkeys(handle for text in texts if isinstance(text, str)
                                  for handle in handles_in(text) if handle in known))

    def _salvage(self) -> dict | None:
        """The usable part of an answer that did not pass: rows that exist (at the most
        cautious confidence when theirs does not hold), buttons that are well formed and
        the sentences that state no price. None when nothing is left to say."""
        raw = self.last_finish if isinstance(self.last_finish, dict) else {}
        reply = _text(raw.get("reply")) or self.last_text
        kept = ""
        for sentence in _sentences(reply):
            if PRICE.search(sentence) or self._handles(sentence) \
                    or len(kept) + len(sentence) + 1 > REPLY_LIMIT:
                continue
            kept = f"{kept} {sentence}".strip()
        question, written = _asked(raw)
        question = question[:400]
        options, seen = [], set()
        for option in written:
            label = option["label"][:LABEL_LIMIT].strip()
            if label and _fold(label) not in seen and len(options) < MAX_OPTIONS \
                    and not self._handles(label):
                seen.add(_fold(label))
                options.append({"label": label, "meaning": option["meaning"] or label})
        if PRICE.search(question) or self._handles(question):
            question = ""
        suggestions = [{"label": option["label"][:LABEL_LIMIT].strip(),
                        "meaning": option["meaning"]}
                       for option in _options(raw.get("suggestions"))
                       if option["label"] and not self._handles(option["label"])]
        products: list[dict] = []
        removed: list[dict] = []
        if self.toolbox is not None and isinstance(raw.get("products"), list):
            sourced, taken = self._sourced(_text(raw.get("car_label"), 80)), set()
            entries = [(item, handle) for item in raw["products"] if isinstance(item, dict)
                       for handle in _named(item)]
            for item, handle in entries[:MAX_PRODUCTS]:
                if item.get("confidence") == DEPENDS:
                    continue  # Kept back for an answer: never shown without the question.
                product_id = self.toolbox.handles.get(handle)
                row = self.toolbox.index.row(product_id) if product_id else None
                if row is None or product_id in taken or self._current(product_id) is None:
                    continue
                taken.add(product_id)
                veto = self._barred(row, _text(raw.get("car_label"), 80))
                if veto:
                    removed.append({"handle": handle, "product": product_id, "reason": veto})
                    continue
                confidence = item.get("confidence")
                if confidence not in CONFIDENCES or (
                        confidence == "confirmed" and not self.toolbox.confirmed_part(row, sourced)
                        and not self._verified_oil(product_id, self._current(product_id))):
                    confidence = "check"
                evidence = _text(item.get("evidence"), 300).strip("\"'`„“”«»")
                text = f"{row.name} {row.vehicle} {row.brand} {row.code} " \
                       f"{' '.join(map(str, row.cross_codes or ()))}"
                reason, group = _text(item.get("reason"), 300), _text(item.get("group"), 60)
                products.append({
                    "handle": handle, "product": product_id, "row": row,
                    "confidence": confidence,
                    "evidence": evidence if evidence and _bare(evidence) in _bare(text) else "",
                    "reason": "" if PRICE.search(reason) or self._handles(reason) else reason,
                    "group": "" if self._handles(group) else group})
        if not (kept or question or products):
            return None
        asking = bool(question and len(options) >= 2)
        outcome = raw.get("outcome") if raw.get("outcome") in OUTCOMES else (
            "offered" if products else "asked" if asking else "chat")
        return {
            "reply": kept if asking or not question else f"{kept}\n{question}".strip(),
            "language": raw.get("language") if raw.get("language") in LANGUAGES
            else self.language,
            "car_label": _text(raw.get("car_label"), 80), "products": products,
            "removed": removed,
            "question": {"text": question, "options": options} if asking else None,
            "suggestions": suggestions[:MAX_SUGGESTIONS],
            "notes": _text(raw.get("notes"), NOTES_LIMIT) or _text(self.memory.get("notes"),
                                                                    NOTES_LIMIT),
            "outcome": outcome,
            # An answer that failed a cosmetic check still tells the staff what they must know.
            "staff": self._staff(raw.get("staff"), lambda *_: None)}

    # -- the response --------------------------------------------------------------------------

    def _card(self, item: dict, language: str) -> dict:
        row, current = item["row"], self._current(item["product"])
        rules = self.rules
        if isinstance(current, dict):
            product = {key: current.get(key) for key in PRODUCT_KEYS if key in current}
            counts, observed = current.get("stock"), current.get("last_observed_at")
            status = current.get("status") or "PROVISIONAL"
        else:
            # The database could not be read: the snapshot's own quantities are shown.
            self.warnings.append("PRODUCT_REFRESH_UNAVAILABLE")
            product = {"source_product_id": str(row.id), "product_code": row.shop_code,
                       "product_name": row.name, "product_model": row.vehicle,
                       "original_code": row.code,
                       "duplicate_code": "/".join(map(str, row.cross_codes or ())),
                       "brand": row.brand}
            # Its age is the snapshot's: a database that cannot be read must not make week-old
            # quantities look current (the 13 h warning and the 25 h "ask the shop" apply).
            counts, observed = {"store": row.store, "tuz11": row.warehouse}, \
                self._snapshot_time()
            status = "PROVISIONAL"
        price = row.price if isinstance(row.price, int | float) and row.price > 0 else None
        litres = oil_pack(row.name)
        if is_oil(row.name):
            price = pack_price(price, litres) if litres else None
            if litres:
                counts = pack_counts(counts, litres)
        product.update(price=price, currency="GEL" if price is not None else None,
                       stock=counts, status=status, matched_codes=[])
        state = stock.classify(counts, rules.min_store, rules.min_tuz11)
        age = stock.age_hours(observed)
        too_old = age is not None and age > rules.hide_after_hours
        if too_old:
            state = stock.UNKNOWN
        elif state in {stock.IN_STORE, stock.WITHIN_HOURS} and stock.is_stale(
                observed, hours=rules.stale_hours):
            self.warnings.append("STOCK_MAY_BE_STALE")
        words = "ka" if language == "ka" else "en"
        verified = item["confidence"] == "confirmed" and self._verified_oil(
            item["product"], current)
        return {
            "product": product, "match_source": "assistant",
            "fitment_status": "VERIFIED" if verified else "UNKNOWN",
            "compatible": True if verified else None,
            "availability": stock.STALE_TEXT[0 if words == "ka" else 1] if too_old else
            stock.availability_text(counts, words, rules.min_store, rules.min_tuz11),
            "availability_status": state, "basis": item["confidence"],
            "matched_via": [item["reason"]] if item["reason"] else [],
            "pack_litres": litres, "role": None, "warnings": [],
            "confidence": item["confidence"], "reason": item["reason"],
            "evidence": item["evidence"], "group": item["group"]}

    def _oil_quote(self, requested: Any, cards: list[dict], language: str) -> str:
        if not isinstance(requested, dict):
            return ""
        by_id = {str(card["product"]["source_product_id"]): card for card in cards}
        oil = by_id.get(self.toolbox.handles.get(handle_of(requested.get("oil_id"))))
        oil_filter = by_id.get(self.toolbox.handles.get(handle_of(requested.get("filter_id"))))
        filter_name = (oil_filter["product"].get("product_name") or "") if oil_filter else ""
        if not (oil and oil_filter and oil["pack_litres"]
                and is_engine_oil(oil["product"].get("product_name") or "")
                and is_oil_filter(filter_name)
                and not self.rules.part_conflict(filter_name, "oil_filter")):
            return ""
        if any(card["confidence"] == "check" for card in (oil, oil_filter)):
            return ""
        litres = requested.get("litres")
        if isinstance(litres, bool) or not isinstance(litres, int | float):
            return ""
        if litres == 0 and oil["fitment_status"] == "VERIFIED":
            litres = self.toolbox.oil_capacities.get(oil["product"]["source_product_id"])
        elif litres not in requested_litres(self.message):
            if (not (followup.asks_for_total(self.message) or followup.refers_to_oil(self.message))
                    or oil["product"]["source_product_id"] != self.followup.get("oil_product")
                    or litres != self.followup.get("oil_litres")):
                return ""
        if litres is None or not 0 < litres <= 100:
            return ""
        total = oil_filter_total(oil, oil_filter, litres)
        if total is None:
            return ""
        packs, price = total
        return OIL_TOTAL[language].format(packs=packs, size=oil["pack_litres"], total=price)

    def _oil_clarification(self, decision: dict) -> dict:
        if self.toolbox is None:
            return decision
        guide = self.toolbox.oil_guide_result
        pending = self.memory.get("oil_question")
        vehicle = {**self.form, **self.followup["vehicle"], **self.facts}
        fitment = oil_intake.fitment_request(self.message, bool(self.vin or vehicle.get("model")))
        quoted_oil = (followup.refers_to_oil(self.message) and self.followup.get("oil_grade")
                      and self.followup.get("oil_product") and decision.get("oil_quote"))
        active = (fitment or (bool(guide) and not followup.oil_grade(self.message)
                              and not quoted_oil)
                  or (pending and (oil_intake.answers_question(self.message)
                                   or followup.stated_vehicle(self.message)
                                   or oil_intake.stated_market(self.message))))
        if not active:
            return decision

        spec = self.followup.get("oil_spec")
        grades = set((guide.get("needs") or {}).get("grades", []))
        codes = (set(guide.get("catalogue_codes", []))
                 if guide.get("status") == "answered" else set())
        verified = {row[0] for row in guide.get("rows", [])} if guide.get("status") == "verified" \
            else set()
        products, matched = [], False
        for item in decision["products"]:
            row = item["row"]
            if not oil_intake.oil_candidate(row.name, row.code):
                products.append(item)
                continue
            current = self._current(item["product"])
            current = current if isinstance(current, dict) else {}
            name = current.get("product_name") or row.name
            grade = normalize_grade(name)
            code = normalize_code(current.get("original_code") or row.code)
            if ((item["handle"] in verified and
                 self._verified_oil(item["product"], current))
                    or (grade in grades and code in codes)
                    or (spec and (not grades or grade in grades)
                        and oil_intake.states_spec(name, spec))):
                products.append(item)
                matched = True
        if matched:
            return decision if len(products) == len(decision["products"]) else {
                **decision, "products": products,
                "reply": oil_intake.MATCH[decision["language"]], "oil_quote": None,
            }

        language = decision["language"]
        field = guide.get("detail")
        if not field and (guide.get("engine_variants")
                          or guide.get("status") == "engine_ambiguous"):
            field = "engine"
        if not field:
            field = "spec" if guide.get("status") == "answered" else oil_intake.needed(vehicle)
        if field == "vehicle" and self.vin:
            field = "vehicle_after_vin"
        if oil_intake.unsure(self.message) and pending and field != "spec":
            field = "spec"
        handoff = (bool(spec) or guide.get("status") == "verified"
                   or (pending == "spec" and oil_intake.unsure(self.message)))
        question = None if handoff else oil_intake.question(field, language)
        choices = (followup.engine_options(self.toolbox.engine_variants, language)
                   if field == "engine" and question else [])
        if choices:
            question = {"text": followup.ENGINE_QUESTION[language], "options": choices}
        return {**decision, "products": products, "question": question,
                "reply": "" if question else oil_intake.FALLBACK[language],
                "outcome": "asked" if question else "handoff", "oil_quote": None,
                "engine_choices": {option["label"]: option["meaning"] for option in choices},
                "oil_question": field if question else None}

    def _respond(self, decision: dict, status: str | None = None) -> dict:
        if status in (None, "OK", "CLARIFICATION"):
            decision = self._oil_clarification(decision)
        language = decision["language"]
        cards = [self._card(item, language) for item in decision["products"]]
        # Groups in the model's order; inside a group what is in the shop comes first.
        order: dict[str, int] = {}
        for card in cards:
            order.setdefault(card["group"], len(order))
        cards.sort(key=lambda card: (order[card["group"]],
                                     stock.RANKS.get(card["availability_status"], 2)))
        # A card names its group by id; a heading past the sixth is not shown.
        ids = {label: f"g{number}" for number, label in enumerate(
            [label for label in order if label][:MAX_GROUPS], 1)}
        groups = [{"id": identifier, "label": label,
                   "count": sum(1 for card in cards if card["group"] == label)}
                  for label, identifier in ids.items()]
        for card in cards:
            card["group"] = ids.get(card["group"], "")
        asked = decision["question"]
        suggestions = decision["suggestions"]
        reply = decision["reply"]
        if not asked and not cards and any(item["reason"] == OIL_GRADE_CONFLICT
                             for item in decision.get("removed") or []):
            reply = OIL_LABEL_CHECK[language]
        if decision["outcome"] == "offered" and not asked:
            if quote := self._oil_quote(decision.get("oil_quote"), cards, language):
                reply = f"{reply}\n{quote}".strip()
        answer = reply
        if asked and asked["text"] not in reply:
            answer = f"{reply}\n{asked['text']}".strip()
        labels = [option["label"] for option in (asked["options"] if asked else suggestions)]
        labels = labels[:MAX_OPTIONS]
        outcome = decision["outcome"]
        if outcome == "offered" and not cards:
            outcome = "asked" if asked else "nothing_found"
        elif outcome == "asked" and not asked:
            outcome = "offered" if cards else "chat"
        for item in decision.get("removed") or []:
            self.diagnostics["removed"].append({"product": item["product"],
                                                "rule": ("oil_grade_conflict" if item["reason"]
                                                         == OIL_GRADE_CONFLICT else "never_offer"),
                                                "reason": item["reason"]})
        if status is None:
            status = "CLARIFICATION" if asked and not cards else "OK"
        if status not in {"OK", "CLARIFICATION"}:
            self.warnings.append(status)
        memory = self._memory(decision, answer, [card["product"]["source_product_id"]
                                                 for card in cards])
        self.diagnostics.update(
            # What the model chose, when the rows it chose could not be shown: the staff are told.
            outcome=outcome, chosen_outcome=decision["outcome"],
            car_label=decision["car_label"], notes=decision["notes"],
            asked=asked["text"] if asked else None, buttons=labels,
            offered=[{"product": item["product"], "confidence": item["confidence"],
                      "group": item["group"]} for item in decision["products"]],
            # The intent only: the phone and the note stay out of what the website log keeps.
            staff_intent=(decision.get("staff") or NO_STAFF)["intent"],
            elapsed_ms=round((time.monotonic() - self.started) * 1000))
        failed = status not in {"OK", "CLARIFICATION"}
        response = {
            "status": status, "act": ACTS.get(outcome, "help"), "answer": answer, "note": "",
            "quick_replies": labels, "language": "ka" if language == "ka" else "en",
            "vehicle": {key: value for key, value in {**self.form, **self.facts}.items()
                        if key in VEHICLE_KEYS},
            "cards": cards, "supplier_only": [],
            # A failed turn reports no results, so the store marks it failed, not partial.
            "results": [] if failed else self.results,
            "warnings": list(dict.fromkeys(self.warnings)), "usage": self.usage,
            "requirements": self.requirements, "progress": self.entries,
            "diagnostics": self.diagnostics,
            "clarification_choices": [{"label": label, "value": label} for label in labels],
            "pending": memory["question"],
            "question": {"text": asked["text"], "options": [
                {"label": option["label"], "value": option["label"]}
                for option in asked["options"]]} if asked else None,
            "suggestions": [{"label": option["label"],
                             "value": option.get("value") or option["label"]}
                            for option in suggestions],
            "groups": groups, "car_label": decision["car_label"], "outcome": outcome,
            "memory": memory,
            # For the Live chats page (``live_alerts.turn_alerts``); the widget never sees it.
            "staff": decision.get("staff") or dict(NO_STAFF),
        }
        return json.loads(self.redact(_dump(response)))

    def _memory(self, decision: dict, answer: str, shown: list[str]) -> dict:
        """What the next turn needs, and nothing the store has to understand."""
        toolbox = self.toolbox
        handles = dict(toolbox.handles) if toolbox else dict(self.memory.get("handles") or {})
        for stale in list(handles)[:max(0, len(handles) - HANDLE_LIMIT)]:
            del handles[stale]
        asked = decision["question"]
        if asked is None and decision.get("keep_question"):
            asked = self.question  # A failed turn leaves the customer's open question open.
        transcript = [turn for turn in self.memory.get("transcript") or []
                      if isinstance(turn, dict)]
        said = "\n".join([*self.unanswered, self.message])
        transcript.append({"user": said[:1500], "assistant": answer[:1500]})
        context = dict(self.followup)
        oils = [product_id for product_id in shown if toolbox and
                (row := toolbox.index.row(product_id)) is not None and is_engine_oil(row.name)]
        if len(oils) > 1:
            context.pop("oil_product", None)
        if len(oils) == 1:
            context["oil_product"] = oils[0]
        return {
            "language": decision["language"], "notes": decision["notes"],
            "car_label": decision["car_label"],
            "question": {"act": "clarify", "question": asked["text"],
                         "options": asked["options"],
                         "choices": [option["label"] for option in asked["options"]]}
            if asked else None,
            "suggestions": [{"label": option["label"], "meaning": option.get("meaning", "")}
                            for option in decision["suggestions"]],
            "handles": handles,
            "shown": shown or list(self.memory.get("shown") or []),
            # Rows waiting for the customer's answer: handed back next turn, then dropped.
            "held": list(decision.get("held") or [])[:KNOWN_ROWS],
            # While a question is open, the rows the searches returned are handed back too.
            "seen": (list(self.seen) or list(self.memory.get("seen") or []))[:KNOWN_ROWS]
            if asked else [],
            "sourced": sorted(self._sourced(decision["car_label"]))[:SOURCED_LIMIT],
            "transcript": transcript[-TRANSCRIPT_TURNS:],
            "vin_key": self.memory.get("vin_key"), "vin_facts": self.memory.get("vin_facts"),
            "followup": context,
            "engine_choices": decision.get("engine_choices") or {},
            "oil_question": decision.get("oil_question"),
            "phone": self._phone_state(decision),
        }

    def _phone_state(self, decision: dict) -> str:
        """'on_file' once the customer gave a phone number, 'asked' once the reply asked for
        one, else '': the next turn neither asks twice nor forgets that it already did."""
        staff = decision.get("staff") or NO_STAFF
        kept = self.memory.get("phone")
        state = kept if kept in ("asked", "on_file") else ""
        if staff["phone"]:
            return "on_file"
        if getattr(self, "phone_rejected", False) and state != "on_file":
            return ""  # what the customer typed was not taken: the next turn may ask again
        if staff["intent"] != "none" and state != "on_file":
            return "asked"
        # After hours a reply that could not offer a part asks for a number, so the staff can
        # send the details in the morning (a website visitor cannot be answered otherwise).
        if (self._shop_closed() and state != "on_file" and (
                staff["missing"] or decision.get("outcome") in ("nothing_found", "sold_out"))):
            return "asked"
        return state

    def _failure(self, status: str, texts: dict | None = None) -> dict:
        """An honest reply when no answer could be made: what happened is in ``status``
        and the customer can send the message again. When the provider's account refuses
        every request, asking again is not offered and the shop's log says why."""
        language = self.language
        account = status in ACCOUNT_STATUSES
        if account:
            LOGGER.error("assistant_provider_account status=%s: the provider refuses every "
                         "request until the account is put right", status)
        texts = texts or (NOT_CONFIGURED if status == "NOT_CONFIGURED"
                          else UNAVAILABLE if account else FAILED)
        retry = [] if status == "NOT_CONFIGURED" or account else [
            {"label": RETRY[language], "meaning": "The customer sends the last message again.",
             "value": self.message}]
        return self._respond({
            "reply": texts[language], "language": language,
            "car_label": _text(self.memory.get("car_label"), 80), "products": [],
            "removed": [],
            "question": None, "keep_question": True, "suggestions": retry,
            "notes": _text(self.memory.get("notes"), NOTES_LIMIT), "outcome": "handoff",
        }, status=status)

