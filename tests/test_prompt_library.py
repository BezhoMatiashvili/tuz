"""The prompt registry, the store of the owner's edits, the texts the live bot sends with
them, and the dashboard's Prompts routes. The PostgreSQL side (migration 0019, its grants and
row-level security) is in tests/test_llm_prompts_database.py."""

from __future__ import annotations

import ast
import re
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import psycopg

from automotive import assistant_prompt, prompt_library
from automotive.assistant_research import RESEARCH_SYSTEM, ResearchService
from automotive.prompt_library import (
    BY_KEY,
    EDITABLE,
    GROUPS,
    MAX_CHARS,
    PROMPTS,
    PromptConflict,
    PromptStore,
    clean_text,
    default_text,
    resolve,
)
from tests.test_assistant import Script, done, run, says, search, service, use
from tests.test_site_admin import AdminCase

ROOT = Path(__file__).resolve().parents[1]
EDIT = "You are the Garage 1886 parts assistant. Edited by the owner."

# Files that call a model without a prompt of their own: they send the prompts of the modules
# named here (the registry lists those).
PASS_THROUGH = {
    "automotive/assistant.py": "assistant_prompt",
    "automotive/chat_review.py": "replays the assistant's own requests",
    "automotive/assistant_extras.py": "assistant_research",
    "automotive/model_client.py": "forwards whatever it is given",
    "automotive/knowledge_worker.py": "knowledge_judge and research",
}


class Rows:
    """A fake ``psycopg`` connection that answers the store's read with the owner's rows."""

    def __init__(self, owner: FakeDatabase) -> None:
        self.owner = owner

    def __enter__(self) -> Rows:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, statement: str, parameters: tuple = ()):
        self.owner.reads += 1
        if self.owner.error is not None:
            raise self.owner.error
        if self.owner.gate is not None:
            self.owner.gate.wait(5)
        return SimpleNamespace(fetchall=lambda: [dict(row) for row in self.owner.rows])


class FakeDatabase:
    configured = True

    def __init__(self, rows=(), error: Exception | None = None) -> None:
        self.rows, self.error, self.reads, self.gate = list(rows), error, 0, None

    def connect(self):
        return Rows(self)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def settled() -> None:
    """Wait for the background read the last call started."""
    for thread in threading.enumerate():
        if thread.name == "prompt-store":
            thread.join(5)


