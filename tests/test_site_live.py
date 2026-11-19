"""Dashboard Live chats API: both roles, the guard's write protection, codes, availability.

The real application around a double of the store, signed in through the real login as the admin
("boss") and the employee ("worker"): the guard's session, Origin and CSRF checks are the real ones.
"""

from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime, timedelta

import psycopg

from automotive.live_store import AlreadyOpen
from automotive.meta_store import MetaConflict
from tests.test_site_admin import AdminCase
from tests.test_site_guard import ORIGIN

PREFIX = "/api/admin/live"
PSID = "7000000000001234"
KEY, WEB = "m-" + PSID, "w-" + "a" * 32
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
SECRET = "private-marker-77aa"


def alert(alert_id: int = 1, **extra) -> dict:
    return {"alert_id": alert_id, "channel": "messenger", "key": KEY, "tail": "1234",
            "name": "Giorgi", "kind": "part_not_found", "reason": "nothing_found",
            "status": "open", "summary": "front brake pads", "items": [], "occurrences": 1,
            "car_label": "Honda Fit 2014", "phone": None, "phone_at": None, "created_at": NOW,
            "updated_at": NOW, "handled_by": "", "handled_at": None, "version": 1,
            "bot_enabled": True, **extra}


def chat(key: str = KEY, **extra) -> dict:
    return {"key": key, "channel": "messenger" if key[0] == "m" else "web", "name": "Giorgi",
            "tail": "1234", "car_label": "", "language": "", "bot_enabled": True, "version": 1,
            "needs_attention": False, "attention_reason": "", "waiting": False,
            "alerts": ["part_not_found"], "last_sender": "customer", "last_body": "hello",
            "last_at": NOW, "first_seen_at": NOW - timedelta(hours=1), "source": "", **extra}


def message(number: int = 11) -> dict:
    return {"id": f"m{number}", "key": KEY, "channel": "messenger", "sender": "customer",
            "body": "do you have pads", "attachments": [], "offered": [], "at": NOW}


