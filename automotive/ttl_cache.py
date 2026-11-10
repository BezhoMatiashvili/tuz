"""Private, disposable cache for normalized automotive source records.

Only catalog, chooser and research data belongs here. Offers always come from
the current warehouse snapshot. Keys are hashed and VINs/credentials are refused.
Cache failures are misses, so a corrupt or unavailable file cannot stop a turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from threading import Lock
from typing import Any

from automotive.public_http import VIN_IN_TEXT

SCHEMA_VERSION = 1
SUCCESS_TTL = 24 * 3600
EMPTY_TTL = 60
FORBIDDEN_FIELDS = {
    'vin', 'raw_vin', 'stock', 'quantity', 'availability', 'price', 'unit_price',
    'api_key', 'apikey', 'authorization', 'cookie', 'password', 'credentials',
    'database_url', 'operator_token', 'vin_hmac_key', 'anthropic_api_key',
}


def _safe(value: Any) -> bool:
    if isinstance(value, dict):
        return all(str(key).casefold() not in FORBIDDEN_FIELDS and _safe(child)
                   for key, child in value.items())
    if isinstance(value, list | tuple):
        return all(_safe(child) for child in value)
    if isinstance(value, str):
        return not VIN_IN_TEXT.search(value)
    return value is None or isinstance(value, int | float | bool)


class SQLiteTTLCache:
    def __init__(self, path: str | Path | None = None, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path or 'data/automotive-cache.sqlite3')
        self.clock = clock
        self._lock = Lock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Refuse symlinks, and create the file privately before sqlite opens it.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        db = sqlite3.connect(self.path, timeout=0.1)
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in {0, SCHEMA_VERSION}:
                raise sqlite3.DatabaseError('Unsupported automotive cache schema')
            db.execute('''CREATE TABLE IF NOT EXISTS source_cache (
                namespace TEXT NOT NULL, cache_key TEXT NOT NULL,
                expires_at REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(namespace, cache_key))''')
            db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
            db.commit()
        except Exception:
            db.close()
            raise
        return db

    @staticmethod
    def _key(namespace: str, key: Any) -> str | None:
        if namespace.split('.')[0] not in {'catalog', 'chooser', 'research'} or not _safe(key):
            return None
        try:
            encoded = json.dumps(key, sort_keys=True, separators=(',', ':'),
                                 ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            return None
        return hashlib.sha256(encoded.encode()).hexdigest()

    def get(self, namespace: str, key: Any) -> Any | None:
        digest = self._key(namespace, key)
        if digest is None:
            return None
        try:
            with self._lock, closing(self._connect()) as db, db:
                row = db.execute('''SELECT payload FROM source_cache
                    WHERE namespace = ? AND cache_key = ? AND expires_at > ?''',
                                 (namespace, digest, self.clock())).fetchone()
                value = json.loads(row[0]) if row else None
                return value if _safe(value) else None
        except (OSError, sqlite3.Error, ValueError, RecursionError):
            return None

    def put(self, namespace: str, key: Any, value: Any, *, empty: bool = False) -> bool:
        digest = self._key(namespace, key)
        if digest is None or not _safe(value):
            return False
        try:
            payload = json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
            if len(payload.encode()) > 2_000_000:
                return False
            with self._lock, closing(self._connect()) as db, db:
                now = self.clock()
                expiry = now + (EMPTY_TTL if empty else SUCCESS_TTL)
                db.execute('DELETE FROM source_cache WHERE expires_at <= ?', (now,))
                db.execute('''INSERT OR REPLACE INTO source_cache
                    (namespace, cache_key, expires_at, payload) VALUES (?, ?, ?, ?)''',
                           (namespace, digest, expiry, payload))
            return True
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            return False