class RegistryTests(unittest.TestCase):
    def test_every_prompt_has_a_packaged_text_titles_and_a_known_group(self):
        self.assertEqual(len(BY_KEY), len(PROMPTS))
        for item in PROMPTS:
            with self.subTest(item.key):
                self.assertRegex(item.key, r"^[a-z][a-z0-9_.]{1,63}$")  # the table's CHECK
                self.assertIn(item.group, GROUPS)
                self.assertTrue(item.title["ka"] and item.title["en"])
                self.assertTrue(item.about["ka"] and item.about["en"])
                self.assertTrue(default_text(item.key).strip())
                self.assertLessEqual(len(default_text(item.key)), MAX_CHARS)

    def test_only_what_the_live_bot_sends_is_editable(self):
        self.assertEqual(EDITABLE, {"assistant.system", "assistant.nudge",
                                    "assistant.last_round", "assistant.out_of_time",
                                    "assistant.search_budget", "research.system"})
        self.assertEqual(default_text("assistant.system"), assistant_prompt.SYSTEM_PROMPT)
        self.assertEqual(default_text("research.system"), RESEARCH_SYSTEM)

    def test_mappings_are_shown_one_entry_a_paragraph(self):
        text = default_text("assistant.tools")
        for name, description in assistant_prompt.TOOL_DESCRIPTIONS.items():
            self.assertIn(f"{name}: {description}", text)

    def test_every_prompt_constant_in_the_code_is_listed(self):
        """A new prompt constant (``..._SYSTEM``, ``..._PROMPT``, ``..._CHECK``) must be added to
        prompt_library.PROMPTS, or this fails."""
        listed = {item.source for item in PROMPTS}
        pattern = re.compile(r"^(SYSTEM|[A-Z_]*_SYSTEM|[A-Z_]*PROMPT|[A-Z_]*_CHECK|"
                             r"ROW_READING|TOOL_NOTE)$")
        missing = []
        for path in sorted((ROOT / "automotive").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                target = node.targets[0]
                if not isinstance(target, ast.Name) or not pattern.match(target.id):
                    continue
                value = node.value
                text = (isinstance(value, ast.Constant) and isinstance(value.value, str)) \
                    or isinstance(value, ast.JoinedStr | ast.BinOp)
                if text and f"automotive.{path.stem}:{target.id}" not in listed:
                    missing.append(f"automotive.{path.stem}:{target.id}")
        self.assertEqual(missing, [])

    def test_every_file_that_calls_a_model_has_its_prompts_listed(self):
        """A new call site (``messages.create``, ``--system-prompt-file``) in a file whose
        prompts are not listed fails here."""
        modules = {item.source.partition(":")[0] for item in PROMPTS}
        calls = re.compile(r"messages\.create\(|--system-prompt-file")
        unlisted = []
        for path in sorted((ROOT / "automotive").glob("*.py")):
            name = f"automotive/{path.name}"
            if not calls.search(path.read_text(encoding="utf-8")):
                continue
            if f"automotive.{path.stem}" not in modules and name not in PASS_THROUGH:
                unlisted.append(name)
        self.assertEqual(unlisted, [])


class CleanTextTests(unittest.TestCase):
    def test_line_ends_are_normalised_and_nothing_else_changes(self):
        self.assertEqual(clean_text("  a\r\nb\rc  "), ("  a\nb\nc  ", ""))

    def test_blank_long_and_wrong_texts_are_refused(self):
        self.assertEqual(clean_text(" \n\t "), ("", "REQUIRED"))
        self.assertEqual(clean_text("x" * (MAX_CHARS + 1)), ("", "TOO_LONG"))
        self.assertEqual(clean_text("a\x00b"), ("", "INVALID"))
        self.assertEqual(clean_text(None), ("", "INVALID"))
        self.assertEqual(clean_text("x" * MAX_CHARS)[1], "")


class StoreTests(unittest.TestCase):
    def store(self, rows=(), error=None, **options) -> tuple[PromptStore, FakeDatabase, Clock]:
        database, clock = FakeDatabase(rows, error), Clock()
        return PromptStore(database, clock=clock, **options), database, clock

    def test_an_edit_is_returned_and_everything_else_is_the_packaged_text(self):
        store, _, _ = self.store([{"prompt_key": "assistant.system", "body": EDIT},
                                  {"prompt_key": "knowledge.judge", "body": "not editable"},
                                  {"prompt_key": "assistant.nudge", "body": "   "}])
        self.assertEqual(store.text("assistant.system"), EDIT)
        self.assertIsNone(store.text("knowledge.judge"))  # read-only keys never apply
        self.assertIsNone(store.text("assistant.nudge"))  # nor a blank row
        self.assertEqual(resolve(store, "assistant.nudge", assistant_prompt.NUDGE),
                         assistant_prompt.NUDGE)
        self.assertTrue(store.available)

    def test_reads_again_only_after_the_refresh_time_and_never_holds_a_later_call(self):
        store, database, clock = self.store([{"prompt_key": "assistant.system", "body": EDIT}])
        store.text("assistant.system")
        store.text("assistant.system")
        self.assertEqual(database.reads, 1)
        database.rows = [{"prompt_key": "assistant.system", "body": "second"}]
        clock.now += prompt_library.REFRESH_SECONDS + 1
        database.gate = threading.Event()  # the next read hangs until released
        self.assertEqual(store.text("assistant.system"), EDIT)  # the last copy, at once
        database.gate.set()
        settled()
        self.assertEqual(store.text("assistant.system"), "second")

    def test_a_failed_read_keeps_the_last_copy(self):
        store, database, clock = self.store([{"prompt_key": "assistant.system", "body": EDIT}])
        store.text("assistant.system")
        database.error = psycopg.OperationalError("down")
        clock.now += prompt_library.REFRESH_SECONDS + 1
        store.text("assistant.system")
        settled()
        self.assertEqual(store.text("assistant.system"), EDIT)
        self.assertFalse(store.available)

    def test_a_missing_table_or_grant_means_the_packaged_texts(self):
        for error in (psycopg.errors.UndefinedTable("no table"),
                      psycopg.errors.InsufficientPrivilege("no grant")):
            store, _, _ = self.store(error=error)
            self.assertIsNone(store.text("assistant.system"))
            self.assertEqual(resolve(store, "assistant.system", "packaged"), "packaged")

    def test_a_database_that_hangs_from_the_start_costs_one_short_wait(self):
        store, database, _ = self.store(first_read_seconds=0.05)
        database.gate = threading.Event()
        self.assertIsNone(store.text("assistant.system"))
        database.gate.set()
        settled()

    def test_an_unconfigured_login_reads_nothing(self):
        database = FakeDatabase([{"prompt_key": "assistant.system", "body": EDIT}])
        database.configured = False
        self.assertIsNone(PromptStore(database).text("assistant.system"))
        self.assertEqual(database.reads, 0)

    def test_a_read_begun_before_a_write_here_does_not_bring_the_old_text_back(self):
        store, database, clock = self.store([{"prompt_key": "assistant.system", "body": "old"}])
        store.text("assistant.system")
        clock.now += prompt_library.REFRESH_SECONDS + 1
        database.gate = threading.Event()
        store.text("assistant.system")  # starts a read that will return "old"
        database.rows = [{"prompt_key": "assistant.system", "body": "new"}]  # the commit
        store._apply("assistant.system", "new")  # what save() does after it
        database.gate.set()
        settled()
        self.assertEqual(store._texts, {"assistant.system": "new"})  # the late read dropped
        self.assertEqual(store.text("assistant.system"), "new")
        settled()
        self.assertEqual(store.text("assistant.system"), "new")

    def test_resolve_never_raises(self):
        broken = SimpleNamespace(text=lambda key: 1 / 0)
        self.assertEqual(resolve(broken, "assistant.system", "packaged"), "packaged")
        self.assertEqual(resolve(None, "assistant.system", "packaged"), "packaged")


class Edits:
    """A stand-in store for the live paths: fixed edits by key."""

    configured = True

    def __init__(self, texts: dict[str, str] | None = None) -> None:
        self.texts = dict(texts or {})

    def text(self, key: str) -> str | None:
        return self.texts.get(key)


class Backend:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def run(self, question, system, timeout):
        self.seen.append(system)
        return {"findings": [], "answer": "", "note": ""}, {}


QUESTION = {"kind": "numbers", "make": "Toyota", "model": "Camry", "part": "front brake pads"}


class LiveTextTests(unittest.TestCase):
    def turn(self, store) -> Script:
        script = Script(says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))),
                        done(reply="რომელი?"), done())
        chat = service(script)
        chat.prompt_texts = store
        run(chat, "Camry ხუნდები")
        self.assertTrue(script.requests)
        return script

    def test_an_edited_system_prompt_is_what_every_request_of_the_turn_sends(self):
        script = self.turn(Edits({"assistant.system": EDIT}))
        for request in script.requests:
            self.assertEqual(request["system"][0]["text"], EDIT)
            self.assertIn("CATALOGUE GUIDE", request["system"][1]["text"])

    def test_without_an_edit_or_with_a_broken_store_the_packaged_prompt_is_sent(self):
        for store in (Edits(), SimpleNamespace(text=lambda key: 1 / 0), None):
            script = self.turn(store)
            self.assertEqual(script.requests[0]["system"][0]["text"],
                             assistant_prompt.SYSTEM_PROMPT)

    def test_an_edited_search_budget_reminder_gets_the_turns_limit(self):
        script = Script(says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ხუნდი"]))),
                        says(use("search_catalog", search(["TOYOTA"], ["CAMRY"], ["ფილტრი"]))),
                        done(reply="რომელი?"), done())
        chat = service(script, assistant_max_tool_calls=1)
        chat.prompt_texts = Edits({"assistant.search_budget": "No more searches ({limit})."})
        run(chat, "Camry ხუნდები")
        sent = [block.get("text") for request in script.requests
                for message in request["messages"] if message["role"] == "user"
                for block in (message["content"] if isinstance(message["content"], list)
                              else []) if block.get("type") == "text"]
        self.assertIn("No more searches (1).", sent)

    def test_research_jobs_send_the_edited_instructions(self):
        for instructions, expected in ((lambda: "Edited research", "Edited research"),
                                       (None, RESEARCH_SYSTEM), (lambda: "  ", RESEARCH_SYSTEM),
                                       (lambda: 1 / 0, RESEARCH_SYSTEM)):
            backend = Backend()
            ResearchService(backend, cache_path=None, wait_seconds=5,
                            instructions=instructions).ask(dict(QUESTION))
            self.assertEqual(backend.seen, [expected])

    def test_the_research_service_reads_the_chat_services_edits(self):
        from automotive.assistant_extras import research_service

        backend = Backend()
        chat = SimpleNamespace(prompt_texts=Edits({"research.system": "Edited research"}))
        research_service(SimpleNamespace(assistant_research_cache_path=""), chat,
                         backend=backend).ask(dict(QUESTION))
        self.assertEqual(backend.seen, ["Edited research"])


