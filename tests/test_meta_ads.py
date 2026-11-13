"""Garage's ad results, read-only (``automotive.meta_ads``).

The Graph fake answers with recorded, Meta-shaped JSON and refuses every write, so a sync that
tried to change an ad would fail here. The database tests (skipped without
WAREHOUSE_TEST_ADMIN_DATABASE_URL) write as the worker's role and read as the dashboard's.
"""

from __future__ import annotations

import re
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from automotive import meta_ads
from automotive.meta_ads import (
    CHAT_ACTION,
    ad_accounts,
    ad_results,
    best_ads,
    daily_spend,
    garage_ads,
    parse_ad,
    parse_insight,
    summarize,
    sync_ads,
)
from automotive.meta_graph import GraphError
from automotive.meta_page_stats import ImageFetcher, image, image_key
from automotive.meta_sync import TBILISI
from tests.test_meta_page_stats import (
    ADMIN_URL,
    JPEG,
    PAGE,
    FakeGraph,
    FakeSession,
    FeatureDatabase,
    picture,
    refused,
)

OTHER_PAGE = "61500000000009"
THUMB = "https://scontent.xx.fbcdn.net/v/t45.1600-4/thumb.jpg?stp=c0.5000x0.5000f_dst-emg0"


def ad(ad_id: str, *, page: str = PAGE, how: str = "spec", status: str = "ACTIVE",
       creative_id: str = "", thumbnail: str = THUMB) -> dict:
    """An ``act_<id>/ads`` item, promoting ``page`` the way ``how`` says."""
    creative: dict[str, Any] = {"id": creative_id or f"7{ad_id}", "thumbnail_url": thumbnail}
    adset: dict[str, Any] = {"id": "6001", "name": "თბილისი 25-55"}
    if how == "spec":
        creative["object_story_spec"] = {"page_id": page, "link_data": {
            "name": "ზეთის შეცვლა", "message": "მოგვწერეთ Messenger-ში", "link": "https://m.me/x"}}
    elif how == "story":
        creative["effective_object_story_id"] = f"{page}_8888"
    elif how == "promoted":
        adset["promoted_object"] = {"page_id": page}
    return {"id": ad_id, "name": f"Garage ad {ad_id}", "status": status,
            "effective_status": status, "created_time": "2026-10-01T10:00:00+0400",
            "campaign": {"id": "5001", "name": "Messages", "objective": "OUTCOME_ENGAGEMENT"},
            "adset": adset, "creative": creative}


def insight(ad_id: str, day: date, spend: str = "10.50", chats: str = "3", **counts: str) -> dict:
    return {"ad_id": ad_id, "date_start": day.isoformat(), "date_stop": day.isoformat(),
            "spend": spend, "impressions": counts.get("impressions", "1000"),
            "reach": counts.get("reach", "800"), "clicks": counts.get("clicks", "25"),
            "actions": [{"action_type": "link_click", "value": "20"},
                        {"action_type": CHAT_ACTION, "value": chats},
                        {"action_type": "post_engagement", "value": "40"}]}


