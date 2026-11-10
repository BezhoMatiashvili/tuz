"""Wires the garage site into the automotive application.

``automotive.api.create_app`` calls ``install_site`` once. Everything public or staff-facing
that the site adds is registered here, and the guard is added last so it runs first.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from automotive.rules import Rules
from automotive.site_admin import build_admin_router
from automotive.site_auth import SiteAuth, build_auth_router
from automotive.site_catalog import Catalog, CatalogStore, build_catalog_router
from automotive.site_chat import build_chat_router, build_public_chat
from automotive.site_content import SiteContent
from automotive.site_guard import DailyBudget, SiteGuard
from automotive.site_pages import MediaCache, Pages, build_pages_router
from automotive.site_repository import SiteDatabaseNotConfigured, SiteRepository

LOGGER = logging.getLogger(__name__)
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


@dataclass
class SiteContext:
    repository: Any
    auth: SiteAuth
    content: SiteContent
    catalog_store: CatalogStore
    chat: Any
    pages: Pages


def check_serving(settings: Any) -> None:
    """Refuse to start in a shape that would publish the unauthenticated staff API, or
    put every visitor behind one rate limit."""
    public = [host for host in getattr(settings, "allowed_hosts", ()) if host not in LOOPBACK]
    if public and not getattr(settings, "staff_auth", False):
        raise ValueError(
            "The staff login cannot be switched off while a public host is allowed. "
            "Remove AUTOMOTIVE_STAFF_AUTH=false or the public origins.")
    if public and not getattr(settings, "trusted_proxies", ()):
        raise ValueError(
            "A public host needs AUTOMOTIVE_TRUSTED_PROXIES: without the proxy's address "
            "every visitor shares one rate limit.")


def install_site(app: FastAPI, settings: Any, *, repository: Any, vin_service: Any,
                 chat_service: Any, search_service: Any = None,
                 site: dict | None = None) -> SiteContext:
    """Register the site's routes and middleware. ``site`` carries test doubles:
    ``repository`` (SiteRepository-like), ``catalog`` (a Catalog), ``public_search`` and
    ``buckets``. ``search_service`` is the staff coordinator and is deliberately not reused
    for visitors."""
    overrides = site or {}
    origins = tuple(getattr(settings, "public_origins", ()) or ())
    site_repository = overrides.get("repository") or SiteRepository(
        getattr(settings, "site_database_url", ""))
    auth = SiteAuth(site_repository,
                    secure_cookies=any(origin.startswith("https://") for origin in origins))
    content = SiteContent(site_repository)
    rules = getattr(chat_service, "rules", None)
    catalog_store = CatalogStore(getattr(settings, "database_url", ""),
                                 rules if isinstance(rules, Rules) else None)
    if isinstance(overrides.get("catalog"), Catalog):
        catalog_store.replace(overrides["catalog"])

    chat = None
    key = str(getattr(settings, "vin_hmac_key", "") or "")
    if "public_search" in overrides or (getattr(settings, "anthropic_api_key", "")
                                        and len(key.encode()) >= 32):
        try:
            chat = build_public_chat(
                settings, chat_service, repository, vin_service, catalog_store,
                DailyBudget(getattr(settings, "site_chat_daily_turns", 300)),
                overrides.get("public_search"))
        except Exception:  # The site still serves pages and search without the assistant.
            LOGGER.warning("site_chat_unavailable")

    pages = Pages(content=content, catalog_store=catalog_store,
                  chat_available=lambda: chat is not None,
                  public_origin=origins[0] if origins else "")
    media = MediaCache(site_repository)

    @asynccontextmanager
    async def lifespan(_: Any):
        catalog_store.start()
        yield
        catalog_store.stop()
        if chat is not None:
            chat.close()

    # An empty router whose only job is to carry the lifespan into the application.
    app.include_router(APIRouter(lifespan=lifespan))
    app.include_router(build_pages_router(pages, media))
    app.include_router(build_catalog_router(
        catalog_store, vin_service,
        DailyBudget(getattr(settings, "site_vin_daily_decodes", 200))))
    app.include_router(build_chat_router(
        chat, lambda: bool(content.snapshot()["sections"]["chat"].get("enabled"))))
    app.include_router(build_auth_router(auth))
    app.include_router(build_admin_router(
        auth=auth, repository=site_repository, content=content, catalog_store=catalog_store,
        settings=settings, media_cache=media))

    @app.exception_handler(SiteDatabaseNotConfigured)
    async def site_database_missing(request: Request,
                                    exc: SiteDatabaseNotConfigured) -> JSONResponse:
        return JSONResponse({"detail": {"code": "SITE_NOT_CONFIGURED"}}, status_code=503)

    # Added last, so it is the outermost middleware and runs before ``protections``.
    app.add_middleware(SiteGuard, auth=auth, settings=settings,
                       buckets=overrides.get("buckets"))
    context = SiteContext(site_repository, auth, content, catalog_store, chat, pages)
    app.state.site = context
    return context
