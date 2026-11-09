"""HMAC signing for immutable warehouse-product observations."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping


def version_integrity_tag(
    key: bytes,
    *,
    source_product_id: str,
    observed_run_id: int,
    source_page: int,
    source_columns: Mapping[str, str],
    row_hash: str,
) -> str:
    """Return a deterministic HMAC tag without putting its key in PostgreSQL."""
    canonical = json.dumps(
        {
            "format": 1,
            "source_product_id": source_product_id,
            "observed_run_id": observed_run_id,
            "source_page": source_page,
            "source_columns": source_columns,
            "row_hash": row_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def verify_version_integrity_tag(key: bytes, tag: str, **fields: object) -> bool:
    """Verify a stored tag using constant-time comparison."""
    expected = version_integrity_tag(key, **fields)  # type: ignore[arg-type]
    return hmac.compare_digest(expected, tag)
