"""Ephemeral, bounded conversation turns. Nothing in this module writes to storage."""

from __future__ import annotations

import copy
import re
import secrets
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock, Thread
from typing import Any

from automotive.chat import VIN_PATTERN, _redact
from automotive.vin import validate_vin

TERMINAL = {"COMPLETE", "PARTIAL", "FAILED"}


class ConversationError(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class ConversationStore:
    """Opaque IDs are capabilities for a staff-only, single-process local application.

    Queue admission counts outstanding workers even after deletion. A cancelled provider
    cannot publish, and repeated resets cannot create an unbounded executor queue.
    """

    def __init__(self, chat_service: Any, search_service: Any, *,
                 ttl_seconds: float = 1800, turn_seconds: float = 180,
                 maximum: int = 20, clock: Any = time.monotonic) -> None:
        self.chat_service = chat_service
        self.search_service = search_service
        self.ttl_seconds, self.turn_seconds = ttl_seconds, turn_seconds
        self.maximum, self.clock = maximum, clock
        self._lock = RLock()
        self._conversations: dict[str, dict] = {}
        self._outstanding = 0
        self._closed = Event()
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="parts-chat")
        self._janitor: Thread | None = None

    def _safe(self, value: Any) -> Any:
        settings = getattr(self.chat_service, "settings", None)
        private = tuple(getattr(settings, key, "") for key in (
            "anthropic_api_key", "operator_token", "vin_hmac_key", "database_url",
            "enrichment_database_url",
        ))
        private = tuple(v for v in private if isinstance(v, str))
        if isinstance(value, str):
            return _redact(value, private)
        if isinstance(value, dict):
            return {self._safe(str(key)): self._safe(item) for key, item in value.items()}
        if isinstance(value, list | tuple | deque):
            return [self._safe(item) for item in value]
        if value is None or isinstance(value, bool | int | float):
            return value
        return self._safe(str(value))

    def _remove(self, identifier: str) -> None:
        conversation = self._conversations.pop(identifier, None)
        if conversation:
            for turn in conversation["turns"].values():
                turn["cancel"].set()
            conversation["vin"] = None
            conversation["history"].clear()

    def _sweep(self) -> None:
        now = self.clock()
        for identifier, conversation in list(self._conversations.items()):
            if conversation["touched"] + self.ttl_seconds <= now:
                self._remove(identifier)
                continue
            for turn in conversation["turns"].values():
                if turn["status"] == "RUNNING" and turn["deadline"] <= now:
                    turn["cancel"].set()
                    partial = turn["payload"]
                    partial.update(status="PARTIAL" if partial.get("cards") else "FAILED",
                                   answer=("ძიების დრო ამოიწურა. სცადეთ უფრო ზუსტი კითხვა."
                                           if turn["language"] == "ka" else
                                           "The search timed out. Try a more specific question."),
                                   warnings=list(dict.fromkeys(
                                       partial.get("warnings", []) + ["TURN_DEADLINE"])) )
                    turn["status"] = partial["status"]

    def _reap(self) -> None:
        while not self._closed.wait(15):
            with self._lock:
                self._sweep()

    def create(self) -> dict:
        with self._lock:
            self._sweep()
            if self._closed.is_set():
                raise ConversationError("CHAT_CLOSED", 503)
            if len(self._conversations) >= self.maximum:
                raise ConversationError("CONVERSATION_CAPACITY", 429)
            identifier = secrets.token_urlsafe(32)
            self._conversations[identifier] = {
                "touched": self.clock(), "vehicle": {}, "vin": None, "category": "",
                "history": deque(maxlen=12), "turns": {},
            }
            if self._janitor is None:
                self._janitor = Thread(target=self._reap, name="chat-expiry", daemon=True)
                self._janitor.start()
            return {"conversation_id": identifier, "expires_in": self.ttl_seconds}

    def delete(self, identifier: str) -> None:
        with self._lock:
            self._remove(identifier)

    def _conversation(self, identifier: str) -> dict:
        self._sweep()
        conversation = self._conversations.get(identifier)
        if conversation is None:
            raise ConversationError("CONVERSATION_EXPIRED", 404)
        conversation["touched"] = self.clock()
        return conversation

    @staticmethod
    def _category(message: str, previous: str) -> str:
        from automotive.categories import POSITIONS, category_for_query
        from automotive.codes import parse_codes

        category = category_for_query(message)
        # Keep the actual subtype and axle; a canonical category alone loses follow-ups.
        if category:
            if category.startswith('brake') and previous and not any(
                re.search(pattern, message, re.IGNORECASE) for pattern in POSITIONS.values()
            ):
                for position, pattern in POSITIONS.items():
                    if re.search(pattern, previous, re.IGNORECASE):
                        return (message + ' ' + position)[:160]
            return message[:160]
        lower = message.casefold()
        if parse_codes(message) and not re.search(
            r'engine|ძრავ|\byear\b|წელი|წლის', lower,
        ):
            return ""
        changes = (
            (("rear", "უკანა"), ("front", "წინა")),
            (("front", "წინა"), ("rear", "უკანა")),
            (("pads", "pad", "ხუნდი", "ხუნდები"), ("discs", "disc", "rotors", "დისკი")),
            (("discs", "disc", "rotors", "დისკი"), ("pads", "pad", "ხუნდი", "ხუნდები")),
        )
        if previous and any(any(term in lower for term in terms) for terms, _ in changes):
            for incoming, outgoing in changes:
                if any(term in lower for term in incoming):
                    for term in outgoing:
                        previous = re.sub(re.escape(term), "", previous, flags=re.IGNORECASE)
            return (previous + " " + message)[:160]
        return previous

    def submit(self, identifier: str, message: str, *, vehicle: dict | None = None,
               vin: str | None = None, language: str = "ka") -> dict:
        embedded = VIN_PATTERN.findall(message)
        vins = {validate_vin(value) for value in embedded}
        if vin is not None:
            vins.add(validate_vin(vin))
        if len(vins) > 1:
            raise ConversationError("CONFLICTING_VINS", 422)
        incoming_vin = next(iter(vins), None)
        safe_message = self._safe(message)
        with self._lock:
            conversation = self._conversation(identifier)
            old_vin = conversation["vin"]
            identity_changed = vehicle is not None and any(
                vehicle.get(key) and vehicle.get(key) != conversation["vehicle"].get(key)
                for key in ("make", "model", "model_year")
            )
            changed = ((incoming_vin is not None and incoming_vin != conversation["vin"])
                       or (vehicle is not None and any(
                           value != conversation["vehicle"].get(key)
                           for key, value in vehicle.items())))
            running = [t for t in conversation["turns"].values() if t["status"] == "RUNNING"]
            if running and not changed:
                raise ConversationError("TURN_ALREADY_ACTIVE", 409)
            if self._outstanding >= self.maximum:
                raise ConversationError("CHAT_WORKER_CAPACITY", 429)
            if changed:
                for turn in conversation["turns"].values():
                    turn["cancel"].set()
                conversation["turns"].clear()
                conversation["history"].clear()
                if identity_changed or (incoming_vin is not None and incoming_vin != old_vin):
                    conversation["category"] = ""
                # A VIN change must discard the previous vehicle and supplier associations.
                if incoming_vin is not None and incoming_vin != conversation["vin"]:
                    conversation["vehicle"] = self._safe(vehicle or {})
                elif vehicle is not None:
                    if identity_changed:
                        conversation["vehicle"] = self._safe(vehicle)
                        conversation["vin"] = None
                    else:
                        conversation["vehicle"].update(self._safe(vehicle))
            if incoming_vin is not None and not (identity_changed and incoming_vin == old_vin):
                conversation["vin"] = incoming_vin
            conversation["category"] = self._category(safe_message, conversation["category"])
            turn_id = secrets.token_urlsafe(24)
            turn = {"status": "RUNNING", "cancel": Event(), "language": language,
                    "deadline": self.clock() + self.turn_seconds,
                    "payload": {"status": "RUNNING", "answer": "", "progress": [],
                                "cards": [], "supplier_only": [], "results": [],
                                "warnings": [], "vehicle": dict(conversation["vehicle"])}}
            # Twelve completed turns plus the active turn bound stored cards and tool results.
            while len(conversation["turns"]) >= 12:
                conversation["turns"].pop(next(iter(conversation["turns"])))
            conversation["turns"][turn_id] = turn
            self._outstanding += 1
            self._workers.submit(self._run, identifier, turn_id, safe_message,
                                 conversation["vin"], copy.deepcopy(conversation["vehicle"]),
                                 list(conversation["history"]), conversation["category"], turn)
            return {"conversation_id": identifier, "turn_id": turn_id, "status": "RUNNING"}

    def _run(self, identifier: str, turn_id: str, message: str, vin: str | None,
             vehicle: dict, history: list, category: str, turn: dict) -> None:
        def cancelled() -> bool:
            return turn["cancel"].is_set() or self.clock() >= turn["deadline"]

        def progress(snapshot: dict) -> None:
            with self._lock:
                if cancelled() or identifier not in self._conversations:
                    return
                safe = self._safe(snapshot)
                for key in ("progress", "vehicle", "cards", "supplier_only", "warnings"):
                    if key in safe:
                        turn["payload"][key] = safe[key]

        try:
            if cancelled():
                return
            result = self.chat_service.respond_conversation(
                message, vehicle=vehicle, language=turn["language"], vin=vin,
                history=history, category=category, search_service=self.search_service,
                progress=progress, deadline=turn["deadline"], cancelled=cancelled,
            )
            result = self._safe(result)
            with self._lock:
                self._sweep()
                conversation = self._conversations.get(identifier)
                if (cancelled() or not conversation
                        or conversation["turns"].get(turn_id) is not turn):
                    return
                engine_status = result.get("status", "FAILED")
                status = (engine_status if engine_status in TERMINAL else
                          "COMPLETE" if engine_status in {"OK", "CLARIFICATION"} else
                          "PARTIAL" if result.get("cards") or result.get("results") else "FAILED")
                turn["status"] = status
                turn["payload"].update(result, status=status, engine_status=engine_status)
                if isinstance(result.get("category"), str):
                    conversation["category"] = result["category"][:160]
                if result.get("vehicle"):
                    conversation["vehicle"] = result["vehicle"]
                    conversation["identity"] = {k: result["vehicle"].get(k)
                                                for k in ("make", "model", "model_year")}
                conversation["history"].append({"question": message[:4000],
                                                "answer": result.get("answer", "")[:4000]})
        except Exception:
            with self._lock:
                if not cancelled():
                    turn["status"] = "PARTIAL" if turn["payload"].get("cards") else "FAILED"
                    turn["payload"].update(
                        status=turn["status"], warnings=["CHAT_UNAVAILABLE"],
                        answer=("სერვისი დროებით მიუწვდომელია. სცადეთ ხელახლა."
                                if turn["language"] == "ka" else
                                "The service is temporarily unavailable. Please try again."),
                    )
        finally:
            with self._lock:
                self._outstanding -= 1

    def get(self, identifier: str, turn_id: str, offset: int = 0, limit: int = 20) -> dict:
        with self._lock:
            conversation = self._conversation(identifier)
            turn = conversation["turns"].get(turn_id)
            if turn is None:
                raise ConversationError("TURN_EXPIRED", 404)
            payload = copy.deepcopy(turn["payload"])
            cards = payload.get("cards", [])
            offset, limit = max(0, offset), min(100, max(1, limit))
            payload.update(conversation_id=identifier, turn_id=turn_id, status=turn["status"],
                           cards=cards[offset:offset + limit], total=len(cards), offset=offset,
                           next_offset=offset + limit if offset + limit < len(cards) else None)
            return payload

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            for identifier in list(self._conversations):
                self._remove(identifier)
        self._workers.shutdown(wait=False, cancel_futures=True)
