"""Use the same Claude model through OpenRouter when the direct account cannot pay.

Both clients speak the Anthropic Messages API, so conversation blocks, tools, cache
controls and inference settings do not need translation. Only the model ID and routing
metadata change. Never switch providers after a streamed response has started.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from threading import Lock
from typing import Any

import anthropic
import httpx

LOGGER = logging.getLogger(__name__)
# After the direct account refuses for money, requests go straight to the backup and the
# direct account is asked again once the wait is over. Each further refusal doubles the
# wait up to the cap: an account left unfunded costs one refused request an hour (about a
# third of a second of one customer's turn, measured 2026-11-19), and a top-up is in use
# within RECHECK_MAX_SECONDS, without a restart.
RECHECK_SECONDS = 60.0
RECHECK_MAX_SECONDS = 3600.0
# OpenRouter sends nothing, headers included, until the model's first event, and that
# wait varies: 1.3 s to 7.3 s with the chat's cached prompt (measured 2026-11-11). A turn's
# 10 s stall limit, right for the direct API, would end the longer waits, so a backup
# request may stay silent this long, within what is left of the request's own time;
# twice the longest wait seen, and short enough that a 30 s turn can still ask again.
BACKUP_READ_SECONDS = 15.0
# Explicit equivalents: an unknown release must never silently become another model.
OPENROUTER_MODELS = {
    "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    "claude-haiku-5-5": "anthropic/claude-haiku-5.5",
}
BILLING_TYPES = ("billing_error", "insufficient_credits", "insufficient_quota")
BILLING_PHRASES = (
    "credit balance is too low", "insufficient credit", "insufficient balance",
    "insufficient funds", "credit balance is exhausted", "out of credits",
    # A spend limit set on the organization or on a workspace: a 400 until it is raised.
    "reached your specified api usage limits",
    "reached your specified workspace api usage limits",
)
# The usage tier's monthly spend cap is a 429 like a rate limit, told apart by this code.
SPEND_CAP = "enforced_spend_limit_reached"


def openrouter_client(key: str, **options: Any) -> anthropic.Anthropic:
    """A client for OpenRouter's Anthropic Messages endpoint that sends only this key.

    The empty api_key keeps ANTHROPIC_API_KEY from the environment out of its requests,
    the fixed base URL keeps ANTHROPIC_BASE_URL from redirecting them, and the direct
    account's headers (its key, its workspace) are never added. ``options`` are client
    settings such as ``timeout`` and ``max_retries`` (one attempt by default)."""
    return anthropic.Anthropic(
        api_key="", auth_token=key, base_url="https://openrouter.ai/api",
        default_headers={"X-Api-Key": anthropic.omit},
        **{"timeout": 25.0, "max_retries": 0, **options},
    )


def _error(exc: anthropic.APIStatusError) -> dict:
    body = exc.body
    if not isinstance(body, dict):
        return {}
    error = body.get("error", body)
    return error if isinstance(error, dict) else {}


def out_of_credit(exc: anthropic.APIStatusError) -> bool:
    """Recognize the account refusing for money (no credit, a spend limit, the monthly
    spend cap, a billing problem) without treating invalid input, rate limits or outages
    as billing."""
    if exc.status_code == 402:
        return True
    error = _error(exc)
    message = str(error.get("message", "")).casefold()
    if exc.status_code == 429:
        details = error.get("details")
        return (isinstance(details, dict) and details.get("error_code") == SPEND_CAP) \
            or "api usage limits" in message
    if exc.status_code not in (400, 403):
        return False
    return error.get("type") in BILLING_TYPES or any(
        phrase in message for phrase in BILLING_PHRASES)


class _Funding:
    """What the clients of one service know about the direct account: until when it is
    skipped and how long the next wait is. Copies of a client share it."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._until = 0.0
        self._wait = RECHECK_SECONDS
        self._refusing = False

    def skipped(self) -> bool:
        return time.monotonic() < self._until

    def refused(self) -> tuple[float, bool]:
        """The account refused for money: skip it for the wait, the next one twice as long.
        Requests refused together start one wait between them. Returns the seconds until
        it is asked again and whether it had been answering until now."""
        with self._lock:
            now = time.monotonic()
            if now >= self._until:
                self._until = now + self._wait
                self._wait = min(RECHECK_MAX_SECONDS, self._wait * 2)
            first, self._refusing = not self._refusing, True
            return self._until - now, first

    def answered(self) -> bool:
        """The account answered; True when it had been refusing until now."""
        with self._lock:
            restored, self._refusing = self._refusing, False
            self._until, self._wait = 0.0, RECHECK_SECONDS
            return restored


