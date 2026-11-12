"""The services the assistant's newer read tools use, built once per application.

``create_app`` attaches the result to the chat service as ``assistant_extras``; the
assistant turn hands it to its ``Toolbox``. Each entry is optional: a tool whose service is
missing tells the model it cannot answer here, and the turn goes on without it.

- ``car_numbers``: the crawled manufacturer catalogues (MANN-FILTER, NGK, ...) asked which
  part numbers they list for a car (``assistant_sources.CarNumbers``).
- ``research``: a web search sub-request for numbers, a check of numbers, or a fact about a
  car (``assistant_research.ResearchService``), with the chats it is allowed in.
- ``site_content``: the shop's own facts for ``shop_info``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
RESEARCH_MODES = ("off", "staff", "all")


def research_audiences(mode: str) -> tuple[str, ...]:
    """The chats research may run in: ``staff`` by default, the public widget too only once
    the owner has checked the provider path (``all``)."""
    return {"all": ("staff", "public"), "staff": ("staff",)}.get(mode, ())


def extras_for(settings: Any, chat_service: Any, repository: Any, site: Any = None) -> dict:
    """The extras of one application. Nothing here sends a request: services start lazily."""
    extras: dict[str, Any] = {}
    content = getattr(site, "content", None)
    if content is not None:
        extras["site_content"] = content
    for setting, key in (("assistant_vehicle_readings_path", "vehicle_readings"),
                         ("assistant_name_readings_path", "name_readings")):
        path = str(getattr(settings, setting, "") or "")
        if path:
            from automotive.assistant_vehicles import load

            readings = load(path)
            if readings:
                extras[key] = readings
    if repository is not None:
        try:
            from automotive.assistant_sources import CarNumbers

            extras["car_numbers"] = CarNumbers(repository)
        except Exception as exc:  # The crawled catalogues are optional.
            LOGGER.info("assistant_car_numbers_unavailable %s", type(exc).__name__)
    mode = str(getattr(settings, "assistant_research", "staff") or "off")
    audiences = research_audiences(mode)
    if audiences:
        try:
            extras["research"] = research_service(settings, chat_service)
            extras["research_audiences"] = audiences
            extras["research_seconds"] = float(getattr(settings, "assistant_research_seconds",
                                                       8.0) or 8.0)
        except Exception as exc:
            LOGGER.info("assistant_research_unavailable %s", type(exc).__name__)
    return extras


def research_service(settings: Any, chat_service: Any, backend: Any = None) -> Any:
    """Web research through the chat's own client (the billing fallback included), with
    OpenRouter's own search shape as the fallback when the provider refuses Anthropic's."""
    from automotive.assistant_research import ApiBackend, OpenRouterBackend, ResearchService

    model = str(getattr(settings, "assistant_research_model", "") or "claude-haiku-5-5")
    if backend is None:
        client = chat_service.assistant_client() if hasattr(chat_service, "assistant_client") \
            else chat_service._client()
        backend = ApiBackend(client, model=model)
    fallback = None
    key = str(getattr(settings, "openrouter_api_key", "") or "")
    if key and isinstance(backend, ApiBackend):
        def factory() -> Any:
            from automotive.model_client import openrouter_client

            return openrouter_client(key)

        fallback = OpenRouterBackend(factory, model=_openrouter_model(model))
    path = str(getattr(settings, "assistant_research_cache_path", "") or "")
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return ResearchService(
        backend, cache_path=path or None,
        wait_seconds=float(getattr(settings, "assistant_research_seconds", 8.0) or 8.0),
        fallback=fallback)


def bench_research(settings: Any, cache_path: str | os.PathLike | None = None) -> Any:
    """Research for the subscription bench: the same service, its searches run by headless
    Claude Code with only WebSearch and WebFetch, so nothing is billed."""
    from automotive.assistant_research import BenchBackend, ResearchService

    model = str(getattr(settings, "assistant_research_model", "") or "claude-haiku-5-5")
    return ResearchService(
        BenchBackend(model=model), cache_path=str(cache_path) if cache_path else None,
        wait_seconds=float(getattr(settings, "assistant_research_seconds", 8.0) or 8.0))


def check_research(service: Any, out: Any, wait: float = 60.0) -> int:
    """Ask ``service`` one fixed public question (no customer data) and print what came
    back: status, seconds, searches and the first numbers with their sources' sites.
    Returns 0 when the provider path answered (numbers, or an honest none), else 1."""
    from urllib.parse import urlparse

    result = service.ask(dict(CHECK_QUESTION), wait=wait)
    status = str(result.get("status") or "")
    findings = [item for item in result.get("findings") or [] if isinstance(item, dict)]
    print(f"status={status} seconds={result.get('seconds', '-')} "
          f"searches={result.get('searches', 0)} findings={len(findings)}", file=out)
    for item in findings[:5]:
        sites = sorted({urlparse(str(url)).hostname or "" for url in
                        item.get("source_urls") or []} - {""})
        print(f"  {item.get('brand') or '-'} {item.get('number') or '-'} "
              f"({item.get('kind') or '-'}) from {', '.join(sites[:3]) or '-'}", file=out)
    if status in ("answered", "no_findings"):
        print("The provider path answers: AUTOMOTIVE_RESEARCH=all also turns research on "
              "in the public widget.", file=out)
        return 0
    print(f"Research did not answer ({status}): keep AUTOMOTIVE_RESEARCH at staff or off; "
          "the assistant_research log line above names the failure.", file=out)
    return 1


# A public question with no customer data: a common car and part with listed numbers.
CHECK_QUESTION = {"kind": "numbers", "make": "Toyota", "model": "Camry", "year": 2015,
                  "trim": "2.5", "engine_code": "2AR-FE", "body": "sedan",
                  "part": "front brake pads", "position": "front", "numbers": [],
                  "question": ""}


def _openrouter_model(model: str) -> str:
    """OpenRouter's name of the same model; an unknown model keeps its own name."""
    known = {"claude-haiku-5-5": "anthropic/claude-haiku-5.5",
             "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5"}
    return known.get(model, model)
