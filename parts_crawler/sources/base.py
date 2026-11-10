"""Adapter contract: seeds, optional URL classification for HAR import, and parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

from bs4 import BeautifulSoup

from parts_crawler.http import Page
from parts_crawler.records import Link, ParseResult, normalize_brand

# Makes common in Georgia are crawled first; everything else follows.
POPULAR_MAKES = (
    "TOYOTA", "MERCEDESBENZ", "MERCEDES", "BMW", "HYUNDAI", "LEXUS", "VOLKSWAGEN", "VW",
    "HONDA", "NISSAN", "KIA", "FORD", "SUBARU", "OPEL", "AUDI", "MITSUBISHI", "CHEVROLET",
    "MAZDA", "SKODA", "PORSCHE", "LANDROVER", "JEEP",
)


def make_priority(make: str) -> int:
    key = normalize_brand(make)
    for index, popular in enumerate(POPULAR_MAKES):
        if key == popular or key.startswith(popular):
            return index
    return 50


@dataclass
class CrawledPage:
    url: str
    kind: str
    context: dict
    status: int
    body: bytes
    content_type: str = ""
    _soup: BeautifulSoup | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        return Page(self.url, self.status, self.body, self.content_type).text

    def soup(self) -> BeautifulSoup:
        if self._soup is None:
            self._soup = BeautifulSoup(self.text, "html.parser")
        return self._soup

    def json(self) -> Any:
        """The body as JSON; raises ValueError when the response is not JSON."""
        return json.loads(self.text)


class SourceAdapter:
    name: ClassVar[str] = ""
    hosts: ClassVar[frozenset[str]] = frozenset()
    parser_version: ClassVar[str] = "1"
    daily_budget: ClassVar[int] = 15000
    min_delay: ClassVar[float | None] = None
    # Sources behind bot protection are filled from operator-recorded HAR files.
    manual_capture: ClassVar[bool] = False
    # The site's robots.txt disallows this source's URLs. It runs only when the operator
    # lists it in PARTS_CRAWLER_ROBOTS_EXEMPT; otherwise it does not start. The exemption
    # covers `robots_exempt_paths` only; every other robots rule still applies.
    needs_robots_exemption: ClassVar[bool] = False
    robots_exempt_paths: ClassVar[tuple[str, ...]] = ()
    # Plain site preferences (for example a language choice) sent with every request.
    cookies: ClassVar[dict[str, str] | None] = None
    request_headers: ClassVar[dict[str, str] | None] = None
    # The site's normal pages embed a captcha widget (a contact form, say), so only a
    # challenge header or title counts as bot protection, not the widget's markup.
    embeds_captcha_widget: ClassVar[bool] = False
    note: ClassVar[str] = ""

    def seeds(self) -> list[Link]:
        raise NotImplementedError

    def code_seeds(self, codes: list[tuple[str, str]]) -> list[Link]:
        """Search pages for warehouse codes; sources without a code search return []."""
        return []

    # Queue priorities below this value are part requests for seeded vehicles. The worker
    # then alternates between them and the rest of the queue, so finding new vehicles
    # and fetching parts both keep moving. None: plain priority order.
    fair_split: ClassVar[int | None] = None
    # Group-name and part-name filters used when vehicles are seeded automatically.
    auto_groups: ClassVar[str] = ""
    auto_part_names: ClassVar[str] = ""

    def vehicle_seeds(self, vehicles: list[dict], pattern: str | None,
                      parts: str | None = None) -> list[Link]:
        """Deep-crawl links for stored vehicles (`seed-vehicles`); most sources have none."""
        return []

    def family(self, vehicle: dict) -> str | None:
        """Vehicles of one family share their part lists closely enough that a single
        representative is crawled for parts. None: this vehicle cannot be seeded."""
        return None

    def classify(self, url: str) -> str | None:
        """The queue kind for a URL seen in a HAR file, or None to ignore it."""
        return None

    def parse(self, page: CrawledPage) -> ParseResult:
        raise NotImplementedError