class BillingFallbackClient:
    """The Messages API subset used by chat, with one lazy, separately authenticated backup.

    A shared client remembers an unfunded account and asks it again after a wait that
    grows while it keeps refusing, so replenishing its balance needs no restart.
    """

    def __init__(self, primary: anthropic.Anthropic, openrouter_key: str, *,
                 funding: _Funding | None = None, options: dict | None = None) -> None:
        self._primary = primary
        self._key = openrouter_key
        self._funding = funding or _Funding()
        # The backup's client settings; with_options gives a copy its own.
        self._options = {"timeout": 25.0, "max_retries": 0, **(options or {})}
        self._backup: anthropic.Anthropic | None = None
        self._lock = Lock()
        self.messages = self

    def _backup_client(self) -> anthropic.Anthropic:
        with self._lock:
            if self._backup is None:
                self._backup = openrouter_client(self._key, **self._options)
            return self._backup

    def with_options(self, **options: Any) -> BillingFallbackClient:
        """The SDK's per-use copy (another timeout or retry count) that still fails over;
        the copy shares what is known about the direct account."""
        backup = {key: options[key] for key in ("timeout", "max_retries") if key in options}
        return BillingFallbackClient(
            self._primary.with_options(**options), self._key, funding=self._funding,
            options={**self._options, **backup})

    copy = with_options

    def _backup_arguments(self, arguments: dict, elapsed: float = 0) -> dict:
        request = {**arguments, "model": OPENROUTER_MODELS[arguments["model"]], "extra_body": {
            **(arguments.get("extra_body") or {}),
            # Anthropic's own endpoint only, never another provider: the model, its tools
            # and its settings are the ones the direct API serves. No require_parameters:
            # OpenRouter's own check answers a value the model refuses (tool_choice "any"
            # on Claude Sonnet 5.5) with a 404 that names nothing to drop, where Anthropic's
            # own 400 names the setting and the turn asks again without it, as it does on
            # the direct API.
            "provider": {"only": ["anthropic"], "allow_fallbacks": False},
        }}
        if request.get("max_tokens") == 0:
            # A cache warm-up writes nothing; OpenRouter reads 0 as unset and writes a whole
            # reply (measured 2026-11-11), so it is asked for the one token it cannot skip.
            request["max_tokens"] = 1
        # Retrying an account refusal must not give the same turn a second time budget.
        timeout = arguments.get("timeout")
        if isinstance(timeout, int | float):
            request["timeout"] = max(0.001, timeout - elapsed)
        elif isinstance(timeout, httpx.Timeout):
            parts = timeout.as_dict()
            bounded = [value for value in parts.values() if value is not None]
            if bounded:
                remaining = max(0.001, max(bounded) - elapsed)
                if parts.get("read") is not None:
                    parts["read"] = max(parts["read"], BACKUP_READ_SECONDS)
                request["timeout"] = httpx.Timeout(**{
                    key: min(value, remaining) if value is not None else None
                    for key, value in parts.items()})
        return request

    def _call(self, send: Callable[[Any, dict], Any], arguments: dict) -> Any:
        model = arguments.get("model")
        supported = model in OPENROUTER_MODELS
        if supported and self._funding.skipped():
            return send(self._backup_client(), self._backup_arguments(arguments))
        started = time.monotonic()
        try:
            result = send(self._primary, arguments)
        except anthropic.APIStatusError as exc:
            if not supported or not out_of_credit(exc):
                raise
            wait, first = self._funding.refused()
            # Account errors and request contents may contain secrets: log neither.
            LOGGER.log(logging.WARNING if first else logging.INFO,
                       "chat_billing_fallback provider=openrouter model=%s status=%s "
                       "recheck_in_s=%d", model, exc.status_code, round(wait))
        else:
            if self._funding.answered():
                LOGGER.warning("chat_billing_restored provider=anthropic model=%s", model)
            return result
        # Outside the handler: a backup failure propagates once, without a provider loop.
        return send(self._backup_client(), self._backup_arguments(
            arguments, elapsed=time.monotonic() - started))

    def create(self, **arguments: Any) -> Any:
        return self._call(lambda client, args: client.messages.create(**args), arguments)

    @contextmanager
    def stream(self, **arguments: Any) -> Iterator[Any]:
        with ExitStack() as stack:
            events = self._call(
                lambda client, args: stack.enter_context(client.messages.stream(**args)),
                arguments,
            )
            # Exceptions while reading an opened stream never retry the conversation.
            yield events

    def close(self) -> None:
        try:
            self._primary.close()
        finally:
            if self._backup is not None:
                self._backup.close()


def model_client(settings: Any) -> Any:
    """Build the direct client; configuring a backup key enables billing failover.

    The direct client sends only its API key: a bearer token in the environment is not
    added to its requests."""
    headers = {"Authorization": anthropic.omit}
    if settings.anthropic_workspace_id:
        headers["anthropic-workspace-id"] = settings.anthropic_workspace_id
    primary = anthropic.Anthropic(
        api_key=settings.anthropic_api_key, default_headers=headers,
        timeout=25.0, max_retries=0,
    )
    key = getattr(settings, "openrouter_api_key", "")
    return BillingFallbackClient(primary, key) if key else primary
