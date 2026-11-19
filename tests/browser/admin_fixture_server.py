"""Disposable dashboard for browser checks; all data and external services are test doubles.

Run with ``python -m tests.browser.admin_fixture_server --port 8792``. Never loads .env.
"""

from __future__ import annotations

import argparse
import copy
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import uvicorn

from automotive.api import create_app
from automotive.config import Settings
from automotive.site_catalog import Catalog
from automotive.site_content import DEFAULT_SECTIONS
from tests.test_site_admin import SERVICE
from tests.test_site_catalog import ROWS, row
from tests.test_site_guard import (
    FakeSiteRepository,
    LegacyInventory,
    NoSearch,
    QuietChat,
    RecordingBuckets,
    StaticVin,
)
from tests.test_site_live import KEY, WEB, FakeLiveStore, alert, chat, message
from tests.test_site_meta_activity import FakeStore, Features
from tests.test_site_meta_admin import PSID, SETTINGS, FakeGraph, FakeMetaStore

NOW = datetime.now(UTC)
LONG_NAME = "სატესტო მომხმარებელი Test Customer " + "LongName" * 8


class BrowserLiveStore(FakeLiveStore):
    def __init__(self):
        super().__init__()
        self.alerts = [alert(1, name=LONG_NAME, created_at=NOW, updated_at=NOW)]
        self.rows = {
            KEY: chat(name=LONG_NAME, last_at=NOW),
            WEB: chat(WEB, last_at=NOW, alerts=[], channel="web"),
        }
        self.messages = [
            {**message(i), "body": f"Message {i}: ნაწილების შესახებ კითხვა და პასუხი. "
             + ("https://example.test/" + "part" * 60 if i % 5 == 0 else "Sample response."),
             "sender": "customer" if i % 2 else "bot", "at": NOW + timedelta(seconds=i)}
            for i in range(1, 81)
        ]

    def pulse(self):
        return {**super().pulse(), "stamp": NOW,
                "open": {"part_not_found": 1, "total": 1}, "cursor": {"m": 80, "w": 0}}

    def feed(self, after_m, after_w):
        return {"messages": [], "chats": self.rows, "cursor": {"m": 80, "w": 0}}

    def chats(self, filter="all", limit=40):
        return [copy.deepcopy(item) for item in self.rows.values()
                if filter == "all" or (filter == "off" and not item["bot_enabled"])]

    def chat(self, key, limit=50, before=None):
        if key not in self.rows:
            return None
        end = int(before[1:]) - 1 if before else len(self.messages)
        return {"chat": copy.deepcopy(self.rows[key]),
                "alerts": copy.deepcopy(self.alerts) if key == KEY else [],
                "messages": [{**item, "key": key} for item in self.messages[max(0, end-limit):end]],
                "has_older": end > limit}

    def set_bot(self, key, enabled, actor, *, version=None, client_ip=None):
        item = self.rows.get(key)
        if item is None:
            return None
        item.update(bot_enabled=enabled, version=item["version"] + 1)
        return {name: item[name] for name in ("key", "bot_enabled", "version")}


def build_app(port: int, case: unittest.TestCase):
    repository = FakeSiteRepository()
    for username, role in (("boss", "admin"), ("worker", "employee")):
        user = repository.add_user(username, role)
        repository.users[user["user_id"]]["display_name"] = LONG_NAME
    for key, document in DEFAULT_SECTIONS.items():
        repository.save_section(key, document, 0, 1)
    for i in range(3):
        repository.create_service({**SERVICE, "title_en": f"Diagnostics {i + 1}"})
    repository.runs = [{"run_id": 1, "status": "succeeded", "finished_at": NOW}]
    repository.audit({"user_id": 1, "username": "boss"}, "content.updated", "hero", {})
    catalog = Catalog.from_rows(ROWS + [
        row(f"sample-{i}", "სატესტო ნაწილი " + "LongPart" * 12,
            "TOYOTA CAMRY 2012-2017", "SATO", "45.5", "3", "0", code=f"PART-{i}")
        for i in range(70)
    ])
    meta = FakeMetaStore()
    meta.add_thread(PSID, name=LONG_NAME, last=NOW)
    for i in range(12):
        meta.add_message(PSID, "customer" if i % 2 else "bot",
                         "სატესტო შეტყობინება " + "https://example.test/" + "long" * 40,
                         NOW + timedelta(seconds=i))
    features = Features()
    features.install(case)
    settings = replace(Settings(), staff_auth=True, automotive_cache_path="",
                       fuchs_enabled=False, shell_enabled=False, pakelo_enabled=False,
                       jsfilter_enabled=False,
                       allowed_origins=(f"http://127.0.0.1:{port}",))
    app = create_app(settings=settings, repository=LegacyInventory(), vin_service=StaticVin(),
                      chat_service=QuietChat(), search_service=NoSearch(), site={
                          "repository": repository, "catalog": catalog,
                          "buckets": RecordingBuckets(), "live": {"store": BrowserLiveStore()},
                          "meta": {"store": meta, "graph": FakeGraph(), "meta_settings": SETTINGS},
                          "meta_activity": {"store": FakeStore(), "graph": object(),
                                            "meta_settings": SETTINGS},
                      })
    @app.get("/__dashboard_fixture")
    def fixture_info():
        return {"fixture": True, "sections": list(DEFAULT_SECTIONS), "live": [KEY, WEB],
                "facebook": PSID}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    if args.port == 8767:
        parser.error("Use a disposable port, not the shop's port 8767")
    fixture_case = unittest.TestCase()
    try:
        uvicorn.run(build_app(args.port, fixture_case), host="127.0.0.1", port=args.port,
                    lifespan="off", log_level="warning", access_log=False)
    finally:
        fixture_case.doCleanups()
