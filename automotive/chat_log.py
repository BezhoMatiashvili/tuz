"""Local, redacted chat session log for improving the parts assistant.

The log is a private SQLite file. Writes happen on a background thread so a slow disk or a
logging failure can never delay or break a customer turn. Callers pass text that was already
redacted (VINs and configured secrets); raw VINs are never stored, only a keyed hash that
groups turns by vehicle. Conversation IDs are bearer capabilities, so only a hash is stored.

Export for analysis: ``uv run python -m automotive.chat_log --export chat_log.jsonl``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import sqlite3
import sys
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Any, TextIO

LOGGER = logging.getLogger(__name__)
DEFAULT_PATH = "data/chat_logs/chat_log.sqlite3"
SCHEMA_VERSION = 1
MAX_TEXT = 4000
MAX_CARDS = 20
_STOP = object()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    started_at REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    language TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    status TEXT NOT NULL,
    engine_status TEXT,
    category TEXT NOT NULL DEFAULT '',
    vin_hash TEXT,
    vehicle_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    cards_json TEXT NOT NULL DEFAULT '[]',
    card_total INTEGER NOT NULL DEFAULT 0,
    supplier_only_total INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL DEFAULT '{}',
    diagnostics_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS turns_session ON turns (session_id, turn_index);
CREATE INDEX IF NOT EXISTS turns_started ON turns (started_at);
CREATE INDEX IF NOT EXISTS turns_status ON turns (status);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _text(value: Any) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


class ChatLogger:
    def __init__(self, path: str | os.PathLike[str], *, retention_days: int = 0,
                 hmac_key: str = "", queue_size: int = 1000) -> None:
        self.path = Path(path)
        self.retention_days = max(0, retention_days)
        self._hmac_key = hmac_key.encode()
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._lock = Lock()
        self._closed = False
        self._prepare()
        self._writer = Thread(target=self._run, name="chat-log", daemon=True)
        self._writer.start()

    @classmethod
    def from_settings(cls, settings: Any) -> ChatLogger | None:
        """Build a logger from settings. An empty path disables logging; failures never raise."""
        path = getattr(settings, "chat_log_path", "")
        if not path:
            return None
        try:
            return cls(path, retention_days=getattr(settings, "chat_log_retention_days", 0),
                       hmac_key=getattr(settings, "vin_hmac_key", ""))
        except (OSError, sqlite3.Error):
            LOGGER.warning("chat_log_disabled: storage could not be opened")
            return None

    # -- identifiers -------------------------------------------------------------------

    @staticmethod
    def session_key(conversation_id: str) -> str:
        return hashlib.sha256(conversation_id.encode()).hexdigest()[:32]

    def vin_hash(self, vin: str | None) -> str | None:
        """A keyed hash groups turns by vehicle; without a key no VIN-derived value is kept."""
        if not vin or not self._hmac_key:
            return None
        return hmac.new(self._hmac_key, vin.upper().encode(), hashlib.sha256).hexdigest()[:24]

    # -- public recording API (non-blocking) -------------------------------------------

    def session_started(self, conversation_id: str) -> None:
        self._put(("session_start", self.session_key(conversation_id), time.time()))

    def session_ended(self, conversation_id: str, reason: str) -> None:
        self._put(("session_end", self.session_key(conversation_id), time.time(), reason))

    def turn(self, conversation_id: str, *, question: str, status: str, language: str,
             started_at: float, duration_ms: int, payload: dict | None = None,
             vin: str | None = None) -> None:
        payload = payload or {}
        cards = payload.get("cards") or []
        record = {
            "session_id": self.session_key(conversation_id),
            "started_at": started_at,
            "duration_ms": max(0, int(duration_ms)),
            "language": language if language in {"ka", "en"} else "ka",
            "question": _text(question),
            "answer": _text(payload.get("answer")),
            "status": status,
            "engine_status": payload.get("engine_status"),
            "category": _text(payload.get("category"))[:160],
            "vin_hash": self.vin_hash(vin),
            "vehicle": payload.get("vehicle") or {},
            "warnings": payload.get("warnings") or [],
            "cards": [self._card(card) for card in cards[:MAX_CARDS] if isinstance(card, dict)],
            "card_total": len(cards),
            "supplier_only_total": len(payload.get("supplier_only") or []),
            "usage": payload.get("usage") or {},
            "diagnostics": payload.get("diagnostics") or {},
        }
        self._put(("turn", record))

    @staticmethod
    def _card(card: dict) -> dict:
        product = card.get("product") if isinstance(card.get("product"), dict) else {}
        return {"product_code": product.get("product_code"),
                "source_product_id": product.get("source_product_id"),
                "match_source": card.get("match_source"),
                "fitment_status": card.get("fitment_status")}

    def flush(self, timeout: float = 5.0) -> None:
        """Block until queued records are written (used on shutdown and in tests)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put(_STOP, timeout=2)
        except queue.Full:
            pass
        self._writer.join(timeout=5)

    # -- export ------------------------------------------------------------------------

    def export_jsonl(self, stream: TextIO) -> int:
        """Write every turn, with its session, as one JSON object per line."""
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        count = 0
        try:
            rows = connection.execute(
                "SELECT t.*, s.started_at AS session_started_at, s.ended_at AS session_ended_at,"
                " s.end_reason FROM turns t LEFT JOIN sessions s USING (session_id)"
                " ORDER BY t.started_at, t.id")
            for row in rows:
                item = dict(row)
                for key in ("vehicle", "warnings", "cards", "usage", "diagnostics"):
                    item[key] = json.loads(item.pop(f"{key}_json"))
                stream.write(_json(item) + "\n")
                count += 1
        finally:
            connection.close()
        return count

    # -- internals ---------------------------------------------------------------------

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create the file private before SQLite opens it; chat text is sensitive.
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        connection = self._connect()
        try:
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
            self._prune(connection)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _prune(self, connection: sqlite3.Connection) -> None:
        if not self.retention_days:
            return
        cutoff = time.time() - self.retention_days * 86400
        connection.execute("DELETE FROM turns WHERE started_at < ?", (cutoff,))
        connection.execute(
            "DELETE FROM sessions WHERE started_at < ? AND session_id NOT IN"
            " (SELECT DISTINCT session_id FROM turns)", (cutoff,))
        connection.commit()

    def _put(self, item: tuple) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            LOGGER.warning("chat_log_dropped: writer queue is full")

    def _run(self) -> None:
        try:
            connection = self._connect()
        except sqlite3.Error:
            LOGGER.warning("chat_log_writer_unavailable")
            self._drain()
            return
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is _STOP:
                        return
                    self._write(connection, item)
                except Exception:  # The log must never take the application down.
                    LOGGER.warning("chat_log_write_failed")
                finally:
                    self._queue.task_done()
        finally:
            connection.close()

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            self._queue.task_done()
            if item is _STOP:
                return

    def _write(self, connection: sqlite3.Connection, item: tuple) -> None:
        kind = item[0]
        if kind == "session_start":
            connection.execute(
                "INSERT OR IGNORE INTO sessions (session_id, started_at) VALUES (?, ?)",
                (item[1], item[2]))
        elif kind == "session_end":
            connection.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE session_id = ?",
                (item[2], item[3], item[1]))
        elif kind == "turn":
            record = item[1]
            connection.execute(
                "INSERT OR IGNORE INTO sessions (session_id, started_at) VALUES (?, ?)",
                (record["session_id"], record["started_at"]))
            index = connection.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id = ?",
                (record["session_id"],)).fetchone()[0] + 1
            connection.execute(
                "INSERT INTO turns (session_id, turn_index, started_at, duration_ms, language,"
                " question, answer, status, engine_status, category, vin_hash, vehicle_json,"
                " warnings_json, cards_json, card_total, supplier_only_total, usage_json,"
                " diagnostics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["session_id"], index, record["started_at"], record["duration_ms"],
                 record["language"], record["question"], record["answer"], record["status"],
                 record["engine_status"], record["category"], record["vin_hash"],
                 _json(record["vehicle"]), _json(record["warnings"]), _json(record["cards"]),
                 record["card_total"], record["supplier_only_total"], _json(record["usage"]),
                 _json(record["diagnostics"])))
        connection.commit()


def main(argv: list[str] | None = None) -> int:
    from automotive.config import Settings

    parser = argparse.ArgumentParser(description="Export the local chat log.")
    parser.add_argument("--export", metavar="FILE", required=True,
                        help="write turns as JSON lines ('-' for stdout)")
    parser.add_argument("--path", help="log file (defaults to AUTOMOTIVE_CHAT_LOG_PATH)")
    args = parser.parse_args(argv)
    path = args.path or Settings.from_env().chat_log_path or DEFAULT_PATH
    if not Path(path).is_file():
        print(f"No chat log at {path}", file=sys.stderr)
        return 1
    logger = ChatLogger.__new__(ChatLogger)  # Export is read-only; skip the writer thread.
    logger.path = Path(path)
    if args.export == "-":
        count = logger.export_jsonl(sys.stdout)
    else:
        fd = os.open(args.export, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            count = logger.export_jsonl(stream)
    print(f"Exported {count} turns", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
