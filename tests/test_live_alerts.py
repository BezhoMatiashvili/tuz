"""Alarms and phone numbers from a finished bot turn (automotive.live_alerts): pure functions."""

from __future__ import annotations

import unittest

from automotive.live_alerts import AlertDraft, TurnAlerts, clean_phone, text, turn_alerts

IN_STORE = {"availability_status": "IN_STORE"}
SOLD = {"availability_status": "UNAVAILABLE"}


def payload(**extra):
    return {"engine_status": "OK", "cards": [], "car_label": "", **extra}


def staff(intent="none", note="", phone="", missing=()):
    return {"intent": intent, "note": note, "phone": phone, "missing": list(missing)}


def kinds(turn):
    return [(draft.kind, draft.reason) for draft in turn.drafts]


class CleanPhoneTests(unittest.TestCase):
    def check(self, value, said, expected):
        with self.subTest(value=value, said=said):
            self.assertEqual(clean_phone(value, said), expected)

    def test_separators_do_not_matter(self):
        for said in ("call me on 599 12 34 56 please", "599-12-34-56", "(599) 12 34 56",
                     "599.123.456", "599123456", "my number: 599123456."):
            self.check("599123456", [said], "599123456")

    def test_full_width_digits_are_digits(self):
        self.check("599123456", ["５９９１２３４５６"], "599123456")

    def test_the_model_may_add_or_drop_the_country_code(self):
        self.check("+995599123456", ["599 12 34 56"], "+995599123456")
        self.check("599123456", ["+995 599 12 34 56"], "599123456")
        self.check("00995599123456", ["+995 599 12 34 56"], "+995599123456")
        self.check("995599123456", ["599 12 34 56"], "+995599123456")

    def test_a_number_the_customer_never_wrote_is_dropped(self):
        self.check("599123457", ["call me on 599 12 34 56"], "")
        self.check("599123456", ["hello"], "")
        self.check("599123456", [], "")
        self.check("599123456", [None, 5], "")

    def test_a_piece_of_a_longer_number_is_not_a_phone(self):
        self.check("599123456", ["Bosch 1599123456789"], "")

    def test_a_phone_beside_a_year_is_found(self):
        self.check("599123456", ["Honda Fit 2014 599 12 34 56"], "599123456")

    def test_numbers_of_the_wrong_length_are_not_phones(self):
        self.check("12345", ["12345"], "")
        self.check("1" * 16, ["1" * 16], "")
        self.check(None, ["599123456"], "")
        self.check("", ["599123456"], "")

    def test_another_country_keeps_its_digits_and_plus(self):
        self.check("+49 151 23456789", ["+49 151 23456789"], "+4915123456789")

    def test_a_cut_out_of_a_longer_number_is_not_a_phone_even_beside_a_country_code(self):
        self.check("1234567", ["part 9951234567"], "")
        self.check("599123456", ["Bosch 995599123456789"], "")
        self.check("986424815", ["0986424815"], "")  # a part number is not a phone

    def test_any_separator_will_do(self):
        for said in ("599/12/34/56", "599,12,34,56", "599–12–34–56", "599_12_34_56",
                     "599 - 12 - 34 - 56"):
            self.check("599123456", [said], "599123456")

    def test_an_international_prefix_comes_back_as_a_plus(self):
        self.check("0049 151 23456789", ["0049 151 23456789"], "+4915123456789")
        self.check("004915123456789", ["+49 151 23456789"], "+4915123456789")


class TextTests(unittest.TestCase):
    def test_hidden_format_characters_never_reach_the_staff(self):
        # A text direction override, a zero-width space, a soft hyphen and an isolate.
        self.assertEqual(text("pads‮ evil​­ ok⁦", 50), "pads evil ok")
        self.assertEqual(text("a\x00b\tc", 50), "a b c")


