"""Billing failover through the real Anthropic SDK and in-memory HTTP transports."""

from __future__ import annotations

import copy
import json
import logging
import os
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx

from automotive.chat import ChatService
from automotive.config import Settings
from automotive.model_client import LOGGER

SDK = anthropic.Anthropic
PRIMARY_KEY = "fixture-primary-key"
BACKUP_KEY = "fixture-openrouter-key"
MODEL = "claude-sonnet-5-5"
ROUTED_MODEL = "anthropic/claude-sonnet-5.5"
BILLING = "Your credit balance is too low to access the Anthropic API."


def error(status=400, message=BILLING, kind="invalid_request_error"):
    return httpx.Response(status, json={"type": "error", "error": {
        "type": kind, "message": message}})


def reply(model=MODEL):
    return {"id": "msg_fixture", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": "ready"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}


def stream_reply():
    start = reply(ROUTED_MODEL)
    start.update(content=[], stop_reason=None)
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "ready"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))


class BillingFallbackTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.clients = []
        self.primary = lambda _: error()
        self.backup = lambda r: stream_reply() if json.loads(r.content).get("stream") else \
            httpx.Response(200, json=reply(ROUTED_MODEL))

        def factory(**options):
            def answer(request):
                self.sent.append(request)
                handler = self.backup if request.url.host == "openrouter.ai" else self.primary
                return handler(request)
            options["http_client"] = httpx.Client(transport=httpx.MockTransport(answer))
            client = SDK(**options)
            self.clients.append(client)
            return client

        self.factory = self.enterContext(patch("anthropic.Anthropic", side_effect=factory))
        # The switch is logged as a warning; keep it out of the test output.
        quiet = logging.NullHandler()
        LOGGER.addHandler(quiet)
        self.addCleanup(LOGGER.removeHandler, quiet)
        # The environment must not be able to inject the direct key into gateway auth.
        self.enterContext(patch.dict(os.environ, {"ANTHROPIC_API_KEY": PRIMARY_KEY,
                                                  "ANTHROPIC_AUTH_TOKEN": "env-token"}))

    def tearDown(self):
        for client in self.clients:
            client.close()

    def chat(self, key=BACKUP_KEY):
        options = asdict(Settings(anthropic_api_key=PRIMARY_KEY,
                                  anthropic_workspace_id="workspace-private"))
        options["openrouter_api_key"] = key
        return ChatService(None, None, SimpleNamespace(**options))

    def arguments(self, **changes):
        return {"model": MODEL, "max_tokens": 100,
                "system": [{"type": "text", "text": "Use the catalogue only.",
                            "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"name": "lookup", "description": "Read parts", "strict": True,
                           "input_schema": {"type": "object", "properties": {}}}],
                "tool_choice": {"type": "auto"},
                "extra_body": {"output_config": {"effort": "low"}}, **changes}

    def test_insufficient_credit_retries_the_same_request_and_model_via_openrouter(self):
        arguments = self.arguments(thinking={"type": "disabled"})
        before = copy.deepcopy(arguments)
        response = self.chat().assistant_client().messages.create(**arguments)
        self.assertEqual(response.content[0].text, "ready")
        self.assertEqual([str(r.url) for r in self.sent], [
            "https://api.anthropic.com/v1/messages", "https://openrouter.ai/api/v1/messages"])
        first, second = [json.loads(r.content) for r in self.sent]
        self.assertEqual(second.pop("model"), ROUTED_MODEL)
        first.pop("model")
        # No require_parameters: OpenRouter's own check turns a refused value into a 404.
        self.assertEqual(second.pop("provider"), {"only": ["anthropic"], "allow_fallbacks": False})
        self.assertEqual(first, second)
        self.assertEqual(arguments, before)

    def test_streaming_billing_refusal_is_retried_before_any_events_are_delivered(self):
        with self.chat().assistant_client().messages.stream(**self.arguments()) as events:
            observed = list(events)
            final = events.get_final_message()
        self.assertEqual(final.content[0].text, "ready")
        self.assertEqual(observed[0].type, "message_start")
        self.assertTrue(all(json.loads(r.content)["stream"] for r in self.sent))
        self.assertEqual(len(self.sent), 2)

    def test_payment_required_and_typed_billing_errors_use_the_same_fallback(self):
        cases = [(402, "Payment required", "payment_required"),
                 (400, "Account disabled", "billing_error"),
                 (403, "Insufficient credits", "permission_error")]
        for status, message, kind in cases:
            with self.subTest(status=status, kind=kind):
                self.sent.clear()
                self.primary = lambda _, s=status, m=message, k=kind: error(s, m, k)
                self.chat().assistant_client().messages.create(**self.arguments())
                self.assertEqual([r.url.host for r in self.sent], [
                    "api.anthropic.com", "openrouter.ai"])

    def test_existing_tool_results_and_thinking_signatures_survive_a_mid_turn_switch(self):
        history = [
            {"role": "user", "content": "Camry pads"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Read the catalogue", "signature": "sig"},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "part A"}]},
        ]
        self.chat().assistant_client().messages.create(**self.arguments(messages=history))
        self.assertEqual(json.loads(self.sent[-1].content)["messages"], history)

    def test_a_read_failure_after_the_stream_opens_is_never_replayed_by_failover(self):
        body = error().content
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        failure = anthropic.BadRequestError("billing", response=httpx.Response(
            400, request=request), body=json.loads(body))
        primary = unittest.mock.MagicMock()
        primary.messages.stream.return_value.__enter__.return_value.__iter__.side_effect = failure
        from automotive.model_client import BillingFallbackClient

        client = BillingFallbackClient(primary, BACKUP_KEY)
        created_before = self.factory.call_count
        with self.assertRaises(anthropic.BadRequestError):
            with client.messages.stream(**self.arguments()) as events:
                list(events)
        self.assertEqual(self.factory.call_count, created_before)

    def test_direct_success_never_uses_the_backup(self):
        self.primary = lambda _: httpx.Response(200, json=reply())
        response = self.chat().assistant_client().messages.create(**self.arguments())
        self.assertEqual(response.model, MODEL)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.clients), 1)

    def test_no_other_provider_failure_triggers_failover(self):
        cases = [(400, "messages: invalid tool schema"), (401, "bad key"),
                 (403, "model access denied"), (404, "unknown model"),
                 (429, "rate limited"), (500, "internal error"),
                 (400, "The word billing is not a valid tool name")]
        for status, message in cases:
            with self.subTest(status=status, message=message):
                self.sent.clear()
                self.primary = lambda _, s=status, m=message: error(s, m)
                with self.assertRaises(anthropic.APIStatusError):
                    self.chat().assistant_client().messages.create(**self.arguments())
                self.assertEqual(len(self.sent), 1)

    def test_missing_backup_key_keeps_the_original_billing_error(self):
        with self.assertRaises(anthropic.BadRequestError):
            self.chat(key="").assistant_client().messages.create(**self.arguments())
        self.assertEqual(len(self.sent), 1)

    def test_backup_errors_are_propagated_once_without_a_provider_loop(self):
        self.backup = lambda _: error(402, "OpenRouter has no credits")
        with self.assertRaises(anthropic.APIStatusError) as caught:
            self.chat().assistant_client().messages.create(**self.arguments())
        self.assertEqual(caught.exception.status_code, 402)
        self.assertEqual(len(self.sent), 2)

    def test_each_credential_is_sent_only_to_its_own_provider(self):
        self.chat().assistant_client().messages.create(**self.arguments())
        first, second = self.sent
        self.assertEqual(first.headers["x-api-key"], PRIMARY_KEY)
        self.assertEqual(first.headers["anthropic-workspace-id"], "workspace-private")
        self.assertNotIn("authorization", first.headers)
        self.assertEqual(second.headers["authorization"], "Bearer " + BACKUP_KEY)
        self.assertNotIn("x-api-key", second.headers)
        self.assertNotIn("anthropic-workspace-id", second.headers)
        self.assertNotIn(PRIMARY_KEY, str(second.headers))
        self.assertNotIn("env-token", str(second.headers))
        self.assertNotIn(BACKUP_KEY, str(first.headers))

    def test_following_calls_skip_the_unfunded_account_then_retry_it_after_cooldown(self):
        client = self.chat().assistant_client()
        with patch("automotive.model_client.time.monotonic", return_value=100):
            client.messages.create(**self.arguments())
            client.messages.create(**self.arguments())
        self.assertEqual([r.url.host for r in self.sent], [
            "api.anthropic.com", "openrouter.ai", "openrouter.ai"])
        self.primary = lambda _: httpx.Response(200, json=reply())
        with patch("automotive.model_client.time.monotonic", return_value=161):
            client.messages.create(**self.arguments())
            client.messages.create(**self.arguments())
        self.assertEqual([r.url.host for r in self.sent][-2:], ["api.anthropic.com"] * 2)

    def test_billing_retry_uses_only_the_remaining_request_timeout(self):
        for timeout in (20.0, httpx.Timeout(20, connect=5, read=10)):
            with self.subTest(timeout=timeout):
                self.sent.clear()
                client = self.chat().assistant_client()
                now = [100.0]

                def refused(_):
                    now[0] += 15
                    return error()

                self.primary = refused
                with patch("automotive.model_client.time.monotonic", side_effect=lambda: now[0]):
                    client.messages.create(**self.arguments(timeout=timeout))
                self.assertEqual(self.sent[-1].extensions["timeout"]["read"], 5)
                self.assertEqual(self.sent[-1].extensions["timeout"]["connect"], 5)

    def test_legacy_chat_client_uses_the_same_billing_fallback(self):
        response = self.chat()._client().messages.create(**self.arguments(
            model="claude-haiku-5-5"))
        self.assertEqual(response.content[0].text, "ready")
        self.assertEqual(json.loads(self.sent[-1].content)["model"],
                         "anthropic/claude-haiku-5.5")

    def test_unknown_model_is_never_replaced_with_a_different_model(self):
        with self.assertRaises(anthropic.BadRequestError):
            self.chat().assistant_client().messages.create(**self.arguments(
                model="claude-custom-release"))
        self.assertEqual(len(self.sent), 1)

    def test_gateway_cannot_be_redirected_by_anthropic_base_url_from_environment(self):
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}):
            self.chat().assistant_client().messages.create(**self.arguments())
        self.assertEqual(self.sent[-1].url.host, "openrouter.ai")

    def test_closing_the_shared_client_closes_both_provider_connections(self):
        chat = self.chat()
        chat.assistant_client().messages.create(**self.arguments())
        chat.close()
        self.assertTrue(all(c.is_closed() for c in self.clients))

    def test_spend_limits_and_the_monthly_spend_cap_use_the_same_fallback(self):
        # The documented refusals: a limit the owner set (400), the tier's cap (429).
        regain = "You will regain access on 2026-12-01 at 00:00 UTC."
        cap = {"type": "error", "error": {
            "type": "rate_limit_error", "details": {"error_code": "enforced_spend_limit_reached"},
            "message": "You have reached your API usage limits: your organization has crossed "
                       f"its monthly API usage threshold. {regain}"}}
        coded = copy.deepcopy(cap)
        coded["error"]["message"] = "Monthly spend cap reached."  # the code alone decides
        worded = copy.deepcopy(cap)
        del worded["error"]["details"]  # the documented wording alone decides
        cases = {
            "organization limit": lambda _: error(
                400, f"You have reached your specified API usage limits. {regain}"),
            "workspace limit": lambda _: error(
                400, f"You have reached your specified workspace API usage limits. {regain}"),
            "tier spend cap": lambda _: httpx.Response(429, json=cap),
            "spend cap by its code": lambda _: httpx.Response(429, json=coded),
            "spend cap by its words": lambda _: httpx.Response(429, json=worded),
        }
        for name, refusal in cases.items():
            with self.subTest(name):
                self.sent.clear()
                self.primary = refusal
                response = self.chat().assistant_client().messages.create(**self.arguments())
                self.assertEqual(response.content[0].text, "ready")
                self.assertEqual([r.url.host for r in self.sent],
                                 ["api.anthropic.com", "openrouter.ai"])

    def test_openrouter_may_stay_silent_longer_than_a_turn_lets_the_direct_api(self):
        # A turn sets a 10 s stall limit; OpenRouter's first byte can take longer.
        limits = httpx.Timeout(60, connect=5, read=10)
        self.chat().assistant_client().messages.create(**self.arguments(timeout=limits))
        direct, backup = (r.extensions["timeout"] for r in self.sent)
        self.assertEqual((direct["read"], direct["connect"]), (10, 5))
        self.assertEqual((backup["read"], backup["connect"]), (15, 5))
        # Never beyond what is left of the request's own time.
        self.sent.clear()
        self.chat().assistant_client().messages.create(**self.arguments(
            timeout=httpx.Timeout(12, connect=5, read=10)))
        self.assertAlmostEqual(self.sent[-1].extensions["timeout"]["read"], 12, delta=0.5)

    def test_the_public_openrouter_client_sends_only_its_own_key_to_openrouter(self):
        from automotive.model_client import openrouter_client

        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}):
            client = openrouter_client(BACKUP_KEY)
            client.messages.create(**{**self.arguments(), "model": ROUTED_MODEL})
        sent = self.sent[-1]
        self.assertEqual(str(sent.url), "https://openrouter.ai/api/v1/messages")
        self.assertEqual(sent.headers["authorization"], "Bearer " + BACKUP_KEY)
        self.assertNotIn("x-api-key", sent.headers)
        self.assertNotIn(PRIMARY_KEY, str(sent.headers))
        self.assertEqual(set(sent.extensions["timeout"].values()), {25.0})

    def test_the_backup_makes_one_attempt_per_request(self):
        def slow(request):
            raise httpx.ReadTimeout("no first byte", request=request)

        for name, backup, raised in (
                ("timeout", slow, anthropic.APITimeoutError),
                ("server error", lambda _: error(500, "upstream failed", "api_error"),
                 anthropic.InternalServerError)):
            with self.subTest(name):
                self.sent.clear()
                self.backup = backup
                with self.assertRaises(raised):
                    self.chat().assistant_client().messages.create(**self.arguments())
                self.assertEqual([r.url.host for r in self.sent],
                                 ["api.anthropic.com", "openrouter.ai"])

    def test_a_request_without_its_own_timeout_waits_as_long_on_either_provider(self):
        self.chat().assistant_client().messages.create(**self.arguments())
        direct, backup = (r.extensions["timeout"] for r in self.sent)
        self.assertEqual(direct, backup)
        self.assertEqual(set(backup.values()), {25.0})

    def test_an_unmapped_model_asks_the_direct_account_even_while_it_is_skipped(self):
        client = self.chat().assistant_client()
        client.messages.create(**self.arguments())  # the direct account is skipped now
        with self.assertRaises(anthropic.BadRequestError):
            client.messages.create(**self.arguments(model="claude-custom-release"))
        self.assertEqual([r.url.host for r in self.sent],
                         ["api.anthropic.com", "openrouter.ai", "api.anthropic.com"])

    def test_a_recheck_that_is_refused_again_is_logged_quietly(self):
        client, now = self.chat().assistant_client(), [100.0]
        with patch("automotive.model_client.time.monotonic", side_effect=lambda: now[0]), \
                self.assertLogs("automotive.model_client", level="INFO") as logs:
            client.messages.create(**self.arguments())
            now[0] = 161
            client.messages.create(**self.arguments())
        self.assertEqual([record.levelname for record in logs.records], ["WARNING", "INFO"])
        self.assertTrue(all(f"model={MODEL} status=400" in line for line in logs.output))

    def test_a_cache_warm_up_asks_openrouter_for_one_token_not_a_whole_reply(self):
        self.chat().assistant_client().messages.create(**self.arguments(max_tokens=0))
        first, second = [json.loads(r.content) for r in self.sent]
        self.assertEqual((first["max_tokens"], second["max_tokens"]), (0, 1))

    def test_an_ordinary_rate_limit_is_not_a_spend_cap(self):
        self.primary = lambda _: error(429, "This request would exceed the rate limit for your "
                                            "organization of 4,000 requests per minute.",
                                       "rate_limit_error")
        with self.assertRaises(anthropic.RateLimitError):
            self.chat().assistant_client().messages.create(**self.arguments())
        self.assertEqual([r.url.host for r in self.sent], ["api.anthropic.com"])

    def test_a_copy_with_other_options_still_fails_over_and_shares_the_wait(self):
        shared = self.chat().assistant_client()
        copy = shared.with_options(timeout=7.0, max_retries=0)
        self.assertEqual(copy.messages.create(**self.arguments()).content[0].text, "ready")
        self.assertEqual(self.sent[-1].url.host, "openrouter.ai")
        self.assertEqual(self.sent[-1].extensions["timeout"]["read"], 7.0)
        # What the copy learned about the direct account, the shared client knows too.
        shared.messages.create(**self.arguments())
        self.assertEqual([r.url.host for r in self.sent],
                         ["api.anthropic.com", "openrouter.ai", "openrouter.ai"])
        copy.close()

    def test_an_account_that_keeps_refusing_is_asked_less_often_until_it_answers(self):
        client = self.chat().assistant_client()
        now = [100.0]
        with patch("automotive.model_client.time.monotonic", side_effect=lambda: now[0]):
            client.messages.create(**self.arguments())  # refused: skipped for 60 s
            now[0] = 161
            client.messages.create(**self.arguments())  # asked, refused again: 120 s
            now[0] = 280
            client.messages.create(**self.arguments())  # still skipped
            now[0] = 282
            self.primary = lambda _: httpx.Response(200, json=reply())
            client.messages.create(**self.arguments())  # answered: the waits start over
            self.primary = lambda _: error()
            now[0] = 283
            client.messages.create(**self.arguments())  # refused: 60 s again
            now[0] = 344
            client.messages.create(**self.arguments())
        self.assertEqual([r.url.host for r in self.sent], [
            "api.anthropic.com", "openrouter.ai",
            "api.anthropic.com", "openrouter.ai",
            "openrouter.ai",
            "api.anthropic.com",
            "api.anthropic.com", "openrouter.ai",
            "api.anthropic.com", "openrouter.ai"])

    def test_the_wait_doubles_to_ten_minutes_and_refusals_together_start_one_wait(self):
        from automotive.model_client import _Funding

        funding, now, waits = _Funding(), [0.0], []
        with patch("automotive.model_client.time.monotonic", side_effect=lambda: now[0]):
            for _ in range(6):
                wait, _first = funding.refused()
                waits.append(wait)
                self.assertEqual(funding.refused()[0], wait)  # refused at the same moment
                now[0] += wait
        self.assertEqual(waits, [60, 120, 240, 480, 600, 600])

    def test_the_switch_and_the_return_are_logged_once_without_secrets(self):
        client = self.chat().assistant_client()
        with self.assertLogs("automotive.model_client", level="INFO") as logs:
            client.messages.create(**self.arguments())
            client.messages.create(**self.arguments())
            self.primary = lambda _: httpx.Response(200, json=reply())
            with patch("automotive.model_client.time.monotonic", return_value=10.0 ** 9):
                client.messages.create(**self.arguments())
                client.messages.create(**self.arguments())
        text = "\n".join(logs.output)
        self.assertEqual((text.count("chat_billing_fallback"), text.count("WARNING")), (1, 2))
        self.assertEqual(text.count("chat_billing_restored"), 1)
        for private in (BILLING, PRIMARY_KEY, BACKUP_KEY, "workspace-private", "hello"):
            self.assertNotIn(private, text)


class FallbackSettingsTests(unittest.TestCase):
    def test_openrouter_key_is_loaded_without_appearing_in_settings_repr(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": BACKUP_KEY}, clear=True), \
                patch("automotive.config.load_dotenv"):
            settings = Settings.from_env()
        self.assertEqual(settings.openrouter_api_key, BACKUP_KEY)
        self.assertNotIn(BACKUP_KEY, repr(settings))


if __name__ == "__main__":
    unittest.main()
