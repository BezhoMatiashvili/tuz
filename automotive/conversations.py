"""Ephemeral, bounded conversation turns.

Live conversation state stays in process memory. The only thing that leaves memory is an
optional, already-redacted record handed to ``ChatLogger`` (a private local file); the
store itself never writes to storage and a logging failure never affects a turn.
"""

from __future__ import annotations

import copy
import logging
import re
import secrets
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock, Thread
from typing import Any

from automotive import dialogue
from automotive.chat import VIN_PATTERN, _redact, engine_name
from automotive.vin import validate_vin

LOGGER = logging.getLogger(__name__)
TERMINAL = {"COMPLETE", "PARTIAL", "FAILED"}
IDENTITY = ("make", "model", "model_year")
CONFIGURATION = (*IDENTITY, "engine_code", "displacement_l", "fuel", "fuel_type", "trim",
                 "body", "drive", "market", "transmission")
RESOLUTION_STATE = ("customer_stated", "resolution_candidates", "selected_configuration",
                    "unresolved_fields", "clarification_choices")
PUBLIC_PROGRESS = ("progress", "vehicle", "cards", "supplier_only", "warnings",
                   "vehicle_revision", "resolution_candidates", "selected_configuration",
                   "clarification_choices", "claims", "citations")


def _same(left: Any, right: Any) -> bool:
    return re.sub(r"[^\w]+", "", str(left or "").casefold()) == re.sub(
        r"[^\w]+", "", str(right or "").casefold())


def _shown(cards: list) -> list[dict]:
    """What a customer can refer back to: ordered, compact, no private fields."""
    return [{"id": card["product"]["source_product_id"],
             "brand": card["product"].get("brand"), "code": card["product"].get("product_code"),
             "basis": card.get("basis"), "fitment_status": card.get("fitment_status"),
             "matched_via": card.get("matched_via") or []}
            for card in cards[:20] if isinstance(card, dict)
            and isinstance(card.get("product"), dict)
            and card["product"].get("source_product_id")]