class Prompts:
    """The dashboard's store with version checks, in memory."""

    configured = True

    def __init__(self) -> None:
        self.stored: dict[str, dict] = {}
        self.log: list[dict] = []
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise psycopg.errors.UndefinedTable("garage.llm_prompts")

    def rows(self) -> dict:
        self._check()
        return {key: dict(row) for key, row in self.stored.items()}

    def history(self, key: str) -> list[dict]:
        self._check()
        return [row for row in reversed(self.log) if row["prompt_key"] == key]

    def text(self, key: str) -> str | None:
        row = self.stored.get(key)
        return row["body"] if row else None

    def save(self, key, body, expected_version, actor) -> int:
        self._check()
        current = self.stored.get(key)
        if (current["version"] if current else 0) != expected_version:
            raise PromptConflict(key)
        version = expected_version + 1
        self.stored[key] = {"prompt_key": key, "body": body, "version": version,
                            "updated_at": "2026-11-21T18:00:00+04:00", "updated_by": actor}
        self.log.append({"prompt_key": key, "action": "saved", "body": body,
                         "version": version, "saved_by": actor})
        return version

    def reset(self, key, expected_version, actor) -> bool:
        self._check()
        current = self.stored.get(key)
        if current is None:
            return False
        if current["version"] != expected_version:
            raise PromptConflict(key)
        del self.stored[key]
        self.log.append({"prompt_key": key, "action": "reset", "body": "",
                         "version": expected_version, "saved_by": actor})
        return True


