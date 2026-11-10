"""robots.txt rules with RFC 9309 wildcards, longest-match precedence and Crawl-delay.

urllib.robotparser ignores `*` and `$` inside paths, which would silently allow URLs
such as NGK's `Disallow: /*?id=*`, so the small matcher lives here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if char == "*" else re.escape(char) for char in body)
    return re.compile(regex + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    rules: list[tuple[bool, re.Pattern, int]] = field(default_factory=list)
    crawl_delay: float | None = None

    def allowed(self, path: str) -> bool:
        best: tuple[bool, int] | None = None
        for allow, pattern, length in self.rules:
            if pattern.match(path) and (
                best is None or length > best[1] or (length == best[1] and allow)
            ):
                best = (allow, length)
        return True if best is None else best[0]


ALLOW_ALL = RobotsRules()
DISALLOW_ALL = RobotsRules([(False, _compile("/"), 1)])


def parse_robots(text: str, token: str) -> RobotsRules:
    """Merge every group naming our token; otherwise every `*` group."""
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    lines: list[tuple[str, str]] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                groups.append((agents, lines))
                agents, lines, in_rules = [], [], False
            agents.append(value.lower())
        elif key in {"allow", "disallow", "crawl-delay"}:
            if agents:
                in_rules = True
                lines.append((key, value))
    if agents:
        groups.append((agents, lines))

    token = token.lower()
    specific = [rules for names, rules in groups
                if any(name != "*" and name in token for name in names)]
    selected = specific or [rules for names, rules in groups if "*" in names]
    result = RobotsRules()
    for rules in selected:
        for key, value in rules:
            if key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0:
                    result.crawl_delay = max(result.crawl_delay or 0.0, min(delay, 120.0))
            elif value:
                result.rules.append((key == "allow", _compile(value), len(value)))
    return result
