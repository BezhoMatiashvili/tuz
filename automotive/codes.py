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
