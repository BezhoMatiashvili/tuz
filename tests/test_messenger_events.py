"""Inbox rows read as Messenger events: every kind the receiver stores, and anything malformed
becomes Ignored without raising. Payloads are shaped like Meta's v26 webhooks."""

from __future__ import annotations

import json
import random
import unittest
from datetime import UTC, datetime, timedelta

from automotive import messenger_events as events
from automotive.messenger_events import (
    CommentEvent,
    CustomerMessage,
    Ignored,
    Oversized,
    PageEcho,
    Postback,
    Referral,
    ReferralEvent,
    parse,
)

PAGE, PSID, APP, OTHER_APP = "61595318832800", "24681357924680", "1234567890123", "263902037430900"
MS = 1_762_855_200_000  # 2025-11-11 10:00:00 UTC in milliseconds
AT = datetime(2025, 11, 11, 10, 0, tzinfo=UTC)
PHOTO = "https://scontent.example/private-photo.jpg?token=private-token"  # gitleaks:allow


def item(**fields) -> dict:
    """A messaging item from the customer to the page, as the receiver stores it."""
    return {"sender": {"id": PSID}, "recipient": {"id": PAGE}, "timestamp": MS,
            events.ENTRY_TIME_KEY: MS + 500, **fields}


def echo(**message) -> dict:
    return {"sender": {"id": PAGE}, "recipient": {"id": PSID}, "timestamp": MS,
            "message": {"mid": "m_echo1", "is_echo": True, **message}}


def comment(**value) -> dict:
    return {"field": "feed", "value": {
        "item": "comment", "verb": "add", "comment_id": "987_654", "post_id": f"{PAGE}_987",
        "parent_id": f"{PAGE}_987", "from": {"id": PSID, "name": "ნინო ბერიძე"},
        "message": "F30 B46 ხუნდები გაქვთ?", "created_time": 1_762_855_200, **value},
        events.ENTRY_TIME_KEY: 1_762_855_260}


