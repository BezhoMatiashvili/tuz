"""Local automotive API, isolated from the database explorer and sync writer."""

from __future__ import annotations

import contextlib
import hmac
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

STATIC = Path(__file__).parent / "static"
Code = Annotated[str, Query(min_length=1, max_length=120)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class VehicleConfiguration(StrictModel):
    make: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    model_year: int | None = Field(default=None, ge=1980, le=2100)
    engine_code: str | None = Field(default=None, max_length=100)
    trim: str | None = Field(default=None, max_length=100)
    fuel: str | None = Field(default=None, max_length=100)
    fuel_type: str | None = Field(default=None, max_length=100)
    dpf: bool | None = None
    displacement_l: float | None = Field(default=None, gt=0, le=20)
    engine_cylinders: int | None = Field(default=None, ge=1, le=24)
    generation: str | None = Field(default=None, max_length=100)
    aspiration: str | None = Field(default=None, max_length=100)
    transmission: str | None = Field(default=None, max_length=100)
    market: str | None = Field(default=None, max_length=100)
    body: str | None = Field(default=None, max_length=100)
    drive: str | None = Field(default=None, max_length=100)


class IdentifyRequest(StrictModel):
    vin: str = Field(min_length=17, max_length=17)
    model_year: int | None = Field(default=None, ge=1980, le=2100)


class MotoriumLookupRequest(StrictModel):
    vin: str = Field(min_length=17, max_length=17)
    part_query: str = Field(default="", max_length=160)
    limit: int = Field(default=6, ge=1, le=6)


class VINSearchRequest(IdentifyRequest):
    part_query: str = Field(default="", max_length=160)
    configuration: VehicleConfiguration | None = None


class VehicleRequest(StrictModel):
    vehicle: VehicleConfiguration


class VehicleInventoryRequest(VehicleRequest):
    limit: int = Field(default=30, ge=1, le=100)


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    vehicle: VehicleConfiguration | None = None
    language: Literal["ka", "en"] = "ka"


class ConversationMessage(ChatRequest):
    vin: str | None = Field(default=None, min_length=17, max_length=17)
    # "auto" follows the customer's letters and keeps the language for codes and VINs.
    language: Literal["ka", "en", "auto"] = "auto"
    # Which answer button was tapped, counted from 1; the message carries its text.
    choice: int | None = Field(default=None, ge=1, le=8)


class ImportRequest(StrictModel):
    format: Literal["csv", "json"]
    content: str = Field(min_length=1, max_length=1_000_000)


class JobRequest(StrictModel):
    kind: Literal["index"] = "index"


class StockInfo(BaseModel):
    store: float | None = None
    tuz11: float | None = None


class CatalogProduct(BaseModel):
    source_product_id: str
    source_fingerprint: str | None = None
    product_code: str = ""
    product_name: str = ""
    product_model: str = ""
    original_code: str = ""
    duplicate_code: str = ""
    brand: str = ""
    last_observed_at: Any = None
    price: float | None = None
    currency: str | None = None
    stock: StockInfo | None = None
    status: str = "UNKNOWN"
    matched_codes: list[Any] = Field(default_factory=list)


class SearchResult(BaseModel):
    items: list[CatalogProduct]
    status: str = "UNKNOWN"
    warnings: list[str] = Field(default_factory=list)
    query: str | None = None


class VehicleInventoryItem(BaseModel):
    product: CatalogProduct
    status: Literal["VERIFIED", "PROVISIONAL", "CONFLICTED", "UNKNOWN"]
    compatible: bool | None = None
    match_type: Literal["VERIFIED_FITMENT", "FITMENT_CANDIDATE", "CATALOG_TEXT", "CODE_CANDIDATE"]
    warnings: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class VehicleInventoryResult(BaseModel):
    status: str
    items: list[VehicleInventoryItem]
    verified_count: int
    candidate_count: int
    excluded_count: int
    has_more: bool
    clarification: str | None = None
    warnings: list[str] = Field(default_factory=list)


class RecordsResult(BaseModel):
    compatible: bool | None = None
    candidates: list[CatalogProduct] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "UNKNOWN"
    warnings: list[str] = Field(default_factory=list)
    evidence: list[Any] = Field(default_factory=list)


class JobResult(BaseModel):
    job_id: str
    kind: str
    status: str
    checkpoint: Any = None
    attempts: int = 0
    created_at: Any = None
    updated_at: Any = None
    error_code: str | None = None


class StructuredResult(BaseModel):
    """Structured service records are serialized without interpreting provider text."""

    model_config = ConfigDict(extra="allow")
    status: str = "UNKNOWN"
    warnings: list[str] = Field(default_factory=list)


class ServiceStatus(BaseModel):
    inventory_configured: bool
    vin_configured: bool
    vin_cache_configured: bool
    chat_configured: bool
    enrichment_configured: bool
    provider_health_checked: bool = False
    motorium_configured: bool = False
    fuchs_configured: bool = False
    shell_configured: bool = False
    pakelo_configured: bool = False
    jsfilter_configured: bool = False
    catalog_api_configured: bool = False


class MotoriumPart(BaseModel):
    provider_product_id: int
    name: str | None = None
    manufacturer: str | None = None
    mpn: str
    oem_codes: list[str]
    supplier_sku: str | None = None
    source_url: str
    status: Literal["PROVISIONAL", "CONFLICTED"]
    provider_fitment_text: str | None = None
    warnings: list[str]


class MotoriumResult(BaseModel):
    provider: Literal["Motorium"]
    status: Literal["PROVISIONAL", "CONFLICTED", "INSUFFICIENT_DATA"]
    vehicle: dict[str, Any]
    vehicle_context_matched: bool
    items: list[MotoriumPart]
    has_more: bool
    observed_at: str
    warnings: list[str]
    clarification: str | None = None


class SourceProgress(BaseModel):
    elapsed_ms: int | None = None
    source_id: str
    label: str
    status: str
    count: int
    completed_queries: int
    total_queries: int
    coverage_complete: bool = False
    error_code: str | None = None


class UnifiedPart(BaseModel):
    part_id: str
    name: str
    manufacturer: str
    main_code: str
    status: Literal["VERIFIED", "PROVISIONAL", "CONFLICTED", "UNKNOWN"]
    compatible: bool | None = None
    warehouse_records: list[VehicleInventoryItem]
    supplier_records: list[MotoriumPart]
    source_names: list[str]
    warnings: list[str]


class OilItem(BaseModel):
    product: CatalogProduct
    status: str
    required_approvals: list[str]
    required_viscosities: list[str]
    evidence: list[dict[str, Any]]


class OilResult(BaseModel):
    status: str
    items: list[OilItem]
    evidence: list[dict[str, Any]]
    warnings: list[str]
    clarification: str | None = None
    capacity_liters: float | None = None
    filter_references: list[str] = Field(default_factory=list)
    has_more: bool = False


class FluidProduct(BaseModel):
    kind: Literal["oil", "filter"]
    component: str
    product_name: str
    product_code: str | None = None
    viscosity: str | None = None
    interval: str | None = None
    capacity: str | None = None
    approvals: list[str] = Field(default_factory=list)


class FluidReport(BaseModel):
    provider: str
    status: str
    vehicle_label: str = ""
    engine_code: str | None = None
    items: list[FluidProduct] = Field(default_factory=list)
    clarification: str | None = None
    warnings: list[str] = Field(default_factory=list)
    provenance: str = "live"
    validation_status: str | None = None
    source_url: str = ""
    provider_ids: dict[str, Any] = Field(default_factory=dict)


class VINSearchResult(BaseModel):
    search_id: str
    status: Literal["RUNNING", "COMPLETE", "PARTIAL", "FAILED", "CONFLICTED"]
    vehicle: dict[str, Any]
    sources: list[SourceProgress]
    warnings: list[str]
    items: list[UnifiedPart]
    total: int
    offset: int
    next_offset: int | None
    verified_count: int
    candidate_count: int
    coverage_complete: bool = False
    created_at: str
    maintenance: OilResult | None = None
    fluids: list[FluidReport] = Field(default_factory=list)
    cross_references: list[dict[str, Any]] = Field(default_factory=list)
    knowledge: list[dict[str, Any]] = Field(default_factory=list)
    provenance: str = "live"
    validation_timestamp: str | None = None


class ConversationCard(BaseModel):
    product: CatalogProduct
    match_source: str
    fitment_status: str
    compatible: bool | None = None
    availability: str = "Found in our catalog; stock unconfirmed."
    availability_status: str = "UNKNOWN"
    basis: str = ""
    matched_via: list[str] = Field(default_factory=list)
    pack_litres: float | None = None
    role: str | None = None
    warnings: list[str] = Field(default_factory=list)
    # The assistant engine: the heading a card is listed under, why it is offered, how
    # sure the offer is and the words of the catalogue row it rests on.
    group: str | None = None
    reason: str | None = None
    confidence: str | None = None
    evidence: str | None = None

    @field_validator("evidence", mode="before")
    @classmethod
    def _quoted_text_only(cls, value: Any) -> Any:
        # The older engines attach evidence records under this key; those stay server-side.
        return value if isinstance(value, str) else None


class ChatOption(BaseModel):
    label: str = ""
    value: str = ""


class ChatQuestion(BaseModel):
    text: str = ""
    options: list[ChatOption] = Field(default_factory=list, max_length=8)


class ChatGroup(BaseModel):
    id: str = ""
    label: str = ""
    count: int = 0


def clamp_turn(payload: dict) -> dict:
    """Cut the lists the response model bounds. One button too many must never fail a
    whole turn in response validation."""
    for key, limit in (("quick_replies", 8), ("clarification_choices", 8), ("suggestions", 4),
                       ("groups", 40)):
        if isinstance(payload.get(key), list):
            payload[key] = payload[key][:limit]
    question = payload.get("question")
    if isinstance(question, dict) and isinstance(question.get("options"), list):
        payload["question"] = {**question, "options": question["options"][:8]}
    return payload


class ConversationTurn(BaseModel):
    knowledge: list[dict[str, Any]] = Field(default_factory=list)
    conversation_id: str
    turn_id: str
    status: Literal["RUNNING", "COMPLETE", "PARTIAL", "FAILED"]
    engine_status: str | None = None
    act: str = ""
    answer: str = ""
    note: str = ""
    quick_replies: list[str] = Field(default_factory=list, max_length=8)
    language: Literal["ka", "en"] = "ka"
    vehicle: dict[str, Any] = Field(default_factory=dict)
    vehicle_revision: int = Field(default=0, ge=0)
    resolution_candidates: list[dict[str, Any]] = Field(default_factory=list)
    selected_configuration: dict[str, Any] = Field(default_factory=dict)
    clarification_choices: list[Any] = Field(default_factory=list, max_length=8)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    progress: list[dict[str, Any]] = Field(default_factory=list)
    cards: list[ConversationCard] = Field(default_factory=list)
    supplier_only: list[dict[str, Any]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    requirements: dict[str, Any] = Field(default_factory=dict)
    total: int = 0
    offset: int = 0
    next_offset: int | None = None
    # The assistant engine: the question with its answer buttons, next-step buttons, the
    # headings the cards are listed under, the car as understood and how the turn ended.
    question: ChatQuestion | None = None
    suggestions: list[ChatOption] = Field(default_factory=list, max_length=4)
    groups: list[ChatGroup] = Field(default_factory=list, max_length=40)
    car_label: str = ""
    outcome: str = ""


class RateLimiter:
    """Bounded, process-local limiter. Proxy headers are deliberately ignored."""

    def __init__(self) -> None:
        self.requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.lock = Lock()

    def allow(self, client: str, category: str, maximum: int) -> bool:
        now = time.monotonic()
        with self.lock:
            for key in list(self.requests):
                if not self.requests[key] or self.requests[key][-1] < now - 60:
                    del self.requests[key]
            key = (client, category)
            if key not in self.requests and len(self.requests) >= 10_000:
                return False
            queue = self.requests[key]
            while queue and queue[0] < now - 60:
                queue.popleft()
            if len(queue) >= maximum:
                return False
            queue.append(now)
            return True


def create_app(
    settings: Any = None,
    repository: Any = None,
    vin_service: Any = None,
    chat_service: Any = None,
    enrichment_repository: Any = None,
    motorium_service: Any = None,
    search_service: Any = None,
    site: Any = None,
) -> FastAPI:
    from automotive.brembo import BremboClient
    from automotive.config import Settings
    from automotive.fuchs import FuchsClient
    from automotive.jsfilter import JsFilterClient
    from automotive.motorium import MotoriumClient, MotoriumError
    from automotive.pakelo import PakeloClient
    from automotive.repository import DatabaseNotConfigured
    from automotive.shell_oil import ShellOilClient
    from automotive.vin import ProviderUnavailable
    from automotive.vin_search import VINSearchCoordinator

    settings = settings or Settings.from_env()
    from automotive.rules import load_rules

    rules = load_rules()  # An invalid rules.toml stops startup with a named error.
    motorium_service = motorium_service or MotoriumClient()
    if repository is None:
        from automotive.repository import AutomotiveRepository

        repository = AutomotiveRepository(settings.database_url)
    if vin_service is None:
        from automotive.vin import VinService

        vin_service = VinService(repository, settings.vin_hmac_key)
    if chat_service is None:
        from automotive.chat import ChatService

        chat_service = ChatService(repository, vin_service, settings, rules=rules)
    if enrichment_repository is None and settings.enrichment_database_url:
        from automotive.repository import AutomotiveRepository

        enrichment_repository = AutomotiveRepository(settings.enrichment_database_url)

    from automotive.ttl_cache import SQLiteTTLCache

    source_cache = (SQLiteTTLCache(settings.automotive_cache_path)
                    if settings.automotive_cache_path else None)
    catalog_service = None
    if settings.catalog_api_key:
        from automotive.catalog_api import CatalogApiClient

        catalog_service = CatalogApiClient(
            settings.catalog_api_key, base_url=settings.catalog_api_base_url,
            host=settings.catalog_api_host, key_header=settings.catalog_api_key_header,
            hmac_key=settings.vin_hmac_key,
            cache=source_cache,
        )
    owns_search_service = search_service is None
    from automotive.knowledge import KnowledgeService
    from automotive.knowledge_search import LearningSearchCoordinator

    knowledge = KnowledgeService.from_settings(settings)
    search_type = LearningSearchCoordinator if knowledge else VINSearchCoordinator
    search_service = search_service or search_type(
        repository, vin_service, motorium_service, settings.vin_hmac_key,
        motorium_enabled=settings.motorium_enabled,
        fuchs_service=FuchsClient() if settings.fuchs_enabled else None,
        shell_service=ShellOilClient() if settings.shell_enabled else None,
        pakelo_service=PakeloClient() if settings.pakelo_enabled else None,
        jsfilter_service=JsFilterClient() if settings.jsfilter_enabled else None,
        brembo_service=BremboClient() if settings.brembo_enabled else None,
        catalog_service=catalog_service, rules=rules,
        cache=source_cache,
        fuchs_enabled=settings.fuchs_enabled, shell_enabled=settings.shell_enabled,
        pakelo_enabled=settings.pakelo_enabled, jsfilter_enabled=settings.jsfilter_enabled,
        brembo_enabled=settings.brembo_enabled,
        **({"knowledge": knowledge} if knowledge else {}),
    )
    from automotive.chat_log import ChatLogger
    from automotive.conversations import ConversationError, ConversationStore

    chat_log = ChatLogger.from_settings(settings)
    conversations = ConversationStore(chat_service, search_service, chat_log=chat_log,
                                      turn_seconds=settings.chat_turn_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        conversations.close()
        if callable(getattr(chat_service, "close", None)):
            chat_service.close()  # The assistant engine's kept connection.
        if chat_log:
            chat_log.close()  # Drains queued records before exit.
        if owns_search_service:
            search_service.close()

    app = FastAPI(title="Automotive intelligence", version="0.1.0", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.conversations = conversations
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    limiter = RateLimiter()

    @app.middleware("http")
    async def protections(request: Request, call_next: Any) -> Any:
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        # automotive.site_guard already limited and size-capped the garage site's own API.
        guarded = getattr(request.state, "site_guarded", False)
        if request.url.path.startswith("/api/") and not guarded:
            client = request.client.host if request.client else "unknown"
            # Turn polling only reads in-memory state; its own allowance keeps a few
            # multi-second AI turns from exhausting the general request budget.
            polling = (request.method == "GET" and "/turns/" in request.url.path
                       and request.url.path.startswith("/api/v1/chat/conversations/"))
            category, maximum = (("poll", settings.rate_limit_per_minute * 5) if polling
                                 else ("all", settings.rate_limit_per_minute))
            if not limiter.allow(client, category, maximum):
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            if request.url.path in {
                "/api/v1/chat", "/api/v1/vehicles/identify", "/api/v1/vehicles/part-codes",
                "/api/v1/vehicles/search",
            } or (request.method == "POST" and
                  request.url.path.startswith("/api/v1/chat/conversations/") and
                  request.url.path.endswith("/messages")):
                if not limiter.allow(client, "provider", settings.expensive_rate_limit_per_minute):
                    return JSONResponse({"detail": "Provider rate limit exceeded"}, status_code=429)
            content_length = request.headers.get("content-length")
            if content_length and (not content_length.isdigit() or int(content_length) > 1_100_000):
                return JSONResponse({"detail": "Request is too large"}, status_code=413)
            # Also enforce the cap on streamed/chunked bodies with no Content-Length.
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 1_100_000:
                    return JSONResponse({"detail": "Request is too large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        try:
            response = await call_next(request)
        except Exception:
            # Do not log provider messages, SQL credentials, request bodies or VINs.
            response = JSONResponse({"detail": "Service temporarily unavailable"}, status_code=503)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'; object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default includes input values, which can contain VINs and credentials.
        return JSONResponse({"detail": "Invalid request fields"}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid automotive request"}, status_code=422)

    @app.exception_handler(ConversationError)
    async def conversation_error(request: Request, exc: ConversationError) -> JSONResponse:
        return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status)

    @app.exception_handler(DatabaseNotConfigured)
    async def missing_database(request: Request, exc: DatabaseNotConfigured) -> JSONResponse:
        return JSONResponse({"detail": {"code": "DATABASE_NOT_CONFIGURED"}}, status_code=503)

    @app.exception_handler(ProviderUnavailable)
    async def missing_vin_provider(request: Request, exc: ProviderUnavailable) -> JSONResponse:
        return JSONResponse({"detail": {"code": "VIN_PROVIDER_UNAVAILABLE"}}, status_code=503)

    @app.exception_handler(MotoriumError)
    async def motorium_error(request: Request, exc: MotoriumError) -> JSONResponse:
        code = exc.code if exc.code in {
            "RATE_LIMITED", "ACCESS_REQUIRED", "PROVIDER_UNAVAILABLE", "INVALID_RESPONSE",
        } else "PROVIDER_UNAVAILABLE"
        return JSONResponse({"detail": {"code": "MOTORIUM_" + code}},
                            status_code=429 if code == "RATE_LIMITED" else 503)

    def operator(authorization: Annotated[str | None, Header()] = None) -> Any:
        expected = settings.operator_token
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise HTTPException(401, "Operator authentication required")
        if enrichment_repository is None:
            raise HTTPException(503, "Enrichment database is not configured")
        return enrichment_repository

    @app.get("/api/v1/health", response_model=StructuredResult)
    def health() -> dict[str, Any]:
        return {"status": "OK", "warnings": []}

    @app.get("/api/v1/status", response_model=ServiceStatus)
    def service_status() -> dict[str, bool]:
        # Configuration flags only: this does not assert provider/database health.
        return {
            "inventory_configured": bool(settings.database_url),
            "vin_configured": len(settings.vin_hmac_key.encode()) >= 32,
            "vin_cache_configured": bool(settings.database_url),
            "chat_configured": bool(settings.anthropic_api_key),
            "enrichment_configured": bool(
                settings.enrichment_database_url and settings.operator_token
            ),
            "provider_health_checked": False,
            "motorium_configured": settings.motorium_enabled,
            "fuchs_configured": settings.fuchs_enabled,
            "shell_configured": settings.shell_enabled,
            "pakelo_configured": settings.pakelo_enabled,
            "jsfilter_configured": settings.jsfilter_enabled,
            "catalog_api_configured": bool(settings.catalog_api_key),
        }

    @app.get("/api/v1/inventory/search", response_model=SearchResult)
    def search(
        code: Code,
        manufacturer: Annotated[str | None, Query(max_length=120)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> Any:
        return repository.search_parts(code, manufacturer=manufacturer, limit=limit)

    @app.get("/api/v1/inventory/products/{product_id}", response_model=CatalogProduct)
    def product(product_id: Annotated[str, PathParameter(max_length=200)]) -> Any:
        result = repository.get_product(product_id)
        if result is None:
            raise HTTPException(404, "Product not found")
        return result

    @app.post("/api/v1/inventory/vehicle", response_model=VehicleInventoryResult)
    def vehicle_inventory(payload: VehicleInventoryRequest) -> Any:
        from automotive.vehicle_inventory import VehicleInventoryService

        return VehicleInventoryService(repository).find(
            payload.vehicle.model_dump(exclude_none=True), limit=payload.limit,
        )

    @app.post("/api/v1/vehicles/identify", response_model=StructuredResult)
    def identify(payload: IdentifyRequest) -> Any:
        return vin_service.decode(payload.vin, model_year=payload.model_year)

    @app.post("/api/v1/vehicles/search", response_model=VINSearchResult, status_code=202)
    def start_vehicle_search(payload: VINSearchRequest) -> Any:
        try:
            return search_service.start(
                payload.vin, payload.part_query, payload.model_year,
                payload.configuration.model_dump(exclude_none=True)
                if payload.configuration else None,
            )
        except RuntimeError:
            raise HTTPException(429, detail={"code": "SEARCH_CAPACITY"}) from None

    @app.get("/api/v1/vehicles/search/{search_id}", response_model=VINSearchResult)
    def get_vehicle_search(
        search_id: Annotated[str, PathParameter(pattern=r"^[a-f0-9]{32}$")],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Any:
        result = search_service.get(search_id, offset, limit)
        if result is None:
            raise HTTPException(404, detail={"code": "SEARCH_EXPIRED"})
        return result

    @app.post("/api/v1/vehicles/part-codes", response_model=MotoriumResult)
    def motorium_part_codes(payload: MotoriumLookupRequest) -> Any:
        if not settings.motorium_enabled:
            raise HTTPException(503, detail={"code": "MOTORIUM_DISABLED"})
        return motorium_service.lookup(payload.vin, payload.part_query, limit=payload.limit)

    @app.get("/api/v1/parts/fitments", response_model=RecordsResult)
    def fitments(
        code: Code,
        make: Annotated[str | None, Query(max_length=100)] = None,
        model: Annotated[str | None, Query(max_length=100)] = None,
        model_year: Annotated[int | None, Query(ge=1980, le=2100)] = None,
        engine_code: Annotated[str | None, Query(max_length=100)] = None,
        market: Annotated[str | None, Query(max_length=100)] = None,
        fuel: Annotated[str | None, Query(max_length=100)] = None,
        transmission: Annotated[str | None, Query(max_length=100)] = None,
        body: Annotated[str | None, Query(max_length=100)] = None,
        drive: Annotated[str | None, Query(max_length=100)] = None,
    ) -> Any:
        vehicle = VehicleConfiguration(
            make=make, model=model, model_year=model_year, engine_code=engine_code, market=market,
            fuel=fuel, transmission=transmission, body=body, drive=drive,
        ).model_dump(exclude_none=True)
        return repository.fitments(code, vehicle=vehicle or None)

    @app.get("/api/v1/parts/cross-references", response_model=RecordsResult)
    def cross_references(code: Code) -> Any:
        return repository.cross_references(code)

    @app.get("/api/v1/parts/catalog", response_model=StructuredResult)
    def parts_catalog(
        code: Code,
        brand: Annotated[str | None, Query(max_length=120)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Any:
        from automotive.parts_catalog import PartsCatalogService

        return PartsCatalogService(repository).lookup(code, brand=brand, limit=limit)

    @app.get("/api/v1/parts/alternatives", response_model=StructuredResult)
    def parts_alternatives(
        code: Code,
        brand: Annotated[str | None, Query(max_length=120)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Any:
        from automotive.parts_catalog import PartsCatalogService

        return PartsCatalogService(repository).alternatives(code, brand=brand, limit=limit)

    @app.post("/api/v1/recommendations/oil", response_model=StructuredResult)
    def oil(payload: VehicleRequest) -> Any:
        from automotive.recommendations import RecommendationService

        return RecommendationService(repository).recommend_oil(
            payload.vehicle.model_dump(exclude_none=True)
        )

    @app.get("/api/v1/recommendations/oil/vehicles", response_model=StructuredResult)
    def oil_vehicles(product_id: Code, offset: Annotated[int, Query(ge=0)] = 0,
                     limit: Annotated[int, Query(ge=1, le=100)] = 50) -> Any:
        from automotive.recommendations import RecommendationService

        return RecommendationService(repository).oil_vehicles(
            product_id, offset=offset, limit=limit,
        )

    @app.get("/api/v1/evidence/{evidence_id}", response_model=StructuredResult)
    def evidence(evidence_id: str) -> Any:
        if len(evidence_id) > 100:
            raise HTTPException(422, "Invalid evidence identifier")
        result = repository.get_evidence(evidence_id)
        if result is None:
            raise HTTPException(404, "Evidence not found")
        return result

    @app.post("/api/v1/chat", response_model=StructuredResult)
    def chat(payload: ChatRequest) -> Any:
        return chat_service.respond(
            payload.message,
            vehicle=payload.vehicle.model_dump(exclude_none=True) if payload.vehicle else None,
            language=payload.language,
        )

    @app.post("/api/v1/chat/conversations", status_code=201)
    @app.post("/api/v1/chat/conversations/", status_code=201, include_in_schema=False)
    def create_conversation() -> dict:
        return conversations.create()

    @app.post("/api/v1/chat/conversations/{conversation_id}/messages", status_code=202)
    def conversation_message(
        conversation_id: Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{43}$")],
        payload: ConversationMessage,
    ) -> dict:
        return conversations.submit(
            conversation_id, payload.message, vin=payload.vin, language=payload.language,
            vehicle=payload.vehicle.model_dump(exclude_none=True) if payload.vehicle else None,
            **({"choice": payload.choice} if payload.choice is not None else {}),
        )

    @app.get("/api/v1/chat/conversations/{conversation_id}/turns/{turn_id}",
             response_model=ConversationTurn)
    def conversation_turn(
        conversation_id: Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{43}$")],
        turn_id: Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{32}$")],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict:
        return clamp_turn(conversations.get(conversation_id, turn_id, offset, limit))

    @app.delete("/api/v1/chat/conversations/{conversation_id}", status_code=204)
    def delete_conversation(
        conversation_id: Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{43}$")],
    ) -> None:
        conversations.delete(conversation_id)

    @app.post("/api/v1/operator/evidence/import", response_model=StructuredResult)
    def import_evidence(payload: ImportRequest, writer: Any = Depends(operator)) -> Any:
        from automotive.importer import parse_import

        records = parse_import(payload.content, payload.format)
        return writer.import_evidence(records)

    @app.post("/api/v1/operator/enrichment/jobs", response_model=JobResult, status_code=202)
    def create_job(payload: JobRequest, writer: Any = Depends(operator)) -> Any:
        # The repository transaction commits before this function returns HTTP 202.
        return writer.create_job(payload.kind, {})

    @app.get("/api/v1/operator/enrichment/jobs/{job_id}", response_model=JobResult)
    def get_job(job_id: str, writer: Any = Depends(operator)) -> Any:
        if len(job_id) > 100:
            raise HTTPException(422, "Invalid job identifier")
        result = writer.get_job(job_id)
        if result is None:
            raise HTTPException(404, "Job not found")
        return result

    # The staff workspace. "/" is the public landing page registered by install_site.
    @app.get("/staff", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    from automotive.site import install_site

    context = install_site(app, settings, repository=repository, vin_service=vin_service,
                           chat_service=chat_service, search_service=search_service, site=site)
    # The assistant engine searches the snapshot the site already holds in memory.
    if getattr(chat_service, "catalog_store", None) is None:
        with contextlib.suppress(AttributeError):
            chat_service.catalog_store = context.catalog_store
    # The services of the newer read tools: crawled fitments, web research, shop facts.
    if getattr(chat_service, "assistant_extras", None) is None:
        with contextlib.suppress(Exception):
            from automotive.assistant_extras import extras_for

            chat_service.assistant_extras = extras_for(settings, chat_service, repository,
                                                       context)
    return app
