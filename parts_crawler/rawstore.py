"""Content-addressed gzip storage for fetched pages, so parsers can be rerun offline."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import tempfile
from pathlib import Path


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, source: str, body: bytes) -> tuple[str, str]:
        if not re.fullmatch(r"[a-z0-9_]{2,32}", source):
            raise ValueError("Invalid source name")
        sha = hashlib.sha256(body).hexdigest()
        relative = f"{source}/{sha[:2]}/{sha}.gz"
        target = self.root / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
            try:
                with os.fdopen(handle, "wb") as raw, \
                        gzip.GzipFile(fileobj=raw, mode="wb") as zipped:
                    zipped.write(body)
                os.replace(temporary, target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        return sha, relative

    def load(self, relative: str) -> bytes:
        target = (self.root / relative).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError("Raw page path escapes the storage directory")
        with gzip.open(target, "rb") as handle:
            return handle.read()
