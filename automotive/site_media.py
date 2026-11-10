"""Image checks for dashboard uploads and the packaged gallery photos.

The type comes from the bytes, never from a file name or a request header, and SVG is not
accepted: it can carry script. Dimensions are read from the container headers so no image
library is needed.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

MAX_BYTES = 1_572_864  # Matches the CHECK on garage.media.data.
MAX_SIDE = 4096
GALLERY = Path(__file__).parent / "static" / "site" / "img" / "gallery"


def _png(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _jpeg(data: bytes) -> tuple[int, int] | None:
    position = 2
    while position + 9 < len(data):
        if data[position] != 0xFF:
            return None
        marker = data[position + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        length = struct.unpack(">H", data[position + 2:position + 4])[0]
        # Start-of-frame markers carry the size; C4, C8 and CC are tables, not frames.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[position + 5:position + 9])
            return width, height
        position += 2 + length
    return None


def _webp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


def sniff(data: bytes) -> tuple[str, int, int] | None:
    """(content type, width, height) for an acceptable JPEG, PNG or WebP; otherwise None."""
    if not isinstance(data, bytes | bytearray) or not 0 < len(data) <= MAX_BYTES:
        return None
    data = bytes(data)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, size = "image/png", _png(data)
    elif data.startswith(b"\xff\xd8\xff"):
        kind, size = "image/jpeg", _jpeg(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind, size = "image/webp", _webp(data)
    else:
        return None
    if not size or not (0 < size[0] <= MAX_SIDE and 0 < size[1] <= MAX_SIDE):
        return None
    return kind, size[0], size[1]


def describe(data: bytes) -> dict | None:
    """The stored fields for an upload, or None when the bytes are not an allowed image."""
    found = sniff(data)
    if not found:
        return None
    return {"sha256": hashlib.sha256(data).hexdigest(), "content_type": found[0],
            "data": bytes(data), "width": found[1], "height": found[2]}


def packaged_gallery(directory: Path | None = None) -> list[dict]:
    """The shop photos shipped with the site, ready for SiteRepository.add_media."""
    directory = directory or GALLERY
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return []
    photos = []
    for entry in json.loads(manifest.read_text(encoding="utf-8")):
        path = directory / Path(str(entry.get("file", ""))).name
        described = describe(path.read_bytes()) if path.is_file() else None
        if described:
            photos.append({**described, "alt_ka": str(entry.get("alt_ka", ""))[:160],
                           "alt_en": str(entry.get("alt_en", ""))[:160],
                           "published": bool(entry.get("published", False))})
    return photos
