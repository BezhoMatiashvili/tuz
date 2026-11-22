"""Messenger layout of a public turn view: the 2,000-character split, part lines, quick-reply
limits, the numbered fallback, the answer a customer chose, and the channel's own texts."""

from __future__ import annotations

import random
import re
import unittest

from automotive import messenger_render as render_module
from automotive.messenger_render import (
    CHOICE_PREFIX,
    MORE_PAYLOAD,
    QUICK_REPLY_LIMIT,
    TEXT_LIMIT,
    RenderedReply,
    availability_text,
    bot_failed_text,
    bot_help_text,
    bot_on_refused_text,
    bot_status_text,
    bot_switched_text,
    cap_notice,
    card_line,
    conflicting_vins_text,
    fallback_text,
    late_apology,
    media_only_text,
    parse_choice,
    price_text,
    render,
    split_text,
)

GEORGIAN = re.compile("[Ⴀ-ჿ]")
WORDS = ("სამუხრუჭე", "ხუნდი", "წინა", "უკანა", "ზეთის", "ფილტრი", "მანქანა", "ძრავი",
         "ნაწილი", "მარაგშია", "ფასი", "ლარი", "BMW", "F30", "B46")
NBSP = " "


def units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def view(**changes) -> dict:
    """A public turn view as site_chat.public_turn returns it."""
    return {"status": "COMPLETE", "answer": "", "note": "", "language": "ka", "car": "",
            "question": None, "suggestions": [], "quick_replies": [], "groups": [],
            "cards": [], "total": 0, "offset": 0, "next_offset": None, "checked": 0,
            "checking": 0, **changes}


def card(name: str = "სამუხრუჭე ხუნდი წინა", **changes) -> dict:
    return {"name": name, "fits": "TOYOTA CAMRY 2012-2017", "brand": "SATO", "price": 45.5,
            "availability": "in_stock", "group": "", "reason": "", "confidence": "listed",
            **changes}


def options(*labels: str) -> dict:
    return {"options": [{"label": label, "value": label} for label in labels]}


def text_of(reply: RenderedReply) -> str:
    return "\n".join(message["text"] for message in reply.messages)


def check_messages(test: unittest.TestCase, reply: RenderedReply) -> None:
    """The Send API's limits hold for every message, and only the last has quick replies."""
    for index, message in enumerate(reply.messages):
        test.assertTrue(message["text"].strip())
        test.assertLessEqual(units(message["text"]), TEXT_LIMIT)
        if index < len(reply.messages) - 1:
            test.assertNotIn("quick_replies", message)
    quick = reply.messages[-1].get("quick_replies", []) if reply.messages else []
    test.assertLessEqual(len(quick), QUICK_REPLY_LIMIT)
    for button in quick:
        test.assertEqual(button["content_type"], "text")
        test.assertTrue(0 < units(button["title"]) <= 20)
        test.assertTrue(0 < len(button["payload"]) <= 1000)


