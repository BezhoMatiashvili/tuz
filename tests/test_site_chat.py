"""Public chat facade: what a visitor may see, the caps in front of the store, the routes."""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from automotive import chat_contract, stock
from automotive.config import Settings
from automotive.conversations import ConversationError, ConversationStore
from automotive.site_catalog import Catalog
from automotive.site_chat import (
    AVAILABILITY,
    REASONS,
    PublicChat,
    card_codes,
    plain,
    public_card,
    public_turn,
    without_codes,
)
from automotive.site_content import DEFAULT_SECTIONS
from automotive.site_guard import DailyBudget
from automotive.site_strings import STRINGS
from automotive.vin_search import VINSearchCoordinator
from tests.test_site_catalog import ROWS, row
from tests.test_site_guard import Clock, RecordingBuckets, SiteAppCase

TURN_KEYS = {"status", "answer", "note", "language", "car", "question", "suggestions",
             "quick_replies", "groups", "cards", "total", "offset", "next_offset", "checked",
             "checking"}
CARD_KEYS = {"name", "fits", "brand", "sap_code", "price", "availability", "group", "reason",
             "confidence"}
FORBIDDEN_KEYS = {"product_code", "source_product_id", "stock", "usage", "results",
                  "matched_codes", "supplier_only", "claims", "citations", "product",
                  "vehicle", "warnings", "progress", "conversation_id", "turn_id",
                  "original_code", "duplicate_code", "source_fingerprint", "basis",
                  "matched_via", "requirements", "knowledge", "engine_status"}
PRIVATE_VALUES = ("sid-0001", "04465", "private-", "f" * 64, "1HGCM82633A004352",
                  "2AR-FE")
A, B, C = "203.0.113.7", "198.51.100.9", "192.0.2.50"


def card(sid: str = "sid-0001", status: str = "IN_STORE", **product) -> dict:
    """A staff conversation card, with everything the staff workspace is shown."""
    return {
        "product": {"source_product_id": sid, "source_fingerprint": "f" * 64,
                    "product_code": "ZQ-1234", "product_name": "სამუხრუჭე ხუნდი წინა",
                    "product_model": "TOYOTA CAMRY 2012-2017", "original_code": "04465-33450",
                    "duplicate_code": "", "brand": "SATO", "price": 999.0, "currency": "GEL",
                    "stock": {"store": 3.0, "tuz11": 0.0}, "status": "IN_STORE",
                    "matched_codes": ["ZQ1234"], **product},
        "match_source": "EXACT_CODE", "fitment_status": "VERIFIED", "compatible": True,
        "availability": "private-availability-text: 3 in the store",
        "availability_status": status, "basis": "private-basis 04465-33450",
        "matched_via": ["04465-33450"], "pack_litres": None, "role": None,
        "warnings": ["private-card-warning"]}


def staff_turn(**changes) -> dict:
    """A finished staff turn as ConversationStore.get returns it, private fields included."""
    return {
        "conversation_id": "c" * 43, "turn_id": "t" * 32, "status": "COMPLETE",
        "engine_status": "OK", "act": "offer", "answer": "ნაპოვნია ერთი ნაწილი.",
        "note": "ფასი დაზუსტებულია.", "quick_replies": ["უკანა ხუნდები"], "language": "ka",
        "vehicle": {"make": "TOYOTA", "engine_code": "2AR-FE"}, "vehicle_revision": 2,
        "resolution_candidates": [{"engine_code": "2AR-FE"}],
        "selected_configuration": {"engine_code": "2AR-FE"}, "clarification_choices": [],
        "claims": [{"text": "private-claim"}],
        "citations": [{"url": "https://private-citation.example"}],
        "progress": [{"source_id": "warehouse", "label": "private-source", "status": "COMPLETE"},
                     {"source_id": "motorium", "status": "RUNNING"},
                     {"source_id": "fuchs", "status": "PENDING"}, {"label": "no source id"},
                     "not a dict"],
        "cards": [card()], "supplier_only": [{"mpn": "private-supplier"}],
        "results": [{"tool": "private-result"}], "warnings": ["private-warning"],
        "usage": {"input_tokens": 1234, "output_tokens": 56},
        "requirements": {"oil": "private-requirement"}, "knowledge": [{"k": "private-fact"}],
        "total": 1, "offset": 0, "next_offset": None, **changes}


CAMRY = {"make": "TOYOTA", "model": "Camry", "model_year": 2015, "trim": "XLE",
         "engine_code": "2AR-FE", "displacement_l": 2.5, "vin": "4T1BF1FK5FU000001"}
PAD_CARD = {"name": "სამუხრუჭე ხუნდი წინა", "fits": "TOYOTA CAMRY 2012-2017", "brand": "SATO",
            "price": 45.5, "sap_code": "ZQ-1234"}


def engine_turn(**changes) -> dict:
    """A finished turn from an engine that states its question, suggestions and groups."""
    front = card()
    front.update(group="front", confidence="confirmed", evidence="private-evidence 04465",
                 reason="ემთხვევა ამ მანქანის კატალოგის ჩანაწერს (ZQ-1234).")
    rear = card("sid-0004", "WITHIN_1_2_HOURS", product_code="ZQ-1234B")
    rear.update(group="rear", confidence="listed", evidence="private-evidence",
                reason="CAMRY 2012-2017 მოიცავს 2015 წელს.")
    return staff_turn(**{
        "car_label": "Toyota Camry 2015", "outcome": "offered", "question": None,
        "suggestions": [{"label": "ზეთის ფილტრიც", "value": "ზეთის ფილტრი მინდა",
                         "meaning": "private-meaning"}],
        "groups": [{"id": "front", "label": "წინა ხუნდები", "count": 1},
                   {"id": "rear", "label": "უკანა ხუნდები", "count": 1}],
        "cards": [front, rear], "total": 2, "quick_replies": [],
        "memory": {"notes": "private-notes"}, **changes})


