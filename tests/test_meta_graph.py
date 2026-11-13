"""Graph client: fixed host, proofs, bounded retries, error classes, paging, no token leaks.

A fake requests session stands in for the network; nothing here reaches Meta.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import traceback
import unittest

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from automotive.meta_config import MetaConfigError, MetaSettings
from automotive.meta_graph import (
    GraphClient,
    GraphError,
    TokenChoice,
    _new_session,
    _NoCookies,
    appsecret_proof,
)

APP, SECRET = "1234567890", "fixtureappsecret0123456789abcdef"
TOKEN = "EAAfixturePageToken0123456789abc"
PAGE, PSID, COMMENT = "61595318832800", "7000000000001234", "61595318832800_123456"
BASE = "https://graph.facebook.com/v26.0/"
SETTINGS = MetaSettings(app_id=APP, app_secret=SECRET, verify_token="v" * 40,
                        public_url="https://hook.example", send_timeout_seconds=17.0)


class FakeResponse:
    def __init__(self, status: int = 200, body: object = None, raw: bytes | None = None) -> None:
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps({} if body is None else body).encode()
        self.closed = False

    def iter_content(self, size: int):
        for start in range(0, len(self.raw), size):
            yield self.raw[start:start + size]

    def close(self) -> None:
        self.closed = True


def graph_error(code: int, subcode: int | None = None, status: int = 400,
                message: str = "(#10) refused") -> FakeResponse:
    error = {"message": message, "type": "OAuthException", "code": code,
             "fbtrace_id": "AbCdEf123"}
    if subcode is not None:
        error["error_subcode"] = subcode
    return FakeResponse(status, {"error": error})


class FakeSession:
    """Plays back responses (or raises exceptions) and records every request."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse(200, {})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def client(*outcomes: object, settings: MetaSettings = SETTINGS) -> tuple[GraphClient, FakeSession,
                                                                         list[float]]:
    session, sleeps = FakeSession(*outcomes), []
    return (GraphClient(settings, session=session, sleep=sleeps.append, jitter=lambda: 0.0),
            session, sleeps)