class CustomerMessageTests(unittest.TestCase):
    def test_a_text_message_with_a_quick_reply_and_a_reply_to(self):
        event = parse("messages", item(message={
            "mid": "m_1", "text": "  წინა ხუნდები  ", "quick_reply": {"payload": "CHOICE:2"},
            "reply_to": {"mid": "m_0"}}), page_id=PAGE)
        self.assertEqual(event, CustomerMessage(
            psid=PSID, page_id=PAGE, mid="m_1", text="წინა ხუნდები", attachments=(),
            sticker=False, quick_reply="CHOICE:2", referral=None, reply_to_mid="m_0",
            sent_at=AT))
        self.assertFalse(event.standby)

    def test_attachments_keep_their_type_and_never_their_link(self):
        event = parse("messages", item(message={"mid": "m_2", "attachments": [
            {"type": "image", "payload": {"url": PHOTO}},
            {"type": "audio", "payload": {"url": PHOTO}},
            {"type": "location", "payload": {"coordinates": {"lat": 41.7, "long": 44.8}}},
            {"type": "Some Odd Type!", "payload": None}, "not an attachment"]}))
        self.assertIsInstance(event, CustomerMessage)
        self.assertEqual(event.text, "")
        self.assertEqual(event.attachments, (
            {"type": "image", "has_url": True}, {"type": "audio", "has_url": True},
            {"type": "location", "has_url": False}, {"type": "unknown", "has_url": False}))
        self.assertFalse(event.sticker)
        self.assertNotIn("private", repr(event))

    def test_a_sticker_is_flagged(self):
        for message in ({"mid": "m_3", "sticker_id": 369239263222822, "attachments": [
                            {"type": "image", "payload": {"url": PHOTO,
                                                          "sticker_id": 369239263222822}}]},
                        {"mid": "m_4", "attachments": [
                            {"type": "image", "payload": {"sticker_id": "369239263222822"}}]}):
            with self.subTest(mid=message["mid"]):
                event = parse("messages", item(message=message))
                self.assertTrue(event.sticker)
                self.assertEqual(event.attachments[0]["type"], "image")

    def test_an_unsupported_message_counts_as_something_unreadable(self):
        event = parse("messages", item(message={"mid": "m_5", "is_unsupported": True}))
        self.assertEqual(event.attachments, ({"type": "unsupported", "has_url": False},))

    def test_text_is_capped_and_cleaned(self):
        event = parse("messages", item(message={"mid": "m_6", "text": "ა\x00" * 5000}))
        self.assertEqual(len(event.text), events.TEXT_LIMIT)
        self.assertNotIn("\x00", event.text)

    def test_a_referral_from_an_ad_keeps_the_ad_and_never_its_media(self):
        referral = {"source": "ADS", "type": "OPEN_THREAD", "ad_id": "120211234567890",
                    "ref": "garage-oil", "ads_context_data": {
                        "ad_title": "ზეთის შეცვლა", "photo_url": PHOTO, "post_id": "1_2"}}
        for where in ("message", "item"):
            with self.subTest(where=where):
                message = {"mid": f"m_{where}", "text": "გამარჯობა"}
                fields = {"message": message}
                if where == "message":
                    message["referral"] = referral
                else:
                    fields["referral"] = referral
                event = parse("messages", item(**fields))
                self.assertEqual(event.referral, Referral(
                    source="ADS", type="OPEN_THREAD", ad_id="120211234567890",
                    ref="garage-oil", ads_title="ზეთის შეცვლა"))
                self.assertEqual(event.referral.channel, "ads")
                self.assertNotIn("private", repr(event))

    def test_ids_and_times_meta_may_send_as_numbers(self):
        event = parse("messages", {"sender": {"id": int(PSID)}, "recipient": {"id": int(PAGE)},
                                   "timestamp": str(MS), "message": {"mid": "m_7", "text": "x"}})
        self.assertEqual((event.psid, event.page_id, event.sent_at), (PSID, PAGE, AT))

    def test_a_missing_or_absurd_time_falls_back_to_the_entry_then_the_row(self):
        received = AT + timedelta(minutes=5)
        for timestamp, extra, expected in (
                (None, {events.ENTRY_TIME_KEY: MS + 1000}, AT + timedelta(seconds=1)),
                (10**18, {events.ENTRY_TIME_KEY: MS + 1000}, AT + timedelta(seconds=1)),
                (True, {}, received)):
            with self.subTest(timestamp=timestamp):
                raw = {"sender": {"id": PSID}, "recipient": {"id": PAGE}, "timestamp": timestamp,
                       "message": {"mid": "m_8", "text": "x"}, **extra}
                event = parse("messages", raw, received_at=received)
                self.assertEqual(event.sent_at, expected)
                self.assertEqual(event.sent_at.tzinfo, UTC)
        now = parse("messages", {"sender": {"id": PSID}, "recipient": {"id": PAGE},
                                 "message": {"mid": "m_9", "text": "x"}})
        self.assertLess(abs((now.sent_at - datetime.now(UTC)).total_seconds()), 60)

    def test_standby_items_are_parsed_and_marked(self):
        message = parse("standby", item(message={"mid": "m_sb", "text": "ფასი?"}))
        self.assertIsInstance(message, CustomerMessage)
        self.assertTrue(message.standby)
        echoed = parse("standby", echo(text="დიახ, გვაქვს", app_id=int(OTHER_APP)))
        self.assertIsInstance(echoed, PageEcho)
        self.assertTrue(echoed.standby)


class EchoTests(unittest.TestCase):
    def test_an_echo_names_the_customer_it_went_to_and_the_app_that_sent_it(self):
        event = parse("messages", echo(text="მარაგშია, 85 ₾", app_id=int(APP),
                                       metadata="bot:reply:42"), page_id=PAGE)
        self.assertEqual(event, PageEcho(
            psid=PSID, page_id=PAGE, mid="m_echo1", text="მარაგშია, 85 ₾", app_id=APP,
            metadata="bot:reply:42", sent_at=AT))

    def test_an_echo_without_an_app_id_or_text_is_still_the_shops_message(self):
        typed = parse("messages", echo(text="ხვალ მობრძანდით"))
        self.assertEqual((typed.app_id, typed.text), ("", "ხვალ მობრძანდით"))
        photo = parse("messages", echo(attachments=[{"type": "image",
                                                     "payload": {"url": PHOTO}}]))
        self.assertIsInstance(photo, PageEcho)
        self.assertEqual((photo.text, photo.attachments),
                         ("", ({"type": "image", "has_url": True},)))
        self.assertNotIn("private", repr(photo))
        bare = parse("messages", echo())
        self.assertIsInstance(bare, PageEcho)
        self.assertEqual(parse("messages", echo(app_id="not-a-number")).app_id, "")


