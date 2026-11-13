"""Comments on the page's posts: stored, answered once in private, listed for the dashboard
(``automotive.meta_comments``).

A fake engine stands in for the assistant and a fake Graph for Meta; nothing leaves the process
and no model is called. The database tests (skipped without WAREHOUSE_TEST_ADMIN_DATABASE_URL)
run the worker's steps as a member of garage_messenger and the dashboard's as a member of
garage_site, so a query outside migration 0013's grants fails here as it would in production.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from automotive import meta_comments
from automotive.messenger_engine import EngineResult
from automotive.messenger_events import CommentEvent
from automotive.messenger_render import TEXT_LIMIT, card_line, late_apology
from automotive.meta_comments import (
    POST_LABEL,
    CommentActionError,
    CommentRetry,
    comment_link,
    engine_text,
    handle_comment_event,
    list_comments,
    parse_comment,
    post_excerpt,
    public_reply,
    reply_message,
    reply_skip,
    set_hidden,
    sync_comments,
)
from automotive.meta_graph import GraphError
from tests.test_meta_page_stats import (
    ADMIN,
    ADMIN_URL,
    APP,
    PAGE,
    FakeGraph,
    FeatureDatabase,
    meta_time,
    refused,
)

POST = f"{PAGE}_555"
PSID = "7000000000001234"
VIN = "WBA8E9C5XGK000001"
POST_TEXT = (f"ახალი წინა ხუნდები BMW F30-ისთვის, ვაყენებთ ადგილზე. VIN {VIN} მანქანაზე "
             "გუშინ დავაყენეთ. მოგვწერეთ!")
STATE = {"vehicle": {}, "vin": None, "category": "", "language": "ka", "pending": None,
         "history": [{"question": "F30 B46-ზე წინა ხუნდები გაქვთ?",
                      "answer": "დიახ, გვაქვს.", "shown": []}],
         "shown": [], "vehicle_revision": 0, "customer_stated": {},
         "resolution_candidates": [], "selected_configuration": {}, "unresolved_fields": [],
         "clarification_choices": [],
         "memory": {"language": "ka", "transcript": [
             {"user": "F30 B46-ზე წინა ხუნდები გაქვთ?", "assistant": "დიახ, გვაქვს."}]}}


def view(answer: str = "BMW F30-ისთვის წინა ხუნდები გვაქვს.", cards: int = 0,
         total: int | None = None, note: str = "") -> dict:
    """``site_chat.public_turn``'s shape."""
    return {"status": "OK", "answer": answer, "note": note, "language": "ka", "car": "BMW F30",
            "question": None, "suggestions": [], "quick_replies": [], "groups": [],
            "cards": [{"name": f"წინა ხუნდი {number}", "fits": "", "brand": "Brembo",
                       "price": 120.5, "availability": "in_stock", "group": "", "reason": "",
                       "confidence": "high"} for number in range(cards)],
            "total": cards if total is None else total, "offset": 0, "next_offset": None,
            "checked": 0, "checking": 0}


def outcome(status: str = "ok", *, error_code: str = "", public: dict | None = None,
            engine_status: str = "OK", state: dict | None = None) -> EngineResult:
    return EngineResult(status=status, public=view(cards=2) if public is None else public,
                        state=state or STATE, engine_status=engine_status, language="ka",
                        usage={"model_requests": 1, "input_tokens": 1200, "output_tokens": 80},
                        error_code=error_code, unsent_state=state or STATE)


class FakeEngine:
    """``MessengerEngine``'s surface: ``respond``, ``ready`` and ``shop_phone``."""

    def __init__(self, result: EngineResult | None = None, *, ready: bool = True,
                 during: Any = None) -> None:
        self.result = result or outcome()
        self.ready = ready
        self.during = during
        self.calls: list[dict] = []

    def respond(self, state, text, *, choice=None, language="auto", timeout=60, cancel=None):
        self.calls.append({"state": state, "text": text, "language": language,
                           "timeout": timeout, "choice": choice})
        if self.during is not None:
            self.during()
        return self.result

    def shop_phone(self) -> str:
        return "596 33 33 74"


def event(comment: str = "555_1001", *, verb: str = "add", author: str = "900000000000001",
          name: str = "ნინო", text: str = "F30 B46-ზე წინა ხუნდები გაქვთ?",
          created: datetime | None = None, parent: str = POST, hidden: bool | None = None,
          page: str = PAGE) -> CommentEvent:
    return CommentEvent(page_id=page, verb=verb, item="comment", comment_id=comment,
                        post_id=POST, parent_id=parent, from_id=author, from_name=name,
                        message=text, created_time=created or datetime.now(UTC), hidden=hidden)