def proof_of(token: str) -> str:
    return hmac.new(SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


class RequestShapeTests(unittest.TestCase):
    def test_reads_go_to_the_fixed_base_with_the_token_and_its_proof(self):
        graph, session, _ = client(FakeResponse(200, {"id": PAGE}))
        self.assertEqual(graph.get("/me/accounts", TOKEN, {"fields": "id,name", "limit": 100,
                                                           "skip": None}), {"id": PAGE})
        (call,) = session.calls
        self.assertEqual((call["method"], call["url"]), ("GET", BASE + "me/accounts"))
        self.assertEqual(call["params"], {"fields": "id,name", "limit": "100",
                                          "access_token": TOKEN,
                                          "appsecret_proof": proof_of(TOKEN)})
        self.assertEqual(appsecret_proof(TOKEN, SECRET), proof_of(TOKEN))
        self.assertIsNone(call["data"])
        self.assertEqual((call["allow_redirects"], call["timeout"], call["stream"]),
                         (False, (5.0, 20.0), True))

    def test_the_proof_is_left_out_for_a_token_of_another_app(self):
        for token, proof in ((TOKEN, False), (TokenChoice(TOKEN, False, "send_page"), True),
                             (TokenChoice(TOKEN, True, "page"), False)):
            graph, session, _ = client(FakeResponse(200, {}))
            graph.get("me", token, proof=proof)
            self.assertNotIn("appsecret_proof", session.calls[0]["params"])
            self.assertEqual(session.calls[0]["params"]["access_token"], TOKEN)
        graph, session, _ = client(FakeResponse(200, {}))
        graph.get("me", TokenChoice(TOKEN, True, "page"))
        self.assertEqual(session.calls[0]["params"]["appsecret_proof"], proof_of(TOKEN))
        self.assertNotIn(TOKEN, repr(TokenChoice(TOKEN, True, "page")))
        token, proof = TokenChoice(TOKEN, False, "send_page")
        self.assertEqual((token, proof), (TOKEN, False))

    def test_writes_carry_the_token_in_the_body_never_in_the_url(self):
        graph, session, _ = client(FakeResponse(200, {"success": True}))
        graph.post(f"{PAGE}/subscribed_apps", TOKEN, {"subscribed_fields": "messages",
                                                       "is_hidden": False})
        (call,) = session.calls
        self.assertIsNone(call["params"])
        self.assertEqual(call["data"], {"subscribed_fields": "messages", "is_hidden": "false",
                                        "access_token": TOKEN,
                                        "appsecret_proof": proof_of(TOKEN)})
        self.assertNotIn(TOKEN, call["url"])
        with self.assertRaises(ValueError):
            graph.post("me", TOKEN, {"access_token": "EAAsmuggled0000000000"})

    def test_only_relative_graph_paths_are_accepted(self):
        graph, session, _ = client()
        for path in ("https://evil.example/x", "//evil.example/x", "../x", "me/../../x",
                     "me?fields=id", "me#x", "a b", "", "me/accounts.json", "a/b/c/d/e",
                     "me%2Faccounts", None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                graph.get(path, TOKEN)
        for token in ("", "EAA with space", "EAA\nnewline", None):
            with self.subTest(token=token), self.assertRaises(ValueError):
                graph.get("me", token)
        self.assertEqual(session.calls, [])
        with self.assertRaises(MetaConfigError):
            GraphClient(MetaSettings(app_id=APP, app_secret=SECRET,
                                     graph_version="v26.0/../../evil"))
        with self.assertRaises(MetaConfigError):
            client(settings=MetaSettings(app_id=APP))[0].get("me", TOKEN)  # no secret, no proof

    def test_a_redirect_is_refused_not_followed(self):
        graph, session, _ = client(FakeResponse(302, raw=b""), FakeResponse(200, {}))
        with self.assertRaises(GraphError) as refused:
            graph.get("me", TOKEN)
        self.assertEqual((refused.exception.type, refused.exception.http_status), ("redirect", 302))
        self.assertEqual(len(session.calls), 1)

    def test_the_default_session_ignores_the_environment_and_keeps_no_cookies(self):
        session = _new_session()
        self.assertFalse(session.trust_env)
        self.assertIsInstance(session.cookies._policy, _NoCookies)
        self.assertFalse(session.cookies._policy.set_ok(None, None))

    def test_urllib3_debug_lines_lose_the_query_secrets(self):
        with self.assertLogs("urllib3.connectionpool", level="DEBUG") as captured:
            logging.getLogger("urllib3.connectionpool").debug(
                '%s://%s:%s "%s %s %s" %s %s', "https", "graph.facebook.com", 443, "GET",
                f"/v26.0/debug_token?input_token={TOKEN}&access_token={APP}|{SECRET}"
                f"&appsecret_proof={proof_of(TOKEN)}&fields=id", "HTTP/1.1", 200, 10)
        line = captured.output[0]
        for secret in (TOKEN, SECRET, proof_of(TOKEN)):
            self.assertNotIn(secret, line)
        self.assertIn("fields=id", line)


class RetryTests(unittest.TestCase):
    def test_reads_retry_on_throttling_5xx_and_broken_connections_with_backoff(self):
        graph, session, sleeps = client(graph_error(4), FakeResponse(503, raw=b"<html>"),
                                        FakeResponse(200, {"ok": 1}))
        self.assertEqual(graph.get("me", TOKEN), {"ok": 1})
        self.assertEqual((len(session.calls), sleeps), (3, [1.0, 2.0]))
        graph, session, sleeps = client(requests.exceptions.ConnectionError("reset"),
                                        FakeResponse(200, {"ok": 2}))
        self.assertEqual(graph.get("me", TOKEN), {"ok": 2})
        self.assertEqual(len(sleeps), 1)

    def test_reads_give_up_after_two_retries_and_never_retry_other_errors(self):
        graph, session, _ = client(*(graph_error(613) for _ in range(5)))
        with self.assertRaises(GraphError) as refused:
            graph.get("me", TOKEN)
        self.assertTrue(refused.exception.throttled)
        self.assertEqual(len(session.calls), 3)
        for code in (100, 190, 10, 200):
            graph, session, sleeps = client(graph_error(code), FakeResponse(200, {}))
            with self.assertRaises(GraphError):
                graph.get("me", TOKEN)
            self.assertEqual((len(session.calls), sleeps), (1, []), code)

    def test_writes_run_once_unless_declared_idempotent(self):
        for outcome in (graph_error(4), FakeResponse(500, raw=b""),
                        requests.exceptions.ConnectionError("reset")):
            graph, session, _ = client(outcome, FakeResponse(200, {}))
            with self.assertRaises(GraphError):
                graph.post("me/feed", TOKEN, {"message": "x"})
            self.assertEqual(len(session.calls), 1)
        graph, session, _ = client(graph_error(17), FakeResponse(200, {"success": True}))
        self.assertEqual(graph.subscribe_page(PAGE, TOKEN, ["messages"]), {"success": True})
        self.assertEqual(len(session.calls), 2)

    def test_messages_to_customers_are_never_retried(self):
        sends = (lambda graph: graph.send_message(PAGE, TOKEN, PSID, {"text": "hi"}),
                 lambda graph: graph.sender_action(PAGE, TOKEN, PSID, "typing_on"),
                 lambda graph: graph.private_reply(PAGE, TOKEN, COMMENT, {"text": "hi"}),
                 lambda graph: graph.comment_reply(TOKEN, COMMENT, "Thanks"))
        for send in sends:
            for outcome in (graph_error(613), graph_error(80006), FakeResponse(503, raw=b""),
                            requests.exceptions.ReadTimeout("slow")):
                graph, session, sleeps = client(outcome, FakeResponse(200, {}))
                with self.assertRaises(GraphError):
                    send(graph)
                self.assertEqual((len(session.calls), sleeps), (1, []))
                self.assertEqual(session.calls[0]["timeout"], (5.0, 17.0))


class ErrorTests(unittest.TestCase):
    def test_meta_error_codes_are_classified(self):
        cases = {
            (190, None): {"token_dead"}, (190, 463): {"token_dead"},
            (10, None): {"gated"}, (10, 2018278): {"outside_window"},
            (100, 2018278): {"outside_window", "invalid"},
            # Meta's Send API error codes page: the gate as documented, window and person
            # codes, and 2018109, which is an attachment over the size limit.
            (200, 2018028): {"gated"}, (200, 2018027): {"gated"}, (10, 2018028): {"gated"},
            (10, 2018065): {"outside_window"}, (10, 2534022): {"outside_window"},
            (100, 2018109): {"invalid"},
            (551, None): {"unavailable"}, (551, 1545041): {"unavailable"},
            (200, 1545041): {"unavailable"}, (10, 2018108): {"unavailable"},
            # Neither the gate nor a closed window: another app holds the chat, the page is
            # restricted, the person opted out.
            (10, 2018300): set(), (10, 1893063): set(), (10, 1893015): set(),
            (100, None): {"invalid"},
            (4, None): {"throttled"}, (17, None): {"throttled"}, (32, None): {"throttled"},
            (613, None): {"throttled"}, (80004, None): {"throttled"},
            (80006, None): {"throttled"}, (200, None): set(),
        }
        flags = ("token_dead", "gated", "outside_window", "unavailable", "throttled", "invalid")
        for (code, subcode), expected in cases.items():
            graph, _, _ = client(graph_error(code, subcode, status=400))
            with self.assertRaises(GraphError) as refused:
                graph.post("me/messages", TOKEN, {})
            error = refused.exception
            self.assertEqual({flag for flag in flags if getattr(error, flag)}, expected,
                             (code, subcode))
            self.assertEqual((error.code, error.subcode, error.type, error.fbtrace_id,
                              error.http_status), (code, subcode, "OAuthException", "AbCdEf123",
                                                   400))

    def test_answers_that_are_not_graph_json(self):
        cases = ((FakeResponse(502, raw=b"<html>Bad gateway</html>"), "http", False),
                 (FakeResponse(200, raw=b"not json"), "truncated", True),
                 (FakeResponse(200, raw=b"[1, 2]"), "truncated", True))
        for response, kind, applied in cases:
            graph, _, _ = client(response)
            with self.assertRaises(GraphError) as refused:
                graph.post("me/messages", TOKEN, {})
            self.assertEqual((refused.exception.type, refused.exception.maybe_applied),
                             (kind, applied))
        graph, _, _ = client(FakeResponse(200, raw=b"true"))
        self.assertEqual(graph.delete(f"{PAGE}/subscribed_apps", TOKEN), {"success": True})
        graph, _, _ = client(FakeResponse(200, raw=b"x" * (8 * 1024 * 1024 + 70000)))
        with self.assertRaises(GraphError) as refused:
            graph.post("me/messages", TOKEN, {})
        self.assertEqual(refused.exception.message, "response_too_large")

    def test_connection_failures_say_whether_the_request_may_have_been_applied(self):
        never_sent = requests.exceptions.ConnectionError(MaxRetryError(
            None, "/x", reason=NewConnectionError(None, "refused")))
        cases = ((requests.exceptions.ConnectTimeout("connect"), "connect_timeout", False),
                 (never_sent, "connection_error", False),
                 (requests.exceptions.ConnectionError(ProtocolError("aborted")),
                  "connection_error", True),
                 (requests.exceptions.ReadTimeout("read"), "read_timeout", True),
                 (requests.exceptions.ChunkedEncodingError("cut"), "request_error", True))
        for exception, message, applied in cases:
            graph, _, _ = client(exception)
            with self.assertRaises(GraphError) as refused:
                graph.send_message(PAGE, TOKEN, PSID, {"text": "hi"})
            error = refused.exception
            self.assertEqual((error.http_status, error.network, error.message,
                              error.maybe_applied), (0, True, message, applied))

    def test_no_secret_reaches_the_exception_its_text_or_its_traceback(self):
        url = (f"{BASE}oauth/access_token?access_token={APP}|{SECRET}&client_secret={SECRET}"
               f"&fb_exchange_token={TOKEN}")
        leaky = (requests.exceptions.ConnectionError(MaxRetryError(None, url, reason=None)),
                 requests.exceptions.ReadTimeout(f"Read timed out. (url: {url})"),
                 graph_error(190, message=f"Bad token {TOKEN} and EAAother0123456789abcdef "
                                          f"for secret {SECRET}"))
        for outcome in leaky:
            graph, _, _ = client(outcome, outcome, outcome)
            with self.assertRaises(GraphError) as refused:
                graph.exchange_user_token(TOKEN)
            error = refused.exception
            text = " ".join([str(error), repr(error), repr(error.args), error.message,
                             "".join(traceback.format_exception(error)),
                             json.dumps(error.summary())])
            for secret in (TOKEN, SECRET, proof_of(f"{APP}|{SECRET}"), "EAAother0123456789"):
                self.assertNotIn(secret, text)
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__cause__)


class EndpointTests(unittest.TestCase):
    def test_paging_follows_the_after_cursor_on_the_same_path(self):
        first = {"data": [{"id": "1"}, {"id": "2"}],
                 "paging": {"cursors": {"before": "B0", "after": "A1"},
                            "next": "https://evil.example/next?access_token=stolen"}}
        second = {"data": [{"id": "3"}, "junk"], "paging": {"cursors": {"after": "A2"}}}
        graph, session, _ = client(FakeResponse(200, first), FakeResponse(200, second))
        rows = graph.paged("me/accounts", TOKEN, {"fields": "id", "before": "x"})
        self.assertEqual(rows, [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        self.assertEqual([call["url"] for call in session.calls], [BASE + "me/accounts"] * 2)
        self.assertNotIn("after", session.calls[0]["params"])
        self.assertNotIn("before", session.calls[0]["params"])
        self.assertEqual(session.calls[1]["params"]["after"], "A1")
        endless = {"data": [{"id": "x"}], "paging": {"cursors": {"after": "A"}, "next": "n"}}
        graph, session, _ = client(*(FakeResponse(200, endless) for _ in range(5)))
        self.assertEqual(len(graph.paged("me/accounts", TOKEN, max_pages=3)), 3)
        self.assertEqual(len(session.calls), 3)

    def test_app_calls_use_the_app_token(self):
        graph, session, _ = client(FakeResponse(200, {"data": {"is_valid": True,
                                                               "type": "USER"}}),
                                   FakeResponse(200, {"access_token": "EAAlong0000000000000",
                                                      "expires_in": 5183944}))
        self.assertEqual(graph.debug_token(TOKEN), {"is_valid": True, "type": "USER"})
        self.assertEqual(graph.exchange_user_token(TOKEN),
                         {"access_token": "EAAlong0000000000000", "expires_in": 5183944})
        debug, exchange = session.calls
        self.assertEqual(debug["url"], BASE + "debug_token")
        self.assertEqual((debug["params"]["input_token"], debug["params"]["access_token"]),
                         (TOKEN, f"{APP}|{SECRET}"))
        self.assertEqual({key: exchange["params"][key] for key in (
            "grant_type", "client_id", "client_secret", "fb_exchange_token")},
            {"grant_type": "fb_exchange_token", "client_id": APP, "client_secret": SECRET,
             "fb_exchange_token": TOKEN})
        self.assertEqual(graph.app_token(), f"{APP}|{SECRET}")
        with self.assertRaises(MetaConfigError):
            GraphClient(MetaSettings(app_secret=SECRET), session=FakeSession()).app_token()

    def test_subscriptions(self):
        graph, session, _ = client(FakeResponse(500, raw=b""), FakeResponse(200, {"success": 1}),
                                   FakeResponse(200, {"data": []}), FakeResponse(200, {}),
                                   FakeResponse(200, {"data": []}))
        graph.subscribe_app("https://hook.example/meta/webhook", "v" * 40,
                            ["messages", "feed"])
        subscribe = session.calls[1]  # retried once after the 500
        self.assertEqual((subscribe["method"], subscribe["url"]),
                         ("POST", f"{BASE}{APP}/subscriptions"))
        self.assertEqual({key: subscribe["data"][key] for key in (
            "object", "callback_url", "verify_token", "fields", "include_values",
            "access_token")}, {"object": "page", "callback_url": "https://hook.example/meta/webhook",
                               "verify_token": "v" * 40, "fields": "messages,feed",
                               "include_values": "true", "access_token": f"{APP}|{SECRET}"})
        graph.app_subscriptions()
        graph.unsubscribe_page(PAGE, TOKEN)
        graph.page_subscriptions(PAGE, TOKEN)
        unsubscribe = session.calls[3]
        self.assertEqual((unsubscribe["method"], unsubscribe["url"], unsubscribe["data"]),
                         ("DELETE", f"{BASE}{PAGE}/subscribed_apps", None))
        self.assertEqual(unsubscribe["params"]["access_token"], TOKEN)
        self.assertEqual(session.calls[4]["url"], f"{BASE}{PAGE}/subscribed_apps")
        for url in ("http://hook.example/meta/webhook", ""):
            with self.assertRaises(ValueError):
                graph.subscribe_app(url, "v" * 40, ["messages"])
        for fields in ([], ["Messages"], ["messages,feed"]):
            with self.assertRaises(ValueError):
                graph.subscribe_page(PAGE, TOKEN, fields)

    def test_send_api_payloads(self):
        graph, session, _ = client(FakeResponse(200, {"recipient_id": PSID,
                                                      "message_id": "m_abc"}),
                                   FakeResponse(200, {"recipient_id": PSID}),
                                   FakeResponse(200, {"recipient_id": PSID, "message_id": "m_2"}),
                                   FakeResponse(200, {"id": "123_789"}),
                                   FakeResponse(200, {"success": True}))
        message = {"text": "გამარჯობა", "quick_replies": [{"content_type": "text",
                                                            "title": "BMW", "payload": "CHOICE:1"}]}
        self.assertEqual(graph.send_message(PAGE, TOKEN, PSID, message),
                         {"recipient_id": PSID, "message_id": "m_abc"})
        self.assertEqual(graph.sender_action(PAGE, TOKEN, PSID, "typing_on"),
                         {"recipient_id": PSID, "message_id": ""})
        graph.private_reply(PAGE, TokenChoice(TOKEN, False, "send_page"), COMMENT,
                            {"text": "Hi"})
        self.assertEqual(graph.comment_reply(TOKEN, COMMENT, "Thanks"), {"id": "123_789"})
        graph.set_comment_hidden(TOKEN, COMMENT, True)
        send, action, private, reply, hide = session.calls
        self.assertEqual(send["url"], f"{BASE}{PAGE}/messages")
        self.assertEqual((json.loads(send["data"]["recipient"]),
                          json.loads(send["data"]["message"]), send["data"]["messaging_type"]),
                         ({"id": PSID}, message, "RESPONSE"))
        self.assertIn("გამარჯობა", send["data"]["message"])
        self.assertEqual(action["data"]["sender_action"], "typing_on")
        self.assertEqual(json.loads(private["data"]["recipient"]), {"comment_id": COMMENT})
        self.assertNotIn("messaging_type", private["data"])
        self.assertNotIn("appsecret_proof", private["data"])
        self.assertEqual((reply["url"], reply["data"]["message"]),
                         (f"{BASE}{COMMENT}/comments", "Thanks"))
        self.assertEqual((hide["url"], hide["data"]["is_hidden"]), (BASE + COMMENT, "true"))
        refused = (lambda: graph.send_message(PAGE, TOKEN, "not-a-psid", {"text": "x"}),
                   lambda: graph.send_message("page", TOKEN, PSID, {"text": "x"}),
                   lambda: graph.send_message(PAGE, TOKEN, PSID, {"text": "x"}, "MESSAGE_TAG"),
                   lambda: graph.send_message(PAGE, TOKEN, PSID, {}),
                   lambda: graph.sender_action(PAGE, TOKEN, PSID, "typing_forever"),
                   lambda: graph.private_reply(PAGE, TOKEN, "../me", {"text": "x"}),
                   lambda: graph.comment_reply(TOKEN, COMMENT, "  "))
        for call in refused:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(len(session.calls), 5)


if __name__ == "__main__":
    unittest.main()