class PostbackAndReferralTests(unittest.TestCase):
    def test_a_postback_with_a_referral(self):
        event = parse("messages", item(postback={
            "mid": "m_pb", "title": "დაწყება", "payload": "GET_STARTED",
            "referral": {"ref": "fb-ad", "source": "ADS", "type": "OPEN_THREAD",
                         "ad_id": "120211234567890"}}))
        self.assertEqual(event, Postback(
            psid=PSID, page_id=PAGE, mid="m_pb", title="დაწყება", payload="GET_STARTED",
            referral=Referral(source="ADS", type="OPEN_THREAD", ad_id="120211234567890",
                              ref="fb-ad", ads_title=""), sent_at=AT))

    def test_a_postback_without_a_mid_or_referral(self):
        event = parse("messages", item(postback={"title": "Start", "payload": "GET_STARTED"}))
        self.assertEqual((event.mid, event.referral), ("", None))

    def test_a_referral_from_a_link(self):
        event = parse("messages", item(referral={"ref": "oil-change", "source": "shortlink",
                                                 "type": "open_thread"}))
        self.assertEqual(event, ReferralEvent(psid=PSID, page_id=PAGE, referral=Referral(
            source="SHORTLINK", type="OPEN_THREAD", ad_id="", ref="oil-change", ads_title=""),
            sent_at=AT))
        self.assertEqual(event.referral.channel, "shortlink")
        self.assertEqual(Referral("CUSTOMER_CHAT_PLUGIN", "OPEN_THREAD", "", "", "").channel,
                         "other")

    def test_an_empty_referral_is_ignored(self):
        self.assertEqual(parse("messages", item(referral={"foo": 1})), Ignored("bad_referral"))


class CommentTests(unittest.TestCase):
    def test_a_new_comment(self):
        event = parse("feed", comment(), page_id=PAGE)
        self.assertEqual(event, CommentEvent(
            page_id=PAGE, verb="add", item="comment", comment_id="987_654",
            post_id=f"{PAGE}_987", parent_id=f"{PAGE}_987", from_id=PSID,
            from_name="ნინო ბერიძე", message="F30 B46 ხუნდები გაქვთ?", created_time=AT,
            hidden=None))

    def test_the_page_comes_from_the_post_when_the_row_does_not_say(self):
        self.assertEqual(parse("feed", comment()).page_id, PAGE)

    def test_an_edited_comment_with_an_iso_time(self):
        event = parse("feed", comment(verb="edited", message="შეცვლილი",
                                      created_time="2025-11-11T10:00:00+0000"))
        self.assertEqual((event.verb, event.message, event.created_time),
                         ("edited", "შეცვლილი", AT))

    def test_a_removed_comment_has_no_text_and_maybe_no_time(self):
        value = comment(verb="remove")
        del value["value"]["message"], value["value"]["created_time"]
        event = parse("feed", value)
        self.assertEqual((event.verb, event.message, event.created_time), ("remove", "", None))

    def test_a_new_comment_without_a_time_is_as_old_as_its_delivery(self):
        value = comment()
        del value["value"]["created_time"]
        self.assertEqual(parse("feed", value).created_time, AT + timedelta(seconds=60))

    def test_hidden_comes_from_the_flag_or_the_verb(self):
        self.assertTrue(parse("feed", comment(verb="hide")).hidden)
        self.assertFalse(parse("feed", comment(verb="unhide")).hidden)
        self.assertTrue(parse("feed", comment(verb="edited", is_hidden=True)).hidden)

    def test_other_feed_items_and_fields_are_ignored(self):
        self.assertEqual(parse("feed", comment(item="status")), Ignored("not_a_comment"))
        self.assertEqual(parse("feed", comment(item="reaction")), Ignored("not_a_comment"))
        self.assertEqual(parse("mention", {"field": "mention", "value": {}}),
                         Ignored("unsupported_field"))
        self.assertEqual(parse("unknown", {"field": "Bad Field!", "value": {}}),
                         Ignored("unsupported_field"))

    def test_ids_the_table_could_not_hold_are_refused(self):
        self.assertEqual(parse("feed", comment(comment_id="12'; DROP")), Ignored("bad_comment"))
        self.assertEqual(parse("feed", comment(post_id=None)), Ignored("bad_comment"))
        self.assertEqual(parse("feed", comment(verb=None)), Ignored("bad_comment"))
        self.assertEqual(parse("feed", comment(**{"from": {"id": "x"}})).from_id, "")