class FakeLiveStore:
    """LiveStore's surface, with the answers the real one would give."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.there = True
        self.fail: Exception | None = None
        self.fail_ready: Exception | None = None
        self.alerts = [alert(1), alert(2, status="done", handled_by="boss", handled_at=NOW),
                       alert(3)]

    def _call(self, *call) -> None:
        self.calls.append(call)
        if self.fail:
            raise self.fail

    def ready(self) -> bool:
        self.calls.append(("ready",))
        if self.fail_ready:
            raise self.fail_ready
        return self.there

    def pulse(self) -> dict:
        self._call("pulse")
        return {"open": {"part_not_found": 2, "visit_request": 0, "service_booking": 0,
                         "needs_staff": 0, "total": 2},
                "waiting": 1, "attention": 0, "messenger_bot": "on", "stamp": NOW,
                "cursor": {"m": 10, "w": 4}}

    def feed(self, after_m, after_w) -> dict:
        self._call("feed", after_m, after_w)
        return {"messages": [message()] if after_m is not None else [],
                "chats": {KEY: chat()},
                "cursor": {"m": 12 if after_m is not None else None,
                           "w": 5 if after_w is not None else None}}

    def list_alerts(self, status="open", limit=100) -> list[dict]:
        self._call("list_alerts", status, limit)
        return [row for row in copy.deepcopy(self.alerts)
                if status == "all" or row["status"] == status]

    def recent_done(self, hours=24, limit=50) -> list[dict]:
        self._call("recent_done", hours, limit)
        return [row for row in copy.deepcopy(self.alerts) if row["status"] == "done"]

    def set_status(self, alert_id, status, actor, *, version=None, client_ip=None):
        self._call("set_status", alert_id, status, actor["username"], version)
        found = [row for row in self.alerts if row["alert_id"] == alert_id]
        if not found:
            return None
        if version is not None and version != found[0]["version"]:
            raise MetaConflict("alert")
        if alert_id == 3 and status == "open":
            raise AlreadyOpen("alert")
        found[0].update(status=status, version=found[0]["version"] + 1,
                        handled_by=actor["username"] if status == "done" else "")
        return copy.deepcopy(found[0])

    def chats(self, filter="all", limit=40) -> list[dict]:
        self._call("chats", filter, limit)
        return [chat(), chat(WEB, channel="web")]

    def chat(self, key, limit=50, before=None):
        self._call("chat", key, limit, before)
        if key == KEY:
            return {"chat": chat(), "alerts": [alert(1)], "messages": [message()],
                    "has_older": False}
        return None

    def set_bot(self, key, enabled, actor, *, version=None, client_ip=None):
        self._call("set_bot", key, enabled, actor["username"], version, client_ip)
        if key != KEY:
            return None
        if version == 99:
            raise MetaConflict("chat")
        return {"key": key, "bot_enabled": enabled, "version": 2}

    def clear_attention(self, key, actor, *, client_ip=None) -> bool:
        self._call("clear_attention", key, actor["username"])
        return key == KEY


class LiveCase(AdminCase):
    """AdminCase with the Live chats section wired to the double (``site={"live": ...}``)."""

    async def asyncSetUp(self) -> None:
        self.store = FakeLiveStore()
        await super().asyncSetUp()

    def serve(self, settings=None, **kwargs):
        kwargs.setdefault("live", {"store": self.store})
        return super().serve(settings, **kwargs)

    async def live(self, method: str, path: str, body=None, *, who: dict | None = None,
                   **kwargs):
        return await self.call(method, PREFIX + path, body, who=who, **kwargs)

    def routes(self) -> list[tuple]:
        return [
            ("GET", "/pulse", None),
            ("GET", "/pulse?detail=1&after_m=5&after_w=2", None),
            ("GET", "/alerts", None),
            ("PUT", "/alerts/1", {"status": "done"}),
            ("GET", "/chats", None),
            ("GET", f"/chats/{KEY}", None),
            ("PUT", f"/chats/{KEY}/bot", {"enabled": False}),
            ("POST", f"/chats/{KEY}/attention/clear", None),
        ]


class AccessTests(LiveCase):
    async def test_both_roles_may_use_every_route(self):
        for who, name in ((self.admin, "admin"), (self.employee, "employee")):
            for method, path, body in self.routes():
                response = await self.live(method, path, body, who=who)
                with self.subTest(role=name, method=method, path=path):
                    self.assertEqual(response.status_code, 200, response.text)

    async def test_an_employee_may_switch_the_bot_off_for_one_customer(self):
        response = await self.live("PUT", f"/chats/{KEY}/bot", {"enabled": False, "version": 1},
                                   who=self.employee)
        self.assertEqual(response.json(),
                         {"chat": {"key": KEY, "bot_enabled": False, "version": 2}})
        call = [item for item in self.store.calls if item[0] == "set_bot"][0]
        self.assertEqual(call[:5], ("set_bot", KEY, False, "worker", 1))
        self.assertTrue(call[5])  # the client address, for the audit row

    async def test_the_facebook_section_stays_admin_only(self):
        for method, path in (("GET", "/api/admin/facebook/status"),
                             ("GET", "/api/admin/facebook/threads"),
                             ("PUT", f"/api/admin/facebook/threads/{PSID}"),
                             ("DELETE", f"/api/admin/facebook/threads/{PSID}")):
            body = {"is_admin": True} if method == "PUT" else (
                {"confirm": True} if method == "DELETE" else None)
            response = await self.call(method, path, body, who=self.employee)
            with self.subTest(path=path):
                self.assertEqual((response.status_code, response.json()),
                                 (403, {"detail": {"code": "ADMIN_ONLY"}}))

    async def test_every_route_needs_a_session(self):
        for method, path, body in self.routes():
            response = await self.client.request(method, PREFIX + path, json=body)
            with self.subTest(method=method, path=path):
                self.assertEqual((response.status_code, response.json()),
                                 (401, {"detail": {"code": "NOT_SIGNED_IN"}}))
        self.assertEqual(self.store.calls, [])

    async def test_writes_need_the_csrf_header_and_the_origin(self):
        cookie = {"Cookie": self.employee["Cookie"]}
        for method, path, body in self.routes():
            if method == "GET":
                continue
            for headers, code in (({**cookie, **ORIGIN}, "CSRF_REQUIRED"),
                                  ({**cookie, "X-CSRF-Token": self.employee["X-CSRF-Token"]},
                                   "ORIGIN_REQUIRED")):
                response = await self.client.request(method, PREFIX + path, json=body,
                                                     headers=headers)
                with self.subTest(method=method, path=path, code=code):
                    self.assertEqual((response.status_code, response.json()),
                                     (403, {"detail": {"code": code}}))
        self.assertEqual(self.store.calls, [])

    async def test_the_store_is_asked_nothing_before_the_session_is_known(self):
        await self.client.get(PREFIX + "/pulse")
        self.assertEqual(self.store.calls, [])


class PulseTests(LiveCase):
    async def test_the_plain_pulse_is_counts_and_cursors_only(self):
        response = await self.live("GET", "/pulse", who=self.employee)
        body = response.json()
        self.assertEqual(set(body), {"now", "open", "waiting", "attention", "messenger_bot",
                                     "stamp", "cursor"})
        self.assertEqual((body["open"]["total"], body["waiting"], body["cursor"]),
                         (2, 1, {"m": 10, "w": 4}))
        self.assertEqual(self.store.calls, [("ready",), ("pulse",)])  # no message was read

    async def test_a_detailed_pulse_adds_messages_chats_and_alarms(self):
        response = await self.live("GET", "/pulse?detail=1&after_m=5&after_w=2",
                                   who=self.employee)
        body = response.json()
        self.assertEqual([item["id"] for item in body["messages"]], ["m11"])
        self.assertEqual(list(body["chats"]), [KEY])
        self.assertEqual([item["alert_id"] for item in body["alerts"]["open"]], [1, 3])
        self.assertEqual([item["alert_id"] for item in body["alerts"]["done"]], [2])
        self.assertEqual(body["cursor"], {"m": 12, "w": 5})
        self.assertIn(("feed", 5, 2), self.store.calls)
        self.assertEqual(body["messages"][0]["at"], NOW.isoformat())

    async def test_the_first_detailed_call_has_no_cursor_and_is_given_the_current_one(self):
        body = (await self.live("GET", "/pulse?detail=1", who=self.employee)).json()
        self.assertIn(("feed", None, None), self.store.calls)
        self.assertEqual((body["messages"], body["cursor"]), ([], {"m": 10, "w": 4}))

    async def test_a_cursor_the_page_cannot_have_is_refused(self):
        for query in ("after_m=-1", "after_m=abc", "after_w=99999999999999999999", "detail=2"):
            response = await self.live("GET", "/pulse?" + query)
            with self.subTest(query=query):
                self.assertEqual(response.status_code, 422)
        self.assertNotIn(("pulse",), self.store.calls)


class AlertTests(LiveCase):
    async def test_alarms_are_listed_by_status(self):
        for status, expected in (("open", [1, 3]), ("done", [2]), ("all", [1, 2, 3])):
            body = (await self.live("GET", f"/alerts?status={status}", who=self.employee)).json()
            with self.subTest(status=status):
                self.assertEqual([item["alert_id"] for item in body["alerts"]], expected)
        self.assertEqual((await self.live("GET", "/alerts?status=gone")).status_code, 422)
        self.assertEqual((await self.live("GET", "/alerts?limit=0")).status_code, 422)

    async def test_an_employee_marks_an_alarm_done_and_it_says_who(self):
        response = await self.live("PUT", "/alerts/1", {"status": "done", "version": 1},
                                   who=self.employee)
        alert_ = response.json()["alert"]
        self.assertEqual((alert_["status"], alert_["handled_by"], alert_["version"]),
                         ("done", "worker", 2))
        reopened = await self.live("PUT", "/alerts/1", {"status": "open"}, who=self.admin)
        self.assertEqual(reopened.json()["alert"]["status"], "open")

    async def test_a_stale_version_a_second_open_alarm_and_a_missing_alarm_have_codes(self):
        for alert_id, body, status, code in (
                (1, {"status": "done", "version": 7}, 409, "VERSION_CONFLICT"),
                (3, {"status": "open"}, 409, "ALREADY_OPEN"),
                (99, {"status": "done"}, 404, "NOT_FOUND")):
            response = await self.live("PUT", f"/alerts/{alert_id}", body)
            with self.subTest(alert=alert_id):
                self.assert_refused(response, status, code)

    async def test_a_malformed_body_names_the_field_and_never_repeats_the_value(self):
        for body, fields in (({}, {"status": "REQUIRED"}),
                             ({"status": SECRET}, {"status": "INVALID"}),
                             ({"status": "done", "version": 0}, {"version": "OUT_OF_RANGE"}),
                             ({"status": "done", "version": "1"}, {"version": "INVALID"}),
                             ({"status": "done", "extra": 1}, {"extra": "UNKNOWN"})):
            response = await self.live("PUT", "/alerts/1", body)
            with self.subTest(body=body):
                self.assert_refused(response, 422, "INVALID_FIELDS", fields)
                self.assertNotIn(SECRET, response.text)
        self.assertEqual((await self.live("PUT", "/alerts/abc", {"status": "done"})).status_code,
                         422)
        self.assertEqual(
            [item for item in self.store.calls if item[0] == "set_status"], [])


class ChatTests(LiveCase):
    async def test_conversations_are_listed_and_one_is_opened(self):
        body = (await self.live("GET", "/chats?filter=waiting&limit=10", who=self.employee)).json()
        self.assertEqual([item["key"] for item in body["chats"]], [KEY, WEB])
        self.assertEqual(body["filter"], "waiting")
        self.assertIn(("chats", "waiting", 10), self.store.calls)
        for name in ("all", "alerts", "waiting", "off", "stuck"):
            response = await self.live("GET", f"/chats?filter={name}", who=self.employee)
            with self.subTest(filter=name):
                self.assertEqual(response.json()["filter"], name)
        opened = (await self.live("GET", f"/chats/{KEY}?limit=20&before=m5",
                                  who=self.employee)).json()
        self.assertEqual(set(opened), {"chat", "alerts", "messages", "has_older"})
        self.assertIn(("chat", KEY, 20, "m5"), self.store.calls)

    async def test_an_unknown_chat_is_404_and_a_malformed_key_never_reaches_the_store(self):
        self.assert_refused(await self.live("GET", f"/chats/m-{'9' * 15}"), 404, "NOT_FOUND")
        for path in ("/chats/x-1", "/chats/m-abc", f"/chats/w-{'g' * 32}",
                     f"/chats/{KEY}?before=zz", "/chats?filter=mystery", "/chats?limit=151"):
            with self.subTest(path=path):
                self.assertEqual((await self.live("GET", path)).status_code, 422)
        self.assertEqual([item[0] for item in self.store.calls if item[0] == "chat"], ["chat"])

    async def test_the_bot_switch_reports_conflicts_and_unknown_chats(self):
        for key, body, status, code in (
                (KEY, {"enabled": False, "version": 99}, 409, "VERSION_CONFLICT"),
                (WEB, {"enabled": False}, 404, "NOT_FOUND")):
            response = await self.live("PUT", f"/chats/{key}/bot", body, who=self.employee)
            with self.subTest(key=key):
                self.assert_refused(response, status, code)

    async def test_a_bot_switch_body_must_be_a_real_boolean(self):
        for body, fields in (({}, {"enabled": "REQUIRED"}), ({"enabled": "no"},
                                                             {"enabled": "INVALID"}),
                             ({"enabled": 0}, {"enabled": "INVALID"}),
                             ({"enabled": True, "is_admin": True}, {"is_admin": "UNKNOWN"})):
            response = await self.live("PUT", f"/chats/{KEY}/bot", body, who=self.employee)
            with self.subTest(body=body):
                self.assert_refused(response, 422, "INVALID_FIELDS", fields)
        self.assertEqual([item for item in self.store.calls if item[0] == "set_bot"], [])

    async def test_an_employee_clears_a_stuck_chat_and_an_unknown_one_is_404(self):
        ok = await self.live("POST", f"/chats/{KEY}/attention/clear", who=self.employee)
        self.assertEqual(ok.json(), {"cleared": True})
        self.assert_refused(await self.live("POST", f"/chats/{WEB}/attention/clear"), 404,
                            "NOT_FOUND")


class AvailabilityTests(LiveCase):
    async def test_without_a_site_database_every_route_says_so_after_the_session_check(self):
        client = self.serve(live={"store": None})
        employee = await self.sign_in(client, "worker")
        for method, path, body in self.routes():
            response = await client.request(method, PREFIX + path, json=body, headers=employee)
            with self.subTest(method=method, path=path):
                self.assertEqual((response.status_code, response.json()),
                                 (503, {"detail": {"code": "LIVE_NOT_CONFIGURED"}}))
        anonymous = await client.get(PREFIX + "/pulse")
        self.assertEqual(anonymous.status_code, 401)
        # The rest of the dashboard is unaffected.
        self.assertEqual((await client.get("/api/admin/overview", headers=employee)).status_code,
                         200)

    async def test_before_the_migration_every_route_says_not_set_up_and_reads_nothing(self):
        self.store.there = False
        for method, path, body in self.routes():
            response = await self.live(method, path, body, who=self.employee)
            with self.subTest(method=method, path=path):
                self.assert_refused(response, 503, "LIVE_NOT_SET_UP")
        self.assertEqual({item[0] for item in self.store.calls}, {"ready"})

    async def test_a_database_that_cannot_be_reached_is_a_503_that_leaks_nothing(self):
        self.store.fail_ready = psycopg.OperationalError(SECRET)
        with self.assertLogs("automotive.site_live", "WARNING") as logs:
            response = await self.live("GET", "/pulse", who=self.employee)
        self.assert_refused(response, 503, "LIVE_UNAVAILABLE")
        self.assertNotIn(SECRET, response.text + "\n".join(logs.output))
        self.assertIn("OperationalError", "\n".join(logs.output))
        self.store.fail_ready = None
        self.store.fail = psycopg.errors.UndefinedTable(SECRET)
        with self.assertLogs("automotive.site_live", "WARNING") as logs:
            response = await self.live("GET", "/alerts", who=self.employee)
        self.assert_refused(response, 503, "LIVE_UNAVAILABLE")
        self.assertNotIn(SECRET, response.text + "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
