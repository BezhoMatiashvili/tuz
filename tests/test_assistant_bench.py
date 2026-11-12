"""The subscription bench client, driven by a fake `claude` executable that speaks the same
stream and MCP protocol as Claude Code, so no model, network or subscription is used."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from automotive import assistant_bench
from automotive.assistant_bench import (
    PREFIX,
    SubscriptionClient,
    SubscriptionLimited,
    child_environment,
)

# The fake binary. Session mode: two parallel read calls, then finish carrying what the
# read calls returned. Plain mode: one JSON result line, or a usage-limit error.
FAKE = textwrap.dedent('''
    #!PYTHON
    import json, sys, threading, urllib.request

    args = sys.argv[1:]
    def value(flag):
        return args[args.index(flag) + 1] if flag in args else ""

    def emit(event):
        sys.stdout.write(json.dumps(event) + "\\n")
        sys.stdout.flush()

    if value("--output-format") == "json":
        question = sys.stdin.read()
        if "LIMIT" in question:
            emit({"type": "result", "is_error": True, "result": "Claude usage limit reached"})
            sys.exit(1)
        emit({"type": "result", "is_error": False, "result": "",
              "structured_output": {"echo": question, "model": value("--model"),
                                    "schema": bool(value("--json-schema"))},
              "usage": {"input_tokens": 7, "output_tokens": 3}})
        sys.exit(0)

    url = json.loads(value("--mcp-config"))["mcpServers"]["s"]["url"]
    first = json.loads(sys.stdin.readline())
    emit({"type": "system", "subtype": "init", "apiKeySource": "none",
          "model": value("--model"), "tools": value("--allowedTools").split()})

    def call(identifier, name, arguments, results):
        body = json.dumps({"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments,
                                      "_meta": {"claudecode/toolUseId": identifier}}})
        request = urllib.request.Request(url, body.encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            results[identifier] = json.loads(response.read())["result"]

    def step(number, uses):
        emit({"type": "stream_event", "event": {"type": "message_start", "message": {
            "id": f"msg_{number}", "model": value("--model"),
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90}}}})
        for index, (identifier, name, arguments) in enumerate(uses):
            emit({"type": "stream_event", "event": {"type": "content_block_start",
                  "index": index, "content_block": {"type": "tool_use", "id": identifier,
                                                    "name": "mcp__s__" + name, "input": {}}}})
            emit({"type": "stream_event", "event": {"type": "content_block_delta",
                  "index": index, "delta": {"type": "input_json_delta",
                                            "partial_json": json.dumps(arguments)}}})
            emit({"type": "stream_event", "event": {"type": "content_block_stop",
                                                    "index": index}})
        emit({"type": "stream_event", "event": {"type": "message_delta",
              "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}})
        emit({"type": "stream_event", "event": {"type": "message_stop"}})

    opening = first["message"]["content"][0]["text"]
    step(1, [("toolu_a", "search_catalog", {"part_terms": ["ხუნდ"], "seen": opening[:20]}),
             ("toolu_b", "lookup_codes", {"numbers": ["PN1521"]})])
    results = {}
    threads = [threading.Thread(target=call, args=(i, n, a, results))
               for i, n, a in (("toolu_a", "search_catalog", {}), ("toolu_b", "lookup_codes", {}))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    texts = {key: results[key]["content"][0]["text"] for key in results}
    step(2, [("toolu_c", "finish", {"reply": json.dumps(texts, sort_keys=True)})])
    call("toolu_c", "finish", {}, {})  # held until the client closes the session
''')


class Engine:
    """What AssistantTurn does with the client: the same message list, growing."""

    TOOLS = [{"name": "search_catalog", "description": "Search.", "strict": True,
              "input_schema": {"type": "object", "properties": {}}},
             {"name": "lookup_codes", "description": "Look up.",
              "input_schema": {"type": "object", "properties": {}}},
             {"name": "finish", "description": "Finish.",
              "input_schema": {"type": "object", "properties": {}}}]

    def __init__(self, client: SubscriptionClient) -> None:
        self.client = client
        self.messages = [{"role": "user", "content": "Camry 2015"},
                         {"role": "assistant", "content": "Which part?"},
                         {"role": "user", "content": [{"type": "text",
                                                       "text": '{"customer_message": "pads"}'}]}]

    def create(self) -> object:
        return self.client.messages.create(
            model="claude-sonnet-5-5", max_tokens=2400, system=[{"type": "text",
                                                                  "text": "Be a clerk."}],
            tools=self.TOOLS, messages=self.messages, tool_choice={"type": "auto"},
            extra_body={"output_config": {"effort": "low"}}, timeout=30)


class BenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="bench-test-"))
        binary = self.folder / "claude"
        binary.write_text(FAKE.lstrip().replace("#!PYTHON", f"#!{sys.executable}"),
                          encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        self.client = SubscriptionClient(work_dir=self.folder / "work", binary=str(binary))
        self.addCleanup(self.client.close)

    def test_a_turn_runs_step_by_step_with_held_calls_and_real_results(self) -> None:
        engine = Engine(self.client)
        first = engine.create()
        self.assertEqual(first.stop_reason, "tool_use")
        calls = [block for block in first.content if block["type"] == "tool_use"]
        self.assertEqual([call["name"] for call in calls], ["search_catalog", "lookup_codes"])
        self.assertEqual(calls[1]["input"], {"numbers": ["PN1521"]})
        # The earlier turns reach the session as plain text before this turn's JSON.
        self.assertTrue(calls[0]["input"]["seen"].startswith("Earlier turns"))
        self.assertEqual(first.usage.cache_read_input_tokens, 90)
        engine.messages.append({"role": "assistant", "content": first.content})
        engine.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_a", "content": '{"rows": 3}'},
            {"type": "tool_result", "tool_use_id": "toolu_b", "content": '{"rows": 1}',
             "is_error": False},
            {"type": "text", "text": "Call finish now."}]})
        second = engine.create()
        finish = [block for block in second.content if block["type"] == "tool_use"]
        self.assertEqual(finish[0]["name"], "finish")
        delivered = json.loads(finish[0]["input"]["reply"])
        self.assertEqual(delivered["toolu_a"], '{"rows": 3}')
        # The reminder rides on the last result.
        self.assertEqual(delivered["toolu_b"], '{"rows": 1}\n\nCall finish now.')
        self.assertEqual(self.client.session_count, 1)

    def test_an_accepted_finish_closes_the_session_after_the_grace(self) -> None:
        engine = Engine(self.client)
        first = engine.create()
        engine.messages.append({"role": "assistant", "content": first.content})
        engine.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_a", "content": "a"},
            {"type": "tool_result", "tool_use_id": "toolu_b", "content": "b"}]})
        engine.create()
        with patch.object(assistant_bench, "FINISH_GRACE", 0.2):
            deadline = time.monotonic() + 8
            while self.client._sessions and time.monotonic() < deadline:
                time.sleep(0.1)
        self.assertEqual(self.client._sessions, {})

    def test_a_new_message_list_is_a_new_turn_even_if_its_id_is_reused(self) -> None:
        engine = Engine(self.client)
        engine.create()
        session = next(iter(self.client._sessions.values()))
        self.assertTrue(session.owns(engine.messages))
        self.assertFalse(session.owns([{"role": "user", "content": "other"}]))

    def test_plain_requests_answer_with_the_structured_json(self) -> None:
        response = self.client.messages.create(
            model="claude-haiku-5-5", max_tokens=300, system="Write.",
            messages=[{"role": "user", "content": '{"task": "write"}'}],
            extra_body={"output_config": {"effort": "low", "format": {
                "type": "json_schema", "schema": {"type": "object"}}}})
        answer = json.loads(response.content[0].text)
        self.assertEqual(answer["echo"], '{"task": "write"}')
        self.assertEqual(answer["model"], "claude-haiku-5-5")
        self.assertTrue(answer["schema"])
        self.assertEqual(response.usage.input_tokens, 7)

    def test_a_usage_limit_is_reported_as_such(self) -> None:
        with self.assertRaises(SubscriptionLimited):
            self.client.messages.create(model="claude-haiku-5-5", max_tokens=10, system="x",
                                        messages=[{"role": "user", "content": "LIMIT"}])
        self.assertIsNotNone(self.client.limited_at)

    def test_a_warm_up_starts_nothing(self) -> None:
        response = self.client.messages.create(model="claude-sonnet-5-5", max_tokens=0,
                                               tools=Engine.TOOLS, messages=[])
        self.assertEqual(response.content, [])
        self.assertEqual(self.client.session_count, 0)

    def test_with_options_keeps_the_client(self) -> None:
        self.assertIs(self.client.with_options(timeout=5, max_retries=2), self.client)
        self.assertFalse(hasattr(self.client.messages, "stream"))


class EnvironmentTests(unittest.TestCase):
    def test_no_key_or_parent_marker_reaches_a_child(self) -> None:
        planted = {"ANTHROPIC_API_KEY": "fixture-key", "OPENROUTER_API_KEY": "fixture-or",
                   "ANTHROPIC_AUTH_TOKEN": "fixture-token", "CLAUDECODE": "1",
                   "ANTHROPIC_BASE_URL": "http://example.invalid"}
        with patch.dict(os.environ, planted):
            environment = child_environment({"ANTHROPIC_API_KEY": "sneaked"})
        for name in planted:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["ENABLE_TOOL_SEARCH"], "false")

    def test_tool_names_lose_the_session_prefix(self) -> None:
        self.assertEqual(PREFIX, "mcp__s__")

    def test_a_usage_warning_is_not_a_limit_and_a_refusal_is(self) -> None:
        import threading

        session = object.__new__(assistant_bench.Session)
        session.lock = threading.Condition()
        session.steps, session.limited, session.error = [], False, ""
        warning = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.85}}
        with self.assertLogs(assistant_bench.LOGGER, "WARNING"):
            session._event(warning)
        self.assertFalse(session.limited)
        session._event({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
        self.assertFalse(session.limited)
        session._event({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
        self.assertTrue(session.limited)

    def test_a_binary_being_updated_is_waited_for_and_other_errors_are_not(self) -> None:
        import errno
        from types import SimpleNamespace

        tries: list[str] = []
        failures = [FileNotFoundError(errno.ENOENT, "gone"),
                    OSError(errno.ENOEXEC, "Exec format error")]

        def start(binary: str) -> str:
            tries.append(binary)
            if failures:
                raise failures.pop(0)
            return "started"

        client = SimpleNamespace(binary="/old/claude")
        with patch.object(assistant_bench.time, "sleep"), \
                patch.object(assistant_bench.shutil, "which", return_value="/new/claude"):
            self.assertEqual(assistant_bench._start_child(start, client), "started")
        self.assertEqual(tries, ["/old/claude", "/new/claude", "/new/claude"])

        def denied(binary: str) -> str:
            raise PermissionError(errno.EACCES, "denied")

        with self.assertRaises(PermissionError):
            assistant_bench._start_child(denied, client)


if __name__ == "__main__":
    unittest.main()
