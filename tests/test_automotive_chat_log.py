"""Local chat log: redaction, persistence, cancellation, retention and failure isolation."""

from __future__ import annotations

import io
import json
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event

from automotive.chat_log import ChatLogger
from automotive.config import Settings
from automotive.conversations import ConversationStore

VIN = "1HGCM82633A004352"


class Engine:
    settings = Settings(anthropic_api_key="fixture-private")

    def __init__(self) -> None:
        self.release = Event()
        self.release.set()

    def respond_conversation(self, message, **kwargs):
        self.release.wait(2)
        return {"status": "OK", "answer": "Found catalog candidates.", "category": "brake pads",
                "vehicle": {"make": "Example", "model": "Car", "model_year": 2013},
                "cards": [{"product": {"product_code": "AP104/2", "source_product_id": "7",
                                       "private_column": "must not be logged"},
                           "match_source": "EXACT_CODE", "fitment_status": "UNKNOWN"}],
                "supplier_only": [], "warnings": [], "usage": {"input_tokens": 12},
                "diagnostics": {"rejections": {"WRONG_AXLE": 2}}}


class ChatLogTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "logs" / "chat.sqlite3"
        self.log = ChatLogger(self.path, hmac_key="k" * 32)
        self.addCleanup(self.log.close)
        self.engine = Engine()
        self.store = ConversationStore(self.engine, None, chat_log=self.log)
        self.addCleanup(self.store.close)

    def finish(self, cid: str, turn: str) -> None:
        for _ in range(200):
            if self.store.get(cid, turn)["status"] != "RUNNING":
                break
            time.sleep(0.005)
        # The record is queued after the turn is published; wait for the worker to finish.
        for _ in range(200):
            if not self.store._outstanding:
                break
            time.sleep(0.005)
        self.log.flush()

    def rows(self, query: str) -> list[tuple]:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(query).fetchall()
        finally:
            connection.close()

    def test_turn_is_logged_redacted_and_without_capabilities(self) -> None:
        cid = self.store.create()["conversation_id"]
        turn = self.store.submit(cid, f"front brake pads {VIN} fixture-private", language="en")
        self.finish(cid, turn["turn_id"])
        self.store.delete(cid)
        self.log.flush()
        (row,) = self.rows("SELECT question, answer, status, category, vin_hash, language,"
                           " cards_json, usage_json, diagnostics_json FROM turns")
        question, answer, status, category, vin_hash, language, cards, usage, diag = row
        self.assertIn("[VIN]", question)
        self.assertNotIn(VIN, question)
        self.assertNotIn("fixture-private", question)
        self.assertEqual((answer, status, category, language),
                         ("Found catalog candidates.", "COMPLETE", "brake pads", "en"))
        self.assertEqual(len(vin_hash), 24)
        self.assertEqual(json.loads(cards), [{"product_code": "AP104/2", "source_product_id": "7",
                                              "match_source": "EXACT_CODE",
                                              "fitment_status": "UNKNOWN"}])
        self.assertEqual(json.loads(usage), {"input_tokens": 12})
        self.assertEqual(json.loads(diag), {"rejections": {"WRONG_AXLE": 2}})
        dump = "\n".join(item.read_bytes().decode("latin-1") for item in self.path.parent.iterdir())
        for secret in (VIN, cid, "private_column", "fixture-private"):
            self.assertNotIn(secret, dump)
        self.assertEqual(self.rows("SELECT end_reason FROM sessions"), [("deleted",)])

    def test_turn_index_and_vin_grouping(self) -> None:
        cid = self.store.create()["conversation_id"]
        for message in ("front pads", "rear ones"):
            turn = self.store.submit(cid, message, vin=VIN, language="ka")
            self.finish(cid, turn["turn_id"])
        self.assertEqual(self.rows("SELECT turn_index FROM turns ORDER BY id"), [(1,), (2,)])
        self.assertEqual(len(self.rows("SELECT DISTINCT vin_hash FROM turns")), 1)

    def test_cancelled_turn_is_logged(self) -> None:
        self.engine.release.clear()
        cid = self.store.create()["conversation_id"]
        self.store.submit(cid, "front pads", language="en")
        self.store.delete(cid)
        self.engine.release.set()
        for _ in range(200):
            self.log.flush()
            if self.rows("SELECT status FROM turns"):
                break
            time.sleep(0.01)
        self.assertEqual(self.rows("SELECT status FROM turns"), [("CANCELLED",)])

    def test_no_vin_hash_without_key(self) -> None:
        unkeyed = ChatLogger(self.path.with_name("unkeyed.sqlite3"))
        self.addCleanup(unkeyed.close)
        self.assertIsNone(unkeyed.vin_hash(VIN))

    def test_file_and_directory_are_private(self) -> None:
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_export_jsonl(self) -> None:
        cid = self.store.create()["conversation_id"]
        self.finish(cid, self.store.submit(cid, "front pads", language="en")["turn_id"])
        stream = io.StringIO()
        self.assertEqual(self.log.export_jsonl(stream), 1)
        item = json.loads(stream.getvalue())
        self.assertEqual(item["question"], "front pads")
        self.assertEqual(item["cards"][0]["product_code"], "AP104/2")
        self.assertNotIn("cards_json", item)

    def test_retention_prunes_old_turns_on_open(self) -> None:
        cid = self.store.create()["conversation_id"]
        self.finish(cid, self.store.submit(cid, "front pads", language="en")["turn_id"])
        self.log.close()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE turns SET started_at = started_at - 10 * 86400")
        connection.execute("UPDATE sessions SET started_at = started_at - 10 * 86400")
        connection.commit()
        connection.close()
        reopened = ChatLogger(self.path, retention_days=7)
        self.addCleanup(reopened.close)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM turns"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions"), [(0,)])

    def test_disabled_and_unwritable_locations_never_raise(self) -> None:
        self.assertIsNone(ChatLogger.from_settings(Settings()))
        blocker = self.path.parent / "file"
        blocker.write_text("x")
        self.assertIsNone(ChatLogger.from_settings(
            Settings(chat_log_path=str(blocker / "nested" / "chat.sqlite3"))))

    def test_logger_failure_does_not_break_a_turn(self) -> None:
        class Broken:
            def session_started(self, *_):
                raise RuntimeError("boom")

            def session_ended(self, *_):
                raise RuntimeError("boom")

            def turn(self, *_, **__):
                raise RuntimeError("boom")

        store = ConversationStore(self.engine, None, chat_log=Broken())
        self.addCleanup(store.close)
        cid = store.create()["conversation_id"]
        turn = store.submit(cid, "front pads", language="en")
        for _ in range(200):
            result = store.get(cid, turn["turn_id"])
            if result["status"] != "RUNNING":
                break
            time.sleep(0.005)
        self.assertEqual(result["status"], "COMPLETE")
        store.delete(cid)


if __name__ == "__main__":
    unittest.main()