class OversizedTests(unittest.TestCase):
    def test_the_receivers_marker_keeps_ids_only(self):
        marker = {events.OVERSIZED_KEY: True, "_bytes": 70_000, "_kind": "msg",
                  "ids": {"sender": PSID, "recipient": PAGE, "timestamp": MS, "mid": "m_big"},
                  events.ENTRY_TIME_KEY: MS}
        self.assertEqual(parse("messages", marker, page_id=PAGE), Oversized(
            kind="msg", psid=PSID, page_id=PAGE, mid="m_big", comment_id="", post_id="",
            sent_at=AT))
        echoed = dict(marker, _kind="echo", ids={"sender": PAGE, "recipient": PSID,
                                                 "mid": "m_e"})
        self.assertEqual(parse("messages", echoed).psid, PSID)
        changed = {events.OVERSIZED_KEY: True, "_kind": "chg",
                   "ids": {"comment_id": "987_654", "post_id": f"{PAGE}_987"}}
        found = parse("feed", changed, page_id=PAGE)
        self.assertEqual((found.kind, found.psid, found.comment_id, found.page_id),
                         ("chg", "", "987_654", PAGE))


class MalformedTests(unittest.TestCase):
    def test_receipts_and_unknown_items_are_ignored_by_name(self):
        self.assertEqual(parse("messages", item(read={"watermark": MS})), Ignored("read"))
        self.assertEqual(parse("messages", item(reaction={"reaction": "love", "mid": "m"})),
                         Ignored("reaction"))
        self.assertEqual(parse("messages", item(something_new={})),
                         Ignored("unknown_messaging"))

    def test_rows_that_cannot_be_a_chat_of_this_page(self):
        cases = [
            (item(message={"text": "no mid"}), "missing_mid"),
            (item(message={"mid": "m" * 201, "text": "x"}), "missing_mid"),
            (item(message={"mid": "m_x"}), "empty_message"),
            (item(message={"mid": "m_x", "text": "x", "is_deleted": True}), "deleted"),
            (item(message="text"), "malformed"),
            (item(sender={"id": "abc"}, message={"mid": "m_x", "text": "x"}), "missing_party"),
            (item(sender=None, message={"mid": "m_x", "text": "x"}), "missing_party"),
            (item(sender={"id": PAGE}, message={"mid": "m_x", "text": "x"}), "page_to_itself"),
        ]
        for payload, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(parse("messages", payload, page_id=PAGE), Ignored(reason))
        other = item(recipient={"id": "11111"}, message={"mid": "m_x", "text": "x"})
        self.assertEqual(parse("messages", other, page_id=PAGE), Ignored("other_page"))
        self.assertIsInstance(parse("messages", other), CustomerMessage)  # no page to compare

    def test_anything_at_all_is_an_event_and_never_an_exception(self):
        self.assertEqual(parse("messages", None), Ignored("malformed"))
        self.assertEqual(parse("messages", ["a", "list"]), Ignored("malformed"))
        self.assertEqual(parse(None, item()), Ignored("malformed"))
        generator = random.Random(1886)
        leaves = [None, True, 0, -1, 1.5, float("nan"), "", "x", "\ud800", "0" * 40, PSID,
                  10**30, [], {}]
        keys = ["sender", "recipient", "message", "postback", "referral", "id", "mid", "text",
                "timestamp", "attachments", "payload", "value", "field", "item", "verb",
                "comment_id", "post_id", "from", "is_echo", "app_id", "quick_reply",
                events.ENTRY_TIME_KEY, events.OVERSIZED_KEY, "ids", "_kind"]

        def junk(depth: int):
            if depth > 3 or generator.random() < 0.3:
                return generator.choice(leaves)
            if generator.random() < 0.3:
                return [junk(depth + 1) for _ in range(generator.randint(0, 3))]
            return {generator.choice(keys): junk(depth + 1)
                    for _ in range(generator.randint(0, 5))}

        for _ in range(3000):
            field = generator.choice(["messages", "standby", "feed", "unknown", ""])
            event = parse(field, junk(0), page_id=generator.choice(["", PAGE, "bad"]))
            self.assertIsInstance(event, CustomerMessage | PageEcho | Postback | ReferralEvent
                                  | CommentEvent | Oversized | Ignored)
            json.dumps(repr(event))  # every value is printable

    def test_the_reserved_keys_match_the_receiver(self):
        from automotive import meta_receiver

        self.assertEqual(events.ENTRY_TIME_KEY, meta_receiver.ENTRY_TIME_KEY)
        self.assertEqual(events.OVERSIZED_KEY, meta_receiver.OVERSIZED_KEY)


if __name__ == "__main__":
    unittest.main()
