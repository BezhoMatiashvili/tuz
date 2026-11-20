"""The Messenger catch-up job (automotive/meta_catchup.py) with a fake Graph and store: what it
reads back, from when, and what it hands the store. No network, no database."""

from __future__ import annotations

import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta

from automotive import meta_catchup
from automotive.meta_catchup import start_time, sync_missed_messages

PAGE = "1437527016116753"
PSID = "7000000000001234"
OTHER = "7000000000005678"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def conversation(cid: str, psid: str, minutes_ago: float) -> dict:
    return {"id": cid, "updated_time": stamp(minutes_ago), "participants": {"data": [
        {"id": psid, "name": "Customer"}, {"id": PAGE, "name": "Garage"}]}}


def message(mid: str, sender: str, minutes_ago: float, text: str = "hello", **extra) -> dict:
    """A message ``minutes_ago`` plus ten minutes old: older than the webhook's own five."""
    return {"id": mid, "created_time": stamp(minutes_ago + 10), "from": {"id": sender},
            "message": text, **extra}


class FakeGraph:
    def __init__(self) -> None:
        self.pages: dict[tuple[str, str], list[dict]] = {}  # (path, after) -> one Graph page
        self.calls: list[tuple[str, dict]] = []

    def add(self, path: str, *pages: list[dict]) -> None:
        for index, data in enumerate(pages):
            after = "" if index == 0 else f"c{index}"
            more = index + 1 < len(pages)
            self.pages[(path, after)] = {"data": data, **({"paging": {
                "cursors": {"after": f"c{index + 1}"}, "next": "https://x"}} if more else {})}

    def get(self, path: str, token: str, params: dict) -> dict:
        self.calls.append((path, dict(params)))
        return self.pages.get((path, params.get("after", "")), {"data": []})


class FakeStore:
    def __init__(self) -> None:
        self.stored: list[tuple[str, list[dict]]] = []
        self.last_ok: datetime | None = None
        self.connected: datetime | None = NOW - timedelta(days=9)
        self.known: set[str] = set()

    def page(self) -> dict:
        return {"page_id": PAGE, "connected_at": self.connected}

    def last_ok_run(self, job: str) -> datetime | None:
        assert job == "catchup"
        return self.last_ok

    def add_caught_up(self, psid: str, page_id: str, rows: list[dict]) -> dict:
        self.stored.append((psid, rows))
        return {"stored": len(rows), "claimed": 2}

    def known_mids(self, mids: list[str]) -> set[str]:
        return {mid for mid in mids if mid in self.known}


class FakeContext:
    def __init__(self) -> None:
        self.graph = FakeGraph()
        self.store = FakeStore()
        self.page_id = PAGE

    def page_token(self) -> str:
        return "page-token"

    def now(self) -> datetime:
        return NOW


class CatchupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = FakeContext()

    def test_the_first_run_reads_from_when_the_page_was_connected(self):
        self.assertEqual(start_time(self.ctx), NOW - timedelta(days=9))
        self.ctx.store.connected = NOW - timedelta(days=90)  # at most FIRST_DAYS back
        self.assertEqual(start_time(self.ctx), NOW - timedelta(days=meta_catchup.FIRST_DAYS))

    def test_later_runs_read_from_an_hour_before_the_last_good_one(self):
        self.ctx.store.last_ok = NOW - timedelta(minutes=10)
        self.assertEqual(start_time(self.ctx), NOW - timedelta(minutes=70))
        # A reconnect (a token that died) moves connected_at: the gap before it is read too.
        self.ctx.store.connected = NOW - timedelta(minutes=30)
        self.assertEqual(start_time(self.ctx), NOW - timedelta(minutes=70))
        # And never more than FIRST_DAYS back.
        self.ctx.store.last_ok = NOW - timedelta(days=90)
        self.assertEqual(start_time(self.ctx), NOW - timedelta(days=meta_catchup.FIRST_DAYS))

    def test_two_messages_in_one_second_are_stored_in_their_true_order(self):
        graph = self.ctx.graph
        graph.add(f"{PAGE}/conversations", [conversation("t_1", PSID, 1)])
        # Newest first, as Graph lists them, with the same whole-second time.
        graph.add("t_1/messages", [message("m_2", PSID, 3), message("m_1", PSID, 3)])
        sync_missed_messages(self.ctx, since=NOW - timedelta(hours=1))
        self.assertEqual([row["mid"] for _, rows in self.ctx.store.stored for row in rows],
                         ["m_1", "m_2"])

    def test_it_never_reads_back_further_than_a_deleted_chat_is_remembered(self):
        from automotive.meta_store import FORGOTTEN_DAYS

        self.assertLess(meta_catchup.FIRST_DAYS, FORGOTTEN_DAYS)

    def test_a_run_cut_off_by_its_page_limits_stores_what_it_read_and_fails(self):
        graph = self.ctx.graph
        graph.add(f"{PAGE}/conversations", [conversation("t_1", PSID, 1)])
        graph.add("t_1/messages", [message("m_2", PSID, 1)], [message("m_1", PSID, 2)])
        with unittest.mock.patch.object(meta_catchup, "MAX_MESSAGE_PAGES", 1), \
                self.assertRaises(meta_catchup.Truncated):
            sync_missed_messages(self.ctx, since=NOW - timedelta(hours=1))
        # What it read (the newest) is kept; the failed run leaves the last good one where it
        # was, so the next run reads the same window again.
        self.assertEqual([row["mid"] for _, rows in self.ctx.store.stored for row in rows],
                         ["m_2"])

    def test_messages_are_read_back_oldest_first_with_their_side_and_attachment_types(self):
        graph = self.ctx.graph
        graph.add(f"{PAGE}/conversations", [conversation("t_1", PSID, 1)])
        graph.add("t_1/messages", [
            message("m_3", PSID, 1, "", attachments={"data": [{"mime_type": "image/jpeg"},
                                                              {"mime_type": "audio/mp4"}]}),
            message("m_2", PAGE, 3, "we have them"),
            message("m_1", PSID, 5, "brake pads?"),
            message("m_x", "999", 6, "somebody else"),  # neither side of this chat
        ])
        counts = sync_missed_messages(self.ctx, since=NOW - timedelta(hours=1))
        ((psid, rows),) = self.ctx.store.stored
        self.assertEqual(psid, PSID)
        self.assertEqual([(row["mid"], row["sender"], row["body"]) for row in rows],
                         [("m_1", "customer", "brake pads?"), ("m_2", "shop", "we have them"),
                          ("m_3", "customer", "")])
        self.assertEqual(rows[2]["attachments"], [{"type": "image"}, {"type": "audio"}])
        self.assertEqual(rows[0]["sent_at"], NOW - timedelta(minutes=15))
        self.assertEqual(counts, {"conversations": 1, "messages": 3, "stored": 3, "claimed": 2,
                                  "known": 0})

    def test_reading_stops_at_the_start_of_the_window_in_both_lists(self):
        graph = self.ctx.graph
        graph.add(f"{PAGE}/conversations", [conversation("t_1", PSID, 10)],
                  [conversation("t_2", OTHER, 300)], [conversation("t_3", OTHER, 400)])
        graph.add("t_1/messages", [message("m_3", PSID, 10)],
                  [message("m_2", PSID, 20), message("m_1", PSID, 200)],
                  [message("m_0", PSID, 300)])
        counts = sync_missed_messages(self.ctx, since=NOW - timedelta(hours=1))
        self.assertEqual(counts["conversations"], 1)  # t_2 was last updated before the window
        self.assertEqual([row["mid"] for _, rows in self.ctx.store.stored for row in rows],
                         ["m_2", "m_3"])
        # Reading stops at the first item older than the window: no page after it is asked for.
        asked = [(path, params.get("after", "")) for path, params in graph.calls]
        self.assertNotIn(("t_1/messages", "c2"), asked)
        self.assertNotIn((f"{PAGE}/conversations", "c2"), asked)

    def test_both_folders_are_read_and_a_conversation_is_counted_once(self):
        graph = self.ctx.graph
        graph.add(f"{PAGE}/conversations", [conversation("t_1", PSID, 1)])
        graph.add("t_1/messages", [message("m_1", PSID, 1)])
        sync_missed_messages(self.ctx, since=NOW - timedelta(hours=1))
        folders = [params.get("folder") for path, params in graph.calls
                   if path == f"{PAGE}/conversations"]
        self.assertEqual(folders, ["inbox", "other"])
        self.assertEqual(len(self.ctx.store.stored), 1)  # the fake lists t_1 in both

    def test_a_dry_run_counts_what_is_known_and_writes_nothing(self):
        graph = self.ctx.graph
        graph.add(f"{PAGE}/conversations", [conversation("t_1", PSID, 1)])
        graph.add("t_1/messages", [message("m_2", PSID, 1), message("m_1", PSID, 2)])
        self.ctx.store.known = {"m_1"}
        counts = sync_missed_messages(self.ctx, since=NOW - timedelta(hours=1), dry_run=True)
        self.assertEqual((counts["messages"], counts["known"], counts["stored"]), (2, 1, 0))
        self.assertEqual(self.ctx.store.stored, [])

    def test_a_message_of_the_last_minutes_is_left_to_the_webhook(self):
        graph = self.ctx.graph
        graph.add(f"{PAGE}/conversations", [conversation("t_1", PSID, 1)])
        graph.add("t_1/messages", [
            {"id": "m_now", "created_time": stamp(1), "from": {"id": PSID}, "message": "hi"},
            message("m_old", PSID, 0)])
        sync_missed_messages(self.ctx, since=NOW - timedelta(hours=1))
        # Taken here first, the webhook's delivery of m_now would be a duplicate and the bot
        # would never answer it; a later run takes it if the webhook never brings it.
        self.assertEqual([row["mid"] for _, rows in self.ctx.store.stored for row in rows],
                         ["m_old"])

    def test_group_chats_and_conversations_without_an_id_are_left_alone(self):
        graph = self.ctx.graph
        group = conversation("t_g", PSID, 1)
        group["participants"]["data"].append({"id": OTHER, "name": "Someone"})
        graph.add(f"{PAGE}/conversations", [group, {"updated_time": stamp(1)}])
        counts = sync_missed_messages(self.ctx, since=NOW - timedelta(hours=1))
        self.assertEqual((counts["conversations"], self.ctx.store.stored), (0, []))


if __name__ == "__main__":
    unittest.main()