class PromptRouteTests(AdminCase):
    async def asyncSetUp(self) -> None:
        self.prompts = Prompts()
        await super().asyncSetUp()

    def serve(self, *args, **site):
        site.setdefault("prompts", self.prompts)
        return super().serve(*args, **site)

    async def test_admins_list_every_prompt_and_employees_are_refused(self):
        response = await self.call("GET", "/api/admin/prompts")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["store"], "ready")
        self.assertEqual([item["key"] for item in body["prompts"]], [p.key for p in PROMPTS])
        self.assertEqual({group["key"] for group in body["groups"]}, set(GROUPS))
        self.assertNotIn("default", body["prompts"][0])  # texts come one prompt at a time
        for path in ("/api/admin/prompts", "/api/admin/prompts/assistant.system"):
            self.assert_refused(await self.call("GET", path, who=self.employee), 403,
                                "ADMIN_ONLY")
        self.assert_refused(await self.call("PUT", "/api/admin/prompts/assistant.system",
                                            {"text": EDIT, "version": 0}, who=self.employee),
                            403, "ADMIN_ONLY")
        self.assertEqual(self.prompts.stored, {})

    async def test_one_prompt_comes_with_its_packaged_text(self):
        body = (await self.call("GET", "/api/admin/prompts/assistant.system")).json()
        self.assertEqual((body["text"], body["default"], body["edited"], body["version"]),
                         (assistant_prompt.SYSTEM_PROMPT, assistant_prompt.SYSTEM_PROMPT,
                          False, 0))
        self.assertTrue(body["editable"])
        self.assert_refused(await self.call("GET", "/api/admin/prompts/nope"), 404, "NOT_FOUND")

    async def test_save_reaches_the_store_and_is_audited_then_reset_restores_the_code(self):
        response = await self.call("PUT", "/api/admin/prompts/assistant.system",
                                   {"text": EDIT + "\r\n", "version": 0})
        self.assertEqual(response.json(), {"version": 1, "edited": True, "text": EDIT + "\n"})
        self.assertEqual(self.prompts.text("assistant.system"), EDIT + "\n")
        self.assertEqual((self.repo.audits[-1]["action"], self.repo.audits[-1]["target"],
                          self.repo.audits[-1]["detail"]),
                         ("prompt.saved", "assistant.system", {"chars": len(EDIT) + 1}))
        body = (await self.call("GET", "/api/admin/prompts/assistant.system")).json()
        self.assertEqual((body["text"], body["edited"], body["version"], body["updated_by"]),
                         (EDIT + "\n", True, 1, "boss"))
        self.assertEqual([row["action"] for row in body["history"]], ["saved"])
        stale = await self.call("PUT", "/api/admin/prompts/assistant.system",
                                {"text": "other", "version": 0})
        self.assert_refused(stale, 409, "VERSION_CONFLICT")
        reset = await self.call("POST", "/api/admin/prompts/assistant.system/reset",
                                {"version": 1})
        self.assertEqual(reset.json(), {"version": 0, "edited": False,
                                        "text": assistant_prompt.SYSTEM_PROMPT})
        self.assertIsNone(self.prompts.text("assistant.system"))
        self.assertEqual((self.repo.audits[-1]["action"], self.repo.audits[-1]["target"]),
                         ("prompt.reset", "assistant.system"))

    async def test_saving_the_packaged_text_keeps_no_edit(self):
        await self.call("PUT", "/api/admin/prompts/assistant.nudge",
                        {"text": "Call finish.", "version": 0})
        response = await self.call("PUT", "/api/admin/prompts/assistant.nudge",
                                   {"text": assistant_prompt.NUDGE, "version": 1})
        self.assertEqual(response.json()["edited"], False)
        self.assertNotIn("assistant.nudge", self.prompts.stored)

    async def test_blank_long_and_read_only_saves_are_refused(self):
        path = "/api/admin/prompts/assistant.system"
        self.assert_refused(await self.call("PUT", path, {"text": "  \n", "version": 0}),
                            422, "INVALID_FIELDS", {"text": "REQUIRED"})
        self.assert_refused(await self.call("PUT", path, {"text": "x" * (MAX_CHARS + 1),
                                                          "version": 0}),
                            422, "INVALID_FIELDS", {"text": "TOO_LONG"})
        self.assert_refused(await self.call("PUT", path, {"text": EDIT, "version": -1}),
                            422, "INVALID_FIELDS", {"version": "INVALID"})
        self.assert_refused(await self.call("PUT", "/api/admin/prompts/knowledge.judge",
                                            {"text": EDIT, "version": 0}),
                            409, "PROMPT_READ_ONLY")
        self.assertEqual(self.prompts.stored, {})

    async def test_before_the_migration_the_page_lists_the_code_and_saving_says_so(self):
        self.prompts.down = True
        body = (await self.call("GET", "/api/admin/prompts")).json()
        self.assertEqual(body["store"], "unavailable")
        detail = (await self.call("GET", "/api/admin/prompts/assistant.system")).json()
        self.assertEqual((detail["text"], detail["history"]),
                         (assistant_prompt.SYSTEM_PROMPT, []))
        self.assert_refused(await self.call("PUT", "/api/admin/prompts/assistant.system",
                                            {"text": EDIT, "version": 0}),
                            503, "PROMPTS_UNAVAILABLE")

    async def test_the_site_hands_its_store_to_the_chat_service(self):
        chat = SimpleNamespace()
        self.serve(chat_service=chat)
        self.assertIs(chat.prompt_texts, self.prompts)


class MessengerWiringTests(unittest.TestCase):
    def test_the_messenger_engine_reads_edits_with_the_workers_own_login(self):
        source = (ROOT / "automotive/messenger_engine.py").read_text(encoding="utf-8")
        start = source.index("def build_engine(")
        body = source[start:source.index("\ndef ", start + 1)]
        self.assertIn('chat_service.prompt_texts = PromptStore(MetaDatabase(\n'
                      '        getattr(meta_settings, "database_url", "")', body)
        # Before the extras, whose research service reads the same store.
        self.assertLess(body.index("chat_service.prompt_texts"),
                        body.index("chat_service.assistant_extras = extras_for"))


if __name__ == "__main__":
    unittest.main()