def keys_in(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(keys_in(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_in(item) for item in value))
    return set()


class WhitelistTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.from_rows(ROWS)

    def test_a_turn_leaves_as_answer_text_and_whitelisted_cards(self):
        turn = public_turn(staff_turn(), self.catalog)
        self.assertEqual(set(turn), TURN_KEYS)
        self.assertEqual(turn["cards"], [{
            **PAD_CARD, "availability": "in_stock", "group": "", "reason": "",
            "confidence": ""}])
        self.assertEqual((turn["status"], turn["answer"], turn["note"], turn["language"]),
                         ("COMPLETE", "ნაპოვნია ერთი ნაწილი.", "ფასი დაზუსტებულია.", "ka"))
        # This engine states no question: its quick replies are next steps, and the old
        # list repeats them for a page that was loaded before the two were told apart.
        self.assertEqual(turn["quick_replies"], [{"label": "უკანა ხუნდები",
                                                  "value": "უკანა ხუნდები"}])
        self.assertEqual((turn["question"], turn["suggestions"], turn["groups"], turn["car"]),
                         (None, turn["quick_replies"], [], ""))
        self.assertEqual((turn["total"], turn["offset"], turn["next_offset"]), (1, 0, None))
        # Sources are only counted, never named.
        self.assertEqual((turn["checking"], turn["checked"]), (3, 1))

    def test_nothing_private_survives_anywhere_in_the_output(self):
        turn = public_turn(staff_turn(), self.catalog)
        self.assertEqual(keys_in(turn) & FORBIDDEN_KEYS, set())
        self.assertEqual(keys_in(turn), TURN_KEYS | CARD_KEYS | {"label", "value"})
        text = json.dumps(turn, ensure_ascii=False)
        for private in (*PRIVATE_VALUES, "999"):
            self.assertNotIn(private, text)

    def test_new_engine_fields_never_leak(self):
        payload = staff_turn(debug_trace=["private-trace"], cost={"usd": "private-cost"})
        payload["cards"][0]["supplier_price"] = "private-supplier-price"
        payload["cards"][0]["product"]["purchase_price"] = "private-purchase-price"
        turn = public_turn(payload, self.catalog)
        self.assertEqual(set(turn), TURN_KEYS)
        self.assertEqual(set(turn["cards"][0]), CARD_KEYS)
        self.assertNotIn("private-", json.dumps(turn, ensure_ascii=False))

    def test_price_comes_from_the_catalog_never_from_the_card(self):
        self.assertEqual(public_card(card(price=999.0), self.catalog)["price"], 45.5)
        # Price "0" and an unreadable price are "on request"; so is an unknown product.
        for sid in ("sid-0002", "sid-0003", "sid-missing", "", None):
            self.assertIsNone(public_card(card(sid, price=999.0), self.catalog)["price"], sid)
        self.assertIsNone(public_card(card(), Catalog.from_rows([]))["price"])
        self.assertEqual(public_card(card("sid-0008"), self.catalog)["price"], 850.0)

    def test_assistant_oil_card_converts_catalogue_litre_price_to_pack_price(self):
        catalog = Catalog.from_rows([
            row("oil", "ძრავის ზეთი 5W-30 4L", "", "FUCHS", "20,25", "9", "0"),
        ])
        offered = {**card("oil", price=999.0), "match_source": "assistant"}
        self.assertEqual(public_card(offered, catalog)["price"], 81.0)

    def test_sap_code_uses_the_offered_row_with_a_catalogue_fallback(self):
        offered = card(product_code="  00001234  ")
        self.assertEqual(public_card(offered, self.catalog)["sap_code"], "00001234")
        self.assertEqual(public_card(card(product_code=" "), self.catalog)["sap_code"],
                         "ZQ-1234")
        missing = card("sid-missing", product_code="")
        self.assertEqual(public_card(missing, self.catalog)["sap_code"], "")

    def test_availability_maps_stock_states_to_labels(self):
        self.assertEqual(set(AVAILABILITY),
                         {stock.IN_STORE, stock.WITHIN_HOURS, stock.UNAVAILABLE})
        for status, label in ((stock.IN_STORE, "in_stock"), (stock.WITHIN_HOURS, "soon"),
                              (stock.UNAVAILABLE, "unavailable"), (stock.UNKNOWN, "unknown"),
                              (None, "unknown"), ("anything else", "unknown")):
            self.assertEqual(public_card(card(status=status), self.catalog)["availability"],
                             label, status)

    def test_choices_are_bounded_text_pairs(self):
        choices = ["Petrol", {"label": "Diesel", "value": "diesel 2.0"}, {"value": "Hybrid"},
                   {"label": "LPG"}, "", "   ", None, 7, {"label": 5, "value": "x"},
                   {"label": "x" * 200, "value": "y" * 500}, *["More"] * 10]
        turn = public_turn(staff_turn(clarification_choices=choices), self.catalog)
        replies = turn["quick_replies"]
        self.assertEqual(replies[:4], [
            {"label": "Petrol", "value": "Petrol"},
            {"label": "Diesel", "value": "diesel 2.0"}, {"label": "Hybrid", "value": "Hybrid"},
            {"label": "LPG", "value": "LPG"}])
        self.assertLessEqual(len(replies), 8)
        for reply in replies:
            self.assertEqual(set(reply), {"label", "value"})
            self.assertLessEqual(len(reply["label"]), 80)
            self.assertLessEqual(len(reply["value"]), 200)
        # Clarification choices win over the generic quick replies when both are present.
        self.assertNotIn({"label": "უკანა ხუნდები", "value": "უკანა ხუნდები"}, replies)

    def test_long_or_malformed_payloads_are_bounded_not_trusted(self):
        long_card = card(product_name="n" * 500, product_model="m" * 500, brand="b" * 500)
        turn = public_turn(staff_turn(answer="a" * 9000, note="n" * 900, language="fr",
                                      cards=[long_card, "not a card", None],
                                      next_offset="5"), self.catalog)
        self.assertEqual((len(turn["answer"]), len(turn["note"]), turn["language"]),
                         (4000, 400, "ka"))
        self.assertEqual(len(turn["cards"]), 1)
        shown = turn["cards"][0]
        self.assertEqual((len(shown["name"]), len(shown["fits"]), len(shown["brand"])),
                         (200, 200, 80))
        self.assertIsNone(turn["next_offset"])
        empty = public_turn({}, self.catalog)
        self.assertEqual(empty, {"status": "FAILED", "answer": "", "note": "", "language": "ka",
                                 "car": "", "question": None, "suggestions": [],
                                 "quick_replies": [], "groups": [], "cards": [], "total": 0,
                                 "offset": 0, "next_offset": None, "checked": 0, "checking": 0})
        self.assertEqual(public_card({"product": "not a dict"}, self.catalog),
                         {"name": "", "fits": "", "brand": "", "sap_code": "", "price": None,
                          "availability": "unknown", "group": "", "reason": "",
                          "confidence": ""})
        self.assertEqual(public_turn(staff_turn(language="en", next_offset=5),
                                     self.catalog)["next_offset"], 5)


class StockNumberTests(unittest.TestCase):
    """The engine names a product by the shop's stock number; the public text does not."""

    def setUp(self):
        self.catalog = Catalog.from_rows(ROWS)

    def shown(self, answer: str, note: str = "", **options) -> tuple[str, str]:
        turn = public_turn(staff_turn(answer=answer, note=note), self.catalog, **options)
        return turn["answer"], turn["note"]

    def test_a_listed_products_stock_number_is_taken_out_of_the_sentences(self):
        answer, note = self.shown("SATO (ZQ-1234) მაღაზიაშია, იგივე ZQ1234.",
                                  "ZQ1234 , zq-1234: ფასი დაზუსტებულია.")
        self.assertEqual(answer, "SATO მაღაზიაშია, იგივე.")
        self.assertEqual(note, "ფასი დაზუსტებულია.")
        self.assertNotIn("ZQ", (answer + note).upper())

    def test_a_number_the_visitor_typed_is_left_alone(self):
        answer, _ = self.shown("I found 1 item for ZQ-1234.",
                               said=plain("do you have zq 1234?"))
        self.assertEqual(answer, "I found 1 item for ZQ-1234.")

    def test_a_manufacturers_reference_number_stays_in_the_sentence(self):
        # Item names show these anyway, and the fields that hold them also hold product
        # descriptions and oil approvals, which a sentence needs.
        text = "OEM 04465-33450 ერგება. Mobil-1 0W-40 (LL01; 229.5) აკმაყოფილებს dexos2-ს."
        oil = card(original_code="MOBIL-1 AFS 0W40 (LL01; 229.5) 4L",
                   duplicate_code="General Motors dexos2, VW50400")
        oil["matched_via"] = ["dexos2: approved", "VW50400"]
        turn = public_turn(staff_turn(answer=text, cards=[card(), oil]), self.catalog)
        self.assertEqual(turn["answer"], text)

    def test_text_that_only_resembles_a_number_is_not_damaged(self):
        text = ("5W-40 and 5W40 for your 2012-2017 Camry 2.5 (2AR-FE), 45.50 GEL, "
                "call 599123456. XZQ-12345 and ZQ-12340 are other parts.")
        self.assertEqual(self.shown(text)[0], text)

    def test_only_a_plain_stock_number_counts(self):
        empty = Catalog.from_rows([])
        ignored = [card(product_code=code) for code in (
            "5W40", "75W140", "SAE5W30", "1698", "65AH", "HSSFIT", "2017", "NM-E11R",
            "KW19 white", "", None, 30026771)]
        self.assertEqual(card_codes(ignored, empty), set())
        self.assertEqual(card_codes([card()], empty), {"ZQ-1234", "ZQ1234"})
        self.assertEqual(card_codes([card(product_code="30026771")], empty), {"30026771"})
        # A field that lists several: each token on its own.
        self.assertEqual(card_codes([card(product_code="1/0107 2462013900084;8697393176607")],
                                    empty), {"2462013900084", "8697393176607"})
        # The catalogue's own record of the listed product counts too.
        self.assertEqual(card_codes([card(product_code="")], self.catalog),
                         {"ZQ-1234", "ZQ1234"})
        self.assertEqual(card_codes([None, "x", {"product": "y"}, {}], self.catalog), set())

    def test_numbers_come_from_every_card_of_the_turn_not_only_the_page_shown(self):
        other = card("sid-0002", product_code="KX-99887")
        page = staff_turn(answer="KX-99887 და ZQ-1234 მარაგშია.", cards=[card()], total=2)
        self.assertEqual(public_turn(page, self.catalog)["answer"], "KX-99887 და მარაგშია.")
        self.assertEqual(public_turn(page, self.catalog, listed=[card(), other])["answer"],
                         "და მარაგშია.")

    def test_a_choice_that_names_a_stock_number_is_left_out(self):
        choices = ["უკანა ხუნდები", "SATO (ZQ-1234)", {"label": "ეს", "value": "zq1234"},
                   {"label": "Petrol", "value": "petrol 2.5"}]
        turn = public_turn(staff_turn(clarification_choices=choices), self.catalog)
        self.assertEqual(turn["quick_replies"], [
            {"label": "უკანა ხუნდები", "value": "უკანა ხუნდები"},
            {"label": "Petrol", "value": "petrol 2.5"}])
        # Unless the visitor wrote the number: then it is theirs to tap.
        typed = public_turn(staff_turn(clarification_choices=choices), self.catalog,
                            said=plain("ZQ-1234"))
        self.assertEqual(len(typed["quick_replies"]), 4)

    def test_without_codes_leaves_other_text_exactly_as_it_was(self):
        self.assertEqual(without_codes("two  spaces , kept", {"ZQ-1234"}), "two  spaces , kept")
        self.assertEqual(without_codes("", {"ZQ-1234"}), "")
        self.assertEqual(without_codes("ZQ-1234", set()), "ZQ-1234")
        self.assertEqual(without_codes("Part [ZQ-1234] is here", {"ZQ-1234"}), "Part is here")

    def test_many_long_numbers_do_not_slow_an_answer_down(self):
        cards = [card(f"sid-{index}", product_code=f"{index:013d}") for index in range(3000)]
        codes = card_codes(cards, Catalog.from_rows([]))
        self.assertLessEqual(len(codes), 420)  # bounded, however many cards there are
        started = time.perf_counter()
        without_codes("ნაწილი 0000000000007 მარაგშია. " * 100, codes)
        self.assertLess(time.perf_counter() - started, 1.0)


class QuestionTests(unittest.TestCase):
    """What a reply asks, what it suggests and how its parts are grouped, kept apart."""

    def setUp(self):
        self.catalog = Catalog.from_rows(ROWS)

    def labels(self, options) -> list[str]:
        return [option["label"] for option in options]

    def test_an_offer_reaches_the_widget_grouped_and_with_next_steps(self):
        turn = public_turn(engine_turn(), self.catalog)
        self.assertEqual(set(turn), TURN_KEYS)
        self.assertEqual((turn["car"], turn["question"]), ("Toyota Camry 2015", None))
        self.assertEqual(turn["suggestions"], [{"label": "ზეთის ფილტრიც",
                                                "value": "ზეთის ფილტრი მინდა"}])
        self.assertEqual(turn["quick_replies"], turn["suggestions"])
        # Group ids are made here: the engine's own ("front", "rear") stay behind.
        self.assertEqual(turn["groups"], [{"id": "g1", "label": "წინა ხუნდები", "count": 1},
                                          {"id": "g2", "label": "უკანა ხუნდები", "count": 1}])
        self.assertEqual(turn["cards"], [
            {**PAD_CARD, "availability": "in_stock", "group": "g1", "confidence": "confirmed",
             "reason": "ემთხვევა ამ მანქანის კატალოგის ჩანაწერს."},
            {**PAD_CARD, "sap_code": "ZQ-1234B", "availability": "soon", "group": "g2",
             "confidence": "listed",
             "reason": "CAMRY 2012-2017 მოიცავს 2015 წელს."}])
        self.assertEqual(keys_in(turn),
                         TURN_KEYS | CARD_KEYS | {"label", "value", "id", "count"})
        self.assertEqual(keys_in(turn) & FORBIDDEN_KEYS, set())
        text = json.dumps(turn, ensure_ascii=False)
        for private in (*PRIVATE_VALUES, "front", "rear", "offered", "outcome", "evidence",
                        "meaning", "memory", "car_label"):
            self.assertNotIn(private, text)

    def test_a_question_carries_its_answers_and_none_of_the_engines_notes(self):
        asked = engine_turn(
            cards=[], total=0, groups=[], suggestions=[], outcome="asked",
            question={"text": "წინა თუ უკანა?", "options": [
                {"label": "წინა", "value": "წინა ხუნდები", "meaning": "private-meaning"},
                {"label": "უკანა", "value": "უკანა ხუნდები"}, "ორივე"]})
        turn = public_turn(asked, self.catalog)
        options = [{"label": "წინა", "value": "წინა ხუნდები"},
                   {"label": "უკანა", "value": "უკანა ხუნდები"},
                   {"label": "ორივე", "value": "ორივე"}]
        self.assertEqual(turn["question"], {"options": options})
        # The old list repeats the answers, so a page loaded earlier still shows them.
        self.assertEqual((turn["suggestions"], turn["quick_replies"]), ([], options))
        self.assertEqual(keys_in(turn), TURN_KEYS | {"options", "label", "value"})
        self.assertNotIn("private-", json.dumps(turn, ensure_ascii=False))
        # Not asking is said with None, an empty question, or anything that is not one.
        for quiet in (None, {"text": "", "options": []}, {"text": "?"}, "წინა?", 7,
                      ["წინა", "უკანა"]):
            self.assertIsNone(public_turn(engine_turn(question=quiet), self.catalog)["question"],
                              quiet)

    def test_the_assistant_engines_searches_are_counted_and_never_named(self):
        # Its progress entries name a stage, not a source. Without the count the widget's
        # line under the log stood still for the whole turn.
        running = engine_turn(status="RUNNING", cards=[], total=0, progress=[
            {"stage": "search_catalog", "status": "OK", "label": "private-label W212"},
            {"stage": "lookup_codes", "status": "ERROR", "label": "private-label 04465"},
            {"stage": "oil_guide", "status": "RUNNING", "label": "private-label oil"},
            {"label": "private-label neither"}, {"stage": ""}, "not a dict"])
        turn = public_turn(running, self.catalog)
        self.assertEqual((turn["checking"], turn["checked"]), (3, 2))
        self.assertEqual(set(turn), TURN_KEYS)
        self.assertNotIn("private-label", json.dumps(turn, ensure_ascii=False))

    def test_old_stock_is_said_when_the_engine_sends_only_its_warning(self):
        # Found on a live walk: the assistant engine reports stock that is getting old as
        # a warning code and writes no note, so the cards said "in stock" with no caveat.
        stale = engine_turn(note="", warnings=["private-warning", "STOCK_MAY_BE_STALE"])
        turn = public_turn(stale, self.catalog)
        self.assertEqual(turn["note"], STRINGS["ka"]["js.search.confirm"])
        self.assertEqual(public_turn({**stale, "language": "en"}, self.catalog)["note"],
                         STRINGS["en"]["js.search.confirm"])
        self.assertEqual(set(turn), TURN_KEYS)
        self.assertNotIn("STOCK_MAY_BE_STALE", json.dumps(turn, ensure_ascii=False))
        # The engine's own note is the one caveat. A reply without parts claims no stock.
        self.assertEqual(public_turn(engine_turn(warnings=["STOCK_MAY_BE_STALE"]),
                                     self.catalog)["note"], "ფასი დაზუსტებულია.")
        self.assertEqual(public_turn({**stale, "cards": [], "total": 0},
                                     self.catalog)["note"], "")
        # An older engine words its notes itself and says each once in a conversation.
        self.assertEqual(public_turn(staff_turn(note="", warnings=["STOCK_MAY_BE_STALE"]),
                                     self.catalog)["note"], "")
        for odd in ("STOCK_MAY_BE_STALE", None, {"STOCK_MAY_BE_STALE": True}):
            self.assertEqual(public_turn({**stale, "warnings": odd}, self.catalog)["note"], "",
                             odd)

    def test_a_question_kept_apart_from_the_answer_is_still_read(self):
        asked = {"text": "წინა თუ უკანა? (ZQ-1234)", "options": ["წინა", "უკანა"]}
        turn = public_turn(engine_turn(answer="Toyota Camry 2015.", question=asked),
                           self.catalog)
        # Buttons under a text that asks nothing would be a riddle.
        self.assertEqual(turn["answer"], "Toyota Camry 2015. წინა თუ უკანა?")
        self.assertEqual(turn["question"], {"options": [{"label": "წინა", "value": "წინა"},
                                                        {"label": "უკანა", "value": "უკანა"}]})
        written = public_turn(engine_turn(answer="წინა თუ უკანა ხუნდები გჭირდებათ?",
                                          question=asked), self.catalog)
        self.assertEqual(written["answer"], "წინა თუ უკანა ხუნდები გჭირდებათ?")

    def test_answers_next_steps_and_headings_are_bounded_not_trusted(self):
        many = [{"label": f"ვარიანტი {index}", "value": f"პასუხი {index}"}
                for index in range(12)]
        turn = public_turn(engine_turn(question={"text": "?", "options": many},
                                       suggestions=many), self.catalog)
        # Twelve answers from the engine are not an error: eight are shown.
        self.assertEqual(turn["question"]["options"], many[:8])
        self.assertEqual((turn["suggestions"], turn["quick_replies"]), (many[:4], many[:8]))
        long = public_turn(engine_turn(
            question={"options": ["x" * 300, {"label": "y" * 300, "value": "z" * 900}]},
            suggestions=["s" * 300]), self.catalog)
        for option in (*long["question"]["options"], *long["suggestions"]):
            self.assertEqual(set(option), {"label", "value"})
            self.assertLessEqual(len(option["label"]), 80)
            self.assertLessEqual(len(option["value"]), 200)
        junk = public_turn(engine_turn(
            question={"options": [None, 5, {}, "", {"label": 7}]},
            suggestions=[None, 5, {}, "კიდევ"],
            groups=["x", {"id": "a"}, {"label": "b"},
                    {"id": "front", "label": "ხ" * 300, "count": "many"},
                    *({"id": f"e{index}", "label": f"ჯგუფი {index}", "count": 10 ** 9}
                      for index in range(9))]), self.catalog)
        self.assertIsNone(junk["question"])
        self.assertEqual(junk["suggestions"], [{"label": "კიდევ", "value": "კიდევ"}])
        self.assertEqual([entry["id"] for entry in junk["groups"]],
                         ["g1", "g2", "g3", "g4", "g5", "g6"])
        self.assertEqual((len(junk["groups"][0]["label"]), junk["groups"][0]["count"],
                          junk["groups"][1]["count"]), (80, 0, 999))
        # A card whose group has no heading is simply listed without one.
        self.assertEqual([shown["group"] for shown in junk["cards"]], ["g1", ""])
        # One answer is no choice, but it can still be tapped, as a next step.
        lone = public_turn(engine_turn(question={"options": ["მხოლოდ წინა"]}), self.catalog)
        self.assertIsNone(lone["question"])
        self.assertEqual(self.labels(lone["suggestions"]), ["მხოლოდ წინა", "ზეთის ფილტრიც"])

    def test_stock_numbers_are_taken_out_of_answers_next_steps_and_headings(self):
        payload = engine_turn(
            question={"options": ["წინა", "უკანა", "SATO (ZQ-1234)"]},
            suggestions=["ზეთის ფილტრიც", {"label": "ეს ნაწილი", "value": "zq1234"}],
            groups=[{"id": "front", "label": "წინა ხუნდები ZQ-1234", "count": 1},
                    {"id": "rear", "label": "ZQ-1234B", "count": 1}])
        turn = public_turn(payload, self.catalog)
        self.assertEqual(self.labels(turn["question"]["options"]), ["წინა", "უკანა"])
        self.assertEqual(self.labels(turn["suggestions"]), ["ზეთის ფილტრიც"])
        # A heading keeps its words; one that was only the number goes, and its cards are
        # listed without a heading.
        self.assertEqual(turn["groups"], [{"id": "g1", "label": "წინა ხუნდები", "count": 1}])
        self.assertEqual([shown["group"] for shown in turn["cards"]], ["g1", ""])
        for shown in turn["cards"]:
            self.assertNotIn("ZQ", shown["reason"].upper())
        surrounding = {key: value for key, value in turn.items() if key != "cards"}
        self.assertNotIn("ZQ", json.dumps(surrounding, ensure_ascii=False).upper())
        # Unless the visitor wrote the numbers: then they are theirs to read and tap.
        typed = public_turn(payload, self.catalog, said=plain("ZQ-1234 ZQ-1234B"))
        self.assertEqual((len(typed["question"]["options"]), len(typed["suggestions"]),
                          len(typed["groups"])), (3, 2, 2))

    def test_an_answer_left_out_before_another_ends_the_numbering(self):
        # A tap sends the place of the answer in the engine's own list. Once an earlier
        # answer is left out those places no longer hold, so the rest are offered as
        # next steps, which send their text and no number.
        turn = public_turn(engine_turn(
            question={"options": ["SATO (ZQ-1234)", "წინა", "უკანა"]}), self.catalog)
        self.assertIsNone(turn["question"])
        self.assertEqual(self.labels(turn["suggestions"]), ["წინა", "უკანა", "ზეთის ფილტრიც"])
        self.assertEqual(turn["quick_replies"], turn["suggestions"])
        # Left out at the end, the places of the others are what they were.
        tail = public_turn(engine_turn(
            question={"options": ["წინა", "უკანა", "ZQ-1234"]}), self.catalog)
        self.assertEqual(self.labels(tail["question"]["options"]), ["წინა", "უკანა"])

    def test_an_older_engines_reply_is_read_from_what_it_does_send(self):
        answers = [{"label": "წინა", "value": "წინა"}, {"label": "უკანა", "value": "უკანა"}]
        for signs in ({"act": "clarify_axle"}, {"engine_status": "CLARIFICATION"},
                      {"pending": {"act": "clarify_axle", "offer": None}}):
            turn = public_turn(staff_turn(quick_replies=["წინა", "უკანა"], **signs),
                               self.catalog)
            self.assertEqual((turn["question"], turn["suggestions"], turn["quick_replies"]),
                             ({"options": answers}, [], answers), signs)
        listed = public_turn(staff_turn(clarification_choices=["წინა", "უკანა"]), self.catalog)
        self.assertEqual((listed["question"], listed["suggestions"]), ({"options": answers}, []))
        # "Need the rear ones too?" is answered with yes or no, in the reply's language,
        # and the engine's own quick replies stay next steps.
        offer = {"act": "results", "offer": {"category": "rear brake pads"}}
        for language, words in (("ka", ("დიახ", "არა")), ("en", ("Yes", "No"))):
            turn = public_turn(staff_turn(pending=offer, language=language), self.catalog)
            self.assertEqual(turn["question"],
                             {"options": [{"label": word, "value": word} for word in words]})
            self.assertEqual(self.labels(turn["suggestions"]), ["უკანა ხუნდები"])
            self.assertEqual(turn["quick_replies"], turn["question"]["options"])
            self.assertNotIn("pending", json.dumps(turn, ensure_ascii=False))
        # A turn that is still running has asked nothing yet, whatever it carries.
        running = public_turn(staff_turn(status="RUNNING", act="clarify_axle", pending=offer,
                                         clarification_choices=["2.5", "3.5"]), self.catalog)
        self.assertEqual((running["question"], running["suggestions"],
                          running["quick_replies"]), (None, [], []))

    def test_the_car_line_is_make_model_and_year_and_nothing_else(self):
        turn = public_turn(staff_turn(vehicle=CAMRY), self.catalog)
        self.assertEqual(turn["car"], "Toyota Camry 2015")
        no_year = {**CAMRY, "make": "BMW", "model": "X5", "model_year": None}
        self.assertEqual(public_turn(staff_turn(vehicle=no_year), self.catalog)["car"], "BMW X5")
        for unknown in ({"make": "TOYOTA", "engine_code": "2AR-FE"}, {"model": "Camry"}, {},
                        None, "Camry"):
            self.assertEqual(public_turn(staff_turn(vehicle=unknown), self.catalog)["car"], "",
                             unknown)
        # The engine's own label is used as it stands...
        def labelled(label: str, vehicle=CAMRY) -> str:
            return public_turn(staff_turn(vehicle=vehicle, car_label=label), self.catalog)["car"]

        self.assertEqual(labelled("  Toyota  Camry 2015 sedan "), "Toyota Camry 2015 sedan")
        self.assertEqual(labelled("Honda Fit 2008", vehicle={}), "Honda Fit 2008")
        # ...unless it carries what stays with staff: then it is put together here.
        stated = public_turn(staff_turn(
            vehicle=CAMRY,
            car_label="  Toyota Camry 2015, 2.5 (2AR-FE) 4T1BF1FK5FU000001 ZQ-1234 "),
            self.catalog)
        self.assertEqual(stated["car"], "Toyota Camry 2015")
        for carrying in ("Toyota Camry XLE 2015", "Camry 2015 2ar-fe", "Camry 2ARFE",
                         "Camry 4t1bf1fk5fu000001"):
            self.assertEqual(labelled(carrying), "Toyota Camry 2015", carrying)
        # A trim that is a word of the model's own name is not something to take out.
        rover = {"make": "LAND ROVER", "model": "Range Rover Sport", "trim": "Sport",
                 "model_year": 2016}
        self.assertEqual(labelled("Range Rover Sport 2016", vehicle=rover),
                         "Range Rover Sport 2016")
        # With no record to rebuild from, what is private is cut out of the label itself.
        self.assertEqual(labelled("Camry 2015 (2AR-FE) 4T1BF1FK5FU000001 ZQ-1234",
                                  vehicle={"engine_code": "2AR-FE"}), "Camry 2015")
        long = public_turn(staff_turn(car_label="Camry " * 40), self.catalog)["car"]
        self.assertTrue(0 < len(long) <= 60, long)
        for shown in (turn, stated):
            text = json.dumps(shown, ensure_ascii=False)
            for private in ("2AR-FE", "2ARFE", "XLE", "4T1BF1FK5FU000001", "vehicle",
                            "pending", "displacement"):
                self.assertNotIn(private, text)

    def test_the_reason_is_the_engines_sentence_or_words_for_an_older_basis(self):
        def shown(basis=None, vehicle=CAMRY, language="ka", **more) -> tuple[str, str]:
            listed = card()
            listed.update({"basis": basis, "fitment_status": "UNKNOWN", **more})
            result = public_card(listed, self.catalog, vehicle=vehicle, language=language,
                                 hidden={"ZQ-1234"})
            return result["confidence"], result["reason"]

        # The engine's own sentence: one line of 160 characters, without a stock number.
        level, reason = shown(confidence="check",
                              reason="წელი არ წერია,\nგადაამოწმეთ (ZQ-1234). " + "ა" * 300)
        self.assertEqual((level, len(reason)), ("check", 160))
        self.assertTrue(reason.startswith("წელი არ წერია, გადაამოწმეთ. ააა"), reason)
        # An older engine: its basis in words, in the language of the reply...
        self.assertEqual(shown("engine"), ("check", REASONS["engine"][0]))
        self.assertEqual(shown("engine", language="en"), ("check", "Matches the engine family"))
        self.assertEqual(shown("name"), ("check", REASONS["name"][0]))
        self.assertEqual(shown("staff_rule"), ("confirmed", REASONS["staff_rule"][0]))
        self.assertEqual(shown("fitment", fitment_status="VERIFIED"),
                         ("confirmed", REASONS["fitment"][0]))
        self.assertEqual(shown("fitment"), ("check", ""))  # a candidate nobody reviewed
        # ...unless the label on the card already says exactly that.
        self.assertEqual(shown("code"), ("confirmed", ""))
        self.assertEqual(shown("name_year"), ("listed", ""))
        # An oil is chosen by its grade: a reason, but none of the three levels fits it.
        self.assertEqual(shown("verified_oil", fitment_status="VERIFIED"),
                         ("", REASONS["verified_oil"][0]))
        self.assertEqual(shown("oil_grade"), ("", REASONS["oil_grade"][0]))
        # Without a car, only a number the visitor typed has anything to say.
        for basis, words in REASONS.items():
            self.assertEqual(shown(basis, vehicle={"make": "TOYOTA"}),
                             ("", words[0] if basis == "typed_code" else ""), basis)
        # Whatever else an engine puts there stays where it is.
        for basis in ("private-basis 04465-33450", "", None, 7, ["name"]):
            self.assertEqual(shown(basis), ("", ""), basis)
        self.assertEqual(shown("private-basis", fitment_status="VERIFIED"), ("confirmed", ""))
        # A level the engine states and the contract does not know is not passed on, and
        # nothing is read out of the basis in its place: no label, never a wrong one.
        for level in ("likely", "exact", "CONFIRMED", "Spec"):
            self.assertEqual(shown("name", confidence=level)[0], "", level)
        self.assertEqual(shown("x", confidence="likely"), ("", ""))
        # Not stated at all, the card is an older engine's and its basis is read.
        for level in (7, None, "", "  "):
            self.assertEqual(shown("name", confidence=level)[0], "check", level)
        # A product chosen by its specification keeps that level and the engine's reason.
        self.assertEqual(shown(confidence="spec", reason="0W-20, ძრავის მოთხოვნით (ZQ-1234)."),
                         ("spec", "0W-20, ძრავის მოთხოვნით."))
        self.assertEqual(shown("oil_grade", confidence="spec", reason="0W-20"), ("spec", "0W-20"))


class FakeStore:
    """The ConversationStore surface PublicChat uses, recording what reaches it."""

    maximum = 40
    turn_seconds = 60.0
    ttl_seconds = 600.0

    def __init__(self) -> None:
        self.created: list[str] = []
        self.submitted: list[tuple] = []
        self.keywords: list[dict] = []  # what each message came with besides its language
        self.fetched: list[tuple] = []
        self.deleted: list[str] = []
        self.turns: dict[str, dict] = {}
        self.gone: set[str] = set()
        self.closed = False
        self._ids = itertools.count(1)

    def create(self) -> dict:
        identifier = f"{next(self._ids):043d}"
        self.created.append(identifier)
        return {"conversation_id": identifier, "expires_in": self.ttl_seconds}

    def submit(self, identifier: str, message: str, *, language: str = "ka",
               **keywords) -> dict:
        if identifier in self.gone:
            raise ConversationError("CONVERSATION_EXPIRED", 404)
        turn_id = f"{next(self._ids):032d}"
        self.submitted.append((identifier, message, language))
        self.keywords.append(keywords)
        self.turns[turn_id] = staff_turn(status="RUNNING", turn_id=turn_id, cards=[], answer="")
        return {"conversation_id": identifier, "turn_id": turn_id, "status": "RUNNING",
                "language": "ka" if language == "auto" else language}

    def get(self, identifier: str, turn_id: str, offset: int = 0, limit: int = 20) -> dict:
        self.fetched.append((identifier, turn_id, offset, limit))
        if turn_id not in self.turns:
            raise ConversationError("TURN_EXPIRED", 404)
        return copy.deepcopy(self.turns[turn_id])

    def delete(self, identifier: str) -> None:
        self.deleted.append(identifier)

    def close(self) -> None:
        self.closed = True

    def finish(self, turn_id: str) -> None:
        self.turns[turn_id] = staff_turn(turn_id=turn_id)


class OlderStore(FakeStore):
    """A store from before an answer could be tapped: its submit() takes no ``choice``."""

    def submit(self, identifier: str, message: str, *, language: str = "ka") -> dict:
        return super().submit(identifier, message, language=language)


class Catalogs:
    def __init__(self) -> None:
        self.catalog = Catalog.from_rows(ROWS)

    def current(self) -> Catalog:
        return self.catalog


class PublicChatTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.clock = Clock()
        self.wall = Clock(86_400.0 * 20_000 + 3_600)
        self.daily = DailyBudget(1000, clock=self.wall)
        self.chat = PublicChat(self.store, Catalogs(), self.daily, clock=self.clock,
                               wall=self.wall)

    def refused(self, code: str, status: int, call, *args) -> None:
        with self.assertRaises(ConversationError) as caught:
            call(*args)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))

    def open(self, address: str = A) -> str:
        return self.chat.create(address)["conversation_id"]

    def test_an_id_this_facade_did_not_issue_never_reaches_the_store(self):
        unknown = "u" * 43
        self.refused("CONVERSATION_EXPIRED", 404, self.chat.submit, A, unknown, "pads", "ka")
        self.refused("CONVERSATION_EXPIRED", 404, self.chat.turn, unknown, "t" * 32, 0, 5)
        self.chat.delete(unknown)
        self.assertEqual((self.store.submitted, self.store.fetched, self.store.deleted),
                         ([], [], []))
        self.assertEqual(self.daily.used, 0)  # and it costs nothing

    def test_a_message_reaches_the_store_and_only_three_fields_come_back(self):
        conversation = self.open()
        started = self.chat.submit(A, conversation, "front pads for camry", "auto")
        self.assertEqual(set(started), {"turn_id", "status", "language"})
        self.assertEqual(started["status"], "RUNNING")
        self.assertEqual(self.store.submitted, [(conversation, "front pads for camry", "auto")])
        self.store.finish(started["turn_id"])
        turn = self.chat.turn(conversation, started["turn_id"], 5, 7)
        self.assertEqual(self.store.fetched, [(conversation, started["turn_id"], 5, 7)])
        self.assertEqual(set(turn), TURN_KEYS)
        self.assertEqual(turn["cards"][0]["price"], 45.5)

    def test_a_tapped_answer_reaches_the_store_with_its_number(self):
        conversation = self.open()
        self.chat.submit(A, conversation, "წინა", "auto", 2)
        self.chat.submit(A, conversation, "front pads", "auto")
        self.assertEqual(self.store.submitted, [(conversation, "წინა", "auto"),
                                                (conversation, "front pads", "auto")])
        # The number goes along only when there is one.
        self.assertEqual(self.store.keywords, [{"choice": 2}, {}])

    def test_a_store_that_does_not_know_tapped_answers_gets_the_text_alone(self):
        store = OlderStore()
        chat = PublicChat(store, Catalogs(), self.daily, clock=self.clock, wall=self.wall)
        conversation = chat.create(A)["conversation_id"]
        started = chat.submit(A, conversation, "წინა", "auto", 2)
        self.assertEqual(started["status"], "RUNNING")
        self.assertEqual((store.submitted, store.keywords),
                         ([(conversation, "წინა", "auto")], [{}]))
        # The real store, with or without the keyword, takes a tapped answer too.
        real = ConversationStore(Engine(), None, ttl_seconds=600, clock=self.clock)
        self.addCleanup(real.close)
        chat = PublicChat(real, Catalogs(), self.daily, clock=self.clock, wall=self.wall)
        conversation = chat.create(B)["conversation_id"]
        self.assertEqual(chat.submit(B, conversation, "წინა", "auto", 1)["status"], "RUNNING")

    def test_at_its_limit_an_address_gives_up_its_least_useful_conversation(self):
        first, second, third = (self.open(A) for _ in range(3))
        self.clock.advance(1)
        self.chat.submit(A, first, "pads", "ka")  # the oldest is the one in use
        self.clock.advance(1)
        fourth = self.open(A)  # nobody is refused for lack of room...
        # ...the oldest conversation nobody wrote in went instead, here and in the store.
        self.assertEqual(self.store.deleted, [second])
        self.refused("CONVERSATION_EXPIRED", 404, self.chat.submit, A, second, "pads", "ka")
        self.open(B)  # another visitor neither pays for this nor is affected by it
        self.assertEqual(self.store.deleted, [second])
        self.chat.submit(A, third, "discs", "ka")
        self.clock.advance(1)
        self.open(A)
        self.assertEqual(self.store.deleted, [second, fourth])
        # With only conversations in use left, the one idle longest goes.
        fifth = next(identifier for identifier, owner in self.chat._owned.items()
                     if owner == A and identifier not in (first, third))
        self.store.turns.clear()
        self.clock.advance(self.store.turn_seconds + 6)  # both turns are over
        self.chat.submit(A, fifth, "oil", "ka")
        self.clock.advance(1)
        self.open(A)
        self.assertEqual(self.store.deleted, [second, fourth, first])
        self.assertEqual(sum(1 for owner in self.chat._owned.values() if owner == A), 3)

    def test_a_full_store_gives_up_its_least_useful_conversation_not_the_newcomer(self):
        store = ConversationStore(Engine(), None, ttl_seconds=600, maximum=2, clock=self.clock)
        self.addCleanup(store.close)
        chat = PublicChat(store, Catalogs(), self.daily, clock=self.clock, wall=self.wall)
        idle = chat.create(A)["conversation_id"]
        self.clock.advance(1)
        used = chat.create(B)["conversation_id"]
        chat.submit(B, used, "pads", "ka")
        self.clock.advance(1)
        fresh = chat.create("192.0.2.50")["conversation_id"]  # the store was full
        self.assertEqual(set(store._conversations), {used, fresh})
        self.refused("CONVERSATION_EXPIRED", 404, chat.submit, A, idle, "pads", "ka")
        # Idle conversations cannot shut anyone out, however many addresses open them.
        for index in range(20):
            chat.create(f"198.51.100.{index}")
        self.assertIn(used, store._conversations)  # the one somebody is using survived
        self.assertEqual(len(store._conversations), 2)
        self.assertLessEqual(len(chat._owned), 2)

    def test_conversations_the_store_expired_stop_counting_against_the_address(self):
        first, second, third = (self.open(A) for _ in range(3))
        self.clock.advance(500)
        started = self.chat.submit(A, first, "pads", "ka")  # activity keeps the first alive
        self.clock.advance(101)  # 601 s idle for the second and third, 101 s for the first
        self.open(A)
        self.open(A)
        self.assertEqual(self.store.deleted, [])  # there was room: nothing had to go
        # The expired ids are no longer accepted either.
        for gone in (second, third):
            self.refused("CONVERSATION_EXPIRED", 404, self.chat.submit, A, gone, "pads", "ka")
        self.assertEqual(len(self.store.submitted), 1)
        self.store.finish(started["turn_id"])
        self.chat.turn(first, started["turn_id"], 0, 5)
        self.chat.submit(A, first, "discs", "ka")

    def test_polling_keeps_a_conversation_alive_like_it_does_in_the_store(self):
        conversation = self.open(A)
        started = self.chat.submit(A, conversation, "pads", "ka")
        for _ in range(5):  # 40 minutes of polling, each well inside the idle timeout
            self.clock.advance(480)
            self.chat.turn(conversation, started["turn_id"], 0, 5)
        self.clock.advance(601)
        self.refused("CONVERSATION_EXPIRED", 404, self.chat.turn, conversation,
                     started["turn_id"], 0, 5)

    def test_in_flight_turns_are_capped_and_a_finished_turn_frees_a_slot(self):
        owners = (A, A, B, B, C)
        conversations = [self.open(address) for address in owners]
        turns = [self.chat.submit(address, conversation, "pads", "ka")["turn_id"]
                 for address, conversation in list(zip(owners, conversations))[:4]]
        self.refused("CHAT_WORKER_CAPACITY", 429, self.chat.submit, C, conversations[4],
                     "pads", "ka")
        self.assertEqual(len(self.store.submitted), 4)
        self.assertEqual(self.daily.used, 4)  # a refused message is not charged
        # Polling a turn that is still running frees nothing.
        self.chat.turn(conversations[0], turns[0], 0, 5)
        self.refused("CHAT_WORKER_CAPACITY", 429, self.chat.submit, C, conversations[4],
                     "pads", "ka")
        self.store.finish(turns[0])
        self.chat.turn(conversations[0], turns[0], 0, 5)
        self.chat.submit(C, conversations[4], "pads", "ka")
        self.refused("CHAT_WORKER_CAPACITY", 429, self.chat.submit, A, conversations[0],
                     "discs", "ka")

    def test_one_address_runs_at_most_two_turns_at_once(self):
        mine = [self.open(A) for _ in range(3)]
        theirs = self.open(B)
        turns = [self.chat.submit(A, conversation, "pads", "ka")["turn_id"]
                 for conversation in mine[:2]]
        self.refused("CHAT_WORKER_CAPACITY", 429, self.chat.submit, A, mine[2], "pads", "ka")
        self.assertEqual((len(self.store.submitted), self.daily.used), (2, 2))
        self.chat.submit(B, theirs, "pads", "ka")  # room is left for everyone else
        self.store.finish(turns[0])
        self.chat.turn(mine[0], turns[0], 0, 5)
        self.chat.submit(A, mine[2], "pads", "ka")

    def test_a_message_the_store_refuses_gives_its_slot_back(self):
        conversation = self.open()
        accept = self.store.submit
        self.store.submit = Mock(side_effect=ConversationError("TURN_ALREADY_ACTIVE", 409))
        for _ in range(5):  # more than either in-flight limit
            self.refused("TURN_ALREADY_ACTIVE", 409, self.chat.submit, A, conversation, "pads",
                         "ka")
        self.store.submit = Mock(side_effect=RuntimeError("engine fault"))
        for _ in range(5):
            with self.assertRaises(RuntimeError):
                self.chat.submit(A, conversation, "pads", "ka")
        self.store.submit = accept
        self.chat.submit(A, conversation, "pads", "ka")
        self.assertEqual(len(self.chat._running), 1)

    def test_a_message_the_store_refuses_is_not_charged(self):
        chat = PublicChat(self.store, Catalogs(), self.daily, per_address_daily=3,
                          clock=self.clock, wall=self.wall)
        conversation = chat.create(A)["conversation_id"]
        accept = self.store.submit
        self.store.submit = Mock(side_effect=ConversationError("TURN_ALREADY_ACTIVE", 409))
        for _ in range(6):  # twice the day's allowance for this address
            self.refused("TURN_ALREADY_ACTIVE", 409, chat.submit, A, conversation, "pads", "ka")
        self.assertEqual(self.daily.used, 0)
        self.store.submit = accept
        chat.submit(A, conversation, "pads", "ka")
        self.assertEqual(self.daily.used, 1)

    def test_a_turn_nobody_polls_frees_its_slot_after_its_deadline(self):
        owners = (A, A, B, B, C)
        conversations = [self.open(address) for address in owners]
        for address, conversation in list(zip(owners, conversations))[:4]:
            self.chat.submit(address, conversation, "pads", "ka")
        self.refused("CHAT_WORKER_CAPACITY", 429, self.chat.submit, C, conversations[4],
                     "discs", "ka")
        self.clock.advance(self.store.turn_seconds + 5.1)
        self.chat.submit(C, conversations[4], "discs", "ka")

    def test_one_address_sends_a_bounded_number_of_messages_per_day(self):
        chat = PublicChat(self.store, Catalogs(), self.daily, per_address_daily=3,
                          inflight=100, inflight_per_address=100, clock=self.clock,
                          wall=self.wall)
        mine, theirs = chat.create(A)["conversation_id"], chat.create(B)["conversation_id"]
        for _ in range(3):
            chat.submit(A, mine, "pads", "ka")
        self.refused("CHAT_DAILY_LIMIT", 429, chat.submit, A, mine, "pads", "ka")
        self.assertEqual((len(self.store.submitted), self.daily.used), (3, 3))
        chat.submit(B, theirs, "pads", "ka")  # someone else still gets through
        # The address is what is counted, not the conversation.
        self.refused("CHAT_DAILY_LIMIT", 429, chat.submit, A, theirs, "pads", "ka")
        self.wall.advance(86_400)
        chat.submit(A, mine, "pads", "ka")

    def test_the_64s_of_one_ipv6_48_share_a_wider_daily_allowance(self):
        chat = PublicChat(self.store, Catalogs(), self.daily, per_address_daily=3,
                          inflight=100, inflight_per_address=100, clock=self.clock,
                          wall=self.wall)
        accepted = 0
        for index in range(4):
            address = f"2001:db8:1:{index:x}::/64"
            conversation = chat.create(address)["conversation_id"]
            for _ in range(3):
                try:
                    chat.submit(address, conversation, "pads", "ka")
                    accepted += 1
                except ConversationError as exc:
                    self.assertEqual(exc.code, "CHAT_DAILY_LIMIT")
        self.assertEqual(accepted, 2 * 3)  # a /48 may send twice what one address may
        self.assertEqual(self.daily.used, 6)
        other = "2001:db8:2:1::/64"  # another /48 still gets through
        chat.submit(other, chat.create(other)["conversation_id"], "pads", "ka")

    def test_the_whole_site_budget_stops_everyone_until_the_next_day(self):
        chat = PublicChat(self.store, Catalogs(), DailyBudget(2, clock=self.wall),
                          inflight=100, inflight_per_address=100, clock=self.clock,
                          wall=self.wall)
        mine, theirs = chat.create(A)["conversation_id"], chat.create(B)["conversation_id"]
        chat.submit(A, mine, "pads", "ka")
        chat.submit(B, theirs, "pads", "ka")
        for address, conversation in ((A, mine), (B, theirs)):
            self.refused("CHAT_DAILY_LIMIT", 429, chat.submit, address, conversation, "pads",
                         "ka")
        self.assertEqual(len(self.store.submitted), 2)
        self.wall.advance(86_400)
        chat.submit(A, mine, "pads", "ka")

    def test_delete_reaches_the_store_once_and_only_for_an_issued_id(self):
        conversation = self.open()
        self.chat.delete(conversation)
        self.chat.delete(conversation)
        self.chat.delete("u" * 43)
        self.assertEqual(self.store.deleted, [conversation])
        self.refused("CONVERSATION_EXPIRED", 404, self.chat.submit, A, conversation, "pads",
                     "ka")
        self.assertEqual(self.store.submitted, [])

    def test_a_conversation_the_store_lost_is_forgotten_here_too(self):
        conversations = [self.open(A) for _ in range(3)]
        self.store.gone.add(conversations[0])
        self.refused("CONVERSATION_EXPIRED", 404, self.chat.submit, A, conversations[0], "pads",
                     "ka")
        self.refused("CONVERSATION_EXPIRED", 404, self.chat.turn, conversations[0], "t" * 32,
                     0, 5)
        self.assertEqual(self.store.fetched, [])
        self.open(A)  # its slot is free again

    def test_store_errors_pass_through_unchanged(self):
        conversation = self.open()
        self.store.submit = Mock(side_effect=ConversationError("TURN_ALREADY_ACTIVE", 409))
        self.refused("TURN_ALREADY_ACTIVE", 409, self.chat.submit, A, conversation, "pads",
                     "ka")
        self.refused("TURN_EXPIRED", 404, self.chat.turn, conversation, "t" * 32, 0, 5)
        self.chat.delete(conversation)  # still ours, so the store is told
        self.assertEqual(self.store.deleted, [conversation])

    def test_issued_ids_are_bounded(self):
        self.store.maximum = 2
        for index in range(60):
            self.chat.create(f"192.0.2.{index}")
        self.assertLessEqual(len(self.chat._owned), 8)
        self.assertLessEqual(len(self.chat._idle), 8)

    def test_close_closes_the_store_and_only_a_search_service_it_owns(self):
        self.store.search_service = Mock()
        self.chat.close()
        self.assertTrue(self.store.closed)
        self.store.search_service.close.assert_not_called()
        self.chat.owns_search = True
        self.chat.close()
        self.store.search_service.close.assert_called_once_with()

    def test_close_drains_the_visitor_chat_log(self):
        self.chat.chat_log = None  # no log configured: nothing to close
        self.chat.close()
        self.chat.chat_log = Mock()
        self.chat.close()
        self.chat.chat_log.close.assert_called_once_with()

    def test_the_facade_and_the_real_store_agree_on_idle_expiry(self):
        store = ConversationStore(Engine(), None, ttl_seconds=600, clock=self.clock)
        self.addCleanup(store.close)
        chat = PublicChat(store, Catalogs(), self.daily, clock=self.clock, wall=self.wall)
        old = [chat.create(A)["conversation_id"] for _ in range(3)]
        self.clock.advance(600)  # the store's own rule: last touch plus ttl has passed
        fresh = chat.create(A)["conversation_id"]
        self.assertEqual(list(store._conversations), [fresh])
        self.refused("CONVERSATION_EXPIRED", 404, chat.submit, A, old[0], "pads", "ka")
        self.assertEqual(self.daily.used, 0)


