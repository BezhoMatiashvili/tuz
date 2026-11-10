"""Server-rendered pages and static assets for the garage site.

Pages render from the in-memory content snapshot with autoescape on and no inline script
or style (the CSP forbids both). Assets are served under ``/assets/<build>/...`` where the
build id changes whenever a file changes, so they can be cached forever and every module
has exactly one URL.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from automotive.site_content import localized
from automotive.site_strings import STRINGS

ROOT = Path(__file__).parent
TEMPLATES = ROOT / "templates" / "site"
ASSET_ROOTS = {"site": ROOT / "static" / "site", "admin": ROOT / "static" / "admin"}
TYPES = {
    ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml", ".json": "application/json", ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp", ".avif": "image/avif", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
}
COMPRESSIBLE = {".js", ".css", ".svg", ".json", ".txt"}
# Records the build scripts keep beside the assets. No page asks for them.
BUILD_RECORDS = {"ASSETS.json", "FONTS.json", "VENDOR.json", "manifest.json"}
GALLERY = "img/gallery/"
LANGUAGES = ("ka", "en")


class Assets:
    """Files under static/site and static/admin, with a content-derived build id."""

    def __init__(self, roots: dict[str, Path] | None = None) -> None:
        self.roots = roots or ASSET_ROOTS
        self._lock = Lock()
        self._build, self._checked = "", 0.0
        self._index: dict[str, Path] = {}
        self._cache: dict[str, tuple[int, bytes, bytes | None, str]] = {}

    def _files(self) -> list[tuple[str, Path]]:
        found = []
        for name, root in self.roots.items():
            if not root.is_dir():
                continue
            base = root.resolve()
            shown = self._publishable(root)
            for path in root.rglob("*"):
                relative = path.relative_to(root).as_posix()
                # Never served: the dev harness, the build scripts' records, a packaged
                # photo the manifest does not mark as publishable (those reach the site
                # only through the gallery, once the owner publishes them), and a link
                # that points outside the asset folder.
                if (path.is_file() and path.suffix.lower() in TYPES
                        and not relative.startswith("dev/")
                        and path.name not in BUILD_RECORDS
                        and (not relative.startswith(GALLERY) or path.name in shown)
                        and base in path.resolve().parents):
                    found.append((f"{name}/{relative}", path))
        return sorted(found)

    @staticmethod
    def _publishable(root: Path) -> set[str]:
        """File names the packaged gallery manifest marks as fit to show."""
        try:
            entries = json.loads((root / GALLERY / "manifest.json").read_text("utf-8"))
            return {str(entry["file"]) for entry in entries if entry.get("published")}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return set()

    def build(self) -> str:
        """Changes whenever any asset's size or modification time does."""
        with self._lock:
            now = time.monotonic()
            if self._build and now - self._checked < 1.0:
                return self._build
            digest, index = hashlib.sha256(), {}
            for key, path in self._files():
                try:
                    stat = path.stat()
                except OSError:  # Removed while the folder was being listed.
                    continue
                digest.update(f"{key}:{stat.st_size}:{stat.st_mtime_ns};".encode())
                index[key] = path
            for key in [key for key in self._cache if key not in index]:
                del self._cache[key]  # A file that went away gives its bytes back.
            self._index = index
            self._build, self._checked = digest.hexdigest()[:12], now
            return self._build

    def url(self, path: str) -> str:
        return f"/assets/{self.build()}/{path}"

    def exists(self, path: str) -> bool:
        return self._resolve(path) is not None

    def _resolve(self, relative: str) -> Path | None:
        """Only a path spelled exactly as the folder lists it. ``site/./app.js`` and every
        other alias is refused, so a file has one URL and one cache entry, the cache cannot
        grow past the number of files, and the ``dev/`` rule cannot be stepped around."""
        self.build()  # Keeps the index at most a second old.
        return self._index.get(relative)

    def read(self, relative: str) -> tuple[bytes, bytes | None, str] | None:
        """(bytes, gzipped bytes or None, content type) for a served file."""
        path = self._resolve(relative)
        if path is None:
            return None
        try:
            stamp = path.stat().st_mtime_ns
            with self._lock:
                cached = self._cache.get(relative)
                if cached and cached[0] == stamp:
                    return cached[1], cached[2], cached[3]
            raw = path.read_bytes()
        except OSError:  # Removed since the folder was last listed.
            return None
        packed = (gzip.compress(raw, 9, mtime=0)
                  if path.suffix.lower() in COMPRESSIBLE and len(raw) > 512 else None)
        with self._lock:
            self._cache[relative] = (stamp, raw, packed, TYPES[path.suffix.lower()])
        return raw, packed, TYPES[path.suffix.lower()]