class SplitTests(unittest.TestCase):
    def test_short_text_is_one_piece_and_nothing_is_empty(self):
        self.assertEqual(split_text("  გამარჯობა!  "), ["გამარჯობა!"])
        self.assertEqual(split_text(""), [])
        self.assertEqual(split_text(None), [])

    def test_words_are_never_broken_unless_one_is_longer_than_the_limit(self):
        generator = random.Random(1886)
        text = " ".join(generator.choice(WORDS) for _ in range(1500))  # no sentence marks
        pieces = split_text(text)
        self.assertGreater(len(pieces), 2)
        for piece in pieces:
            self.assertLessEqual(len(piece), TEXT_LIMIT)
            self.assertTrue(set(piece.split()) <= set(WORDS), "a word was cut")
        self.assertEqual(" ".join(pieces).split(), text.split())
        self.assertEqual(split_text("ა" * 4500), ["ა" * 2000, "ა" * 2000, "ა" * 500])

    def test_paragraphs_then_sentences_then_lines(self):
        first, second = "ა" * 1500 + ".", "ბ" * 1500 + "."
        self.assertEqual(split_text(f"{first}\n\n{second}"), [first, second])
        sentences = " ".join(f"წინადადება ნომერი {number}." for number in range(150))
        pieces = split_text(sentences)
        self.assertTrue(all(piece.endswith(".") for piece in pieces), pieces[0][-20:])
        lines = "\n".join(f"• ხაზი {number} ფასი 10 ₾" for number in range(200))
        pieces = split_text(lines)
        self.assertTrue(all(piece.splitlines()[-1].startswith("• ხაზი") for piece in pieces))
        self.assertEqual("\n".join(pieces), lines)

    def test_a_short_first_paragraph_does_not_become_a_message_of_its_own(self):
        text = "გამარჯობა.\n\n" + " ".join(["ეს არის წინადადება."] * 200)
        pieces = split_text(text)
        self.assertTrue(pieces[0].startswith("გამარჯობა.\n\nეს არის"))
        self.assertGreater(len(pieces[0]), TEXT_LIMIT // 2)

    def test_length_is_counted_as_messenger_counts_it(self):
        pieces = split_text("😀 " * 1500)  # each emoji is two UTF-16 units
        self.assertGreater(len(pieces), 2)
        self.assertTrue(all(units(piece) <= TEXT_LIMIT for piece in pieces))

    def test_no_text_is_lost_across_pieces(self):
        generator = random.Random(7)
        for _ in range(40):
            parts = []
            for _ in range(generator.randint(1, 400)):
                word = generator.choice(WORDS + ("ა" * 2100, "😀", "?", "\n", "\n\n"))
                parts.append(word + generator.choice(("", " ", ". ", "\n", "! ")))
            text = "".join(parts)
            pieces = split_text(text)
            self.assertTrue(all(piece and units(piece) <= TEXT_LIMIT for piece in pieces))
            self.assertEqual("".join("".join(pieces).split()), "".join(text.split()))


class CardTests(unittest.TestCase):
    def test_a_part_line_in_georgian_and_english(self):
        self.assertEqual(card_line(card(sap_code="000123"), "ka"),
                         f"• სამუხრუჭე ხუნდი წინა — SATO — SAP კოდი: 000123 — "
                         f"45.50{NBSP}₾ — მარაგშია")
        self.assertEqual(card_line(card(price=None, availability="soon", brand=""), "en"),
                         "• სამუხრუჭე ხუნდი წინა — Price on request — At the warehouse, 1-2 hours")
        self.assertEqual(card_line(card(name="", fits=""), "ka"), "")

    def test_prices_and_availability(self):
        self.assertEqual(price_text(1234.5, "ka"), f"1{NBSP}234.50{NBSP}₾")
        self.assertEqual(price_text(120, "en"), "₾120")
        self.assertEqual(price_text(0.999, "en"), "₾1")
        for empty in (None, 0, -5, float("nan"), True, "45", 1e300):
            self.assertEqual(price_text(empty, "ka"), "ფასი შეთანხმებით")
        self.assertEqual(availability_text("unavailable", "ka"), "არ არის მარაგში")
        self.assertEqual(availability_text("bogus", "en"), "Ask the shop")
        self.assertEqual(availability_text("in_stock", "ru"), "მარაგშია")

    def test_five_lines_then_how_many_more_and_a_more_button(self):
        cards = [card(f"ნაწილი {number}", sap_code=f"00000{number}")
                 for number in range(1, 9)]
        reply = render(view(answer="ნაპოვნია 8 ნაწილი.", cards=cards, total=8), language="ka")
        check_messages(self, reply)
        text = text_of(reply)
        self.assertEqual(text.count("• "), 5)
        self.assertIn("ნაწილი 5", text)
        self.assertIn("ნაწილი 5 — SATO — SAP კოდი: 000005", text)
        self.assertNotIn("ნაწილი 6", text)
        self.assertIn("კიდევ 3 ნაწილი მოიძებნა.", text)
        self.assertEqual(reply.messages[-1]["quick_replies"],
                         [{"content_type": "text", "title": "მეტის ნახვა",
                           "payload": MORE_PAYLOAD}])
        self.assertEqual(reply.next_offset, 5)
        following = render(view(answer="ნაპოვნია 8 ნაწილი.", cards=cards, total=8),
                           language="ka", offset=reply.next_offset)
        self.assertEqual(text_of(following).count("• "), 3)
        self.assertIn("ნაწილი 6 — SATO — SAP კოდი: 000006", text_of(following))
        self.assertNotIn("ნაპოვნია 8", text_of(following))  # the answer is not repeated
        self.assertNotIn("quick_replies", following.messages[-1])
        self.assertIsNone(following.next_offset)
        english = render(view(cards=cards[:6], total=6), language="en")
        self.assertIn("1 more part found.", text_of(english))
        self.assertIn("SAP code: 000001", text_of(english))
        self.assertEqual(english.messages[-1]["quick_replies"][0]["title"], "Show more")

    def test_group_headings_only_when_the_parts_are_in_several_groups(self):
        groups = [{"id": "g1", "label": "წინა ხუნდები", "count": 2},
                  {"id": "g2", "label": "უკანა ხუნდები", "count": 1}]
        cards = [card("წინა 1", group="g1"), card("წინა 2", group="g1"),
                 card("უკანა 1", group="g2")]
        text = text_of(render(view(cards=cards, total=3, groups=groups), language="ka"))
        self.assertIn("წინა ხუნდები:\n• წინა 1", text)
        self.assertIn("\n\nუკანა ხუნდები:\n• უკანა 1", text)
        single = text_of(render(view(cards=cards[:2], total=2, groups=groups[:1]),
                                language="ka"))
        self.assertNotIn("ხუნდები:", single)


class QuickReplyTests(unittest.TestCase):
    def test_answers_that_fit_become_numbered_quick_replies_on_the_last_message(self):
        reply = render(view(answer="ა" * 3000 + ". წინა თუ უკანა?",
                            question=options("წინა", "უკანა", "ორივე")), language="ka")
        check_messages(self, reply)
        self.assertEqual(len(reply.messages), 2)
        self.assertEqual([(button["title"], button["payload"])
                          for button in reply.messages[-1]["quick_replies"]],
                         [("წინა", "CHOICE:1"), ("უკანა", "CHOICE:2"), ("ორივე", "CHOICE:3")])
        self.assertEqual((reply.options, reply.numbered), (["წინა", "უკანა", "ორივე"], False))

    def test_a_label_too_long_turns_every_answer_into_a_numbered_list(self):
        labels = ("2.0 ბენზინი (N20)", "3.0 დიზელი, xDrive, 2016 წლიდან", "არ ვიცი")
        reply = render(view(answer="რომელი ძრავია?", question=options(*labels)), language="ka")
        check_messages(self, reply)
        self.assertTrue(reply.numbered)
        self.assertEqual(reply.options, list(labels))
        text = text_of(reply)
        for number, label in enumerate(labels, 1):
            self.assertIn(f"{number}. {label}", text)
        self.assertIn("უპასუხეთ ნომრით", text)
        self.assertNotIn("quick_replies", reply.messages[-1])
        many = render(view(answer="Which?", question=options(*[f"Opt {n}" for n in range(14)])),
                      language="en")
        self.assertTrue(many.numbered)
        self.assertIn("14. Opt 13", text_of(many))

    def test_the_numbered_list_joins_the_last_message_within_the_limit(self):
        reply = render(view(answer="ა" * 1990, question=options("ა" * 25, "ბ" * 25)),
                       language="ka")
        check_messages(self, reply)
        self.assertEqual(len(reply.messages), 2)
        self.assertTrue(reply.messages[-1]["text"].startswith("1. "))

    def test_each_suggestion_that_fits_is_a_button(self):
        fitting = render(view(answer="აქ არის.", suggestions=[
            {"label": "ზეთის ფილტრიც", "value": "ზეთის ფილტრი მინდა"},
            {"label": "უკანა ხუნდები", "value": "უკანა ხუნდები"}]), language="ka")
        self.assertEqual([button["payload"] for button in fitting.messages[-1]["quick_replies"]],
                         ["SUGGEST:1", "SUGGEST:2"])
        self.assertEqual((fitting.options, fitting.numbered), ([], False))
        # One label over 20 characters no longer takes the others with it (2026-11-12 dry run:
        # the VIN prompt vanished because the other suggestion was 21 characters long).
        one_long = render(view(answer="აქ არის.", suggestions=[
            {"label": "ძრავის ზეთიც მჭირდება", "value": "x"},
            {"label": "VIN კოდის გაგზავნა", "value": "x"}]), language="ka")
        buttons = one_long.messages[-1]["quick_replies"]
        self.assertEqual([(b["title"], b["payload"]) for b in buttons],
                         [("VIN კოდის გაგზავნა", "SUGGEST:2")])
        all_long = render(view(answer="აქ არის.", suggestions=[
            {"label": "ძრავის ზეთის შეცვლაც გინდათ?", "value": "x"}]), language="ka")
        self.assertNotIn("quick_replies", all_long.messages[-1])

    def test_answers_more_and_suggestions_share_thirteen_places(self):
        labels = [f"პასუხი {number}" for number in range(1, 9)]
        cards = [card(f"ნაწილი {number}") for number in range(7)]
        reply = render(view(answer="აირჩიეთ.", question=options(*labels), cards=cards, total=7,
                            suggestions=[{"label": f"ნაბიჯი {n}", "value": "x"}
                                         for n in range(5)]), language="ka")
        check_messages(self, reply)
        payloads = [button["payload"] for button in reply.messages[-1]["quick_replies"]]
        self.assertEqual(payloads[:8], [f"{CHOICE_PREFIX}{n}" for n in range(1, 9)])
        self.assertEqual(payloads[8], MORE_PAYLOAD)
        self.assertEqual(len(payloads), 9)  # five more suggestions would make 14: none go

    def test_an_answer_that_repeats_as_a_suggestion_is_offered_once(self):
        reply = render(view(answer="?", question=options("წინა", "უკანა"),
                            suggestions=[{"label": "უკანა", "value": "უკანა"}]), language="ka")
        self.assertEqual(len(reply.messages[-1]["quick_replies"]), 2)

    def test_options_keep_their_places(self):
        reply = render(view(answer="?", question={"options": [
            {"label": "A", "value": "a"}, {"label": "", "value": "b"}, "C"]}), language="en")
        self.assertEqual(reply.options, ["A", "b", "C"])


class RenderTests(unittest.TestCase):
    def test_the_answer_note_and_an_opening(self):
        reply = render(view(answer="ნაპოვნია ერთი ნაწილი.", note="მარაგი დაზუსტდება.",
                            cards=[card()], total=1), language="ka",
                       opening=late_apology("ka"))
        check_messages(self, reply)
        self.assertEqual(text_of(reply).split("\n\n"), [
            "ბოდიშს გიხდით დაგვიანებული პასუხისთვის.", "ნაპოვნია ერთი ნაწილი.",
            "მარაგი დაზუსტდება.", f"• სამუხრუჭე ხუნდი წინა — SATO — 45.50{NBSP}₾ — მარაგშია"])
        self.assertEqual(reply.next_offset, None)

    def test_an_empty_view_sends_nothing(self):
        for empty in (view(), None, {}, "not a view"):
            self.assertEqual(render(empty, language="ka"),
                             RenderedReply(messages=[], options=[], numbered=False))

    def test_any_view_fits_the_send_api(self):
        generator = random.Random(42)
        for _ in range(200):
            labels = [generator.choice(WORDS) * generator.randint(1, 3)
                      for _ in range(generator.randint(0, 9))]
            cards = [card(generator.choice(WORDS) * generator.randint(1, 30),
                          price=generator.choice([None, 0, 12.5, 99999.99]))
                     for _ in range(generator.randint(0, 12))]
            reply = render(view(
                answer=" ".join(generator.choice(WORDS) for _ in range(generator.randint(0, 900))),
                note=generator.choice(["", "შენიშვნა."]),
                question=options(*labels) if len(labels) >= 2 else None, cards=cards,
                total=len(cards) + generator.randint(0, 3)),
                language=generator.choice(["ka", "en", "ru", None]))
            check_messages(self, reply)


class ChoiceTests(unittest.TestCase):
    OPTIONS = ["წინა", "უკანა", "ორივე"]

    def test_a_quick_reply_payload(self):
        self.assertEqual(parse_choice("უკანა", "CHOICE:2", self.OPTIONS, False), 2)
        self.assertIsNone(parse_choice("x", "CHOICE:4", self.OPTIONS, False))
        self.assertIsNone(parse_choice("x", "CHOICE:0", self.OPTIONS, False))
        # Options the worker no longer knows: our own payload is passed on for the engine.
        self.assertEqual(parse_choice("უკანა", "CHOICE:2", [], False), 2)
        self.assertIsNone(parse_choice("x", MORE_PAYLOAD, self.OPTIONS, False))
        self.assertIsNone(parse_choice("x", "SUGGEST:1", self.OPTIONS, False))

    def test_a_bare_number_counts_only_under_a_numbered_list(self):
        for text, expected in (("2", 2), (" 3 ", 3), ("1.", 1), ("2)", 2), ("4", None),
                               ("0", None), ("2 ცალი", None), ("²", None), ("", None),
                               ("ორი", None)):
            with self.subTest(text=text):
                self.assertEqual(parse_choice(text, None, self.OPTIONS, True), expected)
        self.assertIsNone(parse_choice("2", None, self.OPTIONS, False))
        self.assertIsNone(parse_choice("2", None, [], True))
        self.assertIsNone(parse_choice(None, None, None, True))


class TextTests(unittest.TestCase):
    TEXTS = (late_apology, media_only_text, conflicting_vins_text, bot_on_refused_text,
             bot_help_text, bot_failed_text, lambda language: fallback_text(language),
             lambda language: cap_notice(language),
             lambda language: bot_switched_text(language, True),
             lambda language: bot_status_text(language, enabled=False))

    def test_every_text_exists_for_any_language_value(self):
        for function in self.TEXTS:
            for language in ("ka", "en", "ru", None, 7, "auto"):
                text = function(language)
                self.assertTrue(text and len(text) < 300)
                self.assertEqual(bool(GEORGIAN.search(text)), language != "en", text)
                self.assertNotIn(" — ", text)  # sentences follow the site's no-em-dash rule

    def test_the_fallback_and_cap_point_to_the_phone(self):
        self.assertEqual(fallback_text("ka", "555 12 34 56"),
                         "ბოდიში, პასუხის მომზადება ახლა ვერ მოხერხდა. დაგვირეკეთ: 555 12 34 56")
        self.assertEqual(fallback_text("en", ""),
                         "Sorry, we could not prepare an answer just now. "
                         "Please write to us again in a little while.")
        self.assertEqual(fallback_text("ka", "555 12 34 56", "ჩვენ მალე დაგეხმარებით."),
                         "ჩვენ მალე დაგეხმარებით.\nტელეფონი: 555 12 34 56")
        self.assertIn("555 12 34 56", cap_notice("ka", " 555 12  34 56 "))
        self.assertIn("{phone}", render_module.TEXTS["en"]["cap_phone"])
        # Put in as it stands, never formatted: braces in a value cannot reach str.format.
        self.assertIn("at {0}{x} or", cap_notice("en", "{0}{x}"))
        self.assertTrue(fallback_text("ka", "{phone}").endswith("დაგვირეკეთ: {phone}"))

    def test_bot_status(self):
        self.assertEqual(bot_status_text("ka", enabled=True, turns_today=12, turn_limit=150,
                                         comments_enabled=False),
                         "ბოტი: ჩართულია\nდღევანდელი პასუხები: 12 / 150\n"
                         "კომენტარები: გამორთულია")
        self.assertEqual(bot_status_text("en", enabled=True, paused="ai_provider",
                                         turns_today=3),
                         "Bot: paused (AI provider account)\nReplies today: 3")
        self.assertEqual(bot_switched_text("en", False, changed=False), "The bot is already off.")
        self.assertIn("Auto reply", bot_on_refused_text("ka"))


if __name__ == "__main__":
    unittest.main()
