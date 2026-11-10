"""Partsouq OEM catalogs. Cloudflare challenges automated clients, so pages normally
come from an operator-recorded HAR file (`import-har`); a challenge blocks the source."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from parts_crawler.sources.generic import EpcAdapter


class PartsouqAdapter(EpcAdapter):
    name = "partsouq"
    hosts = frozenset({"partsouq.com"})
    parser_version = "1"
    manual_capture = True
    start_urls = ("https://partsouq.com/catalogs/",)
    follow_prefixes = ("/en/catalog/", "/catalogs/", "/en/search/")
    exclude = re.compile(r"(login|register|cart|checkout|\.(jpe?g|png|gif|svg|pdf)$)", re.I)
    note = "OEM catalogs; Cloudflare-protected, fill with import-har."

    def make_for(self, url: str, trail: list[str]) -> str:
        make = parse_qs(urlsplit(url).query).get("c")
        return make[0].upper() if make else super().make_for(url, trail)
