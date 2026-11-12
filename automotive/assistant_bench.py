"""The assistant engine's model, served by the Claude subscription instead of the paid API.

``SubscriptionClient`` stands where the Anthropic client stands (``service.client``), so a
turn runs as it does in the chats: ``AssistantTurn`` builds the request, runs the read tools
side by side, checks ``finish`` and asks for corrections. Only the model call is served
differently:

- A request that carries tools is one step of a headless Claude Code session
  (``claude -p``) started for that turn. The session gets the request's system prompt and
  tools; its tool calls reach a local MCP endpoint, which holds each call until the engine
  has run the tool and sends the result in its next request.
- A request without tools (the writer, the customer, the judge, the part-name reader) is one
  ``claude -p`` run that answers with JSON.

Every child process gets a whitelisted environment: no API key can reach it, so only the
subscription login answers, and nothing of the owner's own Claude Code setup (hooks,
plugins, memory, project instructions) is loaded. For the evaluation runners only.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess  # nosec B404 - runs the local Claude Code binary with fixed arguments
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

LOGGER = logging.getLogger(__name__)

SERVER = "s"  # the tools appear to the model as mcp__s__<name>
PREFIX = f"mcp__{SERVER}__"
# Absolute: the children run in their own empty folder, so a relative path would point there.
WORK_DIR = Path(__file__).resolve().parent.parent / "data" / "assistant_bench"
# The only variables a child inherits; everything else (API keys, the parent session's
# own markers, proxies) stays out. A key in the environment would be used before the login.
KEEP_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "TMPDIR")
BANNED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "OPENROUTER_API_KEY", "CLAUDECODE", "CLAUDE_CODE_USE_BEDROCK",
              "CLAUDE_CODE_USE_VERTEX")
CHILD_ENV = {
    "TERM": "dumb", "ENABLE_TOOL_SEARCH": "false", "MCP_CONNECTION_NONBLOCKING": "0",
    "MCP_CONNECT_TIMEOUT_MS": "10000", "MCP_TOOL_TIMEOUT": "300000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
    "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": "20",
}
# Said once in the system prompt: the bench names the engine's tools with a prefix.
TOOL_NOTE = ("\n\n(Tool names in this session carry the prefix mcp__s__: search_catalog is "
             "mcp__s__search_catalog, finish is mcp__s__finish, and so on.)")
LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "rate_limit", "overloaded",
               "too many requests", "429")
STEP_SECONDS = 120.0     # longest wait for one model step
FINISH_GRACE = 10.0      # a session idle this long after a finish step is closed
IDLE_SECONDS = 300.0     # any other session idle this long is closed
PLAIN_SECONDS = 240.0    # longest wait for one plain request


class SubscriptionLimited(RuntimeError):
    """The subscription refused a request for its usage or rate limit."""


class Block(dict):
    """A content block that reads as a dict (what the engine expects) and as an object
    (what the writer and judge code expects)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _usage(values: dict | None) -> SimpleNamespace:
    values = values or {}
    return SimpleNamespace(**{key: int(values.get(key) or 0) for key in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens")}, cache_creation=None)


def _response(content: list[Block], stop: str, usage: dict | None, model: str) -> Any:
    return SimpleNamespace(id=f"bench_{time.monotonic_ns()}", type="message", role="assistant",
                           model=model, content=content, stop_reason=stop,
                           stop_sequence=None, usage=_usage(usage))


def _text_of(content: Any) -> str:
    """The text of a message's content, a string or a list of blocks."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", "")
        if kind == "text":
            parts.append(str(block.get("text") if isinstance(block, dict)
                             else getattr(block, "text", "")))
    return "\n".join(part for part in parts if part)


def _system_text(system: Any) -> str:
    return _text_of(system) if not isinstance(system, str) else system


def child_environment(extra: dict | None = None) -> dict[str, str]:
    """The environment of a child: the whitelist, the bench settings, never a key."""
    environment = {key: os.environ[key] for key in KEEP_ENV if key in os.environ}
    environment.update(CHILD_ENV)
    environment.update(extra or {})
    for banned in BANNED_ENV:
        environment.pop(banned, None)
    return environment


def claude_binary() -> str:
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("The claude command is not installed.")
    return found


def _limited(text: str) -> bool:
    folded = text.casefold()
    return any(word in folded for word in LIMIT_WORDS)


# -- one bot turn: a headless session and its held tool calls ----------------------------


class Step:
    """One model message of a session, as the stream builds it."""

    def __init__(self, identifier: str, model: str, usage: dict | None) -> None:
        self.id, self.model = identifier, model
        self.usage = dict(usage or {})
        self.blocks: dict[int, dict] = {}
        self.stop: str | None = None
        self.done = False

    def content(self) -> list[Block]:
        """The text and tool-use blocks, in order, with the MCP prefix removed."""
        result: list[Block] = []
        for _, block in sorted(self.blocks.items()):
            if block["type"] == "text" and block.get("text", "").strip():
                result.append(Block(type="text", text=block["text"]))
            elif block["type"] == "tool_use":
                raw = block.get("partial") or ""
                try:
                    arguments = json.loads(raw) if raw else block.get("input") or {}
                except ValueError:
                    arguments = block.get("input") or {}
                name = str(block.get("name") or "")
                result.append(Block(type="tool_use", id=block.get("id"),
                                    name=name[len(PREFIX):] if name.startswith(PREFIX)
                                    else name, input=arguments))
        return result

    def tool_ids(self) -> list[str]:
        return [str(block.get("id")) for block in self.blocks.values()
                if block["type"] == "tool_use"]

    def has_finish(self) -> bool:
        return any(block.get("name") == "finish" for block in self.content()
                   if block.get("type") == "tool_use")


class Slot:
    """A tool call the session made, held until the engine sends its result."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.event = threading.Event()
        self.result: dict | None = None

    def answer(self, text: str, error: bool = False) -> None:
        if not self.event.is_set():
            self.result = {"content": [{"type": "text", "text": text}], "isError": bool(error)}
            self.event.set()


class Session:
    """A headless Claude Code session that plays the model for one assistant turn."""

    def __init__(self, client: SubscriptionClient, key: str, arguments: dict) -> None:
        self.client, self.key = client, key
        self.model = str(arguments.get("model") or "claude-sonnet-5-5")
        self.tools = [self._mcp_tool(tool) for tool in arguments.get("tools") or []
                      if isinstance(tool, dict) and tool.get("name")]
        self.lock = threading.Condition()
        self.steps: list[Step] = []
        self.returned = 0
        self.slots: dict[str, Slot] = {}
        self.early: dict[str, tuple[str, bool]] = {}  # results that came before their call
        self.answered_by_cli: set[str] = set()
        self.ended = False
        self.error = ""
        self.limited = False
        self.last_activity = time.monotonic()
        self.finish_pending = False
        self.init: dict = {}
        # The opening message of the turn: a later request of the same turn still holds
        # this very object, a new turn never does (list ids can be reused).
        opening = arguments.get("messages") or []
        self.anchor = opening[-1] if opening else None
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self.events_tail: deque[str] = deque(maxlen=20)
        self.process = self._start(arguments)
        self.reader = threading.Thread(target=self._read, name=f"bench-{key}", daemon=True)
        self.reader.start()
        threading.Thread(target=self._drain, name=f"bench-err-{key}", daemon=True).start()

    def _drain(self) -> None:
        """Read the child's error stream all the time, so it never blocks on a full pipe."""
        with contextlib.suppress(OSError, ValueError):
            for line in self.process.stderr:
                self.stderr_tail.append(line.rstrip()[:400])

    def explain(self) -> str:
        """What the session last said, for a failure report (no secrets: the child has no
        keys, and its prompt is the engine's own)."""
        return (f"error={self.error!r} events={list(self.events_tail)} "
                f"stderr={' | '.join(list(self.stderr_tail)[-6:])}")

    @staticmethod
    def _mcp_tool(tool: dict) -> dict:
        return {"name": tool["name"], "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("input_schema") or {"type": "object"},
                "annotations": {"readOnlyHint": True},
                "_meta": {"anthropic/alwaysLoad": True}}

    def _start(self, arguments: dict) -> subprocess.Popen:
        prompt_file = self.client.prompt_file(_system_text(arguments.get("system")) + TOOL_NOTE)
        names = " ".join(PREFIX + tool["name"] for tool in self.tools)
        effort = str(((arguments.get("extra_body") or {}).get("output_config") or {})
                     .get("effort") or self.client.effort)
        config = json.dumps({"mcpServers": {SERVER: {
            "type": "http", "url": f"{self.client.endpoint_url}/mcp/{self.key}",
            "timeout": int(STEP_SECONDS * 1000) * 3}}})
        command = [self.client.binary, "-p", "--verbose", "--input-format", "stream-json",
                   "--output-format", "stream-json", "--include-partial-messages",
                   "--model", self.model, "--effort", effort,
                   "--system-prompt-file", str(prompt_file), "--restricted", "--tools", "",
                   "--disallowedTools", "EndConversation", "--mcp-config", config,
                   "--strict-mcp-config", "--allowedTools", names,
                   "--permission-mode", "dontAsk", "--no-session-persistence",
                   "--max-turns", "12"]
        max_tokens = int(arguments.get("max_tokens") or 0)
        extra = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens)} if max_tokens > 0 else {}
        process = _start_child(lambda binary: subprocess.Popen(  # nosec B603 - no shell
            [binary, *command[1:]], cwd=self.client.child_cwd, env=child_environment(extra),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", start_new_session=True), self.client)
        self._send_user(process, self._first_message(arguments.get("messages") or []))
        return process

    @staticmethod
    def _first_message(messages: list) -> str:
        """The turn's opening as one text: the earlier turns as plain text, then the JSON of
        this turn (the engine sends the earlier turns as separate plain-text messages)."""
        if not messages:
            return ""
        earlier = []
        for message in messages[:-1]:
            role = message.get("role") if isinstance(message, dict) else ""
            said = _text_of(message.get("content") if isinstance(message, dict) else "")
            if said:
                earlier.append(("Customer: " if role == "user" else "You replied: ") + said)
        last = messages[-1]
        now = _text_of(last.get("content") if isinstance(last, dict) else last)
        if not earlier:
            return now
        return ("Earlier turns of this conversation:\n" + "\n".join(earlier) + "\n\n" + now)

    @staticmethod
    def _send_user(process: subprocess.Popen, text: str) -> None:
        line = json.dumps({"type": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": text or "(empty)"}]}},
            ensure_ascii=False)
        try:
            process.stdin.write(line + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    # -- the child's output -------------------------------------------------------------

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    self.events_tail.append("text:" + line.strip()[:120])
                    continue
                self.events_tail.append(str(event.get("type")) + ":" + str(
                    event.get("subtype") or (event.get("event") or {}).get("type") or ""))
                self._event(event)
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self.ended = True
                for slot in self.slots.values():
                    slot.answer("The session ended.", True)
                self.lock.notify_all()

    def _event(self, event: dict) -> None:
        kind = event.get("type")
        with self.lock:
            self.last_activity = time.monotonic()
            if kind == "system" and event.get("subtype") == "init":
                self.init = event
                source = str(event.get("apiKeySource") or "none").casefold()
                if source not in ("none", "", "null"):
                    self.error = f"an API key reached the session ({source})"
                    self._kill_locked()
            elif kind == "stream_event":
                self._stream(event.get("event") or {})
            elif kind == "user":
                # A tool result Claude Code made itself (it refused the call): not ours.
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        identifier = str(block.get("tool_use_id") or "")
                        if identifier and identifier not in self.slots:
                            self.answered_by_cli.add(identifier)
            elif kind == "rate_limit_event":
                # `allowed_warning` (a share of the window is used up) still serves the
                # request: only a refusal is a limit.
                info = event.get("rate_limit_info") or event
                status = str(info.get("status") or "").casefold()
                if status and status != "ok" and not status.startswith("allowed"):
                    self.limited = True
                elif status == "allowed_warning":
                    LOGGER.warning("subscription usage warning: %s at %.0f%% of its window",
                                   info.get("rateLimitType") or "limit",
                                   100 * float(info.get("utilization") or 0))
            elif kind == "result":
                text = " ".join(str(event.get(key) or "") for key in ("result", "subtype"))
                if event.get("is_error"):
                    self.error = text[:300] or "error"
                    if _limited(text):
                        self.limited = True
                if self.steps and not self.steps[-1].done:
                    self.steps[-1].done = True
            self.lock.notify_all()

    def _stream(self, inner: dict) -> None:
        name = inner.get("type")
        if name == "message_start":
            message = inner.get("message") or {}
            self.steps.append(Step(str(message.get("id") or ""),
                                   str(message.get("model") or self.model),
                                   message.get("usage")))
            return
        if not self.steps:
            return
        step = self.steps[-1]
        index = int(inner.get("index") or 0)
        if name == "content_block_start":
            block = dict(inner.get("content_block") or {})
            block.setdefault("text", "")
            step.blocks[index] = block
        elif name == "content_block_delta":
            delta = inner.get("delta") or {}
            block = step.blocks.setdefault(index, {"type": "text", "text": ""})
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + str(delta.get("text") or "")
            elif delta.get("type") == "input_json_delta":
                block["partial"] = block.get("partial", "") + str(delta.get("partial_json")
                                                                   or "")
        elif name == "message_delta":
            step.stop = (inner.get("delta") or {}).get("stop_reason") or step.stop
            for key, value in (inner.get("usage") or {}).items():
                if isinstance(value, int):
                    step.usage[key] = value
        elif name == "message_stop":
            step.done = True

    # -- the engine's side --------------------------------------------------------------

    def next_step(self, timeout: float) -> Step:
        """The next finished model message. Raises TimeoutError, or SubscriptionLimited."""
        deadline = time.monotonic() + max(1.0, timeout)
        with self.lock:
            while True:
                fresh = self.steps[self.returned:]
                if fresh and fresh[0].done:
                    self.returned += 1
                    self.last_activity = time.monotonic()
                    step = fresh[0]
                    self.finish_pending = step.has_finish()
                    return step
                if self.limited:
                    raise SubscriptionLimited(self.error or "the subscription is limited")
                if self.ended or self.error:
                    raise RuntimeError(f"bench session ended: {self.explain()}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("The bench session gave no step in time.")
                self.lock.wait(min(left, 1.0))

    def deliver(self, messages: list) -> None:
        """Hand the engine's latest message to the session: tool results release the held
        calls (a reminder text rides on the last one); a plain text is a new user message."""
        last = messages[-1] if messages else {}
        content = last.get("content") if isinstance(last, dict) else None
        blocks = content if isinstance(content, list) else []
        results = [block for block in blocks if isinstance(block, dict)
                   and block.get("type") == "tool_result"]
        reminder = "\n".join(str(block.get("text") or "") for block in blocks
                             if isinstance(block, dict) and block.get("type") == "text")
        with self.lock:
            self.last_activity = time.monotonic()
            self.finish_pending = False
            if not results:
                text = reminder or _text_of(content)
                self._send_user(self.process, text)
                return
            for number, block in enumerate(results):
                identifier = str(block.get("tool_use_id") or "")
                text = _text_of(block.get("content")) if not isinstance(
                    block.get("content"), str) else block.get("content")
                if reminder and number == len(results) - 1:
                    text = f"{text}\n\n{reminder}"
                error = bool(block.get("is_error"))
                slot = self.slots.get(identifier)
                if slot is not None:
                    slot.answer(text, error)
                elif identifier not in self.answered_by_cli:
                    self.early[identifier] = (text, error)
            # A call of the previous step that got no result would wait forever.
            for identifier, slot in self.slots.items():
                if not slot.event.is_set() and identifier not in {
                        str(block.get("tool_use_id")) for block in results}:
                    slot.answer("Not run.", True)

    def hold(self, identifier: str, name: str) -> dict:
        """Called by the MCP endpoint for each tool call: wait for the engine's result."""
        with self.lock:
            if not identifier:  # no id from the client: the first unanswered call by name
                short = name[len(PREFIX):] if name.startswith(PREFIX) else name
                for step in reversed(self.steps):
                    for block in step.content():
                        if block.get("type") == "tool_use" and block.get("name") == short \
                                and str(block.get("id")) not in self.slots:
                            identifier = str(block.get("id"))
                            break
                    if identifier:
                        break
            slot = self.slots.get(identifier)
            if slot is None:
                slot = self.slots[identifier] = Slot(name)
                if identifier in self.early:
                    slot.answer(*self.early.pop(identifier))
            if self.ended:
                slot.answer("The session ended.", True)
        slot.event.wait(STEP_SECONDS * 3)
        return slot.result or {"content": [{"type": "text", "text": "The turn ended."}],
                               "isError": True}

    def owns(self, messages: Any) -> bool:
        """Whether a request belongs to this session's turn."""
        return self.anchor is not None and isinstance(messages, list) \
            and any(message is self.anchor for message in messages)

    def close(self) -> None:
        with self.lock:
            self._kill_locked()

    def _kill_locked(self) -> None:
        self.ended = True
        for slot in self.slots.values():
            slot.answer("The turn ended.", True)
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(process.pid, signal.SIGTERM)
            threading.Thread(target=_reap, args=(process,), daemon=True).start()
        self.lock.notify_all()


def _start_child(start: Callable[[str], Any], client: Any, wait: float = 90.0) -> Any:
    """Start a child; while Claude Code updates itself its binary is briefly missing or half
    written (no such file, exec format error, text file busy), so that is waited for (and
    the binary looked up again) instead of failing the turn."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return start(client.binary)
        except OSError as exc:
            updating = isinstance(exc, FileNotFoundError) or exc.errno in (
                errno.ENOEXEC, errno.ETXTBSY)
            if not updating or time.monotonic() >= deadline:
                raise
            time.sleep(2.0)
            found = shutil.which("claude")
            if found:
                client.binary = found


def _reap(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process.pid, signal.SIGKILL)
    for stream in (process.stdin, process.stdout, process.stderr):
        with contextlib.suppress(Exception):
            stream.close()


# -- the MCP endpoint ---------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    client: SubscriptionClient  # set on the server class

    def log_message(self, *_: Any) -> None:  # the bench logs what matters itself
        return

    def _reply(self, status: int, body: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - no server-sent stream is offered
        self._reply(405)

    def do_DELETE(self) -> None:  # noqa: N802
        self._reply(200, {})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            message = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._reply(400, {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "parse error"}})
            return
        key = self.path.rstrip("/").rsplit("/", 1)[-1]
        session = self.server.client.session(key)  # type: ignore[attr-defined]
        method, identifier = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if identifier is None:  # a notification
            if method == "notifications/cancelled" and session is not None:
                request = str((params or {}).get("requestId") or "")
                LOGGER.info("bench_mcp_cancelled key=%s request=%s", key, request)
            self._reply(202)
            return
        if session is None:
            self._reply(200, {"jsonrpc": "2.0", "id": identifier,
                              "error": {"code": -32001, "message": "no such session"}})
            return
        if method == "initialize":
            result: dict = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "assistant-bench", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": session.tools}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            meta = params.get("_meta") or {}
            use_id = str(meta.get("claudecode/toolUseId") or "")
            result = session.hold(use_id, str(params.get("name") or ""))
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        else:
            self._reply(200, {"jsonrpc": "2.0", "id": identifier,
                              "error": {"code": -32601, "message": "unknown method"}})
            return
        self._reply(200, {"jsonrpc": "2.0", "id": identifier, "result": result})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A session killed at the end of its turn drops its connection: not an error."""
        import sys

        if isinstance(sys.exc_info()[1], ConnectionError | BrokenPipeError | TimeoutError):
            return
        super().handle_error(request, client_address)


# -- the client ---------------------------------------------------------------------------


class SubscriptionClient:
    """The Messages API subset the engine and the test runners use, served by headless
    Claude Code sessions on the subscription. Safe to call from several threads."""

    def __init__(self, *, work_dir: Path | str = WORK_DIR, effort: str = "low",
                 binary: str | None = None, port: int = 0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.work_dir = Path(work_dir).resolve()
        (self.work_dir / "prompts").mkdir(parents=True, exist_ok=True)
        self.child_cwd = self.work_dir / "cwd"
        self.child_cwd.mkdir(parents=True, exist_ok=True)
        self.effort = effort
        self.binary = binary or claude_binary()
        self.clock = clock
        self.messages = self
        self._sessions: dict[str, Session] = {}
        self._by_list: dict[int, str] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self.limited_at: float | None = None
        self.plain_requests = 0
        self.session_count = 0
        server = _Server(("127.0.0.1", port), _Handler)
        server.client = self  # type: ignore[attr-defined]
        self._server = server
        self.endpoint_url = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, name="bench-mcp", daemon=True).start()
        self._closed = threading.Event()
        threading.Thread(target=self._reaper, name="bench-reaper", daemon=True).start()

    # The engine and the runners call these.

    def with_options(self, **_: Any) -> SubscriptionClient:
        return self

    def create(self, **arguments: Any) -> Any:
        limit = arguments.get("max_tokens")
        if isinstance(limit, int) and limit <= 0:  # a warm-up: nothing to warm here
            return _response([], "end_turn", None, str(arguments.get("model") or ""))
        timeout = arguments.pop("timeout", None)
        budget = float(timeout) if isinstance(timeout, int | float) else STEP_SECONDS
        if not arguments.get("tools"):
            return self._plain(arguments, budget)
        return self._step(arguments, budget)

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._by_list.clear()
        for session in sessions:
            session.close()
        with contextlib.suppress(Exception):
            self._server.shutdown()
            self._server.server_close()

    # Internals.

    def session(self, key: str) -> Session | None:
        with self._lock:
            return self._sessions.get(key)

    def prompt_file(self, text: str) -> Path:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        path = self.work_dir / "prompts" / f"system-{digest}.txt"
        if not path.exists():
            # Unique per writer: two threads writing the same prompt must not share a file.
            temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        return path

    def _step(self, arguments: dict, budget: float) -> Any:
        if any(isinstance(tool, dict) and tool.get("type") for tool in arguments.get("tools")
               or []):
            # A server tool (web search) is the provider's to run: never served here, and
            # never handed to a paid client either. Bench research uses its own backend.
            raise RuntimeError("The bench does not serve server tools.")
        messages = arguments.get("messages")
        list_id = id(messages)
        with self._lock:
            key = self._by_list.get(list_id)
            session = self._sessions.get(key) if key else None
        if session is not None and not session.owns(messages):
            session = None  # a reused list id: this is a new turn
        elif session is not None and (session.ended or session.error):
            self._forget(session)
            raise RuntimeError("The bench session of this turn was lost: "
                               f"{session.error or 'it ended'}.")
        if session is None:
            with self._lock:
                self._counter += 1
                key = f"t{self._counter}-{os.getpid()}"
            session = Session(self, key, dict(arguments))
            with self._lock:
                self._sessions[key] = session
                self._by_list[list_id] = key
                self.session_count += 1
        else:
            session.deliver(messages or [])
        try:
            step = session.next_step(min(budget, STEP_SECONDS))
        except SubscriptionLimited:
            self.limited_at = self.clock()
            self._forget(session)
            raise
        except (TimeoutError, RuntimeError):
            self._forget(session)
            raise
        content = step.content()
        stop = "tool_use" if any(block["type"] == "tool_use" for block in content) \
            else (step.stop or "end_turn")
        return _response(content, stop, step.usage, step.model)

    def _forget(self, session: Session) -> None:
        session.close()
        with self._lock:
            self._sessions.pop(session.key, None)
            for list_id, key in list(self._by_list.items()):
                if key == session.key:
                    self._by_list.pop(list_id, None)

    def _reaper(self) -> None:
        """Close sessions whose turn is over: the engine accepted a finish (no further
        request comes) or nothing has happened for a long time."""
        while not self._closed.wait(1.0):
            now = time.monotonic()
            with self._lock:
                sessions = list(self._sessions.values())
            for session in sessions:
                idle = now - session.last_activity
                if (session.finish_pending and idle > FINISH_GRACE) or idle > IDLE_SECONDS \
                        or session.ended:
                    self._forget(session)

    def _plain(self, arguments: dict, budget: float) -> Any:
        """One request without tools, answered by one `claude -p` run."""
        model = str(arguments.get("model") or "claude-haiku-5-5")
        config = (arguments.get("extra_body") or {}).get("output_config") or {}
        effort = str(config.get("effort") or self.effort)
        schema = (config.get("format") or {}).get("schema") \
            if (config.get("format") or {}).get("type") == "json_schema" else None
        prompt_file = self.prompt_file(_system_text(arguments.get("system") or ""))
        messages = arguments.get("messages") or []
        question = _text_of(messages[-1].get("content")) if messages else ""
        command = [self.binary, "-p", "--output-format", "json", "--model", model,
                   "--effort", effort, "--system-prompt-file", str(prompt_file),
                   "--restricted", "--tools", "", "--disallowedTools", "EndConversation",
                   "--strict-mcp-config", "--permission-mode", "dontAsk",
                   "--no-session-persistence", "--max-turns", "3"]
        if schema:
            command += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        max_tokens = int(arguments.get("max_tokens") or 0)
        extra = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens)} if max_tokens > 0 else {}
        self.plain_requests += 1
        try:
            done = _start_child(lambda binary: subprocess.run(  # nosec B603 - no shell
                [binary, *command[1:]], cwd=self.child_cwd, env=child_environment(extra),
                input=question, capture_output=True, text=True, encoding="utf-8",
                timeout=min(max(budget, 30.0), PLAIN_SECONDS), check=False), self)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("The bench request gave no answer in time.") from exc
        try:
            result = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() \
                else {}
        except (ValueError, IndexError):
            result = {}
        text_out = str(result.get("result") or "")
        if result.get("is_error") or done.returncode != 0:
            said = text_out + " " + (done.stderr or "")[-300:]
            if _limited(said):
                self.limited_at = self.clock()
                raise SubscriptionLimited(said.strip()[:300])
            raise RuntimeError(f"bench request failed: {said.strip()[:200]}")
        structured = result.get("structured_output")
        text_out = json.dumps(structured, ensure_ascii=False) if structured is not None \
            else text_out
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else None
        return _response([Block(type="text", text=text_out)], "end_turn", usage, model)