class Engine:
    """A chat service whose answer carries everything the staff workspace would show."""

    settings = Settings()

    def __init__(self) -> None:
        self.messages: list[str] = []

    def respond_conversation(self, message: str, **kwargs) -> dict:
        self.messages.append(message)
        return {key: value for key, value in staff_turn(status="OK").items()
                if key not in ("conversation_id", "turn_id")}


class ChatRouteTests(SiteAppCase):
    CREATE = "/api/site/chat/conversations"

    def chat(self, **changes):
        """An application whose public chat sits on a recording store."""
        settings = replace(self.settings, **changes) if changes else self.settings
        client = self.serve(settings, public_search=object(), catalog=Catalog.from_rows(ROWS),
                            buckets=RecordingBuckets())
        self.store = FakeStore()
        self.app.state.site.chat.store = self.store
        return client

    async def test_a_visitor_can_create_message_poll_and_delete(self):
        client = self.chat()
        created = await client.post(self.CREATE)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(set(created.json()), {"conversation_id", "expires_in"})
        conversation = created.json()["conversation_id"]
        sent = await client.post(f"{self.CREATE}/{conversation}/messages",
                                 json={"message": "front pads for camry", "language": "en"})
        self.assertEqual(sent.status_code, 202)
        self.assertEqual(set(sent.json()), {"turn_id", "status", "language"})
        self.assertEqual(self.store.submitted, [(conversation, "front pads for camry", "en")])
        turn_id = sent.json()["turn_id"]
        self.store.finish(turn_id)
        polled = await client.get(f"{self.CREATE}/{conversation}/turns/{turn_id}",
                                  params={"offset": 0, "limit": 3})
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(set(polled.json()), TURN_KEYS)
        self.assertEqual(set(polled.json()["cards"][0]), CARD_KEYS)
        self.assertEqual(polled.json()["cards"][0]["sap_code"], "ZQ-1234")
        self.assertEqual(self.store.fetched, [(conversation, turn_id, 0, 3)])
        for response in (created, sent, polled):
            self.assertEqual(response.headers["cache-control"], "no-store")
            for private in PRIVATE_VALUES:
                self.assertNotIn(private, response.text)
        deleted = await client.delete(f"{self.CREATE}/{conversation}")
        self.assertEqual((deleted.status_code, self.store.deleted), (204, [conversation]))

    async def test_an_unknown_conversation_is_refused_before_the_store(self):
        client = self.chat()
        unknown = "u" * 43
        sent = await client.post(f"{self.CREATE}/{unknown}/messages", json={"message": "pads"})
        polled = await client.get(f"{self.CREATE}/{unknown}/turns/{'t' * 32}")
        for response in (sent, polled):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": {"code": "CONVERSATION_EXPIRED"}})
        self.assertEqual((await client.delete(f"{self.CREATE}/{unknown}")).status_code, 204)
        self.assertEqual((self.store.submitted, self.store.fetched, self.store.deleted),
                         ([], [], []))

    async def test_messages_and_identifiers_are_validated(self):
        client = self.chat()
        conversation = (await client.post(self.CREATE)).json()["conversation_id"]
        url = f"{self.CREATE}/{conversation}/messages"
        accepted = await client.post(url, json={"message": "x" * 500})
        self.assertEqual(accepted.status_code, 202)
        secret = "private-visitor-text"
        for body in ({"message": secret + "x" * 481}, {"message": ""}, {}, {"message": 5},
                     {"message": secret, "language": "fr"}, {"message": secret, "vin": "x"},
                     {"message": secret, "vehicle": {"make": "TOYOTA"}}, [secret]):
            response = await client.post(url, json=body)
            self.assertEqual(response.status_code, 422, body)
            self.assertNotIn(secret, response.text)
        self.assertEqual(len(self.store.submitted), 1)
        turn = "t" * 32
        for path in (f"{self.CREATE}/short/messages", f"{self.CREATE}/{'c' * 44}/messages",
                     f"{self.CREATE}/{'c' * 42}!/messages"):
            self.assertEqual((await client.post(path, json={"message": "pads"})).status_code,
                             422, path)
        for path in (f"{self.CREATE}/{conversation}/turns/short",
                     f"{self.CREATE}/{conversation}/turns/{turn}?limit=21",
                     f"{self.CREATE}/{conversation}/turns/{turn}?limit=0",
                     f"{self.CREATE}/{conversation}/turns/{turn}?offset=-1",
                     f"{self.CREATE}/{conversation}/turns/{turn}?offset=501"):
            self.assertEqual((await client.get(path)).status_code, 422, path)
        self.assertEqual(self.store.fetched, [])
        big = await client.post(url, content=b'{"message": "' + b"x" * 20_000 + b'"}',
                                headers={"Content-Type": "application/json"})
        self.assertEqual(big.status_code, 413)

    async def test_a_tapped_answer_is_validated_and_passed_on(self):
        client = self.chat()
        conversation = (await client.post(self.CREATE)).json()["conversation_id"]
        url = f"{self.CREATE}/{conversation}/messages"
        tapped = await client.post(url, json={"message": "წინა", "choice": 2})
        self.assertEqual(tapped.status_code, 202)
        self.assertEqual((self.store.submitted, self.store.keywords),
                         ([(conversation, "წინა", "auto")], [{"choice": 2}]))
        typed = await client.post(url, json={"message": "წინა", "choice": None})
        self.assertEqual((typed.status_code, self.store.keywords[-1]), (202, {}))
        secret = "private-visitor-text"
        for choice in (0, 9, -1, "2", 2.5, True, [2], {"index": 2}):
            response = await client.post(url, json={"message": secret, "choice": choice})
            self.assertEqual(response.status_code, 422, choice)
            self.assertNotIn(secret, response.text)
        self.assertEqual(len(self.store.submitted), 2)

    async def test_caps_surface_as_429_with_their_own_codes(self):
        client = self.chat(site_chat_daily_turns=2)
        conversations, turns = [], []
        for _ in range(3):
            conversations.append((await client.post(self.CREATE)).json()["conversation_id"])
        for conversation in conversations[:2]:
            response = await client.post(f"{self.CREATE}/{conversation}/messages",
                                         json={"message": "pads"})
            self.assertEqual(response.status_code, 202)
            turns.append(response.json()["turn_id"])
        third = f"{self.CREATE}/{conversations[2]}/messages"
        busy = await client.post(third, json={"message": "pads"})
        self.assertEqual((busy.status_code, busy.json()),
                         (429, {"detail": {"code": "CHAT_WORKER_CAPACITY"}}))
        self.store.finish(turns[0])
        await client.get(f"{self.CREATE}/{conversations[0]}/turns/{turns[0]}")
        spent = await client.post(third, json={"message": "pads"})
        self.assertEqual((spent.status_code, spent.json()),
                         (429, {"detail": {"code": "CHAT_DAILY_LIMIT"}}))
        self.assertEqual(len(self.store.submitted), 2)
        # A fourth conversation is not refused: the one nobody wrote in makes room.
        self.assertEqual((await client.post(self.CREATE)).status_code, 201)
        self.assertEqual(self.store.deleted, [conversations[2]])

    async def test_caps_count_the_visitor_behind_a_trusted_proxy_not_the_proxy(self):
        self.chat(trusted_proxies=("10.0.0.0/8",))
        proxy = self.connect(peer=("10.1.2.3", 4444))
        opened: dict[str, list[str]] = {}
        for visitor in ("203.0.113.50", "198.51.100.7"):
            for _ in range(3):
                response = await proxy.post(self.CREATE, headers={"X-Forwarded-For": visitor})
                self.assertEqual(response.status_code, 201)
                opened.setdefault(visitor, []).append(response.json()["conversation_id"])
        # Six conversations through one proxy and none had to make room for another:
        # each visitor has their own three.
        self.assertEqual(self.store.deleted, [])
        fourth = await proxy.post(self.CREATE, headers={"X-Forwarded-For": "203.0.113.50"})
        self.assertEqual(fourth.status_code, 201)
        self.assertEqual(self.store.deleted, [opened["203.0.113.50"][0]])

    async def test_the_dashboard_switch_turns_the_chat_off(self):
        client = self.chat()
        conversation = (await client.post(self.CREATE)).json()["conversation_id"]
        admin = await self.sign_in(client)
        saved = await client.put("/api/admin/content/chat", headers=admin, json={
            "version": 0, "document": {"enabled": False,
                                       "launcher": {"ka": "ჩატი", "en": "Chat"},
                                       "greeting": {"ka": "გამარჯობა", "en": "Hello"}}})
        self.assertEqual(saved.status_code, 200)
        requests = (
            client.post(self.CREATE),
            client.post(f"{self.CREATE}/{conversation}/messages", json={"message": "pads"}),
            client.get(f"{self.CREATE}/{conversation}/turns/{'t' * 32}"))
        for request in requests:
            response = await request
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": {"code": "CHAT_DISABLED"}})
        self.assertEqual((self.store.submitted, self.store.fetched), ([], []))
        # A visitor can still close what they had open.
        self.assertEqual((await client.delete(f"{self.CREATE}/{conversation}")).status_code,
                         204)
        self.assertEqual(self.store.deleted, [conversation])
        # Switching it back on takes effect just as quickly.
        saved = await client.put("/api/admin/content/chat", headers=admin, json={
            "version": 1, "document": {"enabled": True,
                                       "launcher": {"ka": "ჩატი", "en": "Chat"},
                                       "greeting": {"ka": "გამარჯობა", "en": "Hello"}}})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual((await client.post(self.CREATE)).status_code, 201)

    async def test_without_an_assistant_every_chat_route_says_disabled(self):
        client = self.serve(catalog=Catalog.from_rows(ROWS))
        self.assertIsNone(self.app.state.site.chat)
        conversation = "c" * 43
        requests = (
            client.post(self.CREATE),
            client.post(f"{self.CREATE}/{conversation}/messages", json={"message": "pads"}),
            client.get(f"{self.CREATE}/{conversation}/turns/{'t' * 32}"))
        for request in requests:
            response = await request
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": {"code": "CHAT_DISABLED"}})
        self.assertEqual((await client.delete(f"{self.CREATE}/{conversation}")).status_code,
                         204)

    async def test_visitors_get_their_own_coordinator_store_and_log(self):
        log_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "site_chat.sqlite3"
        settings = replace(
            self.settings, anthropic_api_key="fixture-api-key", vin_hmac_key="x" * 32,
            motorium_enabled=True, chat_turn_seconds=75, site_chat_log_path=str(log_path),
            site_chat_log_retention_days=14)
        self.serve(settings)
        chat, staff = self.app.state.site.chat, self.app.state.conversations
        self.addCleanup(chat.close)
        self.assertIsInstance(chat, PublicChat)
        store = chat.store
        self.assertIsInstance(store, ConversationStore)
        self.assertIsNot(store, staff)
        self.assertEqual((store.ttl_seconds, store.turn_seconds, store.maximum), (600, 60.0, 40))
        # Its own coordinator: no Motorium and no paid catalogue, whatever staff have.
        coordinator = store.search_service
        self.assertIsInstance(coordinator, VINSearchCoordinator)
        self.assertIsNot(coordinator, staff.search_service)
        self.assertEqual((coordinator.motorium_enabled, coordinator.motorium_service,
                          coordinator._catalog), (False, None, None))
        # Its own log file, with its own retention; the staff log is a different object.
        self.assertEqual((chat.chat_log.path, chat.chat_log.retention_days), (log_path, 14))
        self.assertIs(store.chat_log, chat.chat_log)
        self.assertIsNot(staff.chat_log, chat.chat_log)
        with patch.object(coordinator, "close") as closed, \
                patch.object(chat.chat_log, "close") as drained:
            chat.close()
        closed.assert_called_once_with()
        drained.assert_called_once_with()

    async def test_no_assistant_is_built_without_an_api_key_or_with_a_short_vin_key(self):
        for changes in ({"anthropic_api_key": "fixture-api-key", "vin_hmac_key": "x" * 31},
                        {"anthropic_api_key": "", "vin_hmac_key": "x" * 32}):
            self.serve(replace(self.settings, **changes))
            self.assertIsNone(self.app.state.site.chat, changes)

    async def test_the_application_lifespan_starts_the_catalog_and_closes_the_chat(self):
        self.serve(public_search=object())
        site = self.app.state.site
        with (patch.object(site.catalog_store, "start") as start,
              patch.object(site.catalog_store, "stop") as stop,
              patch.object(site.chat, "close") as close):
            async with self.app.router.lifespan_context(self.app):
                start.assert_called_once_with()
                stop.assert_not_called()
                close.assert_not_called()
            stop.assert_called_once_with()
            close.assert_called_once_with()
        site.chat.close()

    async def test_a_real_turn_reaches_the_visitor_without_staff_details(self):
        engine = Engine()
        rows = [*ROWS, row("sid-0009", "Wiper", "UNIVERSAL", "BOSCH", "20", "1", "0")]
        client = self.serve(chat_service=engine, public_search=object(),
                            catalog=Catalog.from_rows(rows), buckets=RecordingBuckets())
        chat = self.app.state.site.chat
        self.addCleanup(chat.close)
        self.assertIsInstance(chat.store, ConversationStore)
        self.assertLessEqual(chat.store.turn_seconds, 60)
        conversation = (await client.post(self.CREATE)).json()["conversation_id"]
        sent = await client.post(f"{self.CREATE}/{conversation}/messages",
                                 json={"message": "front brake pads", "language": "en"})
        self.assertEqual(sent.status_code, 202)
        url = f"{self.CREATE}/{conversation}/turns/{sent.json()['turn_id']}"
        for _ in range(300):
            polled = await client.get(url)
            if polled.json()["status"] != "RUNNING":
                break
            await asyncio.sleep(0.01)
        body = polled.json()
        self.assertEqual(engine.messages, ["front brake pads"])
        self.assertEqual((polled.status_code, body["status"]), (200, "COMPLETE"))
        self.assertEqual(set(body), TURN_KEYS)
        self.assertEqual(body["cards"], [{
            **PAD_CARD, "availability": "in_stock", "group": "", "reason": "",
            "confidence": ""}])
        self.assertEqual(keys_in(body) & FORBIDDEN_KEYS, set())
        for private in PRIVATE_VALUES:
            self.assertNotIn(private, polled.text)
        # The staff route for the same conversation id knows nothing about it.
        staff = await client.get(f"/api/v1/chat/conversations/{conversation}/turns/"
                                 f"{sent.json()['turn_id']}")
        self.assertEqual(staff.status_code, 401)

    async def test_the_page_gives_the_widget_its_hooks_and_the_shops_phone(self):
        client = self.chat()
        page = (await client.get("/en")).text
        for hook in ('class="chat__car" role="group" aria-label="Your car" hidden',
                     "data-chat-car>Change</button>", "data-chat-jump hidden>",
                     '<label class="sr-only" for="chat-input">Message</label>',
                     '<span class="chat__count" aria-hidden="true" hidden>'):
            self.assertIn(hook, page)
        self.assertNotIn("data-phone", page)  # no number stored: no link to ring
        document = copy.deepcopy(DEFAULT_SECTIONS["brand"])
        document["phone"] = "+995 32 212 34 56"
        self.site_repository.sections["brand"] = {"section_key": "brand", "document": document,
                                                  "version": 1}
        page = (await self.chat().get("/")).text
        self.assertIn('<dialog class="chat" id="chat" aria-labelledby="chat-title" '
                      'data-phone="+995322123456" data-call="დარეკვა">', page)


