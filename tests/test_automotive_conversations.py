"""Conversation lifetime, privacy, concurrency, API validation and cancellation."""

from __future__ import annotations

import asyncio
import time
import unittest
from threading import Event
from types import SimpleNamespace

import httpx

from automotive.api import ConversationTurn, VehicleConfiguration, create_app
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

    def test_ai_state_survives_followup_and_revision_rejects_old_progress(self):
        self.engine.settings = Settings(chat_orchestration=True)
        self.engine.respond_conversation = lambda message, **kwargs: (
            self.engine.calls.append((message, kwargs)) or {
                "status": "CLARIFICATION", "answer": "Petrol or diesel?", "cards": [],
                "vehicle": {"make": "MERCEDES-BENZ", "model": "E-Class", "model_year": 2015,
                            "displacement_l": 3.0},
                "vehicle_revision": kwargs["dialogue"]["vehicle_revision"] + 1,
                "customer_stated": {"make": {"value": "MERCEDES-BENZ", "text": "მერსედესსზე"}},
                "resolution_candidates": [{"engine_code": "M276", "fuel": "Gasoline"}],
                "selected_configuration": {}, "unresolved_fields": ["fuel"],
                "clarification_choices": ["Petrol", "Diesel"],
                "pending": {"act": "clarify_engine", "question": "Petrol or diesel?"},
            })
        cid = self.store.create()["conversation_id"]
        first = self.store.submit(cid, "მერსედესსზე 2015 ზეთის შეცვლა")
        result = self.finish(cid, first["turn_id"])
        self.assertEqual(result["vehicle_revision"], 1)
        second = self.store.submit(cid, "petrol")
        self.finish(cid, second["turn_id"])
        state = self.engine.calls[-1][1]["dialogue"]
        self.assertEqual(state["vehicle_revision"], 1)
        self.assertEqual(state["unresolved_fields"], ["fuel"])
        self.assertEqual(state["customer_stated"]["make"]["text"], "მერსედესსზე")
        self.assertEqual(state["pending"]["question"], "Petrol or diesel?")

    def test_ai_correction_supersedes_active_turn_without_old_result(self):
        self.engine.settings = Settings(chat_orchestration=True)
        self.engine.release.clear()
        cid = self.store.create()["conversation_id"]
        first = self.store.submit(cid, "2015 Mercedes E-Class oil")
        self.assertTrue(self.engine.started.wait(1))
        second = self.store.submit(cid, "Actually diesel")
        first_kwargs = self.engine.calls[0][1]
        first_kwargs["progress"]({"vehicle_revision": 0, "vehicle": {"make": "OLD"},
                                  "cards": [self.engine.card("obsolete")]})
        self.engine.release.set()
        result = self.finish(cid, second["turn_id"])
        self.assertNotIn("obsolete", str(result))
        self.assertTrue(first_kwargs["cancelled"]())
        with self.assertRaises(ConversationError):
            self.store.get(cid, first["turn_id"])
        self.assertIn("2015 Mercedes", self.engine.calls[-1][1]["history"][-1]["question"])

    def test_default_turn_deadline_is_thirty_seconds(self):
        cid = self.store.create()["conversation_id"]
        turn = self.store.submit(cid, "hello")
        stored = self.store._conversations[cid]["turns"][turn["turn_id"]]
        self.assertEqual(stored["deadline"] - stored["started"], 30)

    def test_older_revision_cannot_replace_newer_progress_for_same_turn(self):
        self.engine.release.clear()
        cid = self.store.create()["conversation_id"]
        submitted = self.store.submit(cid, "oil")
        self.assertTrue(self.engine.started.wait(1))
        publish = self.engine.calls[0][1]["progress"]
        publish({"vehicle_revision": 3, "vehicle": {"make": "NEW"}, "cards": []})
        publish({"vehicle_revision": 2, "vehicle": {"make": "OLD"},
                 "cards": [self.engine.card("obsolete")]})
        result = self.store.get(cid, submitted["turn_id"])
        self.assertEqual(result["vehicle"], {"make": "NEW"})
        self.assertEqual(result["vehicle_revision"], 3)
        self.assertNotIn("obsolete", str(result))
        self.engine.release.set()

    def test_configuration_and_response_preserve_displacement_and_references(self):
        from pydantic import ValidationError

        vehicle = VehicleConfiguration(displacement_l=3.0, fuel_type="Diesel", trim="E 350")
        self.assertEqual(vehicle.model_dump()["displacement_l"], 3.0)
        for size in (0, 21, "3.0"):
            with self.assertRaises(ValidationError):
                VehicleConfiguration(displacement_l=size)
        result = ConversationTurn(
            conversation_id="c", turn_id="t", status="COMPLETE", vehicle_revision=4,
            resolution_candidates=[{"engine_code": "OM642"}],
            clarification_choices=["Diesel", "Petrol"],
            claims=[{"claim_id": "c1", "text": "Source assertion"}],
            citations=[{"url": "https://example.com/catalog"}],
        ).model_dump()
        self.assertEqual(result["vehicle_revision"], 4)
        self.assertEqual(result["claims"][0]["claim_id"], "c1")
        self.assertEqual(result["citations"][0]["url"], "https://example.com/catalog")

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
        self.assertEqual(self.store._category("Do you have 5W-30?", "front brake pads"),
                         "engine oil Do you have 5W-30?")

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

    def test_small_talk_keeps_the_request_and_never_searches_again(self):
        cid = self.store.create()["conversation_id"]
        self.finish(cid, self.store.submit(cid, "front brake pads", language="en")["turn_id"])
        self.finish(cid, self.store.submit(cid, "thanks!", language="auto")["turn_id"])
        message, kwargs = self.engine.calls[-1]
        self.assertEqual(kwargs["dialogue"]["small_talk"], "thanks")
        self.assertEqual(self.store._conversations[cid]["category"], "front brake pads")
        self.assertEqual(len(kwargs["dialogue"]["last_cards"]), 20)  # Shown cards, compact.
        self.assertEqual(set(kwargs["dialogue"]["last_cards"][0]),
                         {"id", "brand", "code", "basis", "fitment_status", "matched_via"})

    def test_a_typed_car_answers_the_question_and_keeps_the_part(self):
        self.engine.respond_conversation = lambda message, **kwargs: (
            self.engine.calls.append((message, kwargs)) or {
                "status": "CLARIFICATION", "answer": "Which car?", "vehicle": kwargs["vehicle"],
                "cards": [], "pending": {"act": "clarify_year", "offer": None}})
        cid = self.store.create()["conversation_id"]
        self.finish(cid, self.store.submit(cid, "cabin filter for a Honda Accord")["turn_id"])
        self.assertEqual(self.engine.calls[-1][1]["vehicle"],
                         {"make": "HONDA", "model": "Accord"})
        self.finish(cid, self.store.submit(cid, "2015")["turn_id"])
        message, kwargs = self.engine.calls[-1]
        self.assertEqual(kwargs["vehicle"], {"make": "HONDA", "model": "Accord",
                                             "model_year": 2015})
        self.assertEqual(kwargs["category"], "cabin filter for a Honda Accord")
        self.assertTrue(kwargs["dialogue"]["typed_vehicle"])

    def test_a_typed_mercedes_reaches_the_search_as_class_and_trim(self):
        self.engine.respond_conversation = lambda message, **kwargs: (
            self.engine.calls.append((message, kwargs)) or {
                "status": "CLARIFICATION", "answer": "Engine?", "vehicle": kwargs["vehicle"],
                "cards": [], "pending": {"act": "clarify_engine", "offer": None}})
        cid = self.store.create()["conversation_id"]
        self.finish(cid, self.store.submit(
            cid, "Mercedes Benz E400 2015 ზეთის შეცვლა მინდა")["turn_id"])
        self.assertEqual(self.engine.calls[-1][1]["vehicle"],
                         {"make": "MERCEDES-BENZ", "model": "E-Class", "model_year": 2015,
                          "trim": "E 400"})
        # An engine size answers the engine question; the car and the request are kept.
        self.finish(cid, self.store.submit(cid, "3.0 2 ტურბინიანი ძრავია")["turn_id"])
        message, kwargs = self.engine.calls[-1]
        self.assertEqual(kwargs["vehicle"]["displacement_l"], "3.0")
        self.assertEqual(kwargs["vehicle"]["trim"], "E 400")
        self.assertIn("ზეთის შეცვლა", kwargs["category"])
        # Another trim of the same model drops the engine details of the previous one.
        self.finish(cid, self.store.submit(cid, "Mercedes E350 2015")["turn_id"])
        self.assertEqual(self.engine.calls[-1][1]["vehicle"],
                         {"make": "MERCEDES-BENZ", "model": "E-Class", "model_year": 2015,
                          "trim": "E 350"})

    def test_a_georgian_description_then_a_tapped_engine_choice(self):
        options = [{"trim": "E 320", "fuel": "Gasoline"},
                   {"trim": "E 350 BlueTEC", "fuel": "Diesel"},
                   {"trim": "E 400", "fuel": "Gasoline"}]
        self.engine.respond_conversation = lambda message, **kwargs: (
            self.engine.calls.append((message, kwargs)) or {
                "status": "CLARIFICATION", "answer": "Which engine?",
                "vehicle": kwargs["vehicle"], "cards": [],
                "pending": {"act": "clarify_engine", "offer": None, "options": options}})
        cid = self.store.create()["conversation_id"]
        self.finish(cid, self.store.submit(
            cid, "მერსედესისზე ე კლასზე კუპეზე 2015 წლიანზე 3.0 ძრავზე მინდა ზეთის შეცვლა"
        )["turn_id"])
        self.assertEqual(self.engine.calls[-1][1]["vehicle"], {
            "make": "MERCEDES-BENZ", "model": "E-Class", "model_year": 2015, "body": "Coupe",
            "displacement_l": "3.0"})
        self.finish(cid, self.store.submit(cid, "E 400 (ბენზინი)")["turn_id"])
        message, kwargs = self.engine.calls[-1]
        self.assertEqual(kwargs["vehicle"]["trim"], "E 400")
        self.assertEqual(kwargs["vehicle"]["fuel"], "Gasoline")
        self.assertIn("ზეთის შეცვლა", kwargs["category"])

    def test_yes_accepts_the_offer_and_language_stays_for_codes(self):
        self.engine.respond_conversation = lambda message, **kwargs: (
            self.engine.calls.append((message, kwargs)) or {
                "status": "OK", "answer": "Found.", "vehicle": {}, "cards": [],
                "pending": {"act": "results", "offer": {"category": "rear brake pads"}}})
        cid = self.store.create()["conversation_id"]
        self.finish(cid, self.store.submit(cid, "წინა ხუნდები", language="auto")["turn_id"])
        self.finish(cid, self.store.submit(cid, "კი", language="auto")["turn_id"])
        message, kwargs = self.engine.calls[-1]
        self.assertEqual(kwargs["category"], "rear brake pads")
        self.assertIsNone(kwargs["dialogue"]["small_talk"])
        turn = self.store.submit(cid, "AP104/2", language="auto")
        self.assertEqual(turn["language"], "ka")

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

    async def test_reply_note_quick_replies_and_language_reach_the_browser(self):
        engine = Engine()
        engine.respond_conversation = lambda message, **kwargs: {
            "status": "CLARIFICATION", "act": "clarify_axle", "answer": "წინა თუ უკანა?",
            "note": "", "quick_replies": ["წინა", "უკანა"], "language": kwargs["language"],
            "vehicle": {}, "cards": [], "pending": {"act": "clarify_axle", "offer": None},
            "facts": {"internal": True}}
        app = create_app(settings=Settings(), repository=SimpleNamespace(),
                         vin_service=SimpleNamespace(), chat_service=engine,
                         search_service=SimpleNamespace())
        self.addCleanup(app.state.conversations.close)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost:8767") as client:
            cid = (await client.post("/api/v1/chat/conversations")).json()["conversation_id"]
            base = "/api/v1/chat/conversations/" + cid
            posted = await client.post(base + "/messages", json={"message": "ხუნდები"})
            self.assertEqual(posted.json()["language"], "ka")
            for _ in range(100):
                reply = (await client.get(base + "/turns/" + posted.json()["turn_id"])).json()
                if reply["status"] != "RUNNING":
                    break
                await asyncio.sleep(0.01)
        self.assertEqual((reply["act"], reply["quick_replies"], reply["language"]),
                         ("clarify_axle", ["წინა", "უკანა"], "ka"))
        self.assertEqual(reply["note"], "")
        self.assertNotIn("pending", reply)  # Internal dialogue state stays on the server.
        self.assertNotIn("facts", reply)
