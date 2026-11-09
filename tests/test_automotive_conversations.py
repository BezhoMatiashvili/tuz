"""Conversation lifetime, privacy, concurrency, API validation and cancellation."""

from __future__ import annotations

import asyncio
import time
import unittest
from threading import Event
from types import SimpleNamespace

import httpx

from automotive.api import create_app
from automotive.config import Settings
from automotive.conversations import ConversationError, ConversationStore


class Engine:
    settings = Settings(anthropic_api_key="fixture-private")

    def __init__(self):
        self.calls = []
        self.started = Event()
        self.release = Event()
        self.release.set()

    def respond_conversation(self, message, **kwargs):
        self.calls.append((message, kwargs))
        kwargs["progress"]({"cards": [self.card("partial")]})
        self.started.set()
        self.release.wait(2)
        return {"status": "OK", "answer": "Found catalog candidates.",
                "vehicle": kwargs["vehicle"] or {"make": "Example", "model": "Car",
                                               "model_year": 2013, "engine_code": "E1"},
                "cards": [self.card(str(i)) for i in range(25)],
                "warnings": [], "supplier_only": []}

    @staticmethod
    def card(identifier):
        return {"product": {"source_product_id": identifier, "product_code": "AP104/2",
                            "private_column": "must not serialize", "stock": None},
                "match_source": "EXACT_CODE", "fitment_status": "UNKNOWN"}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.now = 100.0
        self.store = ConversationStore(self.engine, None, clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def finish(self, conversation, turn):
        for _ in range(200):
            result = self.store.get(conversation, turn)
            if result["status"] != "RUNNING":
                return result
            time.sleep(0.005)
        self.fail("Synthetic turn did not finish")

    def test_page_history_bound_and_vin_redaction(self):
        cid = self.store.create()["conversation_id"]
        for i in range(14):
            message = ("front brake pads 1HGCM82633A004352 fixture-private"
                       if i == 0 else "rear ones")
            turn = self.store.submit(cid, message, language="en")
            result = self.finish(cid, turn["turn_id"])
        self.assertEqual(result["total"], 25)
        self.assertEqual(len(result["cards"]), 20)
        self.assertEqual(result["next_offset"], 20)
        second = self.store.get(cid, turn["turn_id"], 20)
        self.assertEqual(len(second["cards"]), 5)
        self.assertNotIn("1HGCM82633A004352", str(self.engine.calls[-1][1]["history"]))
        self.assertNotIn("fixture-private", str(self.engine.calls[-1][1]["history"]))
        self.assertEqual(len(self.store._conversations[cid]["history"]), 12)
        self.assertEqual(len(self.store._conversations[cid]["turns"]), 12)
        self.assertIn("rear", self.engine.calls[-1][1]["category"])
        self.assertNotIn("front", self.engine.calls[-1][1]["category"])

    def test_one_active_turn_and_delete_rejects_late_result(self):
        self.engine.release.clear()
        cid = self.store.create()["conversation_id"]
        turn = self.store.submit(cid, "AP104/2")
        self.assertTrue(self.engine.started.wait(1))
        with self.assertRaises(ConversationError) as caught:
            self.store.submit(cid, "again")
        self.assertEqual(caught.exception.status, 409)
        self.store.delete(cid)
        self.engine.release.set()
        with self.assertRaises(ConversationError):
            self.store.get(cid, turn["turn_id"])
        self.assertTrue(self.engine.calls[0][1]["cancelled"]())

    def test_vehicle_change_invalidates_pending_turn_and_history(self):
        self.engine.release.clear()
        cid = self.store.create()["conversation_id"]
        first = self.store.submit(cid, "front pads", vin="1HGCM82633A004352")
        self.assertTrue(self.engine.started.wait(1))
        second = self.store.submit(cid, "oil", vehicle={"make": "Changed"})
        self.engine.release.set()
        self.finish(cid, second["turn_id"])
        with self.assertRaises(ConversationError):
            self.store.get(cid, first["turn_id"])
        self.assertTrue(self.engine.calls[0][1]["cancelled"]())
        self.assertIsNone(self.engine.calls[-1][1]["vin"])
        self.assertEqual(self.engine.calls[-1][1]["history"], [])

    def test_partial_vehicle_context_does_not_reset_followups(self):
        cid = self.store.create()["conversation_id"]
        first = self.store.submit(cid, "pads")
        self.finish(cid, first["turn_id"])
        second = self.store.submit(cid, "rear ones", vehicle={"make": "Example", "model": "Car"})
        self.finish(cid, second["turn_id"])
        self.assertEqual(len(self.engine.calls[-1][1]["history"]), 1)

    def test_engine_clarification_preserves_requested_category(self):
        cid = self.store.create()["conversation_id"]
        first = self.store.submit(cid, "engine oil")
        self.finish(cid, first["turn_id"])
        second = self.store.submit(cid, "engine code B48", vehicle={"engine_code": "B48"})
        self.finish(cid, second["turn_id"])
        self.assertEqual(self.engine.calls[-1][1]["category"], "engine oil")
        self.assertEqual(self.engine.calls[-1][1]["vehicle"]["engine_code"], "B48")

    def test_identity_change_discards_old_engine_and_repeated_old_vin(self):
        cid = self.store.create()["conversation_id"]
        first = self.store.submit(cid, "pads", vin="1HGCM82633A004352")
        self.finish(cid, first["turn_id"])
        second = self.store.submit(cid, "oil", vin="1HGCM82633A004352",
                                   vehicle={"make": "Changed"})
        self.finish(cid, second["turn_id"])
        self.assertEqual(self.engine.calls[-1][1]["vehicle"], {"make": "Changed"})
        self.assertIsNone(self.engine.calls[-1][1]["vin"])

    def test_initial_vehicle_payload_redacts_secrets_and_vin(self):
        self.engine.release.clear()
        cid = self.store.create()["conversation_id"]
        turn = self.store.submit(cid, "hi", vin="1HGCM82633A004352", vehicle={
            "make": "fixture-private", "model": "1HGCM82633A004352",
        })
        payload = self.store.get(cid, turn["turn_id"])
        self.assertNotIn("fixture-private", str(payload))
        self.assertNotIn("1HGCM82633A004352", str(payload))
        self.engine.release.set()

    def test_followup_keeps_axle_and_replaces_case_insensitively(self):
        self.assertEqual(self.store._category("pads", "rear brake discs"), "pads rear")
        self.assertNotIn("front", self.store._category("rear ones", "Front brake pads").lower())
        self.assertEqual(self.store._category("Find AP104/2", "rear brake pads"), "")
        self.assertEqual(self.store._category("engine code B48", "engine oil"), "engine oil")

    def test_idle_expiry_capacity_and_isolation(self):
        ids = [self.store.create()["conversation_id"] for _ in range(20)]
        self.assertEqual(len(set(ids)), 20)
        with self.assertRaises(ConversationError) as caught:
            self.store.create()
        self.assertEqual(caught.exception.status, 429)
        turn = self.store.submit(ids[0], "hello")
        self.finish(ids[0], turn["turn_id"])
        with self.assertRaises(ConversationError):
            self.store.get(ids[1], turn["turn_id"])
        self.now += 1801
        with self.assertRaises(ConversationError):
            self.store.get(ids[0], turn["turn_id"])
        self.assertEqual(len(self.store._conversations), 0)
        self.store.create()

    def test_deadline_preserves_partial_and_blocks_late_publish(self):
        self.engine.release.clear()
        cid = self.store.create()["conversation_id"]
        turn = self.store.submit(cid, "pads")
        self.assertTrue(self.engine.started.wait(1))
        self.now += 181
        result = self.store.get(cid, turn["turn_id"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["cards"][0]["product"]["source_product_id"], "partial")
        self.assertIn("TURN_DEADLINE", result["warnings"])
        self.engine.release.set()

    def test_conflicting_vins_rejected_before_work(self):
        cid = self.store.create()["conversation_id"]
        with self.assertRaises(ConversationError) as caught:
            self.store.submit(cid, "1HGCM82633A004352", vin="WVWZZZ1JZXW000001")
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.engine.calls, [])


class ConversationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoints_validation_pagination_and_serialization(self):
        engine = Engine()
        app = create_app(settings=Settings(), repository=SimpleNamespace(),
                         vin_service=SimpleNamespace(), chat_service=engine,
                         search_service=SimpleNamespace())
        self.addCleanup(app.state.conversations.close)
        async def pulse():
            while True:
                await asyncio.sleep(0.005)
        heartbeat = asyncio.create_task(pulse())
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://localhost:8767") as client:
                created = await client.post("/api/v1/chat/conversations/")
                self.assertEqual(created.status_code, 201)
                cid = created.json()["conversation_id"]
                base = "/api/v1/chat/conversations/" + cid
                invalid = await client.post(base + "/messages", json={"message": "hi", "sql": "x"})
                self.assertEqual(invalid.status_code, 422)
                posted = await client.post(base + "/messages", json={"message": "AP104/2"})
                self.assertEqual(posted.status_code, 202)
                tid = posted.json()["turn_id"]
                for _ in range(100):
                    reply = await client.get(base + "/turns/" + tid + "?offset=20&limit=10")
                    if reply.json()["status"] != "RUNNING":
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(reply.status_code, 200)
                self.assertEqual(len(reply.json()["cards"]), 5)
                self.assertNotIn("private_column", reply.text)
                self.assertIsNone(reply.json()["cards"][0]["product"]["stock"])
                self.assertEqual((await client.delete(base)).status_code, 204)
                self.assertEqual((await client.get(base + "/turns/" + tid)).status_code, 404)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