class ComposingTests(unittest.TestCase):
    def test_the_engine_reads_the_comment_then_the_marked_post_without_its_vin(self):
        excerpt = post_excerpt(POST_TEXT)
        self.assertNotIn(VIN, excerpt)
        self.assertIn("[VIN]", excerpt)
        text = engine_text("ფასი?", excerpt)
        self.assertTrue(text.startswith("ფასი?\n\n["))
        self.assertIn(POST_LABEL, text)
        self.assertIn(f"«{excerpt}»", text)
        self.assertEqual(engine_text("ფასი?", ""), "ფასი?")
        self.assertLessEqual(len(engine_text("x" * 5000, "y" * 400)), 2000)
        long = post_excerpt("სიტყვა " * 200)
        self.assertLessEqual(len(long), 401)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(post_excerpt(None), "")

    def test_one_message_with_three_part_lines_and_never_more_than_messenger_takes(self):
        public = view(cards=5, total=8)
        message = reply_message(public, "ka")
        lines = [card_line(card, "ka") for card in public["cards"][:3]]
        self.assertTrue(message.startswith(public["answer"]))
        for line in lines:
            self.assertIn(line, message)
        self.assertNotIn(card_line(public["cards"][3], "ka"), message)
        self.assertTrue(message.endswith("კიდევ 5 ნაწილი მოიძებნა."))
        huge = reply_message(view("ძალიან გრძელი პასუხი. " * 400, cards=3,
                                  note="შენიშვნა."), "ka", opening=late_apology("ka"))
        self.assertLessEqual(len(huge.encode("utf-16-le")) // 2, TEXT_LIMIT)
        self.assertTrue(huge.startswith(late_apology("ka")))
        self.assertTrue(huge.endswith(card_line(view(cards=3)["cards"][2], "ka")))
        self.assertNotIn("შენიშვნა.", huge)  # the note gives way first
        self.assertEqual(reply_message({}, "ka"), "")
        english = reply_message(view("We have them.", cards=1, total=2), "en")
        self.assertTrue(english.endswith("1 more part found."))

    def test_when_a_comment_may_be_answered(self):
        now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
        on = {"comments_enabled": True, "comments_enabled_at": now - timedelta(days=1),
              "bot_paused_reason": ""}
        fresh = now - timedelta(minutes=5)
        cases = (({**on, "comments_enabled": False}, fresh, "ფასი?", "skipped_off"),
                 ({**on, "comments_enabled_at": None}, fresh, "ფასი?", "skipped_old"),
                 (on, now - timedelta(days=2), "ფასი?", "skipped_old"),
                 ({**on, "comments_enabled_at": now - timedelta(days=30)},
                  now - timedelta(days=8), "ფასი?", "skipped_old"),
                 ({**on, "bot_paused_reason": "ai_provider"}, fresh, "ფასი?", "skipped_paused"),
                 (on, fresh, "  ", "skipped_empty"),
                 (on, fresh, "ფასი?", ""))
        for settings, created, text, expected in cases:
            self.assertEqual(reply_skip(settings, created=created, now=now, text=text), expected)

    def test_backfilled_comments_are_read_defensively(self):
        item = {"id": "555_2001", "message": "რა ღირს?", "created_time": "2026-11-10T09:00:00+0000",
                "from": {"id": "900000000000002", "name": "გიორგი"}, "is_hidden": False,
                "permalink_url": "https://www.facebook.com/61595318832800/posts/555?comment_id=2001",
                "parent": {"id": POST}}
        parsed = parse_comment(item, POST, PAGE)
        self.assertEqual((parsed["comment_id"], parsed["parent_id"], parsed["author_name"],
                          parsed["from_page"], parsed["hidden"]),
                         ("555_2001", "", "გიორგი", False, False))
        page = parse_comment({**item, "from": {"id": PAGE}, "parent": {"id": "555_2002"}},
                             POST, PAGE)
        self.assertEqual((page["from_page"], page["parent_id"]), (True, "555_2002"))
        for broken in ({**item, "id": "x y"}, {**item, "created_time": "soon"}, None, []):
            self.assertIsNone(parse_comment(broken, POST, PAGE))
        self.assertEqual(comment_link("555_2001", POST),
                         f"https://www.facebook.com/{PAGE}/posts/555?comment_id=2001")
        self.assertEqual(comment_link("555_2001", POST, "https://fb.com/x"), "https://fb.com/x")
        self.assertEqual(comment_link("bad", "bad"), "")

    def test_only_comment_events_are_handled(self):
        self.assertEqual(handle_comment_event(object(), "not an event", FakeEngine()),
                         "comment_ignored")


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class CommentsDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = FeatureDatabase("meta_comments")
        cls.addClassCleanup(cls.db.close)

    def setUp(self) -> None:
        self.db.reset()
        self.db.connect_page()
        self.graph = FakeGraph()
        self.graph.answers[POST] = {"id": POST, "message": POST_TEXT,
                                    "created_time": meta_time(datetime.now(UTC)
                                                              - timedelta(days=2)),
                                    "permalink_url": f"https://www.facebook.com/{PAGE}/posts/555"}
        self.worker = self.db.context("worker", self.graph)
        self.site_store = self.db.store("site")
        self.site = self.db.context("site", self.graph)

    def comments_on(self, **settings: Any) -> None:
        self.site_store.set_comments(True, ADMIN)
        if settings:
            self.site_store.update_settings(settings, ADMIN)

    def handle(self, comment_event: CommentEvent, engine: FakeEngine | None = None,
               **options: Any) -> str:
        return handle_comment_event(self.worker, comment_event, engine or self.engine, **options)

    def comment(self, comment_id: str = "555_1001") -> dict | None:
        rows = self.db.rows("SELECT * FROM garage.meta_comments WHERE comment_id = %s",
                            (comment_id,))
        return rows[0] if rows else None

    def attempts(self) -> list[dict]:
        return self.db.rows("SELECT channel, psid, trigger_key, status, engine_status, "
                            "error_code, sent_mids, cost_usd::float8 AS cost FROM "
                            "garage.messenger_replies ORDER BY reply_id")

    @property
    def engine(self) -> FakeEngine:
        if not hasattr(self, "_engine"):
            self._engine = FakeEngine()
        return self._engine

    # -- gating ---------------------------------------------------------------------------

    def test_with_the_switch_off_a_comment_is_kept_and_nothing_is_sent(self):
        self.assertEqual(self.handle(event()), "comment_skipped_off")
        row = self.comment()
        self.assertEqual((row["source"], row["reply_status"], row["body"], row["author_name"],
                          row["parent_id"], row["from_page"]),
                         ("webhook", "skipped_off", "F30 B46-ზე წინა ხუნდები გაქვთ?", "ნინო", "",
                          False))
        self.assertEqual((self.engine.calls, self.graph.replies, self.attempts()), ([], [], []))

    def test_comments_before_the_switch_or_over_a_week_old_are_never_answered(self):
        self.comments_on()
        enabled_at = self.worker.store.settings()["comments_enabled_at"]
        self.assertEqual(self.handle(event(created=enabled_at - timedelta(minutes=1))),
                         "comment_skipped_old")
        self.assertEqual(self.comment()["reply_status"], "skipped_old")
        self.assertEqual(self.handle(event("555_1002", created=datetime.now(UTC)
                                           - timedelta(days=8))), "comment_too_old")
        self.assertIsNone(self.comment("555_1002"))
        self.assertEqual(self.handle(event("555_1003", text="")), "comment_skipped_empty")
        self.assertEqual((self.engine.calls, self.graph.replies), ([], []))

    def test_the_pages_own_comments_are_never_answered_and_mark_what_they_answer(self):
        self.handle(event())
        self.comments_on()
        self.assertEqual(self.handle(event("555_1002", author=PAGE, parent="555_1001")),
                         "comment_from_page")
        self.assertIsNone(self.comment("555_1002"))
        self.assertEqual(self.comment()["public_reply_id"], "555_1002")
        # A comment Meta delivers without an author, which is our recorded public reply.
        self.assertEqual(self.handle(event("555_1002", author="")), "comment_from_page")
        self.assertEqual(self.handle(event("555_1003", page="61500000000009")),
                         "comment_other_page")
        self.assertEqual((self.engine.calls, self.graph.replies), ([], []))

    # -- the reply ------------------------------------------------------------------------

    def test_a_comment_gets_one_private_reply_and_a_chat_that_remembers_it(self):
        self.comments_on()
        self.assertEqual(self.handle(event(), cost=lambda usage: 0.0123), "comment_replied")
        (call,) = self.engine.calls
        self.assertIsNone(call["state"])
        self.assertTrue(call["text"].startswith("F30 B46-ზე წინა ხუნდები გაქვთ?\n\n["))
        self.assertIn("[VIN]", call["text"])
        self.assertNotIn(VIN, call["text"])
        (page_id, comment_id, message), = self.graph.replies
        expected = reply_message(view(cards=2), "ka")
        self.assertEqual((page_id, comment_id, message), (PAGE, "555_1001", {"text": expected}))
        self.assertNotIn("quick_replies", message)
        thread = self.worker.store.get_thread(PSID, with_state=True)
        self.assertEqual((thread["source"], thread["customer_name"], thread["engine_state"]),
                         ("comment", "ნინო", STATE))
        (bot,) = self.worker.store.messages(PSID)
        self.assertEqual((bot["sender"], bot["mid"], bot["body"], bot["app_id"]),
                         ("bot", "m_fake_0001", expected, APP))
        (attempt,) = self.attempts()
        self.assertEqual((attempt["channel"], attempt["psid"], attempt["trigger_key"],
                          attempt["status"], attempt["engine_status"], attempt["sent_mids"],
                          attempt["cost"]),
                         ("comment", PSID, "555_1001", "sent", "OK", ["m_fake_0001"], 0.0123))
        row = self.comment()
        self.assertEqual((row["reply_status"], row["replied_psid"]), ("sent", PSID))
        # Meta's redelivery, or a replay, never answers twice.
        self.assertEqual(self.handle(event()), "comment_already_handled")
        self.assertEqual((len(self.engine.calls), len(self.graph.replies)), (1, 1))
        # The post is read from Graph once and kept for the next comment under it.
        self.graph.reply_answer = {"recipient_id": "7000000000005678", "message_id": "m_2"}
        self.assertEqual(self.handle(event("555_1002")), "comment_replied")
        self.assertEqual(self.graph.paths().count(POST), 1)
        self.assertEqual(self.db.rows("SELECT message FROM garage.meta_posts")[0]["message"],
                         POST_TEXT)
        self.assertEqual(self.worker.store.turns_today(), 2)
        self.assertEqual(self.graph.public, [])

    def test_the_public_reply_goes_only_when_the_dashboard_set_one(self):
        self.comments_on(comment_public_reply="მოგწერეთ პირად შეტყობინებაში.")
        self.assertEqual(self.handle(event()), "comment_replied_public")
        self.assertEqual(self.graph.public, [("555_1001", "მოგწერეთ პირად შეტყობინებაში.")])
        self.assertEqual(self.comment()["public_reply_id"], "111_909")
        # Its own webhook comes back from the page and is never answered.
        self.assertEqual(self.handle(event("111_909", author=PAGE, parent="555_1001")),
                         "comment_from_page")
        self.site_store.update_settings({"comment_public_reply": ""}, ADMIN)
        self.graph.reply_answer = {"recipient_id": "7000000000005678", "message_id": "m_2"}
        self.assertEqual(self.handle(event("555_1002")), "comment_replied")
        self.assertEqual(len(self.graph.public), 1)

    def test_a_failing_ai_account_pauses_the_bot_and_sends_nothing(self):
        self.comments_on(comment_public_reply="მოგწერეთ.")
        engine = FakeEngine(outcome("failed", error_code="PROVIDER_BILLING", public={},
                                    engine_status="PROVIDER_BILLING"))
        with self.assertLogs("automotive.meta_comments", "WARNING") as logs:
            self.assertEqual(self.handle(event(), engine), "comment_paused_provider")
        self.assertEqual(logs.output, ["WARNING:automotive.meta_comments:"
                                       "meta_comment_provider_failed code=PROVIDER_BILLING "
                                       "paused=True"])  # codes only, never the comment
        self.assertEqual((self.graph.replies, self.graph.public), ([], []))
        self.assertEqual(self.worker.store.settings()["bot_paused_reason"], "ai_provider")
        self.assertEqual(self.attempts()[0]["status"], "paused_provider")
        self.assertEqual(self.comment()["reply_status"], "paused_provider")
        self.assertEqual(self.handle(event("555_1002"), engine), "comment_skipped_paused")
        self.assertEqual(len(engine.calls), 1)

    def test_any_other_failure_sends_the_fallback_privately_and_nothing_public(self):
        self.comments_on(comment_public_reply="მოგწერეთ.")
        engine = FakeEngine(outcome("timeout", error_code="TIMEOUT", public={},
                                    engine_status=""))
        self.assertEqual(self.handle(event(), engine), "comment_fallback_sent")
        (_, _, message), = self.graph.replies
        self.assertIn("596 33 33 74", message["text"])
        self.assertEqual(self.graph.public, [])
        thread = self.worker.store.get_thread(PSID)
        self.assertEqual((thread["needs_attention"], thread["attention_reason"]),
                         (True, "comment_reply_failed"))
        attempt = self.attempts()[0]
        self.assertEqual((attempt["status"], attempt["engine_status"]),
                         ("fallback_sent", "TIMEOUT"))
        self.assertEqual(self.comment()["reply_status"], "fallback_sent")
        # A fallback is no answer: the comment stays on the admin's unreplied list.
        listed = list_comments(self.site_store, only_unreplied=True)
        self.assertEqual(([item["comment_id"] for item in listed["comments"]],
                          listed["unreplied"], listed["comments"][0]["replied"]),
                         (["555_1001"], 1, False))

    def test_a_turn_the_worker_stopping_cut_short_sends_no_fallback(self):
        # A restart closes the engine under the turn: CONVERSATION_EXPIRED mid-turn, or
        # CHAT_CLOSED when it starts after. Meta allows one private reply per comment, so the
        # "call us" fallback would be the only answer it ever gets.
        self.comments_on()
        for comment_id, code in (("555_1001", "CONVERSATION_EXPIRED"), ("555_1002", "CHAT_CLOSED")):
            engine = FakeEngine(outcome("failed", error_code=code, public={}, engine_status=""))
            engine.closing = True
            self.assertEqual(self.handle(event(comment_id), engine), "comment_abandoned")
        unflagged = FakeEngine(outcome("failed", error_code="CHAT_CLOSED", public={},
                                       engine_status=""))
        self.assertEqual(self.handle(event("555_1003"), unflagged), "comment_abandoned")
        self.assertEqual(self.graph.replies, [])
        self.assertEqual([row["status"] for row in self.attempts()], ["abandoned"] * 3)
        self.assertEqual(self.comment()["reply_status"], "abandoned")
        self.assertEqual(self.handle(event()), "comment_already_handled")

    def test_an_attempt_that_breaks_midway_ends_failed_and_is_never_repeated(self):
        self.comments_on()

        def broken() -> None:
            raise RuntimeError("engine fault")

        with self.assertRaises(RuntimeError):
            self.handle(event(), FakeEngine(during=broken))
        self.assertEqual(self.attempts()[0]["status"], "failed")
        self.assertEqual(self.comment()["reply_status"], "failed")
        self.assertEqual(self.handle(event()), "comment_already_handled")
        self.assertEqual(self.graph.replies, [])

    def test_switching_comments_off_during_the_turn_sends_nothing(self):
        self.comments_on()
        engine = FakeEngine(during=lambda: self.site_store.set_comments(False, ADMIN))
        self.assertEqual(self.handle(event(), engine), "comment_skipped_off")
        self.assertEqual(self.graph.replies, [])
        attempt = self.attempts()[0]
        self.assertEqual((attempt["status"], attempt["engine_status"]), ("skipped_off", "OK"))
        self.assertEqual(self.worker.store.turns_today(), 1)  # the engine ran: it counts
        self.assertEqual(self.comment()["reply_status"], "skipped_off")

    def test_a_customer_whose_chat_the_admin_took_over_gets_no_comment_reply(self):
        self.comments_on()
        self.assertEqual(self.handle(event()), "comment_replied")  # links her chat
        self.site_store.set_thread_switch(PSID, False, ADMIN)  # the admin answers her now
        self.assertEqual(self.handle(event("555_1002")), "comment_skipped_chat_off")
        self.assertEqual((len(self.engine.calls), len(self.graph.replies)), (1, 1))
        self.assertEqual(self.comment("555_1002")["reply_status"], "skipped_chat_off")
        # Switched off while the comment's turn runs: nothing leaves.
        self.site_store.set_thread_switch(PSID, True, ADMIN)
        engine = FakeEngine(during=lambda: self.site_store.set_thread_switch(PSID, False, ADMIN))
        self.assertEqual(self.handle(event("555_1003"), engine), "comment_skipped_chat_off")
        self.assertEqual(len(self.graph.replies), 1)
        self.assertEqual(self.attempts()[-1]["status"], "skipped_chat_off")
        # Another person's comment is answered as usual.
        self.graph.reply_answer = {"recipient_id": "7000000000005678", "message_id": "m_2"}
        self.assertEqual(self.handle(event("555_1004", author="900000000000009")),
                         "comment_replied")

    def test_the_daily_cap_counts_comment_turns(self):
        self.comments_on(daily_turn_limit=0)
        self.assertEqual(self.handle(event()), "comment_skipped_cap")
        self.assertEqual((self.engine.calls, self.graph.replies), ([], []))
        self.assertEqual(self.attempts()[0]["status"], "skipped_cap")
        self.assertEqual(self.worker.store.turns_today(), 0)
        self.assertEqual(self.handle(event()), "comment_already_handled")

    def test_meta_refusing_the_reply_is_recorded_and_never_retried(self):
        self.comments_on()
        self.graph.write_errors["private_reply"] = GraphError(
            400, 10900, None, "OAuthException", "(#10900) Activity already replied to", "t")
        self.assertEqual(self.handle(event()), "comment_blocked_by_meta")
        attempt = self.attempts()[0]
        self.assertEqual((attempt["status"], attempt["error_code"]), ("blocked_by_meta", 10900))
        self.assertIsNone(self.worker.store.get_thread(PSID))
        self.graph.write_errors["private_reply"] = GraphError(
            0, None, None, "network", "read_timeout", maybe_applied=True)
        self.assertEqual(self.handle(event("555_1002")), "comment_failed")
        self.assertEqual(self.handle(event("555_1002")), "comment_already_handled")

    def test_an_existing_chat_keeps_its_source_and_gains_the_exchange(self):
        store = self.worker.store
        store.upsert_thread(PSID, PAGE, customer_name="ნინო ბ.", source="ads",
                            source_ad_id="9001")
        earlier = {**STATE, "memory": {"language": "ka", "transcript": [
            {"user": "ზეთი გაქვთ?", "assistant": "რომელი მანქანისთვის?"}]}}
        store.save_engine_state(PSID, earlier)
        self.comments_on()
        self.assertEqual(self.handle(event()), "comment_replied")
        thread = store.get_thread(PSID, with_state=True)
        self.assertEqual((thread["source"], thread["source_ad_id"], thread["customer_name"]),
                         ("ads", "9001", "ნინო ბ."))
        transcript = thread["engine_state"]["memory"]["transcript"]
        self.assertEqual(transcript[0], {"user": "ზეთი გაქვთ?",
                                         "assistant": "რომელი მანქანისთვის?"})
        self.assertEqual(transcript[-1]["assistant"], reply_message(view(cards=2), "ka"))
        self.assertTrue(transcript[-1]["user"].startswith("F30 B46-ზე"))

    def test_an_engine_still_loading_claims_nothing(self):
        self.comments_on()
        with mock.patch.object(meta_comments, "READY_WAIT_SECONDS", 0):
            with self.assertRaises(CommentRetry):
                self.handle(event(), FakeEngine(ready=False))
        self.assertEqual((self.attempts(), self.comment()["reply_status"]), ([], ""))
        self.assertEqual(self.handle(event()), "comment_replied")

    def test_edits_hides_and_removals_update_the_row(self):
        self.handle(event())
        self.assertEqual(self.handle(event(verb="edited", text="ახალი ტექსტი")),
                         "comment_edited")
        self.assertEqual(self.comment()["body"], "ახალი ტექსტი")
        self.assertEqual(self.handle(event(verb="hide", hidden=True)), "comment_hidden")
        self.assertTrue(self.comment()["is_hidden"])
        self.assertEqual(self.handle(event(verb="unhide")), "comment_unhidden")
        self.assertFalse(self.comment()["is_hidden"])
        self.assertEqual(self.handle(event(verb="remove")), "comment_removed")
        self.assertIsNone(self.comment())
        self.assertEqual(self.handle(event("555_4040", verb="edited")), "comment_unknown")
        self.assertEqual(self.handle(event(verb="share")), "comment_verb_ignored")
        self.assertEqual(self.engine.calls, [])

    def test_without_a_connected_page_nothing_happens(self):
        self.db.reset()
        self.assertEqual(self.handle(event()), "comment_not_connected")

    def test_retention_and_a_customers_deletion_reach_the_comments(self):
        self.comments_on()
        self.assertEqual(self.handle(event()), "comment_replied")
        self.handle(event("555_1002", text=""))  # the same author again, not answered
        self.handle(event("555_1003", author="900000000000009", text=""))
        self.db.rows("INSERT INTO garage.meta_comments (comment_id, post_id, created_time, "
                     "source) VALUES ('555_0001', %s, now() - interval '400 days', 'backfill')",
                     (POST,))
        worker_db = self.worker.store
        self.assertEqual(meta_comments.purge_comments(worker_db), 1)
        with self.assertRaises(ValueError):
            meta_comments.purge_comments(worker_db, 0)
        # Either login may forget a customer's comments (the dashboard's Delete data does).
        self.assertEqual(meta_comments.forget_customer(self.site_store, PSID), 2)
        self.assertEqual(meta_comments.forget_customer(worker_db, PSID), 0)
        left = [row["comment_id"] for row in self.db.rows(
            "SELECT comment_id FROM garage.meta_comments")]
        self.assertEqual(left, ["555_1003"])
        with self.assertRaises(ValueError):
            meta_comments.forget_customer(worker_db, "not-a-psid")

    def test_a_customers_deletion_keeps_their_comments_out_for_good(self):
        self.comments_on()
        self.db.rows("UPDATE garage.meta_settings SET comments_enabled_at = now() - "
                     "interval '3 hours'")
        self.assertEqual(self.handle(event(created=datetime.now(UTC) - timedelta(hours=2))),
                         "comment_replied")
        self.handle(event("555_1002", text="", created=datetime.now(UTC)
                          - timedelta(hours=1)))  # the same author, not answered
        self.handle(event("555_1003", author="900000000000009", text="",
                          created=datetime.now(UTC) - timedelta(hours=1)))
        removed = self.site_store.delete_customer(PSID, ADMIN)  # the dashboard's Delete data
        self.assertEqual(removed, {"threads": 1, "messages": 1, "replies": 1, "comments": 2,
                                   "inbox": None})
        stored = lambda: sorted(row["comment_id"] for row in self.db.rows(  # noqa: E731
            "SELECT comment_id FROM garage.meta_comments"))
        self.assertEqual(stored(), ["555_1003"])
        # The comments are still on Facebook, so the 10-minute backfill reads them again.
        now = datetime.now(UTC)
        graph = FakeGraph(read_only=True)
        graph.answers[f"{PAGE}/posts"] = [
            {"id": POST, "created_time": meta_time(now - timedelta(days=3)),
             "comments": {"data": [], "summary": {"total_count": 4}}}]
        graph.answers[f"{POST}/comments"] = [
            {"id": "555_1004", "message": "ახალი კითხვა", "created_time": meta_time(
                now + timedelta(minutes=1)), "from": {"id": "900000000000001", "name": "ნინო"}},
            {"id": "555_1003", "message": "", "created_time": meta_time(
                now - timedelta(hours=1)), "from": {"id": "900000000000009", "name": "გიორგი"}},
            {"id": "555_1002", "message": "", "created_time": meta_time(
                now - timedelta(hours=1)), "from": {"id": "900000000000001", "name": "ნინო"}},
            {"id": "555_1001", "message": "F30 B46-ზე წინა ხუნდები გაქვთ?",
             "created_time": meta_time(now - timedelta(hours=2)),
             "from": {"id": "900000000000001", "name": "ნინო"}}]
        result = sync_comments(self.db.context("worker", graph))
        self.assertEqual(int(result), 2)
        # What she wrote before the request stays out; what she writes after it is kept.
        self.assertEqual(stored(), ["555_1003", "555_1004"])
        # A late webhook copy of an erased comment is neither stored nor answered.
        self.assertEqual(self.handle(event(created=now - timedelta(hours=2))),
                         "comment_forgotten")
        self.assertEqual(stored(), ["555_1003", "555_1004"])
        self.assertEqual((len(self.engine.calls), len(self.graph.replies)), (1, 1))

    # -- the backfill ---------------------------------------------------------------------

    def test_the_backfill_keeps_comments_for_the_dashboard_and_never_answers(self):
        self.comments_on()
        graph = FakeGraph(read_only=True)
        worker = self.db.context("worker", graph)
        now = datetime.now(UTC)
        self.db.rows("INSERT INTO garage.meta_ad_accounts (account_id) VALUES ('111')")
        self.db.rows("INSERT INTO garage.meta_ads (ad_id, account_id, story_id) "
                     "VALUES ('9001', '111', %s)", (f"{PAGE}_777",))
        self.db.rows("INSERT INTO garage.meta_comments (comment_id, post_id, created_time, "
                     "source, reply_status) VALUES ('555_2003', %s, now(), 'webhook', "
                     "'pending')", (POST,))
        self.db.rows("INSERT INTO garage.messenger_replies (channel, trigger_key, status) "
                     "VALUES ('comment', '555_2003', 'abandoned')")
        graph.answers[f"{PAGE}/posts"] = [
            {"id": POST, "created_time": meta_time(now - timedelta(days=3)),
             "comments": {"data": [], "summary": {"total_count": 3}}},
            {"id": f"{PAGE}_556", "created_time": meta_time(now - timedelta(days=4)),
             "comments": {"data": [], "summary": {"total_count": 0}}}]
        graph.answers[f"{POST}/comments"] = [
            {"id": "555_2002", "message": "გამოგიგზავნეთ პირადში", "from": {"id": PAGE,
             "name": "Garage"}, "parent": {"id": "555_2001"},
             "created_time": meta_time(now - timedelta(hours=1))},
            {"id": "555_2001", "message": "რა ღირს?", "from": {"id": "900000000000002",
             "name": "გიორგი"}, "created_time": meta_time(now - timedelta(hours=2)),
             "is_hidden": False},
            {"id": "555_2009", "message": "ძველი", "from": {"id": "900000000000003"},
             "created_time": meta_time(now - timedelta(days=40))},
            {"id": "broken"}]
        graph.answers[f"{PAGE}_777/comments"] = [
            {"id": "777_3001", "message": "მისამართი?", "from": {"id": "900000000000004"},
             "created_time": meta_time(now - timedelta(days=1))}]
        result = sync_comments(worker)
        self.assertEqual((int(result), result.status), (2, "ok"))
        self.assertIn("page_replies=1", result.detail)
        self.assertIn("settled=1", result.detail)
        self.assertEqual(graph.methods(), {"GET"})
        self.assertNotIn(f"{PAGE}_556/comments", graph.paths())  # no comments, not read
        stored = {row["comment_id"]: row for row in self.db.rows(
            "SELECT * FROM garage.meta_comments ORDER BY comment_id")}
        self.assertEqual(set(stored), {"555_2001", "555_2003", "777_3001"})
        first = stored["555_2001"]
        self.assertEqual((first["source"], first["reply_status"], first["public_reply_id"],
                          first["author_name"]), ("backfill", "", "555_2002", "გიორგი"))
        self.assertEqual(stored["555_2003"]["reply_status"], "abandoned")
        params = dict(graph.calls[graph.paths().index(f"{POST}/comments")][2])
        self.assertEqual((params["filter"], params["order"]), ("stream", "reverse_chronological"))
        self.assertEqual(self.attempts()[0]["status"], "abandoned")  # nothing new was tried

    # -- the dashboard ----------------------------------------------------------------------

    def test_the_dashboard_lists_hides_and_replies_publicly_with_an_audit(self):
        self.handle(event(created=datetime.now(UTC) - timedelta(minutes=2)))
        self.handle(event("555_1002", name="გიორგი", text="მისამართი?"))
        listed = list_comments(self.site_store, 10, 0)
        self.assertEqual((listed["total"], listed["unreplied"]), (2, 2))
        self.assertEqual([item["comment_id"] for item in listed["comments"]],
                         ["555_1002", "555_1001"])
        first = listed["comments"][1]
        self.assertEqual((first["link"], first["replied"], first["post_excerpt"]),
                         (f"https://www.facebook.com/{PAGE}/posts/555?comment_id=1001", False,
                          None))
        self.assertIsInstance(first["created_time"], str)
        self.assertEqual(set_hidden(self.site, "555_1001", True, ADMIN),
                         {"comment_id": "555_1001", "hidden": True})
        self.assertEqual(self.graph.hidden, [("555_1001", True)])
        self.assertTrue(self.comment()["is_hidden"])
        answer = public_reply(self.site, "555_1001", "  გმადლობთ!  ", ADMIN, client_ip="10.0.0.5")
        self.assertEqual(answer, {"comment_id": "555_1001", "public_reply_id": "111_909"})
        self.assertEqual(self.graph.public, [("555_1001", "გმადლობთ!")])
        unreplied = list_comments(self.site_store, only_unreplied=True)
        self.assertEqual([item["comment_id"] for item in unreplied["comments"]], ["555_1002"])
        audits = self.db.rows("SELECT actor_username, action, target, detail FROM "
                              "garage.audit_log WHERE action LIKE 'messenger.comment%%' "
                              "ORDER BY audit_id")
        self.assertEqual([(row["actor_username"], row["action"], row["target"], row["detail"])
                          for row in audits],
                         [("boss", "messenger.comment_hidden", "555_1001", {}),
                          ("boss", "messenger.comment_public_reply", "555_1001",
                           {"chars": 9, "stored": True})])
        for comment_id, text, code in (("555_9999", "x", "NOT_FOUND"),
                                       ("bad id", "x", "INVALID"),
                                       ("555_1002", "   ", "INVALID")):
            with self.assertRaises(CommentActionError) as caught:
                public_reply(self.site, comment_id, text, ADMIN)
            self.assertEqual(caught.exception.code, code)
        self.graph.write_errors["set_comment_hidden"] = refused(200)
        with self.assertRaises(CommentActionError) as caught:
            set_hidden(self.site, "555_1002", True, ADMIN)
        self.assertEqual((caught.exception.code, caught.exception.detail["meta"]["code"]),
                         ("META_REFUSED", 200))
        self.assertFalse(self.comment("555_1002")["is_hidden"])
        self.assertEqual(self.db.rows("SELECT action FROM garage.audit_log ORDER BY audit_id "
                                      "DESC LIMIT 1")[0]["action"],
                         "messenger.comment_action_failed")


if __name__ == "__main__":
    unittest.main()
