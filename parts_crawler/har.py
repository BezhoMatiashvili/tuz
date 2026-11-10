"""Import pages from a HAR file the operator recorded while browsing a site normally.

Only response bodies of successful catalog pages are kept; request headers, cookies and
tokens in the HAR are never stored. Delete the HAR file after importing it.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from pathlib import Path

from rich.progress import Progress

from parts_crawler.config import Settings
from parts_crawler.http import is_challenge
from parts_crawler.rawstore import RawStore
from parts_crawler.sources.base import CrawledPage, SourceAdapter
from parts_crawler.sources.generic import absolute
from parts_crawler.store import Store

log = logging.getLogger("parts_crawler.har")


def har_pages(path: Path, adapter: SourceAdapter):
    with path.open(encoding="utf-8") as handle:
        entries = json.load(handle).get("log", {}).get("entries", [])
    for entry in entries:
        request, response = entry.get("request", {}), entry.get("response", {})
        content = response.get("content", {})
        url = absolute(request.get("url", ""), request.get("url", ""), adapter.hosts)
        if not url or request.get("method") not in {"GET", "POST"}:
            continue
        if response.get("status") != 200 or not content.get("text"):
            continue
        kind = adapter.classify(url)
        if not kind:
            continue
        if content.get("encoding") == "base64":
            try:
                body = base64.b64decode(content["text"])
            except (binascii.Error, ValueError):
                continue
        else:
            body = content["text"].encode("utf-8")
        yield url, kind, body, str(content.get("mimeType", ""))


def import_har(settings: Settings, adapter: SourceAdapter, path: Path) -> dict:
    pages = list(har_pages(path, adapter))
    store = Store(settings.database_url)
    raw = RawStore(settings.pages_dir)
    totals = {"pages": 0, "skipped": 0, "parse_errors": 0, "parts": 0, "vehicles": 0,
              "fitments": 0, "xrefs": 0, "links": 0}
    try:
        store.ensure_source(adapter.name, adapter.daily_budget)
        with Progress() as progress:
            task = progress.add_task(f"{adapter.name}: HAR pages", total=len(pages))
            for url, kind, body, content_type in pages:
                progress.advance(task)
                if is_challenge(200, {}, body):
                    totals["skipped"] += 1
                    continue
                item = store.upsert_done_item(adapter.name, url, kind, {})
                sha, storage = raw.save(adapter.name, body)
                page = CrawledPage(url, kind, {}, 200, body, content_type)
                try:
                    result, problem = adapter.parse(page), None
                except Exception as error:
                    log.exception("HAR parse failed for %s", url)
                    result, problem = None, f"PARSE_ERROR:{type(error).__name__}"
                    totals["parse_errors"] += 1
                counts = store.save_page(
                    item, url=url, http_status=200, content_type=content_type, sha=sha,
                    storage_path=storage, size=len(body),
                    parser_version=adapter.parser_version if result is not None else None,
                    result=result, parse_error=problem)
                totals["pages"] += 1
                for key, value in counts.items():
                    totals[key] = totals.get(key, 0) + value
    finally:
        store.close()
    return totals