class ParsingTests(unittest.TestCase):
    def test_only_ads_promoting_our_page_are_kept(self):
        ads = [ad("9001", how="spec"), ad("9002", how="story"), ad("9003", how="promoted"),
               ad("9004", page=OTHER_PAGE), ad("9005", page=OTHER_PAGE, how="story"),
               {"id": "9006", "creative": {"effective_object_story_id": f"{PAGE}5_1"}},
               {"id": "not-an-id", "creative": {"object_story_spec": {"page_id": PAGE}}},
               "junk", None]
        kept = garage_ads(ads, PAGE, "111")
        self.assertEqual([row["ad_id"] for row in kept], ["9001", "9002", "9003"])
        self.assertTrue(all(row["account_id"] == "111" for row in kept))

    def test_an_ad_is_read_defensively(self):
        row = parse_ad(ad("9001"), "111")
        self.assertEqual((row["title"], row["body"], row["campaign_name"], row["objective"],
                          row["adset_name"], row["creative_id"], row["story_id"]),
                         ("ზეთის შეცვლა", "მოგვწერეთ Messenger-ში", "Messages",
                          "OUTCOME_ENGAGEMENT", "თბილისი 25-55", "79001", ""))
        self.assertEqual(row["created_time"], datetime(2026, 10, 1, 6, 0, tzinfo=UTC))
        boosted = parse_ad(ad("9002", how="story"), "111")
        self.assertEqual((boosted["story_id"], boosted["title"]), (f"{PAGE}_8888", ""))
        odd = parse_ad({"id": "9", "campaign": {"id": "x1"}, "creative": {
            "id": 5, "effective_object_story_id": "bad id", "thumbnail_url": 7}}, "111")
        self.assertEqual((odd["campaign_id"], odd["creative_id"], odd["story_id"],
                          odd["thumbnail_url"], odd["created_time"]), ("", "5", "", "", None))

    def test_daily_insights_are_parsed_with_metas_chat_count(self):
        row = parse_insight({**insight("9001", date(2026, 11, 10), spend="12.345", chats="3"),
                             "actions": [{"action_type": CHAT_ACTION, "value": "3"},
                                         {"action_type": CHAT_ACTION, "value": "1.0"},
                                         {"action_type": "link_click", "value": "7"},
                                         {"action_type": "bad type!", "value": "1"},
                                         "junk"]})
        self.assertEqual((row["ad_id"], row["day"], row["spend"], row["impressions"],
                          row["reach"], row["clicks"], row["messaging_started"]),
                         ("9001", date(2026, 11, 10), Decimal("12.35"), 1000, 800, 25, 4))
        self.assertEqual([entry["action_type"] for entry in row["actions"]],
                         [CHAT_ACTION, CHAT_ACTION, "link_click"])
        for spend in ("abc", "-5", "1e40", None, True):
            self.assertEqual(parse_insight({**insight("9001", date(2026, 11, 10)),
                                            "spend": spend})["spend"], Decimal("0.00"))
        self.assertIsNone(parse_insight({**insight("9001", date(2026, 11, 10)),
                                         "date_start": "yesterday"}))
        self.assertIsNone(parse_insight({**insight("x", date(2026, 11, 10))}))
        bare = parse_insight({"ad_id": "9001", "date_start": "2026-11-10"})
        self.assertEqual((bare["spend"], bare["impressions"], bare["messaging_started"],
                          bare["actions"]), (Decimal("0.00"), 0, 0, []))

    def test_cost_per_chat_and_ctr(self):
        row = summarize({"ad_id": "9001", "account_id": "111", "spend": Decimal("30.00"),
                         "impressions": 2000, "reach": 1500, "clicks": 50,
                         "messaging_started": 4, "chats": 3, "chats_handled": 2})
        self.assertEqual((row["spend"], row["ctr"], row["cost_per_chat"], row["chats"],
                          row["chats_handled"]), (30.0, 2.5, 7.5, 3, 2))
        self.assertEqual(row["ads_manager_url"], "https://adsmanager.facebook.com/adsmanager/"
                                                 "manage/ads?act=111&selected_ad_ids=9001")
        third = summarize({"ad_id": "1", "account_id": "2", "spend": Decimal("10.00"),
                           "impressions": 3, "clicks": 1, "messaging_started": 3})
        self.assertEqual((third["ctr"], third["cost_per_chat"]), (33.33, 3.33))
        none = summarize({"ad_id": "1", "account_id": "2", "spend": Decimal("5"),
                          "impressions": 0, "clicks": 0, "messaging_started": 0})
        self.assertEqual((none["ctr"], none["cost_per_chat"]), (None, None))

    def test_this_module_never_calls_anything_but_a_graph_read(self):
        source = Path(meta_ads.__file__).read_text(encoding="utf-8")
        calls = set(re.findall(r"graph\.(\w+)\(", source))
        self.assertEqual(calls, {"paged"})
        for name in ("post(", "delete(", "send_message", "private_reply", "comment_reply",
                     "sender_action", "set_comment_hidden", "subscribe"):
            self.assertNotIn(name, source)


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class AdsDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = FeatureDatabase("meta_ads")
        cls.addClassCleanup(cls.db.close)

    def setUp(self) -> None:
        self.db.reset()
        self.db.connect_page()
        self.graph = FakeGraph(read_only=True)
        self.worker = self.db.context("worker", self.graph)
        self.site = self.db.store("site")
        self.today = datetime.now(UTC).astimezone(TBILISI).date()
        self.insight_requests: list[dict] = []
        self.graph.answers["me/adaccounts"] = [
            {"account_id": "111", "id": "act_111", "name": "Owner", "currency": "GEL",
             "timezone_name": "Asia/Tbilisi", "account_status": 1},
            {"account_id": "222", "id": "act_222", "name": "TNLU", "currency": "USD",
             "timezone_name": "Asia/Tbilisi", "account_status": 1},
            {"account_id": "333", "id": "act_333", "name": "Locked", "currency": "EUR",
             "timezone_name": "Not/AZone", "account_status": 2},
            {"account_id": "act_444", "name": "Malformed"}]
        self.graph.answers["act_111/ads"] = [
            ad("9001", how="spec"), ad("9002", how="story", status="PAUSED"),
            ad("9003", how="promoted", status="PAUSED"), ad("9004", page=OTHER_PAGE)]
        self.graph.answers["act_222/ads"] = [ad("9101", page=OTHER_PAGE)]
        self.graph.answers["act_333/ads"] = refused(200)

        def insights(params: dict) -> list[dict]:
            self.insight_requests.append(params)
            return [insight("9001", self.today, spend="20.00", chats="4"),
                    insight("9001", self.today - timedelta(days=1), spend="10.00", chats="1",
                            clicks="5", impressions="500"),
                    insight("9002", self.today - timedelta(days=1), spend="3.00", chats="0"),
                    insight("9004", self.today, spend="99.00", chats="9")]

        self.graph.answers["act_111/insights"] = insights

    def sync(self, session: FakeSession | None = None) -> Any:
        return sync_ads(self.worker, fetcher=ImageFetcher(session or FakeSession({})))

    def test_only_garage_ads_and_their_days_are_stored_with_reads_only(self):
        session = FakeSession({THUMB: picture()})
        result = self.sync(session)
        self.assertEqual(result.status, "ok")
        self.assertEqual(int(result), 3 + 3)  # three Garage ads, three of their days
        self.assertIn("unreadable=1", result.detail)
        self.assertEqual(self.graph.methods(), {"GET"})
        stored = self.db.rows("SELECT ad_id, account_id, story_id, image_key FROM garage.meta_ads "
                              "ORDER BY ad_id")
        self.assertEqual([row["ad_id"] for row in stored], ["9001", "9002", "9003"])
        self.assertEqual(stored[1]["story_id"], f"{PAGE}_8888")
        self.assertEqual(stored[0]["image_key"], image_key("ad_creative:79001"))
        self.assertEqual(image(self.site, stored[0]["image_key"]), ("image/jpeg", JPEG))
        accounts = {row["account_id"]: row["garage_ads"] for row in ad_accounts(self.site)}
        self.assertEqual(accounts, {"111": 3, "222": 0, "333": 0})
        days = self.db.rows("SELECT ad_id, day, spend, messaging_started FROM "
                            "garage.meta_ad_daily ORDER BY ad_id, day")
        self.assertEqual([(row["ad_id"], row["messaging_started"]) for row in days],
                         [("9001", 1), ("9001", 4), ("9002", 0)])  # never the other page's
        (params,) = self.insight_requests
        self.assertEqual((params["level"], params["time_increment"]), ("ad", 1))
        self.assertEqual(params["filtering"], [{"field": "ad.id", "operator": "IN",
                                                "value": ["9001", "9002", "9003"]}])
        self.assertEqual(set(params["fields"].split(",")), {
            "ad_id", "date_start", "spend", "impressions", "reach", "clicks", "actions"})
        self.assertEqual(params["time_range"], {
            "since": (self.today - timedelta(days=89)).isoformat(),
            "until": self.today.isoformat()})
        # Later runs read the last 30 days again.
        self.sync()
        self.assertEqual(self.insight_requests[-1]["time_range"]["since"],
                         (self.today - timedelta(days=29)).isoformat())
        self.assertEqual(self.graph.methods(), {"GET"})

    def test_results_put_metas_chats_next_to_the_bots(self):
        self.sync()
        worker = self.worker.store
        for psid, answered in (("7000000000000001", True), ("7000000000000002", False)):
            worker.upsert_thread(psid, PAGE, source="ads", source_ad_id="9001")
            worker.add_message(psid, f"m_c{psid}", "customer", datetime.now(UTC), page_id=PAGE,
                               body="გამარჯობა", referral_ad_id="9001")
            if answered:
                worker.add_message(psid, f"m_b{psid}", "bot", datetime.now(UTC), page_id=PAGE,
                                   body="გამარჯობა!")
        results = {row["ad_id"]: row for row in ad_results(self.site, 30)}
        self.assertEqual(set(results), {"9001", "9002"})  # 9003: paused, no results, no chats
        first = results["9001"]
        self.assertEqual((first["currency"], first["spend"], first["impressions"],
                          first["clicks"], first["messaging_started"], first["cost_per_chat"],
                          first["ctr"], first["chats"], first["chats_handled"]),
                         ("GEL", 30.0, 1500, 30, 5, 6.0, 2.0, 2, 1))
        self.assertEqual((first["title"], first["status"]), ("ზეთის შეცვლა", "ACTIVE"))
        self.assertEqual((results["9002"]["cost_per_chat"], results["9002"]["ctr"]), (None, 2.5))
        self.assertEqual([row["ad_id"] for row in best_ads(self.site, 30, min_spend=5)],
                         ["9001"])
        self.assertEqual(best_ads(self.site, 30, min_spend=50), [])
        spend = daily_spend(self.site, 30)
        self.assertEqual([(row["day"], row["currency"], row["spend"], row["messaging_started"])
                          for row in spend],
                         [((self.today - timedelta(days=1)).isoformat(), "GEL", 13.0, 1),
                          (self.today.isoformat(), "GEL", 20.0, 4)])
        self.assertEqual(ad_results(self.site, 1)[0]["spend"], 20.0)

    def test_without_an_ads_token_nothing_is_asked(self):
        self.db.reset()
        self.db.connect_page(user=False)
        result = self.sync()
        self.assertEqual((int(result), result.status, result.detail),
                         (0, "skipped", "no_ads_token"))
        self.assertEqual(self.graph.calls, [])

    def test_no_garage_ads_and_refusals_say_why(self):
        self.graph.answers["act_111/ads"] = [ad("9004", page=OTHER_PAGE)]
        result = self.sync()
        self.assertEqual((int(result), result.status), (0, "skipped"))
        self.assertTrue(result.detail.startswith("no_garage_ads accounts=3"))
        self.assertEqual({row["account_id"]: row["garage_ads"] for row in ad_accounts(self.site)},
                         {"111": 0, "222": 0, "333": 0})
        self.assertEqual(self.db.rows("SELECT count(*) AS n FROM garage.meta_ads")[0]["n"], 0)
        self.graph.answers["me/adaccounts"] = refused(200)
        self.assertEqual(self.sync().detail, "ads_read_refused code=200")
        self.graph.answers["me/adaccounts"] = refused(190, 463)
        self.assertEqual(self.sync().detail, "ads_token_dead code=190")
        status = {row["purpose"]: row["status"] for row in self.site.token_rows()}
        self.assertEqual(status, {"page": "ok", "user": "broken"})

    def test_when_no_account_can_be_read_the_run_fails(self):
        for account in ("111", "222", "333"):
            self.graph.answers[f"act_{account}/ads"] = GraphError(500, 1, None, "OAuthException")
        with self.assertRaises(GraphError):
            self.sync()


if __name__ == "__main__":
    unittest.main()
