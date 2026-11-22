"""Every prompt this project sends to a language model, and the owner's edits of them.

``PROMPTS`` lists each prompt once: where it is used, whether the dashboard may change it,
and the module constant that holds its packaged text. The dashboard (Prompts page, admins
only) shows them all; only the ones the live bot sends are ``editable``.

An edit is a row of ``garage.llm_prompts`` (migration 0019). No row means the packaged text,
so benches, evaluations and tests keep measuring the code's constants. ``PromptStore`` keeps
the rows in memory and reads them again at most every ``REFRESH_SECONDS`` in the background:
a turn never waits on the database after the first read, a failed read keeps the last copy,
and a missing table (migration not applied) or login means the packaged texts. The text is
handed on exactly as stored, so the provider's prompt cache sees the same bytes between
refreshes.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

LOGGER = logging.getLogger(__name__)

REFRESH_SECONDS = 30.0
# How long the first use waits for the first read; later reads never hold a turn.
FIRST_READ_SECONDS = 1.0
MAX_CHARS = 60_000
HISTORY_LIMIT = 30


class PromptConflict(RuntimeError):
    """The prompt changed since the editor read it."""


@dataclass(frozen=True)
class Prompt:
    key: str
    group: str
    title: dict[str, str]
    about: dict[str, str]
    source: str  # "module:ATTRIBUTE"
    editable: bool = False


def _pair(ka: str, en: str) -> dict[str, str]:
    return {"ka": ka, "en": en}


GROUPS: dict[str, dict[str, dict[str, str]]] = {
    "bot": {
        "title": _pair("ჩატბოტი (Messenger და საიტი)", "Chatbot (Messenger and website)"),
        "about": _pair(
            "რასაც ბოტი ყოველ პასუხზე კითხულობს. აქ შეცვლილი ტექსტი საიტის ჩატზე მაშინვე "
            "მოქმედებს, Messenger-ზე ერთ წუთში.",
            "What the bot reads for every reply. An edit here reaches the website chat at "
            "once and Messenger within a minute."),
    },
    "research": {
        "title": _pair("ვებ-ძიება ჩატის დროს", "Web research during a chat"),
        "about": _pair(
            "ცალკე მოდელი, რომელიც ჩატის დროს ინტერნეტში ნაწილის ნომრებს ეძებს. უკვე "
            "ნაპოვნი პასუხები ქეშში რჩება თავის ვადამდე.",
            "A separate model that looks up part numbers on the web during a chat. Answers "
            "it already found stay cached until they expire."),
    },
    "knowledge": {
        "title": _pair("ცოდნის შემმოწმებელი (ფონური)", "Knowledge checker (background)"),
        "about": _pair(
            "ფონური სამუშაო, რომელიც ძრავისა და ნაწილების ფაქტებს ამოწმებს. მხოლოდ "
            "სანახავად: მისი შეცვლა კოდში ხდება.",
            "A background job that checks engine and part facts. View only: it is changed "
            "in the code."),
    },
    "catalogue": {
        "title": _pair("კატალოგის წამკითხველები (ხელით)", "Catalogue readers (run by hand)"),
        "about": _pair(
            "ერთჯერადი ბრძანებები, რომლებიც კატალოგის ნაწილებისა და მანქანების სახელებს "
            "განმარტავს. მხოლოდ სანახავად.",
            "One-off commands that explain the catalogue's part and vehicle names. View "
            "only."),
    },
    "legacy": {
        "title": _pair("ძველი ჩატის ძრავები (გამორთული)", "Older chat engines (switched off)"),
        "about": _pair(
            "ჩატის წინა ვერსიები. ახლა არ გამოიყენება, ინახება დასაბრუნებლად. მხოლოდ "
            "სანახავად.",
            "Earlier versions of the chat, not in use, kept for rollback. View only."),
    },
    "testing": {
        "title": _pair("სიზუსტის ტესტები", "Accuracy tests"),
        "about": _pair(
            "მოდელები, რომლებიც ბოტის პასუხებს ტესტების დროს აფასებენ. კლიენტები მათ ვერ "
            "ხედავენ. მხოლოდ სანახავად.",
            "Models that grade the bot's replies during tests. Customers never see them. "
            "View only."),
    },
}


def _prompt(key: str, group: str, title: tuple[str, str], about: tuple[str, str], source: str,
            editable: bool = False) -> Prompt:
    return Prompt(key, group, _pair(*title), _pair(*about), source, editable)


PROMPTS: tuple[Prompt, ...] = (
    # -- the live bot -----------------------------------------------------------------------
    _prompt("assistant.system", "bot",
            ("ბოტის ინსტრუქციები (სისტემური პრომპტი)", "Bot instructions (system prompt)"),
            ("ბოტის მთავარი ინსტრუქცია: როგორ ელაპარაკოს კლიენტს, რა მოძებნოს და როგორ "
             "უპასუხოს. სწორედ ამით იცვლება ბოტის ქცევა.",
             "The bot's main instruction: how it talks to customers, what it looks up and "
             "how it answers. This is what changes the way the bot replies."),
            "automotive.assistant_prompt:SYSTEM_PROMPT", editable=True),
    _prompt("assistant.nudge", "bot",
            ("შეხსენება: პასუხი ხელსაწყოთი", "Reminder: reply through the tool"),
            ("ეგზავნება ბოტს, როცა ის ტექსტს წერს ხელსაწყოს გამოძახების ნაცვლად.",
             "Sent to the bot when it writes text instead of calling its reply tool."),
            "automotive.assistant_prompt:NUDGE", editable=True),
    _prompt("assistant.last_round", "bot",
            ("შეხსენება: ბოლო ძიება", "Reminder: last search round"),
            ("ეგზავნება, როცა ბოტმა ძიების ლიმიტი ამოწურა.",
             "Sent when the bot has used up its search rounds."),
            "automotive.assistant_prompt:LAST_ROUND", editable=True),
    _prompt("assistant.out_of_time", "bot",
            ("შეხსენება: დრო იწურება", "Reminder: time is running out"),
            ("ეგზავნება, როცა პასუხის დრო თითქმის ამოიწურა.",
             "Sent when the reply's time is nearly up."),
            "automotive.assistant_prompt:OUT_OF_TIME", editable=True),
    _prompt("assistant.search_budget", "bot",
            ("შეხსენება: ძიების ლიმიტი", "Reminder: search budget used"),
            ("ეგზავნება, როცა ბოტმა ძიებების ლიმიტი ამოწურა. {limit} ლიმიტის რიცხვით "
             "იცვლება.",
             "Sent when the bot has used up its searches. {limit} is replaced by the "
             "number of searches allowed."),
            "automotive.assistant_prompt:SEARCH_BUDGET", editable=True),
    _prompt("assistant.guide_heading", "bot",
            ("კატალოგის გზამკვლევის სათაური", "Catalogue guide heading"),
            ("სათაური კატალოგიდან ავტომატურად აგებული გზამკვლევის ზემოთ.",
             "The heading above the guide built automatically from the catalogue."),
            "automotive.assistant_prompt:GUIDE_HEADING"),
    _prompt("assistant.tools", "bot",
            ("ხელსაწყოების აღწერები", "Tool descriptions"),
            ("რას აკეთებს ბოტის თითოეული ხელსაწყო. ხელსაწყოების სქემასთანაა მიბმული, "
             "ამიტომ კოდში იცვლება.",
             "What each of the bot's tools does. Tied to the tools' schema, so it is changed "
             "in the code."),
            "automotive.assistant_prompt:TOOL_DESCRIPTIONS"),
    _prompt("assistant.fields", "bot",
            ("ხელსაწყოების ველების აღწერები", "Tool field descriptions"),
            ("ხელსაწყოების თითოეული ველის ახსნა. კოდში იცვლება.",
             "What each field of the tools means. Changed in the code."),
            "automotive.assistant_prompt:FIELD_DESCRIPTIONS"),
    _prompt("assistant.car_fields", "bot",
            ("მანქანის ველების აღწერები", "Car field descriptions"),
            ("მანქანის მონაცემების ველები ხელსაწყოებში. კოდში იცვლება.",
             "The car fields of the tools. Changed in the code."),
            "automotive.assistant_prompt:CAR_FIELDS"),
    # -- web research inside a turn ---------------------------------------------------------
    _prompt("research.system", "research",
            ("ვებ-ძიების ინსტრუქციები", "Web research instructions"),
            ("ეუბნება ძიების მოდელს, როგორ მოძებნოს და შეამოწმოს ნაწილის ნომრები.",
             "Tells the research model how to find and check part numbers."),
            "automotive.assistant_research:RESEARCH_SYSTEM", editable=True),
    # -- the knowledge worker ---------------------------------------------------------------
    _prompt("knowledge.research", "knowledge",
            ("ფაქტების ძიების ინსტრუქციები", "Fact research instructions"),
            ("ძრავის ვარიანტებისა და ნაწილების ფაქტების მოძიება.",
             "Looks up engine variants and part facts."),
            "automotive.research:SYSTEM"),
    _prompt("knowledge.questions", "knowledge",
            ("ფაქტების ძიების კითხვები", "Fact research questions"),
            ("კითხვები, რომლებსაც ძიება თითო სამუშაოზე სვამს.",
             "The questions the research asks for each kind of job."),
            "automotive.research:QUESTIONS"),
    _prompt("knowledge.judge", "knowledge",
            ("ფაქტების შემფასებელი", "Fact judge"),
            ("ამოწმებს, დასტურდება თუ არა ნაპოვნი ფაქტი წყაროებით.",
             "Checks whether a found fact is backed by its sources."),
            "automotive.knowledge_judge:SYSTEM"),
    # -- catalogue readers ------------------------------------------------------------------
    _prompt("names.reader", "catalogue",
            ("ნაწილების სახელების წამკითხველი", "Part name reader"),
            ("განმარტავს კატალოგის ნაწილების სახელებს ბოტის გზამკვლევისთვის.",
             "Explains the catalogue's part names for the bot's guide."),
            "automotive.assistant_names:READER_SYSTEM"),
    _prompt("names.opening", "catalogue",
            ("პირველი შეტყობინებების წამკითხველი", "Opening message reader"),
            ("კითხულობს კლიენტების პირველ შეტყობინებებს ტესტებისთვის.",
             "Reads customers' opening messages for the tests."),
            "automotive.assistant_names:OPENING_SYSTEM"),
    _prompt("names.row_reading", "catalogue",
            ("ნაწილის სტრიქონის შაბლონი", "Part row template"),
            ("შაბლონი, რომლითაც ნაწილის სტრიქონი წამკითხველს ეგზავნება.",
             "The template a part row is sent to the reader in."),
            "automotive.assistant_names:ROW_READING"),
    _prompt("vehicles.vehicle", "catalogue",
            ("მანქანის ტექსტების წამკითხველი", "Vehicle text reader"),
            ("განმარტავს კატალოგის მანქანების ტექსტებს.",
             "Explains the catalogue's vehicle texts."),
            "automotive.assistant_vehicles:VEHICLE_SYSTEM"),
    _prompt("vehicles.name", "catalogue",
            ("ნაწილის სახელის და პოზიციის წამკითხველი", "Part name and position reader"),
            ("განმარტავს, რა ნაწილია და რა პოზიციას აღნიშნავს სახელი.",
             "Explains what each part is and the positions its name states."),
            "automotive.assistant_vehicles:NAME_SYSTEM"),
    # -- older chat engines -----------------------------------------------------------------
    _prompt("legacy.conversational", "legacy",
            ("საუბრის ძრავა (scripted)", "Conversation engine (scripted)"),
            ("ინსტრუქციები ძველი, სკრიპტული ჩატისთვის.",
             "Instructions of the older scripted chat."),
            "automotive.conversational:SYSTEM"),
    _prompt("legacy.conversational_tools", "legacy",
            ("საუბრის ძრავის ხელსაწყოები", "Conversation engine tools"),
            ("ძველი სკრიპტული ჩატის ხელსაწყოების აღწერები.",
             "Tool descriptions of the older scripted chat."),
            "automotive.conversational:DESCRIPTIONS"),
    _prompt("legacy.rephrase", "legacy",
            ("პასუხის გადაწერა", "Reply rephrasing"),
            ("ძველ ჩატში პასუხის თავისუფლად გადაწერა.",
             "Rewords a reply in the older chat."),
            "automotive.conversational:REPHRASE_SYSTEM"),
    _prompt("legacy.orchestrated", "legacy",
            ("ორკესტრირებული ძრავა", "Orchestrated engine"),
            ("ინსტრუქციები ორკესტრირებული ჩატისთვის.",
             "Instructions of the orchestrated chat."),
            "automotive.orchestration:SYSTEM"),
    _prompt("legacy.orchestrated_tools", "legacy",
            ("ორკესტრირებული ძრავის ხელსაწყოები", "Orchestrated engine tools"),
            ("ორკესტრირებული ჩატის ხელსაწყოების აღწერები.",
             "Tool descriptions of the orchestrated chat."),
            "automotive.orchestration:DESCRIPTIONS"),
    _prompt("legacy.router", "legacy",
            ("კითხვების მარშრუტიზატორი", "Question router"),
            ("უძველესი ჩატის ინსტრუქცია, რომელიც კითხვას ხელსაწყოებზე ანაწილებს.",
             "The oldest chat's instruction that routes a question to tools."),
            "automotive.chat:ROUTER_SYSTEM"),
    _prompt("legacy.router_tools", "legacy",
            ("მარშრუტიზატორის ხელსაწყოები", "Question router tools"),
            ("უძველესი ჩატის ხელსაწყოების აღწერები.",
             "Tool descriptions of the oldest chat."),
            "automotive.chat:DESCRIPTIONS"),
    # -- accuracy tests ---------------------------------------------------------------------
    _prompt("eval.writer", "testing",
            ("ტესტის კლიენტის როლი", "Test customer writer"),
            ("თამაშობს კლიენტს, რომელიც ბოტს წერს.",
             "Plays the customer who writes to the bot."),
            "automotive.assistant_eval:WRITER_SYSTEM"),
    _prompt("eval.answer", "testing",
            ("ტესტის კლიენტის პასუხები", "Test customer answers"),
            ("პასუხობს ბოტის დამაზუსტებელ კითხვებს კლიენტის სახელით.",
             "Answers the bot's follow-up questions as the customer."),
            "automotive.assistant_eval:ANSWER_SYSTEM"),
    _prompt("eval.judge", "testing",
            ("პასუხების შემფასებელი", "Reply judge"),
            ("აფასებს ბოტის პასუხებს.", "Grades the bot's replies."),
            "automotive.assistant_eval:JUDGE_SYSTEM"),
    _prompt("exam.number", "testing",
            ("გამოცდა: ნაწილის ნომრები", "Exam: part numbers"),
            ("ამოწმებს ნაწილის ნომრის კითხვებზე პასუხებს.",
             "Checks the answers to part number questions."),
            "automotive.assistant_exam:NUMBER_SYSTEM"),
    _prompt("exam.vin", "testing",
            ("გამოცდა: VIN", "Exam: VIN"),
            ("ამოწმებს VIN-ით ძიების პასუხებს.", "Checks the answers to VIN searches."),
            "automotive.assistant_exam:VIN_SYSTEM"),
    _prompt("exam.vin_message", "testing",
            ("გამოცდა: VIN შეტყობინება", "Exam: VIN message"),
            ("ამოწმებს შეტყობინებებს, რომლებშიც VIN წერია.",
             "Checks messages that carry a VIN."),
            "automotive.assistant_exam:VIN_MESSAGE_SYSTEM"),
    _prompt("exam.alternative", "testing",
            ("გამოცდა: ალტერნატივები", "Exam: alternatives"),
            ("ამოწმებს შემოთავაზებულ ალტერნატიულ ნაწილებს.",
             "Checks the alternative parts offered."),
            "automotive.assistant_exam:ALTERNATIVE_SYSTEM"),
    _prompt("exam.question", "testing",
            ("გამოცდა: დამაზუსტებელი კითხვა", "Exam: follow-up question"),
            ("ამოწმებს ბოტის დამაზუსტებელ კითხვებს.", "Checks the bot's follow-up questions."),
            "automotive.assistant_exam:QUESTION_SYSTEM"),
    _prompt("exam.dispute", "testing",
            ("გამოცდა: სადავო შედეგი", "Exam: disputed result"),
            ("ხელახლა ამოწმებს სადავო შეფასებას.", "Rechecks a disputed grade."),
            "automotive.assistant_exam:DISPUTE_SYSTEM"),
    _prompt("exam.service_check", "testing",
            ("გამოცდა: სერვისის მოთხოვნა", "Exam: service request"),
            ("ამოწმებს, სწორად გადასცა თუ არა ბოტმა სერვისის მოთხოვნა.",
             "Checks that the bot passed a service request on correctly."),
            "automotive.assistant_exam:SERVICE_CHECK"),
    _prompt("conversation_eval.judge", "testing",
            ("საუბრის ტესტის შემფასებელი", "Conversation test judge"),
            ("აფასებს ძველი ჩატის საუბრებს.", "Grades conversations of the older chat."),
            "automotive.conversation_eval:JUDGE_PROMPT"),
    _prompt("customer_eval.question_check", "testing",
            ("რეალური ჩატების კითხვის შემოწმება", "Real chat question check"),
            ("ამოწმებს ბოტის კითხვებს რეალური ჩატების ტესტში.",
             "Checks the bot's questions in the real chat test."),
            "automotive.customer_eval:QUESTION_CHECK"),
    _prompt("bench.tool_note", "testing",
            ("ბენჩის ხელსაწყოების შენიშვნა", "Bench tool note"),
            ("ემატება ბოტის ინსტრუქციას უფასო ბენჩზე, სადაც ხელსაწყოებს პრეფიქსი აქვთ.",
             "Added to the bot's instructions on the free bench, where tools carry a prefix."),
            "automotive.assistant_bench:TOOL_NOTE"),
)

BY_KEY: dict[str, Prompt] = {item.key: item for item in PROMPTS}
EDITABLE = frozenset(item.key for item in PROMPTS if item.editable)


def _rendered(value: Any) -> str:
    """A packaged prompt as text: a mapping (tool descriptions, questions) one entry a
    paragraph, in its own order."""
    if isinstance(value, dict):
        return "\n\n".join(f"{name}: {_rendered(text)}" for name, text in value.items())
    return str(value)


def default_text(key: str) -> str:
    """The packaged text of ``key``, read from its module (imported on first use)."""
    module, _, attribute = BY_KEY[key].source.partition(":")
    return _rendered(getattr(importlib.import_module(module), attribute))


def clean_text(value: Any) -> tuple[str, str]:
    """``(text, error)``: line ends made ``\\n``, nothing else changed. Errors: REQUIRED,
    TOO_LONG, INVALID."""
    if not isinstance(value, str):
        return "", "INVALID"
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        return "", "INVALID"
    if not text.strip():
        return "", "REQUIRED"
    if len(text) > MAX_CHARS:
        return "", "TOO_LONG"
    return text, ""


def resolve(store: Any, key: str, default: str) -> str:
    """The text the live code sends for ``key``: the stored edit, else ``default``. Never
    raises: a missing or broken store means ``default``."""
    if store is None:
        return default
    try:
        text = store.text(key)
    except Exception as exc:  # A turn must never fail because of this lookup.
        LOGGER.warning("prompt_lookup_failed key=%s error=%s", key, type(exc).__name__)
        return default
    return text if isinstance(text, str) and text.strip() else default


class PromptStore:
    """The stored edits, read with whatever login the process has (``garage_site`` on the
    website, ``garage_messenger`` in the Messenger worker). ``database`` has ``configured``
    and ``connect()`` like ``meta_db.MetaDatabase``."""

    def __init__(self, database: Any, *, refresh_seconds: float = REFRESH_SECONDS,
                 first_read_seconds: float = FIRST_READ_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.database = database
        self.refresh_seconds = refresh_seconds
        self.first_read_seconds = first_read_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._texts: dict[str, str] = {}
        self._read_at: float | None = None
        self._reading = False
        self._first = threading.Event()
        self._generation = 0  # Bumped by each write here: a read begun before it is stale.
        self._state = ""
        self.available = False  # The table answered on the last read.

    @property
    def configured(self) -> bool:
        return bool(getattr(self.database, "configured", False))

    # -- the live path ------------------------------------------------------------------

    def text(self, key: str) -> str | None:
        """The stored edit of ``key``, or None for the packaged text."""
        if key not in EDITABLE or not self.configured:
            return None
        return self._current().get(key)

    def invalidate(self) -> None:
        """Read again on the next use (after a save on this process)."""
        with self._lock:
            if self._read_at is not None:
                self._read_at = -float("inf")

    def _current(self) -> dict[str, str]:
        with self._lock:
            fresh = self._read_at is not None and \
                self.clock() - self._read_at < self.refresh_seconds
            start = not fresh and not self._reading
            if start:
                self._reading = True
            first = self._read_at is None
        if start:
            threading.Thread(target=self._read, name="prompt-store", daemon=True).start()
        if first:
            self._first.wait(self.first_read_seconds)
        return self._texts

    def _read(self) -> None:
        state, texts = "ok", None
        with self._lock:
            generation = self._generation
        try:
            with self.database.connect() as connection:
                rows = connection.execute(
                    "SELECT prompt_key, body FROM garage.llm_prompts").fetchall()
            texts = {row["prompt_key"]: row["body"] for row in rows
                     if row["prompt_key"] in EDITABLE and str(row["body"]).strip()}
            self.available = True
        except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege) as exc:
            # Migration 0019 not applied, or the login lacks its grant: the packaged texts.
            texts, self.available = {}, False
            state = f"unavailable:{type(exc).__name__}"
        except Exception as exc:  # Keep the last copy; try again after the refresh time.
            self.available = False
            state = f"failed:{type(exc).__name__}"
        finally:
            with self._lock:
                self._reading = False
                if generation == self._generation:
                    if texts is not None:
                        self._texts = texts
                    self._read_at = self.clock()
                # else: a write here came after this read began; the next use reads again.
            self._first.set()
        if state != self._state:
            self._state = state
            LOGGER.log(logging.INFO if state == "ok" else logging.WARNING,
                       "prompt_store state=%s edited=%s", state,
                       ",".join(sorted(self._texts)) or "none")

    # -- the dashboard ------------------------------------------------------------------

    def rows(self) -> dict[str, dict]:
        """Every stored edit with its version and who saved it, by key."""
        with self.database.connect() as connection:
            return {row["prompt_key"]: row for row in connection.execute(
                "SELECT prompt_key, body, version, updated_at, updated_by "
                "FROM garage.llm_prompts").fetchall()}

    def history(self, key: str, limit: int = HISTORY_LIMIT) -> list[dict]:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT history_id, action, body, version, saved_by, saved_at "
                "FROM garage.llm_prompt_history WHERE prompt_key = %s "
                "ORDER BY history_id DESC LIMIT %s", (key, max(1, min(limit, 100)))).fetchall()

    def save(self, key: str, body: str, expected_version: int, actor: str) -> int:
        """Store ``body`` as the edit of ``key``; ``expected_version`` 0 means "no edit
        stored yet". Returns the new version; raises PromptConflict."""
        with self.database.connect() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO garage.llm_prompts AS p (prompt_key, body, updated_by) "
                "VALUES (%s, %s, %s) ON CONFLICT (prompt_key) DO UPDATE SET "
                "body = EXCLUDED.body, version = p.version + 1, updated_at = now(), "
                "updated_by = EXCLUDED.updated_by WHERE p.version = %s RETURNING version",
                (key, body, actor[:32], expected_version)).fetchone()
            if row is None or (expected_version == 0 and row["version"] != 1):
                raise PromptConflict(key)
            connection.execute(
                "INSERT INTO garage.llm_prompt_history (prompt_key, action, body, version, "
                "saved_by) VALUES (%s, 'saved', %s, %s, %s)",
                (key, body, row["version"], actor[:32]))
        self._apply(key, body)
        return row["version"]

    def reset(self, key: str, expected_version: int, actor: str) -> bool:
        """Drop the edit of ``key`` so the packaged text applies again. False when there
        was none; raises PromptConflict when it changed since ``expected_version``."""
        with self.database.connect() as connection, connection.transaction():
            current = connection.execute(
                "SELECT version FROM garage.llm_prompts WHERE prompt_key = %s FOR UPDATE",
                (key,)).fetchone()
            if current is None:
                return False
            if current["version"] != expected_version:
                raise PromptConflict(key)
            connection.execute("DELETE FROM garage.llm_prompts WHERE prompt_key = %s", (key,))
            connection.execute(
                "INSERT INTO garage.llm_prompt_history (prompt_key, action, body, version, "
                "saved_by) VALUES (%s, 'reset', '', %s, %s)",
                (key, expected_version, actor[:32]))
        self._apply(key, None)
        return True

    def _apply(self, key: str, body: str | None) -> None:
        """A write on this process counts at once (its next turn sends it); the others see
        it on their next read."""
        with self._lock:
            texts = {name: text for name, text in self._texts.items() if name != key}
            if body is not None and key in EDITABLE:
                texts[key] = body
            self._texts = texts
            self._generation += 1
            if self._read_at is not None:
                self._read_at = -float("inf")