class MediaCache:
    """Published gallery images by content hash, kept in memory up to a byte budget."""

    MISS_SECONDS = 60.0
    ERROR_SECONDS = 5.0  # after a failed read, before the database is asked again

    def __init__(self, repository: Any, budget: int = 24 * 1024 * 1024,
                 clock: Any = time.monotonic) -> None:
        self.repository, self.budget, self.clock = repository, budget, clock
        self._items: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        # Unknown or unpublished hashes, so guessing URLs cannot query the database
        # on every request. Cleared with the cache when the dashboard changes media.
        self._misses: OrderedDict[str, float] = OrderedDict()
        self._lock = Lock()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._misses.clear()

    def get(self, sha256: str) -> tuple[str, bytes] | None:
        with self._lock:
            if sha256 in self._items:
                self._items.move_to_end(sha256)
                return self._items[sha256]
            if self._misses.get(sha256, 0.0) > self.clock():
                return None
        if self.repository is None or not getattr(self.repository, "configured", False):
            return None
        try:
            row = self.repository.media_bytes(sha256, published_only=True)
        except Exception:  # A database blip is a missing image, not an error page.
            # Remembered briefly, so an outage is not met with a query per request.
            with self._lock:
                self._misses[sha256] = self.clock() + self.ERROR_SECONDS
            return None
        if not row:
            with self._lock:
                self._misses[sha256] = self.clock() + self.MISS_SECONDS
                while len(self._misses) > 2000:
                    self._misses.popitem(last=False)
            return None
        item = (row["content_type"], bytes(row["data"]))
        with self._lock:
            self._items[sha256] = item
            while sum(len(data) for _, data in self._items.values()) > self.budget:
                self._items.popitem(last=False)
        return item


def _accepts_gzip(request: Request) -> bool:
    return "gzip" in request.headers.get("accept-encoding", "")


def _number(value: Any) -> str:
    """12345 -> `12 345` with a non-breaking space."""
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return ""


def _tel(value: str) -> str:
    return re.sub(r"[^0-9+]", "", value or "")


class Pages:
    """Renders templates; one instance per application."""

    def __init__(self, *, content: Any, catalog_store: Any, assets: Assets | None = None,
                 chat_available: Any = lambda: False, public_origin: str = "") -> None:
        self.content, self.catalog_store = content, catalog_store
        self.assets = assets or Assets()
        self.chat_available = chat_available
        self.public_origin = public_origin.rstrip("/")
        self.environment = Environment(
            loader=FileSystemLoader(str(TEMPLATES)), autoescape=True,
            undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True, auto_reload=True)
        self.environment.filters["num"] = _number
        self.environment.filters["tel"] = _tel
        self.environment.globals["asset"] = self.assets.url

    def render(self, template: str, request: Request, **context: Any) -> Response:
        html = self.environment.get_template(template).render(**context)
        body = html.encode("utf-8")
        headers = {"x-site-cache": context.get("cache", ""), "vary": "Accept-Encoding"}
        if _accepts_gzip(request) and len(body) > 1024:
            body = gzip.compress(body, 5)
            headers["content-encoding"] = "gzip"
        return HTMLResponse(body, headers=headers)

    def _gallery(self, view: dict, snapshot: dict, language: str) -> list[dict]:
        photos = [{"src": f"/media/{row['sha256']}", "width": row["width"],
                   "height": row["height"], "alt": row["alt"]} for row in view["photos"]]
        if photos or snapshot["source"] != "defaults":
            return photos
        # No site database yet: show the packaged photos marked as publishable.
        manifest = ASSET_ROOTS["site"] / "img" / "gallery" / "manifest.json"
        if not manifest.is_file():
            return []
        return [{"src": self.assets.url(f"site/img/gallery/{entry['file']}"),
                 "width": entry.get("width", 960), "height": entry.get("height", 1280),
                 "alt": entry.get(f"alt_{language}", "")}
                for entry in json.loads(manifest.read_text(encoding="utf-8"))
                if entry.get("published")]

    def landing(self, request: Request, language: str) -> Response:
        snapshot = self.content.snapshot()
        view = localized(snapshot, language)
        catalog = self.catalog_store.current()
        strings = STRINGS[language]
        chat_on = bool(view["chat"].get("enabled")) and self.chat_available()
        data = {
            "lang": language,
            "currency": view["brand"].get("currency") or "₾",
            "inStock": len(catalog.available) if self.catalog_store.loaded else None,
            "chat": {"enabled": chat_on, "greeting": view["chat"].get("greeting", "")},
            "strings": {key[3:]: value for key, value in strings.items()
                        if key.startswith("js.")},
        }
        origin = self.public_origin or str(request.base_url).rstrip("/")
        return self.render(
            "landing.html", request, cache="revalidate", lang=language, t=strings, c=view,
            services=view["services"], gallery=self._gallery(view, snapshot, language),
            in_stock=data["inStock"], chat_on=chat_on, data=data,
            year=time.localtime().tm_year,
            canonical=origin + ("/" if language == "ka" else "/en"),
            alternates={"ka": origin + "/", "en": origin + "/en"})

    def simple(self, request: Request, template: str, language: str = "ka",
               **context: Any) -> Response:
        return self.render(template, request, lang=language, t=STRINGS[language], **context)