class ConversationError(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class ConversationStore:
    """Opaque IDs are capabilities for a staff-only, single-process local application.

    Queue admission counts outstanding workers even after deletion. A cancelled provider
    cannot publish, and repeated resets cannot create an unbounded executor queue.
    """

    AUTO_LANGUAGE = True  # submit() accepts language="auto".

    def __init__(self, chat_service: Any, search_service: Any, *,
                 ttl_seconds: float = 1800, turn_seconds: float = 30,
                 maximum: int = 20, clock: Any = time.monotonic,
                 chat_log: Any = None, audience: str = "staff") -> None:
        self.chat_log = chat_log
        # Who reads the replies. The assistant engine gives a "public" conversation no
        # stock numbers of the shop.
        self.audience = audience
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

    @property
    def engine(self) -> str:
        return engine_name(getattr(self.chat_service, "settings", None))

    @property
    def ai_led(self) -> bool:
        return self.engine in {"assistant", "orchestrated"}

    def _safe(self, value: Any) -> Any:
        settings = getattr(self.chat_service, "settings", None)
        private = tuple(getattr(settings, key, "") for key in (
            "anthropic_api_key", "openrouter_api_key", "operator_token", "vin_hmac_key",
            "database_url", "enrichment_database_url", "catalog_api_key",
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

    def _log_session(self, identifier: str, event: str, reason: str = "") -> None:
        """Queue-only and best-effort: a logging fault must never affect a conversation."""
        if not self.chat_log:
            return
        try:
            if event == "start":
                self.chat_log.session_started(identifier)
            else:
                self.chat_log.session_ended(identifier, reason)
        except Exception:
            LOGGER.debug("chat_log_session_skipped")

    def _remove(self, identifier: str, reason: str = "deleted") -> None:
        conversation = self._conversations.pop(identifier, None)
        if conversation:
            self._log_session(identifier, "end", reason)
            for turn in conversation["turns"].values():
                turn["cancel"].set()
            conversation["vin"] = None
            conversation["history"].clear()

    def _sweep(self) -> None:
        now = self.clock()
        for identifier, conversation in list(self._conversations.items()):
            if conversation["touched"] + self.ttl_seconds <= now:
                self._remove(identifier, "expired")
                continue
            for turn in conversation["turns"].values():
                if turn["status"] == "RUNNING" and turn["deadline"] <= now:
                    turn["cancel"].set()
                    partial = turn["payload"]
                    useful = any(partial.get(key) for key in (
                        "cards", "supplier_only", "claims", "resolution_candidates"))
                    partial.update(status="PARTIAL" if useful else "FAILED",
                                   answer=("ძიების დრო ამოიწურა. "
                                           "აქამდე ნაპოვნი შედეგები ნაჩვენებია."
                                           if turn["language"] == "ka" else
                                           "The time limit was reached. "
                                           "Results found so far are shown."),
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
                "history": deque(maxlen=12), "turns": {}, "language": None, "pending": None,
                "shown": [],
                "vehicle_revision": 0, "customer_stated": {}, "resolution_candidates": [],
                "selected_configuration": {}, "unresolved_fields": [],
                "clarification_choices": [],
            }
            self._log_session(identifier, "start")
            prepare = getattr(self.chat_service, "prepare", None)
            if callable(prepare):
                # Off the request path: the assistant engine builds its catalogue index here,
                # so the first message does not wait for it.
                self._workers.submit(self._quietly, prepare)
            if self._janitor is None:
                self._janitor = Thread(target=self._reap, name="chat-expiry", daemon=True)
                self._janitor.start()
            return {"conversation_id": identifier, "expires_in": self.ttl_seconds}

    @staticmethod
    def _quietly(task: Any) -> None:
        try:
            task()
        except Exception:  # Preparation is an optimisation; a turn does the work itself.
            LOGGER.debug("chat_prepare_skipped")

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

        category = category_for_query(message)
        if not category and dialogue.grades(message):
            return ("engine oil " + message)[:160]  # "Do you have 5W-30?" asks about oil.
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
        if dialogue.part_codes(message) and not re.search(
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
               vin: str | None = None, language: str = "ka",
               choice: int | None = None) -> dict:
        embedded = VIN_PATTERN.findall(message)
        vins = {validate_vin(value) for value in embedded}
        if vin is not None:
            vins.add(validate_vin(vin))
        if len(vins) > 1:
            raise ConversationError("CONFLICTING_VINS", 422)
        incoming_vin = next(iter(vins), None)
        if incoming_vin and hasattr(self.search_service, "prewarm"):
            # Resolve the car now so the part question that follows is answered from cache.
            self.search_service.prewarm(incoming_vin)
        safe_message = self._safe(message)
        if self.engine == "assistant":
            return self._submit_assistant(identifier, safe_message, incoming_vin, vehicle,
                                          language, choice, typed_vin=bool(embedded))
        talk = None if embedded else dialogue.small_talk(safe_message)
        with self._lock:
            conversation = self._conversation(identifier)
            if language not in {"ka", "en"}:
                language = dialogue.detect_language(safe_message, conversation.get("language"))
            conversation["language"] = language
            pending = conversation.get("pending") or {}
            offer = pending.get("offer") if isinstance(pending, dict) else None
            requested = None
            if talk == "yes" and offer:
                talk, requested = None, offer.get("category")  # "Need the rear ones too?" "Yes"
            typed = None if embedded or talk or self.ai_led else self._typed_vehicle(
                safe_message, conversation["vehicle"], pending)
            if typed is not None:
                vehicle = typed  # What the customer just typed beats the older form values.
            old_vin = conversation["vin"]
            current = copy.deepcopy(conversation["vehicle"])
            # Filling in a missing detail is not a different car; replacing one is.
            identity_changed = vehicle is not None and any(
                vehicle.get(key) and (current.get(key) or old_vin)
                and not _same(vehicle.get(key), current.get(key))
                for key in IDENTITY
            )
            changed = ((incoming_vin is not None and incoming_vin != conversation["vin"])
                       or (vehicle is not None and any(
                           value != conversation["vehicle"].get(key)
                           for key, value in vehicle.items())))
            running = [t for t in conversation["turns"].values() if t["status"] == "RUNNING"]
            if running and not changed and not self.ai_led:
                raise ConversationError("TURN_ALREADY_ACTIVE", 409)
            if self._outstanding >= self.maximum:
                raise ConversationError("CHAT_WORKER_CAPACITY", 429)
            if changed:
                for turn in conversation["turns"].values():
                    turn["cancel"].set()
                conversation["turns"].clear()
                if not self.ai_led:
                    conversation["history"].clear()
                conversation["shown"] = []
                conversation["vehicle_revision"] += 1
                conversation["resolution_candidates"] = []
                conversation["selected_configuration"] = {}
                conversation["unresolved_fields"] = []
                conversation["clarification_choices"] = []
                conversation["pending"] = None
                switched = identity_changed or (
                    incoming_vin is not None and old_vin is not None and incoming_vin != old_vin)
                # "And for this one?" keeps the part; a bare new car starts over.
                if switched and not dialogue.continues(safe_message):
                    conversation["category"] = ""
                # A VIN change must discard the previous vehicle and supplier associations.
                if incoming_vin is not None and incoming_vin != conversation["vin"]:
                    conversation["vehicle"] = self._safe(vehicle or {})
                elif vehicle is not None:
                    if identity_changed:
                        conversation["vehicle"] = self._safe(vehicle)
                        conversation["vin"] = None
                        conversation["customer_stated"] = {}
                    elif typed is not None:
                        # A typed car is complete: details it dropped must not come back.
                        conversation["vehicle"] = self._safe(vehicle)
                    else:
                        conversation["vehicle"].update(self._safe(vehicle))
                if self.ai_led and vehicle is not None:
                    stated = conversation["customer_stated"]
                    for key in CONFIGURATION:
                        value = conversation["vehicle"].get(key)
                        if value in (None, ""):
                            stated.pop(key, None)
                        elif current.get(key) != value:
                            stated[key] = {"value": value, "quote": None,
                                           "origin": "supplied_configuration"}
            if running and self.ai_led:
                # A correction can arrive while the previous model/provider is working.
                # Retain its stated text, then revoke the worker's publication capability.
                for previous in running:
                    previous["cancel"].set()
                    if previous.get("message"):
                        conversation["history"].append({"question": previous["message"],
                                                        "answer": "", "shown": []})
                conversation["turns"].clear()
            if incoming_vin is not None and not (identity_changed and incoming_vin == old_vin):
                conversation["vin"] = incoming_vin
            if requested:
                conversation["category"] = requested[:160]
            elif (self.ai_led and isinstance(pending, dict)
                  and pending.get("field") in CONFIGURATION):
                # A numeric year/size or short engine answer belongs to the pending
                # vehicle question. The orchestrator can still recognize a new part.
                from automotive.categories import category_for_query

                if category_for_query(safe_message):
                    conversation["category"] = self._category(
                        safe_message, conversation["category"])
            elif not talk:
                conversation["category"] = self._category(safe_message, conversation["category"])
            state = {"small_talk": talk, "typed_vehicle": typed is not None,
                     "last_cards": copy.deepcopy(conversation["shown"]),
                     "vehicle_revision": conversation["vehicle_revision"],
                     "pending": copy.deepcopy(conversation["pending"]),
                     **{key: copy.deepcopy(conversation[key]) for key in RESOLUTION_STATE}}
            turn_id = secrets.token_urlsafe(24)
            turn = {"status": "RUNNING", "cancel": Event(), "language": language,
                    "message": safe_message, "vehicle_revision": conversation["vehicle_revision"],
                    "started": self.clock(), "started_at": time.time(),
                    "deadline": self.clock() + self.turn_seconds,
                    "payload": {"status": "RUNNING", "answer": "", "progress": [],
                                "cards": [], "supplier_only": [], "results": [],
                                "warnings": [], "vehicle": dict(conversation["vehicle"]),
                                "vehicle_revision": conversation["vehicle_revision"],
                                "language": language}}
            # Twelve completed turns plus the active turn bound stored cards and tool results.
            while len(conversation["turns"]) >= 12:
                conversation["turns"].pop(next(iter(conversation["turns"])))
            conversation["turns"][turn_id] = turn
            self._outstanding += 1
            self._workers.submit(self._run, identifier, turn_id, safe_message,
                                 conversation["vin"], copy.deepcopy(conversation["vehicle"]),
                                 list(conversation["history"]), conversation["category"], turn,
                                 state)
            return {"conversation_id": identifier, "turn_id": turn_id, "status": "RUNNING",
                    "language": language}

    def _submit_assistant(self, identifier: str, safe_message: str, incoming_vin: str | None,
                          vehicle: dict | None, language: str, choice: int | None,
                          typed_vin: bool = False) -> dict:
        """Start a turn of the assistant engine: the same VIN handling, superseding and
        turn bookkeeping as above, and nothing that reads the message. The model tells
        small talk from an answer, names the car and the part and sets the language.

        The staff page sends its vehicle form and its VIN field with every message, also
        when they are left over from the customer before. So the store only keeps what the
        form says and tells the engine where the VIN came from (``typed_vin``: it stood in
        a message); whether the message names another car is for the model to read."""
        with self._lock:
            conversation = self._conversation(identifier)
            if language not in {"ka", "en"}:
                language = conversation.get("language") or "ka"
            conversation["language"] = language
            if self._outstanding >= self.maximum:
                raise ConversationError("CHAT_WORKER_CAPACITY", 429)
            old_vin, current = conversation["vin"], conversation["vehicle"]
            # A new VIN is another car. So is a staff form that contradicts the car on
            # record, and then the VIN on record no longer describes it.
            contradicted = vehicle is not None and any(
                vehicle.get(key) and (current.get(key) or old_vin)
                and not _same(vehicle.get(key), current.get(key)) for key in IDENTITY)
            if incoming_vin is not None and incoming_vin != old_vin:
                conversation["vehicle"] = self._safe(vehicle or {})
                conversation["vin"] = incoming_vin
                conversation["vin_typed"] = typed_vin
                conversation["vehicle_revision"] += 1
            elif contradicted:
                conversation["vehicle"] = self._safe(vehicle)
                # The VIN stays when this request carries it beside the form: a form left
                # over from another car must not take away the VIN a message gave.
                conversation["vin"] = incoming_vin
                conversation["vehicle_revision"] += 1
            elif vehicle is not None:
                conversation["vehicle"].update(self._safe(vehicle))
            if typed_vin:
                conversation["vin_typed"] = True
            running = [t for t in conversation["turns"].values() if t["status"] == "RUNNING"]
            if running:
                # A message sent while the last one is being answered replaces that turn;
                # its text is kept so the model reads both.
                for previous in running:
                    previous["cancel"].set()
                    if previous.get("message"):
                        conversation["history"].append({"question": previous["message"],
                                                        "answer": "", "shown": []})
                conversation["turns"].clear()
            tapped = choice if isinstance(choice, int) and not isinstance(choice, bool) else None
            state = {"memory": copy.deepcopy(conversation.get("memory")),
                     "choice": tapped if tapped and tapped > 0 else None,
                     "audience": self.audience,
                     "vin_source": "message" if conversation.get("vin_typed") else "form",
                     "pending": copy.deepcopy(conversation["pending"]),
                     "vehicle_revision": conversation["vehicle_revision"]}
            turn_id = secrets.token_urlsafe(24)
            turn = {"status": "RUNNING", "cancel": Event(), "language": language,
                    "message": safe_message, "vehicle_revision": conversation["vehicle_revision"],
                    "started": self.clock(), "started_at": time.time(),
                    "deadline": self.clock() + self.turn_seconds,
                    "payload": {"status": "RUNNING", "answer": "", "progress": [],
                                "cards": [], "supplier_only": [], "results": [],
                                "warnings": [], "vehicle": dict(conversation["vehicle"]),
                                "vehicle_revision": conversation["vehicle_revision"],
                                "language": language}}
            while len(conversation["turns"]) >= 12:
                conversation["turns"].pop(next(iter(conversation["turns"])))
            conversation["turns"][turn_id] = turn
            self._outstanding += 1
            self._workers.submit(self._run, identifier, turn_id, safe_message,
                                 conversation["vin"], copy.deepcopy(conversation["vehicle"]),
                                 list(conversation["history"]), conversation["category"], turn,
                                 state)
            return {"conversation_id": identifier, "turn_id": turn_id, "status": "RUNNING",
                    "language": language}

    @staticmethod
    def _typed_vehicle(message: str, current: dict, pending: dict) -> dict | None:
        """The car or detail a message states ('Honda Accord 2015', a bare '2015', 'K24W')."""
        asked = pending.get("act") if isinstance(pending, dict) else None
        typed = dialogue.read_vehicle(message)
        if typed.make and typed.model:
            same = _same(typed.make, current.get("make")) and _same(
                typed.model, current.get("model"))
            if not same:
                return typed.as_vehicle()
            kept = dict(current)
            if typed.trim and not _same(typed.trim, current.get("trim")):
                # Engine details described the previous trim.
                for key in ("engine_code", "displacement_l"):
                    kept.pop(key, None)
            return {**kept, **typed.as_vehicle()}
        if typed.make:
            return None if _same(typed.make, current.get("make")) else typed.as_vehicle()
        year = dialogue.year_answer(message)
        if year is None and asked in {"clarify_year", "clarify_model", "clarify_vehicle"}:
            year = typed.model_year
        if year and current.get("make") and current.get("model"):
            return {**current, "model_year": year}
        if asked == "clarify_engine":
            details = dialogue.engine_details(message, current.get("make"),
                                              pending.get("options") or [])
            if details:
                return {**current, **details}
        return None

    def _accept_snapshot(self, conversation: dict, turn_id: str, turn: dict,
                         snapshot: dict) -> bool:
        """Publish only the active turn and its newest configuration revision.

        The orchestrator owns revisions for extracted changes. The store also counts
        configuration changes from legacy services that do not expose revisions.
        """
        if conversation["turns"].get(turn_id) is not turn or turn["cancel"].is_set():
            return False
        revision = snapshot.get("vehicle_revision", turn["payload"]["vehicle_revision"])
        if (isinstance(revision, bool) or not isinstance(revision, int)
                or revision < conversation["vehicle_revision"]
                or revision < turn["payload"]["vehicle_revision"]):
            return False
        incoming = snapshot.get("vehicle")
        changed = isinstance(incoming, dict) and any(
            incoming.get(key) != conversation["vehicle"].get(key) for key in CONFIGURATION)
        if changed:
            if "vehicle_revision" not in snapshot:
                revision += 1
            conversation["shown"] = []
            for key, empty in (("resolution_candidates", []), ("selected_configuration", {}),
                               ("unresolved_fields", []), ("clarification_choices", [])):
                if key not in snapshot:
                    conversation[key] = empty
        conversation["vehicle_revision"] = revision
        turn["payload"]["vehicle_revision"] = revision
        if isinstance(incoming, dict):
            conversation["vehicle"] = incoming
        for key in RESOLUTION_STATE:
            if key in snapshot:
                conversation[key] = snapshot[key]
        for key in PUBLIC_PROGRESS:
            if key in snapshot and key != "vehicle_revision":
                turn["payload"][key] = snapshot[key]
        return True

    def _run(self, identifier: str, turn_id: str, message: str, vin: str | None,
             vehicle: dict, history: list, category: str, turn: dict,
             state: dict | None = None) -> None:
        def cancelled() -> bool:
            return turn["cancel"].is_set() or self.clock() >= turn["deadline"]

        def progress(snapshot: dict) -> None:
            with self._lock:
                conversation = self._conversations.get(identifier)
                if cancelled() or not conversation:
                    return
                safe = self._safe(snapshot)
                self._accept_snapshot(conversation, turn_id, turn, safe)

        try:
            if cancelled():
                return
            result = self.chat_service.respond_conversation(
                message, vehicle=vehicle, language=turn["language"], vin=vin,
                history=history, category=category, search_service=self.search_service,
                progress=progress, deadline=turn["deadline"], cancelled=cancelled,
                **({"dialogue": state} if state is not None else {}),
            )
            result = self._safe(result)
            with self._lock:
                self._sweep()
                conversation = self._conversations.get(identifier)
                if (cancelled() or not conversation
                        or not self._accept_snapshot(conversation, turn_id, turn, result)):
                    return
                engine_status = result.get("status", "FAILED")
                status = (engine_status if engine_status in TERMINAL else
                          "COMPLETE" if engine_status in {"OK", "CLARIFICATION"} else
                          "PARTIAL" if any(result.get(key) for key in (
                              "cards", "results", "supplier_only", "claims")) else "FAILED")
                turn["status"] = status
                result["vehicle_revision"] = conversation["vehicle_revision"]
                # The assistant engine's own state stays here: it is never part of a payload.
                memory = result.pop("memory", None)
                if isinstance(memory, dict):
                    conversation["memory"] = memory
                    if result.get("language") in {"ka", "en"}:
                        # The model set the language of this reply; the next turn starts in it.
                        conversation["language"] = turn["language"] = result["language"]
                turn["payload"].update(result, status=status, engine_status=engine_status)
                if isinstance(result.get("category"), str):
                    conversation["category"] = result["category"][:160]
                if result.get("vehicle"):
                    conversation["vehicle"] = result["vehicle"]
                    conversation["identity"] = {k: result["vehicle"].get(k)
                                                for k in ("make", "model", "model_year")}
                conversation["pending"] = result.get("pending")
                shown = _shown(result.get("cards") or [])
                if shown:
                    conversation["shown"] = shown
                conversation["history"].append({
                    "question": message[:4000], "answer": result.get("answer", "")[:4000],
                    "note": str(result.get("note") or "")[:400],
                    "shown": [f"{c['brand'] or ''} {c['code'] or ''}".strip() for c in shown[:5]],
                })
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
                # Snapshot under the lock; the write itself is queued outside it.
                logged = copy.deepcopy(turn["payload"]) if self.chat_log else None
                status = turn["status"] if turn["status"] != "RUNNING" else "CANCELLED"
            if logged is not None:
                self._log_turn(identifier, turn, message, vin, status, logged)

    def _log_turn(self, identifier: str, turn: dict, message: str, vin: str | None,
                  status: str, payload: dict) -> None:
        try:
            self.chat_log.turn(
                identifier, question=message, status=status, language=turn["language"],
                started_at=turn["started_at"], vin=vin, payload=payload,
                duration_ms=int((self.clock() - turn["started"]) * 1000),
            )
        except Exception:  # Logging is best-effort and must not affect the turn.
            LOGGER.debug("chat_log_turn_skipped")

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
                self._remove(identifier, "shutdown")
        self._workers.shutdown(wait=False, cancel_futures=True)
