"""Conservative inventory identifier parsing; associations never imply fitment."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

PARSER_VERSION = "2"
CODE_FIELDS = ("product_code", "original_code", "duplicate_code")


@dataclass(frozen=True)
class PartCode:
    original: str
    normalized: str
    field: str
    manufacturer: str
    raw_value: str


def normalize_code(value: str) -> str:
    """Normalize display punctuation, retaining the semantically meaningful slash."""
    value = unicodedata.normalize("NFKC", value).upper().strip()
    return re.sub(r"[^A-Z0-9/]+", "", value)


def codes_in_text(text: str, limit: int = 60) -> list[str]:
    """Identifier-shaped strings quoted in prose: 'GDB1916', 'P 50 069', 'A 000 420 30 20'.

    Only exact warehouse matches are ever used, so a stray candidate costs nothing.
    """
    tokens = [t.rstrip(".-/") for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-/]*", text or "")]
    found: list[str] = []
    lead, group_part = re.compile(r"[A-Za-z]{0,2}\d{0,4}"), re.compile(r"\d{1,4}")

    def add(value: str) -> None:
        key = normalize_code(value)
        if (5 <= len(key) <= 20 and sum(c.isdigit() for c in key) >= 4
                and not re.fullmatch(r"(19|20)\d\d", key) and key not in found):
            found.append(key)

    for index, token in enumerate(tokens):
        if re.fullmatch(r"(19|20)\d\d[-–](19|20)?\d\d", token):
            continue  # A model-year range, not an identifier.
        add(token)
        if index and group_part.fullmatch(token) and lead.fullmatch(tokens[index - 1]):
            continue  # Inside a spaced group that starts earlier; never a fragment.
        # Catalogs space long numbers into groups: 'A 000 420 30 20', '0 986 494 520'.
        group = [token]
        for following in tokens[index + 1:index + 6]:
            if not (group_part.fullmatch(following) and lead.fullmatch(group[0])):
                break
            group.append(following)
        if len(group) > 1:
            add(" ".join(group))
    return found[:limit]


def parse_codes(value: str, *, field: str = "", manufacturer: str = "") -> list[PartCode]:
    """Split explicit combined values and parentheses, without splitting AP104/2.

    Spaced slashes separate identifiers; compact slashes remain part of a code.
    Entire numeric space-separated groups (e.g. OE `03 115 562`) remain one code.
    """
    result: list[PartCode] = []
    seen: set[tuple[str, str]] = set()
    chunks = re.split(r"[,;|\n\r()\[\]]|\s+/\s+", value)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        tokens = [chunk] if re.fullmatch(r"[\d\s.-]+", chunk) else chunk.split()
        for token in tokens:
            token = token.strip()
            normalized = normalize_code(token)
            # Bare descriptions and manufacturer names are not identifier claims.
            if not normalized or not any(char.isdigit() for char in normalized):
                continue
            if (token, normalized) in seen:
                continue
            seen.add((token, normalized))
            result.append(PartCode(token, normalized, field, manufacturer.strip(), value))
    return result


def product_codes(product: dict) -> list[PartCode]:
    return [
        code
        for field in CODE_FIELDS
        for code in parse_codes(
            str(product.get(field) or ""), field=field,
            manufacturer=str(product.get("brand") or ""),
        )
    ]