def build_pages_router(pages: Pages, media: MediaCache) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.api_route("/", methods=["GET", "HEAD"])
    def home(request: Request) -> Response:
        return pages.landing(request, "ka")

    @router.api_route("/en", methods=["GET", "HEAD"])
    def home_en(request: Request) -> Response:
        return pages.landing(request, "en")

    @router.api_route("/login", methods=["GET", "HEAD"])
    def login(request: Request) -> Response:
        wanted = request.query_params.get("next", "admin")
        return pages.simple(request, "login.html",
                            data={"next": wanted if wanted in ("admin", "staff") else "admin",
                                  "strings": {key[6:]: value for key, value in
                                              STRINGS["ka"].items()
                                              if key.startswith("login.")},
                                  "stringsEn": {key[6:]: value for key, value in
                                                STRINGS["en"].items()
                                                if key.startswith("login.")}})

    @router.api_route("/admin", methods=["GET", "HEAD"])
    @router.api_route("/admin/{rest:path}", methods=["GET", "HEAD"])
    def admin(request: Request, rest: str = "") -> Response:
        return pages.simple(request, "admin.html", data={})

    @router.api_route("/assets/{build}/{path:path}", methods=["GET", "HEAD"])
    def asset(build: str, path: str, request: Request) -> Response:
        found = pages.assets.read(path)
        if found is None:
            return PlainTextResponse("Not found", status_code=404)
        raw, packed, content_type = found
        headers = {"vary": "Accept-Encoding"}
        if build == pages.assets.build():
            headers["x-site-cache"] = "immutable"
        if packed is not None and _accepts_gzip(request):
            headers["content-encoding"] = "gzip"
            raw = packed
        return Response(raw, media_type=content_type, headers=headers)

    @router.api_route("/media/{sha256}", methods=["GET", "HEAD"])
    def media_file(sha256: str) -> Response:
        # Only a hash the content snapshot lists as published reaches the image store, so
        # a guessed address costs a set look-up and never a database query.
        published = {row["sha256"] for row in pages.content.snapshot()["media"]
                     if row.get("is_published")}
        found = media.get(sha256) if sha256 in published else None
        if found is None:
            return PlainTextResponse("Not found", status_code=404)
        # An hour, not for ever: a photo taken off the site has to stop being shown.
        return Response(found[1], media_type=found[0], headers={"x-site-cache": "hour"})

    @router.get("/robots.txt")
    def robots(request: Request) -> Response:
        origin = pages.public_origin or str(request.base_url).rstrip("/")
        # Staff paths are simply not listed; naming them here would advertise them.
        return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n",
                                 headers={"x-site-cache": "short"})

    @router.get("/sitemap.xml")
    def sitemap(request: Request) -> Response:
        origin = pages.public_origin or str(request.base_url).rstrip("/")
        urls = "".join(f"<url><loc>{origin}{path}</loc></url>" for path in ("/", "/en"))
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
            media_type="application/xml", headers={"x-site-cache": "short"})

    @router.get("/favicon.ico")
    def favicon() -> Response:
        found = pages.assets.read("site/img/favicon.ico")
        if found is None:
            return PlainTextResponse("Not found", status_code=404)
        return Response(found[0], media_type=found[2], headers={"x-site-cache": "short"})

    return router
