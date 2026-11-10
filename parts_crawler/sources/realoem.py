"""RealOEM BMW/MINI catalog. Cloudflare challenges automated clients, so pages normally
come from an operator-recorded HAR file (`import-har`); a challenge blocks the source."""

from __future__ import annotations

import re

from parts_crawler.sources.generic import EpcAdapter


class RealOemAdapter(EpcAdapter):
    name = "realoem"
    hosts = frozenset({"www.realoem.com"})
    parser_version = "1"
    manual_capture = True
    start_urls = ("https://www.realoem.com/bmw/enUS/select",)
    follow_prefixes = ("/bmw/enUS/",)
    exclude = re.compile(r"(login|register|cart|privacy|\.(jpe?g|png|gif|svg|pdf)$)", re.I)
    fixed_make = "BMW"
    note = "BMW/MINI EPC; Cloudflare-protected, fill with import-har."