class TurnAlertsTests(unittest.TestCase):
    def test_an_ordinary_turn_raises_nothing(self):
        self.assertFalse(turn_alerts(payload(outcome="offered", cards=[IN_STORE]), "pads"))
        self.assertFalse(turn_alerts(payload(outcome="chat"), "hello"))
        self.assertEqual(turn_alerts(None, "x"), TurnAlerts())
        self.assertEqual(turn_alerts({}, "x"), TurnAlerts())

    def test_a_part_the_bot_could_not_find(self):
        turn = turn_alerts(payload(outcome="nothing_found", car_label="Honda Fit 2014"),
                           "front brake pads for my fit")
        self.assertEqual(turn.drafts, (AlertDraft("part_not_found", "nothing_found",
                                                  "front brake pads for my fit",
                                                  "Honda Fit 2014"),))

    def test_nothing_found_with_cards_on_screen_is_not_believed(self):
        self.assertFalse(turn_alerts(payload(outcome="nothing_found", cards=[IN_STORE]), "x"))

    def test_sold_out_needs_every_card_out_of_stock(self):
        self.assertEqual(kinds(turn_alerts(payload(outcome="sold_out", cards=[SOLD, SOLD]), "oil")),
                         [("part_not_found", "sold_out")])
        self.assertFalse(turn_alerts(payload(outcome="sold_out", cards=[SOLD, IN_STORE]), "oil"))

    def test_a_part_missing_from_an_answer_that_offered_others(self):
        turn = turn_alerts(payload(outcome="offered", cards=[IN_STORE],
                                   staff=staff(missing=["oil filter", "  ", "cabin filter"])),
                           "oil change")
        self.assertEqual(kinds(turn), [("part_not_found", "partial")])
        self.assertEqual(turn.drafts[0].summary, "oil filter, cabin filter")

    def test_the_missing_list_is_capped(self):
        turn = turn_alerts(payload(outcome="offered", cards=[IN_STORE],
                                   staff=staff(missing=[f"part {number}" for number in range(9)])),
                           "x")
        self.assertEqual(turn.drafts[0].summary.count(","), 5)

    def test_visit_and_service_intents(self):
        for intent, kind in (("visit", "visit_request"), ("service", "service_booking")):
            with self.subTest(intent=intent):
                turn = turn_alerts(payload(outcome="offered", cards=[IN_STORE],
                                           staff=staff(intent, "Wants to come at 5 for the pads")),
                                   "can I come today")
                self.assertEqual(kinds(turn), [(kind, "")])
                self.assertEqual(turn.drafts[0].summary, "Wants to come at 5 for the pads")

    def test_an_unknown_intent_raises_nothing(self):
        self.assertFalse(turn_alerts(payload(outcome="chat", staff=staff("book")), "x"))

    def test_the_customers_words_stand_in_for_a_missing_note(self):
        turn = turn_alerts(payload(outcome="chat", staff=staff("visit")),
                           "  I will come   tomorrow  ")
        self.assertEqual(turn.drafts[0].summary, "I will come tomorrow")

    def test_a_handoff_asks_staff_to_take_over(self):
        turn = turn_alerts(payload(outcome="handoff"), "this is unacceptable")
        self.assertEqual(kinds(turn), [("needs_staff", "handoff")])

    def test_a_failed_turn_alarms_only_where_asked(self):
        failed = {"engine_status": "TIMEOUT", "outcome": "handoff", "cards": []}
        self.assertFalse(turn_alerts(failed, "x"))
        self.assertEqual(kinds(turn_alerts(failed, "x", alarm_failures=True)),
                         [("needs_staff", "failed")])

    def test_a_cancelled_turn_never_alarms(self):
        self.assertFalse(turn_alerts({"engine_status": "CANCELLED"}, "x", alarm_failures=True))

    def test_the_phone_rides_on_the_alarms_and_can_come_alone(self):
        turn = turn_alerts(payload(outcome="offered", cards=[IN_STORE],
                                   staff=staff("visit", "coming", "+995599123456")), "x")
        self.assertEqual(turn.phone, "+995599123456")
        self.assertEqual(turn.drafts[0].phone, "+995599123456")
        alone = turn_alerts(payload(outcome="chat", staff=staff(phone="599123456")),
                            "my number 599123456")
        self.assertEqual(alone.drafts, ())
        self.assertEqual(alone.phone, "599123456")
        self.assertTrue(alone)

    def test_a_malformed_phone_is_dropped(self):
        for bad in ("12", "abc", "599 123 456", "+", "1" * 20):
            with self.subTest(phone=bad):
                turn = turn_alerts(payload(outcome="chat", staff=staff(phone=bad)), "x")
                self.assertEqual(turn.phone, "")

    def test_a_failed_turn_carries_no_phone(self):
        failed = {"engine_status": "TIMEOUT", "staff": staff(phone="599123456")}
        self.assertEqual(turn_alerts(failed, "x", alarm_failures=True).phone, "")

    def test_long_text_is_cut(self):
        long = "x" * 500
        turn = turn_alerts(payload(outcome="chat", staff=staff("visit", long)), long)
        self.assertEqual(len(turn.drafts[0].summary), 160)
        turn = turn_alerts(payload(outcome="nothing_found"), long)
        self.assertEqual(len(turn.drafts[0].summary), 200)

    def test_a_turn_can_raise_two_alarms(self):
        turn = turn_alerts(payload(outcome="nothing_found",
                                   staff=staff("visit", "wants to come", missing=["pads"])),
                           "x")
        self.assertEqual(kinds(turn), [("part_not_found", "nothing_found"), ("visit_request", "")])
        self.assertEqual(turn.drafts[0].summary, "pads")


if __name__ == "__main__":
    unittest.main()