class WidgetStringTests(unittest.TestCase):
    """The widget's script and template, and the string table they read, have to agree."""

    SITE = Path(__file__).resolve().parents[1] / "automotive"

    def test_every_string_the_script_reads_is_in_both_tables(self):
        script = (self.SITE / "static/site/js/chat.js").read_text(encoding="utf-8")
        read = set(re.findall(r"['`]((?:chat|search|avail|fit)\.[a-z_]+)['`]", script))
        self.assertGreater(len(read), 30, "the script's string keys were not found")
        # Two families are read by the value a card carries.
        read |= {f"avail.{state}" for state in ("in_stock", "soon", "unavailable", "unknown")}
        read |= {f"fit.{level}" for level in chat_contract.LEVELS}
        self.assertEqual({f"avail.{label}" for label in AVAILABILITY.values()} - read, set())
        for language, table in STRINGS.items():
            self.assertEqual(sorted(key for key in read if f"js.{key}" not in table), [],
                             language)
            # The placeholders the script fills.
            self.assertIn("{car}", table["js.chat.checking_car"])
            self.assertIn("{count}", table["js.chat.found_count"])
            self.assertIn("{count}", table["js.chat.more"])

    def test_every_string_the_template_reads_is_in_both_tables(self):
        template = (self.SITE / "templates/site/partials/chat.html").read_text(encoding="utf-8")
        read = set(re.findall(r"""\bt\[\s*["']([\w.]+)["']\s*\]""", template))
        read |= set(re.findall(r"""\btt\(\s*["']([\w.]+)["']""", template))
        self.assertGreater(len(read), 8, "the template's string keys were not found")
        for language, table in STRINGS.items():
            self.assertEqual(sorted(read - set(table)), [], language)

    def test_the_words_a_visitor_reads_have_no_long_dashes(self):
        # The site's copy rule, for the string table and for the reasons the facade words.
        texts = [(f"{language} {key}", value) for language, table in STRINGS.items()
                 for key, value in table.items()]
        texts += [(f"reason {basis}", words) for basis, pair in REASONS.items()
                  for words in pair]
        for where, value in texts:
            self.assertNotRegex(value, "[–—]", where)


if __name__ == "__main__":
    unittest.main()
