"""Connect flow, subscriptions self-heal, token checks, the gate probe and ad discovery.

A fake Graph client and a fake store stand in for Meta and PostgreSQL; nothing leaves the process.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from automotive.meta_config import REQUIRED_SCOPES, SUBSCRIBED_FIELDS, MetaSettings
from automotive.meta_connect import (
    ConnectError,
    check,
    connect,
    disconnect,
    discover_ads,
    ensure_subscribed,
    probe,
    promotes_page,
    sync_names,
)
from automotive.meta_context import MetaContext, NotConnected
from automotive.meta_crypto import TokenCipher, TokenCipherError
from automotive.meta_graph import GraphError, TokenChoice

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
APP, OTHER_APP = "1234567890", "9990001112"
PAGE, OTHER_PAGE, USER_ID = "61595318832800", "61500000000009", "4400000001"
SHORT, LONG = "EAAshortLivedFixtureToken0001", "EAAlongLivedFixtureToken0002"
PAGE_TOKEN, OTHER_PAGE_TOKEN = "EAApageFixtureToken000003", "EAAotherPageFixtureToken04"
SEND_TOKEN = "EAAsendOnlyFixtureToken0005"
OLD, FRESH = "7000000000001111", "7000000000002222"
SETTINGS = MetaSettings(app_id=APP, app_secret="s" * 32, verify_token="v" * 40,
                        token_key=b"k" * 32, public_url="https://hook.example")
SECRETS = (SHORT, LONG, PAGE_TOKEN, OTHER_PAGE_TOKEN, SEND_TOKEN, "s" * 32)


def epoch(when: datetime) -> int:
    return int(when.timestamp())


def meta_time(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S+0000")


def user_info(**changes) -> dict:
    return {"is_valid": True, "type": "USER", "app_id": APP, "user_id": USER_ID,
            "scopes": list(REQUIRED_SCOPES), "expires_at": epoch(NOW + timedelta(hours=1)),
            "data_access_expires_at": epoch(NOW + timedelta(days=90)), **changes}


def page_info(**changes) -> dict:
    return {"is_valid": True, "type": "PAGE", "app_id": APP, "profile_id": PAGE,
            "scopes": ["pages_messaging", "pages_show_list"], "expires_at": 0,
            "data_access_expires_at": epoch(NOW + timedelta(days=90)), **changes}


def refused(code: int, subcode: int | None = None, status: int = 400) -> GraphError:
    return GraphError(status, code, subcode, "OAuthException", f"(#{code}) refused", "trace")


class FakeGraph:
    """Answers like Graph; ``errors`` maps a method (or ``method:path``) to an exception."""

    def __init__(self) -> None:
        self.debug = {SHORT: user_info(), LONG: user_info(), PAGE_TOKEN: page_info(),
                      OTHER_PAGE_TOKEN: page_info(profile_id=OTHER_PAGE)}
        self.errors: dict[str, Exception] = {}
        self.accounts = [{"id": PAGE, "name": "Garage • ფარეხი 1886",
                          "access_token": PAGE_TOKEN, "tasks": ["MODERATE", "MESSAGING"]}]
        self.conversations: list[dict] = []
        self.adaccounts: list[dict] = []
        self.ads: dict[str, list[dict]] = {}
        self.page_apps: list[dict] = []
        self.app_objects: list[dict] = []
        self.actions: list[GraphError | None] = []
        self.calls: list[tuple] = []

    def _call(self, name: str, *args, path: str = "") -> None:
        self.calls.append((name, *args))
        error = self.errors.get(f"{name}:{path}") or self.errors.get(name)
        if error is not None:
            raise error

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def debug_token(self, token):
        self._call("debug_token", token)
        value = token.token if isinstance(token, TokenChoice) else token
        return dict(self.debug.get(value, {"is_valid": False, "error": {"code": 190}}))

    def exchange_user_token(self, token):
        self._call("exchange_user_token", token)
        return {"access_token": LONG, "expires_in": 5184000}

    def get(self, path, token, params=None, proof=True):
        self._call("get", path, token, proof, path=path)
        return {"id": USER_ID, "name": "Owner"} if path == "me" else {}

    def paged(self, path, token, params=None, max_pages=10, *, proof=True):
        self._call("paged", path, token, path=path)
        if path == "me/accounts":
            return [dict(page) for page in self.accounts]
        if path == "me/adaccounts":
            return list(self.adaccounts)
        if path.endswith("/conversations"):
            return list(self.conversations)
        if path.endswith("/ads"):
            return list(self.ads.get(path.split("/")[0].removeprefix("act_"), []))
        return []

    def subscribe_app(self, callback_url, verify_token, fields):
        self._call("subscribe_app", callback_url, verify_token, list(fields))
        return {"success": True}

    def subscribe_page(self, page_id, page_token, fields, proof=True):
        self._call("subscribe_page", page_id, page_token, list(fields))
        return {"success": True}

    def page_subscriptions(self, page_id, page_token, proof=True):
        self._call("page_subscriptions", page_id)
        return {"data": self.page_apps}

    def app_subscriptions(self):
        self._call("app_subscriptions")
        return {"data": self.app_objects}

    def unsubscribe_page(self, page_id, page_token, proof=True):
        self._call("unsubscribe_page", page_id, page_token)
        return {"success": True}

    def sender_action(self, page_id, page_token, psid, action, proof=True):
        self._call("sender_action", page_id, page_token, psid, action)
        outcome = self.actions.pop(0) if self.actions else None
        if outcome is not None:
            raise outcome
        return {"recipient_id": psid, "message_id": ""}


class FakeStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.page_row: dict | None = None
        self.tokens: dict[str, tuple[str, str]] = {}
        self.unreadable: set[str] = set()
        self.subscriptions: list[list[str]] = []
        self.audits: list[tuple] = []
        self.marks: list[tuple] = []
        self.threads: dict[str, dict] = {}
        self.candidates: list[str] = []
        self.named: list[dict] = []
        self.expiry = {"expires_at": None, "data_access_expires_at": NOW + timedelta(days=10)}

    def connected(self, *, send_page: bool = False, ads_user: bool = False) -> FakeStore:
        self.page_row = {"page_id": PAGE, "page_name": "Garage", "subscribed_fields": []}
        self.tokens = {"page": (PAGE_TOKEN, APP), "user": (LONG, APP)}
        if send_page:
            self.tokens["send_page"] = (SEND_TOKEN, OTHER_APP)
        if ads_user:
            self.tokens["ads_user"] = ("EAAadsFixtureToken00000006", OTHER_APP)
        return self

    def page(self):
        return self.page_row

    def token(self, purpose):
        if purpose in self.unreadable:
            raise TokenCipherError("cannot decrypt")
        entry = self.tokens.get(purpose)
        return entry[0] if entry else None

    def token_rows(self):
        return [{"purpose": purpose, "app_id": app, "scopes": ["pages_messaging"],
                 "status": "ok", "status_detail": "", **self.expiry}
                for purpose, (_, app) in sorted(self.tokens.items())]

    def save_connection(self, page_id, page_name, app_id, tokens, actor, *, client_ip=None):
        self.saved.append({"page_id": page_id, "page_name": page_name, "app_id": app_id,
                           "tokens": tokens, "actor": actor})
        self.page_row = {"page_id": page_id, "page_name": page_name, "subscribed_fields": []}
        for purpose, entry in tokens.items():
            self.tokens[purpose] = (entry["token"], entry.get("app_id") or app_id)
        return self.page_row

    def set_subscription(self, fields):
        self.subscriptions.append(list(fields))
        return True

    def audit(self, actor, action, target="", detail=None, *, client_ip=None, connection=None):
        self.audits.append((actor, action, target, detail))

    def mark_token(self, purpose, status, detail="", *, expiry=None):
        self.marks.append((purpose, status, detail, expiry))
        return True

    def disconnect(self, actor, *, client_ip=None):
        removed = sorted(self.tokens)
        self.tokens, self.page_row = {}, None
        return {"page_id": PAGE, "tokens_removed": removed}

    def get_thread(self, psid, *, with_state=False):
        return self.threads.get(psid)

    def probe_candidates(self, page_id, *, quiet_hours=25, limit=5):
        return [{"psid": psid} for psid in self.candidates][:limit]

    def apply_conversation_names(self, rows):
        self.named.extend(rows)
        return len(rows)


def context(store: FakeStore | None = None, graph: FakeGraph | None = None,
            settings: MetaSettings = SETTINGS, cipher: bool = True) -> MetaContext:
    return MetaContext(settings, store or FakeStore(), graph or FakeGraph(),
                       TokenCipher(b"k" * 32) if cipher else None, now=lambda: NOW)


class ConnectTests(unittest.TestCase):
    def assert_refused(self, ctx: MetaContext, code: str, **kwargs) -> ConnectError:
        with self.assertRaises(ConnectError) as caught:
            connect(ctx, kwargs.pop("token", SHORT), actor="cli", **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(ctx.store.saved, [])  # nothing stored before every check passed
        self.assertEqual(ctx.store.audits[-1][1:], ("messenger.connect_failed", "",
                                                    {"code": code}))
        for secret in SECRETS:
            self.assertNotIn(secret, repr(caught.exception.detail) + str(caught.exception))
        return caught.exception

    def test_a_good_paste_stores_both_tokens_and_subscribes_the_app_and_page(self):
        ctx = context()
        result = connect(ctx, f"  {SHORT}\n", actor={"user_id": 7, "username": "boss"})
        (saved,) = ctx.store.saved
        self.assertEqual((saved["page_id"], saved["page_name"], saved["app_id"]),
                         (PAGE, "Garage • ფარეხი 1886", APP))
        page, user = saved["tokens"]["page"], saved["tokens"]["user"]
        self.assertEqual((page["token"], page["subject_id"], page["expires_at"]),
                         (PAGE_TOKEN, PAGE, None))  # 0 from Meta means "never"
        self.assertEqual((user["token"], user["subject_id"], user["subject_name"]),
                         (LONG, USER_ID, "Owner"))
        self.assertEqual(user["expires_at"], NOW + timedelta(seconds=5184000))
        self.assertEqual(user["data_access_expires_at"], NOW + timedelta(days=90))
        self.assertEqual(result["subscription"], {"ok": True, "app": True, "page": True,
                                                  "error": None})
        self.assertIn(("subscribe_app", "https://hook.example/meta/webhook", "v" * 40,
                       list(SUBSCRIBED_FIELDS)), ctx.graph.calls)
        self.assertIn(("subscribe_page", PAGE, PAGE_TOKEN, list(SUBSCRIBED_FIELDS)),
                      ctx.graph.calls)
        self.assertEqual(ctx.store.subscriptions, [list(SUBSCRIBED_FIELDS)])
        self.assertEqual((result["page_id"], result["missing_scopes"], result["tasks"]),
                         (PAGE, [], ["MESSAGING", "MODERATE"]))
        self.assertEqual(ctx.graph.names()[:5], ["debug_token", "exchange_user_token", "get",
                                                 "paged", "debug_token"])
        for secret in SECRETS:
            self.assertNotIn(secret, repr(result))

    def test_missing_configuration_stops_before_meta_is_asked(self):
        for ctx in (context(settings=MetaSettings(app_id=APP, token_key=b"k" * 32)),
                    context(cipher=False)):
            self.assert_refused(ctx, "NOT_CONFIGURED")
            self.assertEqual(ctx.graph.calls, [])

    def test_a_token_that_is_not_our_apps_valid_user_token_is_refused(self):
        ctx = context()
        self.assert_refused(ctx, "TOKEN_INVALID", token="EAA has spaces 0123456789")
        self.assertEqual(ctx.graph.calls, [])
        for change, code in ((dict(is_valid=False), "TOKEN_INVALID"),
                             (dict(app_id=OTHER_APP), "WRONG_APP"),
                             (dict(type="PAGE"), "NOT_USER_TOKEN"),
                             (dict(type="SYSTEM_USER"), "NOT_USER_TOKEN"),
                             (dict(user_id=""), "TOKEN_INVALID")):
            graph = FakeGraph()
            graph.debug[SHORT] = user_info(**change)
            if code == "TOKEN_INVALID" and "user_id" in change:
                graph.errors["get:me"] = refused(100)
            self.assert_refused(context(graph=graph), code)
        graph = FakeGraph()
        graph.errors["debug_token"] = refused(190)
        detail = self.assert_refused(context(graph=graph), "TOKEN_INVALID").detail
        self.assertEqual((detail["meta"]["code"], detail["meta"]["message"]),
                         (190, "(#190) refused"))

    def test_essential_permissions_are_required_and_the_rest_are_reported(self):
        graph = FakeGraph()
        graph.debug[SHORT] = user_info(scopes=["pages_show_list", "ads_read"])
        detail = self.assert_refused(context(graph=graph), "MISSING_SCOPES").detail
        self.assertEqual(detail["essential"], ["pages_messaging", "pages_manage_metadata"])
        graph = FakeGraph()
        graph.debug[SHORT] = user_info(scopes=[scope for scope in REQUIRED_SCOPES
                                               if scope not in ("ads_read", "read_insights")])
        result = connect(context(graph=graph), SHORT, actor="cli")
        self.assertEqual(result["missing_scopes"], ["read_insights", "ads_read"])

    def test_exchange_and_page_listing_failures(self):
        graph = FakeGraph()
        graph.errors["exchange_user_token"] = refused(1, status=500)
        self.assert_refused(context(graph=graph), "EXCHANGE_FAILED")
        graph = FakeGraph()
        graph.errors["paged:me/accounts"] = refused(200)
        self.assert_refused(context(graph=graph), "NO_PAGES")
        graph = FakeGraph()
        graph.accounts = [{"id": PAGE, "name": "No token"}]
        self.assertEqual(self.assert_refused(context(graph=graph), "NO_PAGES").detail,
                         {"choices": []})

    def test_several_pages_need_a_choice_and_a_pin_wins(self):
        graph = FakeGraph()
        graph.accounts.append({"id": OTHER_PAGE, "name": "TNLU",
                               "access_token": OTHER_PAGE_TOKEN})
        detail = self.assert_refused(context(graph=graph), "MULTIPLE_PAGES").detail
        self.assertEqual(detail["choices"], [{"id": PAGE, "name": "Garage • ფარეხი 1886"},
                                             {"id": OTHER_PAGE, "name": "TNLU"}])
        chosen = context(graph=graph)
        self.assertEqual(connect(chosen, SHORT, actor="cli", page_id=PAGE)["page_id"], PAGE)
        pinned = MetaSettings(**{**SETTINGS.__dict__, "page_id": PAGE})
        self.assertEqual(connect(context(graph=graph, settings=pinned), SHORT,
                                 actor="cli")["page_id"], PAGE)
        conflict = self.assert_refused(context(graph=graph, settings=pinned), "NO_PAGES",
                                       page_id=OTHER_PAGE).detail
        self.assertEqual(conflict["pinned_page_id"], PAGE)
        missing = MetaSettings(**{**SETTINGS.__dict__, "page_id": "61500000000077"})
        self.assertEqual(len(self.assert_refused(context(graph=graph, settings=missing),
                                                 "NO_PAGES").detail["choices"]), 2)

    def test_the_page_token_must_be_ours_and_for_that_page(self):
        for change in (dict(is_valid=False), dict(type="USER"), dict(profile_id=OTHER_PAGE),
                       dict(app_id=OTHER_APP)):
            graph = FakeGraph()
            graph.debug[PAGE_TOKEN] = page_info(**change)
            self.assert_refused(context(graph=graph), "PAGE_TOKEN_INVALID")

    def test_a_failed_subscription_keeps_the_connection_and_is_reported(self):
        graph = FakeGraph()
        graph.errors["subscribe_app"] = refused(2200)  # the callback could not be verified
        ctx = context(graph=graph)
        result = connect(ctx, SHORT, actor="cli")
        self.assertEqual(len(ctx.store.saved), 1)
        subscription = result["subscription"]
        self.assertEqual((subscription["ok"], subscription["app"], subscription["page"]),
                         (False, False, True))
        self.assertEqual((subscription["error"]["code"], subscription["error"]["step"],
                          subscription["error"]["meta"]["code"]), ("SUBSCRIBE_FAILED", "app", 2200))
        self.assertEqual(ctx.store.subscriptions, [list(SUBSCRIBED_FIELDS)])  # the page's part


class SubscriptionAndHealthTests(unittest.TestCase):
    def test_ensure_subscribed_repairs_only_what_is_missing(self):
        graph = FakeGraph()
        ctx = context(FakeStore().connected(), graph)
        graph.page_apps = [{"id": APP, "subscribed_fields": list(SUBSCRIBED_FIELDS)}]
        graph.app_objects = [{"object": "page", "active": True,
                              "callback_url": SETTINGS.webhook_url,
                              "fields": [{"name": name} for name in SUBSCRIBED_FIELDS]}]
        self.assertEqual(ensure_subscribed(ctx), {"page_id": PAGE, "page_resubscribed": False,
                                                  "app_resubscribed": False})
        self.assertNotIn("subscribe_page", graph.names())
        self.assertEqual(ctx.store.subscriptions, [list(SUBSCRIBED_FIELDS)])  # stored was empty
        ctx.store.page_row["subscribed_fields"] = list(SUBSCRIBED_FIELDS)
        ensure_subscribed(ctx)
        self.assertEqual(len(ctx.store.subscriptions), 1)  # unchanged: "since" is kept
        graph.page_apps = [{"id": OTHER_APP, "subscribed_fields": ["messages"]},
                           {"id": APP, "subscribed_fields": ["messages"]}]
        graph.app_objects[0]["active"] = False
        self.assertEqual(ensure_subscribed(ctx), {"page_id": PAGE, "page_resubscribed": True,
                                                  "app_resubscribed": True})
        graph.page_apps, graph.app_objects = [], []
        graph.errors["subscribe_page"] = refused(4)
        with self.assertRaises(ConnectError) as caught:
            ensure_subscribed(ctx)
        self.assertEqual(caught.exception.code, "SUBSCRIBE_FAILED")
        with self.assertRaises(NotConnected):
            ensure_subscribed(context())
        unconfigured = MetaSettings(app_id=APP, app_secret="s" * 32, token_key=b"k" * 32)
        with self.assertRaises(ConnectError) as caught:
            ensure_subscribed(context(FakeStore().connected(), settings=unconfigured))
        self.assertEqual(caught.exception.detail["missing"],
                         ["MESSENGER_PUBLIC_URL", "META_WEBHOOK_VERIFY_TOKEN"])

    def test_check_marks_dead_tokens_and_warns_before_expiry(self):
        store = FakeStore().connected(send_page=True)
        graph = FakeGraph()
        graph.debug[LONG] = user_info(expires_at=epoch(NOW + timedelta(days=5)))
        graph.debug[PAGE_TOKEN] = {"is_valid": False, "error": {"code": 190}}
        results = {row["purpose"]: row for row in check(context(store, graph))}
        self.assertEqual({purpose: row["status"] for purpose, row in results.items()},
                         {"page": "broken", "send_page": "ok", "user": "ok"})
        self.assertEqual((results["user"]["days_left"], results["user"]["warn"]), (5, True))
        self.assertTrue(results["page"]["warn"])
        marks = {mark[0]: mark for mark in store.marks}
        self.assertEqual(marks["page"][1:3], ("broken", "meta_code_190"))
        self.assertEqual(marks["user"][3]["expires_at"], NOW + timedelta(days=5))
        # The other app's token is checked with its own call, never with our proof.
        self.assertIn(("get", "me", TokenChoice(SEND_TOKEN, False, "send_page"), True),
                      graph.calls)
        self.assertNotIn(("debug_token", SEND_TOKEN), graph.calls)
        # A throttled check, or a seal the key cannot open.
        graph = FakeGraph()
        graph.errors["debug_token"] = refused(4)
        store = FakeStore().connected()
        store.unreadable = {"user"}
        results = {row["purpose"]: row for row in check(context(store, graph))}
        self.assertEqual((results["page"]["checked"], results["page"]["detail"]),
                         (False, "unchecked"))
        self.assertEqual([mark[:3] for mark in store.marks],
                         [("user", "broken", "decrypt_failed")])

    def test_disconnect_can_unsubscribe_first_and_always_deletes(self):
        graph = FakeGraph()
        store = FakeStore().connected()
        self.assertEqual(disconnect(context(store, graph), "cli", unsubscribe=True),
                         {"unsubscribed": True, "error": None, "page_id": PAGE,
                          "tokens_removed": ["page", "user"]})
        self.assertIn(("unsubscribe_page", PAGE, PAGE_TOKEN), graph.calls)
        graph = FakeGraph()
        graph.errors["unsubscribe_page"] = refused(190)
        store = FakeStore().connected()
        result = disconnect(context(store, graph), "cli", unsubscribe=True)
        self.assertEqual((result["unsubscribed"], result["error"]["meta"]["code"],
                          store.tokens), (False, 190, {}))


class ProbeTests(unittest.TestCase):
    def probe_with(self, *outcomes: GraphError | None, store: FakeStore | None = None,
                   graph: FakeGraph | None = None, psid: str | None = None) -> tuple[dict,
                                                                                FakeGraph,
                                                                                FakeStore]:
        store = store or FakeStore().connected()
        if not store.candidates and psid is None and not (graph and graph.conversations):
            store.candidates = [OLD, "7000000000003333", "7000000000004444"]
        graph = graph or FakeGraph()
        graph.actions, graph.calls = list(outcomes), []
        return probe(context(store, graph), psid, actor="cli"), graph, store

    def test_the_result_follows_metas_answer(self):
        cases = ((None, "passed", "accepted"),
                 (refused(10, 2018278), "passed", "outside_window"),
                 (refused(100, 2018278), "passed", "outside_window"),
                 (refused(10), "gated", "gated"),
                 (refused(200, 2018028), "gated", "gated"),  # the gate as Meta documents it
                 (refused(100, 2018109), "inconclusive", "unexpected_error"),
                 (refused(10, 2018065), "inconclusive", "unexpected_error"),
                 (refused(190), "inconclusive", "unexpected_error"),
                 (GraphError(0, None, None, "network", "read_timeout", maybe_applied=True),
                  "inconclusive", "unexpected_error"))
        for outcome, expected, reason in cases:
            result, graph, store = self.probe_with(outcome)
            self.assertEqual((result["result"], result["reason"], result["tried"]),
                             (expected, reason, 1), outcome)
            self.assertEqual(result["psid"], "…1111")
            (action,) = [call for call in graph.calls if call[0] == "sender_action"]
            self.assertEqual(action[3:], (OLD, "typing_on"))
            self.assertEqual(store.audits[-1][1:3], ("messenger.probe", PAGE))
            self.assertNotIn(OLD, repr(store.audits) + repr(result))

    def test_an_unavailable_customer_moves_to_the_next_one(self):
        result, graph, _ = self.probe_with(refused(551, 1545041), refused(551),
                                           refused(10, 2018278))
        self.assertEqual((result["result"], result["tried"], result["psid"]),
                         ("passed", 3, "…4444"))
        result, _, _ = self.probe_with(refused(200, 1545041), refused(10, 2018108),
                                       refused(10, 2018278))
        self.assertEqual((result["result"], result["tried"]), ("passed", 3))
        result, _, _ = self.probe_with(refused(551), refused(551), refused(551))
        self.assertEqual((result["result"], result["reason"], result["tried"]),
                         ("inconclusive", "unavailable", 3))

    def test_the_send_token_is_the_one_probed(self):
        store = FakeStore().connected(send_page=True)
        result, graph, _ = self.probe_with(refused(10, 2018278), store=store)
        (action,) = [call for call in graph.calls if call[0] == "sender_action"]
        self.assertEqual(action[2], TokenChoice(SEND_TOKEN, False, "send_page"))
        self.assertEqual(result["send_purpose"], "send_page")

    def test_candidates_come_from_quiet_conversations_when_the_database_has_none(self):
        graph = FakeGraph()
        graph.conversations = [
            {"updated_time": meta_time(NOW - timedelta(hours=2)),
             "participants": {"data": [{"id": PAGE}, {"id": FRESH, "name": "Recent"}]}},
            {"updated_time": meta_time(NOW - timedelta(hours=30)),
             "participants": {"data": [{"id": PAGE}, {"id": "7000000000009999"}]}},
            {"updated_time": meta_time(NOW - timedelta(hours=40)),
             "participants": {"data": [{"id": PAGE}, {"id": OLD}]}},
            {"updated_time": "not a time", "participants": {"data": [{"id": "70000000000088"}]}}]
        store = FakeStore().connected()
        store.threads["7000000000009999"] = {"is_admin": True, "last_customer_at": None}
        result, graph, _ = self.probe_with(refused(10), store=store, graph=graph)
        self.assertEqual((result["result"], result["psid"]), ("gated", "…1111"))
        graph.conversations = graph.conversations[:1]
        result, graph, _ = self.probe_with(store=FakeStore().connected(), graph=graph)
        self.assertEqual((result["result"], result["reason"], result["tried"]),
                         ("inconclusive", "no_candidate", 0))
        self.assertNotIn("sender_action", graph.names())

    def test_a_database_candidate_that_meta_shows_as_recent_is_skipped(self):
        graph = FakeGraph()
        graph.conversations = [{"updated_time": meta_time(NOW - timedelta(hours=1)),
                                "participants": {"data": [{"id": OLD}]}}]
        store = FakeStore().connected()
        store.candidates = [OLD, "7000000000003333"]
        result, graph, _ = self.probe_with(refused(10, 2018278), store=store, graph=graph)
        self.assertEqual((result["result"], result["psid"]), ("passed", "…3333"))

    def test_an_explicit_customer_gets_the_same_checks_and_is_never_sent_to_when_unsure(self):
        store = FakeStore().connected()
        store.threads = {OLD: {"is_admin": False, "last_customer_at": NOW - timedelta(hours=30)},
                         FRESH: {"is_admin": False, "last_customer_at": NOW - timedelta(hours=3)},
                         "7000000000005555": {"is_admin": True, "last_customer_at": None}}
        graph = FakeGraph()
        graph.conversations = [{"updated_time": meta_time(NOW - timedelta(hours=50)),
                                "participants": {"data": [{"id": "7000000000006666"}]}},
                               {"updated_time": meta_time(NOW - timedelta(minutes=20)),
                                "participants": {"data": [{"id": "7000000000007777"}]}}]
        cases = ((OLD, "passed", "outside_window"), ("7000000000006666", "passed",
                                                    "outside_window"),
                 (FRESH, "inconclusive", "window_may_be_open"),
                 ("7000000000007777", "inconclusive", "window_may_be_open"),
                 ("7000000000005555", "inconclusive", "admin_chat"),
                 ("7000000000008888", "inconclusive", "not_found"),
                 ("12ab", "inconclusive", "invalid_psid"))
        for psid, expected, reason in cases:
            result, used, _ = self.probe_with(refused(10, 2018278), store=store, graph=graph,
                                              psid=psid)
            self.assertEqual((result["result"], result["reason"]), (expected, reason), psid)
            sent = [call for call in used.calls if call[0] == "sender_action"]
            self.assertEqual(len(sent), 1 if expected == "passed" else 0, psid)
        # Without Meta's list, only the database's word that the customer is quiet counts.
        graph.errors["paged"] = refused(4)
        result, _, _ = self.probe_with(refused(10, 2018278), store=store, graph=graph, psid=OLD)
        self.assertEqual(result["result"], "passed")
        result, _, _ = self.probe_with(store=store, graph=graph, psid="7000000000006666")
        self.assertEqual(result["reason"], "conversations_unreadable")

    def test_not_connected(self):
        with self.assertRaises(NotConnected):
            probe(context(), None)


class DiscoveryTests(unittest.TestCase):
    def test_promotes_page_reads_the_creative_the_story_and_the_promoted_object(self):
        self.assertTrue(promotes_page({"creative": {"object_story_spec": {"page_id": PAGE}}},
                                      PAGE))
        self.assertTrue(promotes_page({"creative": {"effective_object_story_id":
                                                    f"{PAGE}_123"}}, PAGE))
        self.assertTrue(promotes_page({"adset": {"promoted_object": {"page_id": PAGE}}}, PAGE))
        for ad in ({}, {"creative": {"effective_object_story_id": f"{OTHER_PAGE}_1"}},
                   {"creative": "junk", "adset": None},
                   {"creative": {"object_story_spec": {"page_id": OTHER_PAGE}}}):
            self.assertFalse(promotes_page(ad, PAGE))

    def test_discover_ads_counts_garage_ads_per_account_and_reads_with_the_ads_token(self):
        graph = FakeGraph()
        graph.adaccounts = [{"account_id": "1383535660550763", "name": "Personal",
                             "currency": "USD", "account_status": 1},
                            {"account_id": "222", "name": "Shop", "currency": "GEL"},
                            {"account_id": "act_bad"}]
        graph.ads = {"1383535660550763": [
            {"id": "1", "creative": {"object_story_spec": {"page_id": PAGE}}},
            {"id": "2", "creative": {"effective_object_story_id": f"{OTHER_PAGE}_5"}}]}
        graph.errors["paged:act_222/ads"] = refused(200)
        store = FakeStore().connected(ads_user=True)
        rows = discover_ads(context(store, graph))
        self.assertEqual([(row["account_id"], row["garage_ads"], row["ads_scanned"],
                           row["error"] and row["error"]["code"]) for row in rows],
                         [("1383535660550763", 1, 2, None), ("222", 0, 0, 200)])
        token = next(call[2] for call in graph.calls if call[:2] == ("paged", "me/adaccounts"))
        self.assertEqual((token.purpose, token.proof), ("ads_user", False))
        store = FakeStore().connected()
        graph.errors.clear()
        discover_ads(context(store, graph))
        token = next(call[2] for call in reversed(graph.calls) if call[1] == "me/adaccounts")
        self.assertEqual((token.purpose, token.proof), ("user", True))

    def test_sync_names_fills_known_chats_from_conversations(self):
        graph = FakeGraph()
        graph.conversations = [{"id": "t_100", "participants": {"data": [
            {"id": PAGE, "name": "Garage"}, {"id": OLD, "name": "Nino"}]}},
            {"id": "t_200", "participants": {"data": [{"id": "x"}]}}]
        store = FakeStore().connected()
        self.assertEqual(sync_names(context(store, graph)), {"conversations": 2, "updated": 1})
        self.assertEqual(store.named, [{"psid": OLD, "name": "Nino", "conversation_id": "t_100"}])


if __name__ == "__main__":
    unittest.main()
