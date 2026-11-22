"""Dashboard API under /api/admin.

The guard has already required a staff session, an allow-listed Origin and the CSRF header
for every route here. This module adds the role split: employees read inventory and the
overview without money figures; everything that changes the site or shows revenue is
admin-only. Bodies are validated by hand so the dashboard gets field codes, never values.
"""

from __future__ import annotations

import contextlib
import ipaddress
from typing import Annotated, Any

import anyio
import psycopg
from fastapi import APIRouter, Body, HTTPException, Query, Request, Response

from automotive import oil_catalog, prompt_library, site_media, site_reports
from automotive.repository import DatabaseNotConfigured
from automotive.site_auth import ROLES, USERNAME, hash_password, password_problem
from automotive.site_catalog import Catalog
from automotive.site_content import SECTIONS, clean, clean_service, describe_schema, missing_setup
from automotive.site_repository import Conflict, LastAdmin

Payload = Annotated[dict[str, Any], Body()]


def _fail(status: int, code: str, fields: dict | None = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code}
    if fields:
        detail["fields"] = fields
    return HTTPException(status, detail=detail)


def _user(request: Request) -> dict:
    user = getattr(request.state, "site_user", None)
    if not user:
        raise _fail(401, "NOT_SIGNED_IN")
    return user


def _admin(request: Request) -> dict:
    user = _user(request)
    if user["role"] != "admin":
        raise _fail(403, "ADMIN_ONLY")
    return user


def _address(request: Request) -> str | None:
    """The client address for the audit row, or None when it is not an IP address."""
    try:
        return str(ipaddress.ip_address(request.client.host if request.client else ""))
    except ValueError:
        return None


def _version(payload: dict) -> int:
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise _fail(422, "INVALID_FIELDS", {"version": "INVALID"})
    return version


def _ids(payload: dict) -> list[int]:
    ids = payload.get("ids")
    if (not isinstance(ids, list) or len(ids) > 500
            or any(isinstance(i, bool) or not isinstance(i, int) for i in ids)):
        raise _fail(422, "INVALID_FIELDS", {"ids": "INVALID"})
    return ids


def _media_row(row: dict) -> dict:
    return {**{key: row[key] for key in ("media_id", "sha256", "content_type", "width",
                                         "height", "alt_ka", "alt_en", "position",
                                         "is_published", "byte_size")},
            "url": f"/api/admin/media/file/{row['sha256']}"}


def _prompt_meta(item: prompt_library.Prompt, row: dict | None) -> dict:
    return {"key": item.key, "group": item.group, "title": item.title, "about": item.about,
            "editable": item.editable, "edited": row is not None,
            "version": row["version"] if row else 0,
            "updated_at": row["updated_at"] if row else None,
            "updated_by": row["updated_by"] if row else ""}


def build_admin_router(*, auth: Any, repository: Any, content: Any, catalog_store: Any,
                       settings: Any, media_cache: Any = None,
                       prompts: Any = None, inventory_repository: Any = None) -> APIRouter:
    router = APIRouter(prefix="/api/admin", include_in_schema=False)

    def changed(user: dict, request: Request, action: str, target: str = "",
                detail: dict | None = None) -> None:
        # The write is already stored, so the caches go first: if the audit insert fails,
        # the editor must not keep reading (and conflicting with) the old version.
        content.invalidate()
        if media_cache is not None:
            media_cache.clear()
        repository.audit(user, action, target, detail, _address(request))

    # -- overview and inventory (both roles) ---------------------------------------------

    @router.get("/overview")
    def overview(request: Request) -> dict:
        user = _user(request)
        inventory = dict(site_reports.inventory_overview(catalog_store.current()))
        if user["role"] != "admin":
            inventory.pop("stock_value", None)
        result = {"user": user, "inventory": inventory,
                  "catalog_ready": catalog_store.loaded,
                  "sync": site_reports.sync_health(repository)}
        if user["role"] == "admin":
            result["setup"] = missing_setup(content.snapshot())
        return result

    @router.get("/inventory")
    def inventory(request: Request, q: Annotated[str, Query(max_length=80)] = "",
                  brand: Annotated[str, Query(max_length=80)] = "",
                  make: Annotated[str, Query(max_length=40)] = "",
                  state: Annotated[str, Query(pattern="^(all|available|out)$")] = "all",
                  category: Annotated[str, Query(pattern="^(all|engine_oil)$")] = "all",
                  page: Annotated[int, Query(ge=1, le=2000)] = 1) -> dict:
        _user(request)
        catalog = catalog_store.current()
        oil_labels = None
        if category == "engine_oil":
            if inventory_repository is None:
                raise _fail(503, "DATA_UNAVAILABLE")
            try:
                reviewed = oil_catalog.read_review(inventory_repository)
            except (DatabaseNotConfigured, psycopg.Error):
                raise _fail(503, "DATA_UNAVAILABLE") from None
            oils = []
            oil_labels = {}
            for product, label in reviewed:
                if label["review_status"] == "OTHER_PRODUCT":
                    continue
                oils.append({**product, "stock_store": label["stock_store"],
                             "stock_tuz11": label["stock_tuz11"]})
                oil_labels[label["product_id"]] = {
                    "grade": label["sae_grade_from_catalog"],
                    "status": label["review_status"],
                    "catalog_specs": label["specs_stated_in_catalog"],
                    "reviewed_approvals": label["verified_approvals"],
                }
            catalog = Catalog.from_rows(oils, catalog.rules)
        return {**catalog.inventory(query=q, brand=brand.strip() or None,
                                    make=make.strip().lower() or None, state=state, page=page,
                                    oil_labels=oil_labels),
                "options": catalog.options()}

    # -- admin-only reports --------------------------------------------------------------

    @router.get("/reports/sales")
    def sales(request: Request, days: Annotated[int, Query(ge=7, le=365)] = 30) -> dict:
        _admin(request)
        return site_reports.trade_report(repository, catalog_store.current(), days)

    @router.get("/reports/chat")
    def chat(request: Request, days: Annotated[int, Query(ge=7, le=365)] = 30) -> dict:
        _admin(request)
        return site_reports.chat_report(settings, days)

    @router.get("/audit")
    def audit(request: Request,
              before: Annotated[int | None, Query(ge=1)] = None) -> dict:
        _admin(request)
        return {"rows": repository.audit_rows(50, before)}

    # -- content -------------------------------------------------------------------------

    @router.get("/content")
    def read_content(request: Request) -> dict:
        _admin(request)
        snapshot = content.snapshot()
        return {"schema": describe_schema(), "source": snapshot["source"],
                "sections": {key: {"document": snapshot["sections"][key],
                                   "version": snapshot["versions"][key]} for key in SECTIONS}}

    @router.put("/content/{key}")
    def save_content(key: str, payload: Payload, request: Request) -> dict:
        user = _admin(request)
        if key not in SECTIONS:
            raise _fail(404, "UNKNOWN_SECTION")
        document, errors = clean(key, payload.get("document"))
        if errors:
            raise _fail(422, "INVALID_FIELDS", errors)
        try:
            version = repository.save_section(key, document, _version(payload),
                                              user["user_id"])
        except Conflict:
            raise _fail(409, "VERSION_CONFLICT") from None
        changed(user, request, "content.saved", key)
        return {"version": version, "document": document}

    # -- services ------------------------------------------------------------------------

    @router.get("/services")
    def services(request: Request) -> dict:
        _admin(request)
        return {"services": content.snapshot()["services"]}

    @router.post("/services", status_code=201)
    def create_service(payload: Payload, request: Request) -> dict:
        user = _admin(request)
        fields, errors = clean_service(payload)
        if errors:
            raise _fail(422, "INVALID_FIELDS", errors)
        row = repository.create_service(fields)
        changed(user, request, "service.created", str(row["service_id"]))
        return row

    @router.put("/services/{service_id}")
    def update_service(service_id: int, payload: Payload, request: Request) -> dict:
        user = _admin(request)
        fields, errors = clean_service(payload)
        if errors:
            raise _fail(422, "INVALID_FIELDS", errors)
        try:
            row = repository.update_service(service_id, fields, _version(payload))
        except Conflict:
            raise _fail(409, "VERSION_CONFLICT") from None
        if row is None:
            raise _fail(404, "NOT_FOUND")
        changed(user, request, "service.saved", str(service_id))
        return row

    @router.delete("/services/{service_id}", status_code=204)
    def delete_service(service_id: int, request: Request) -> None:
        user = _admin(request)
        if not repository.delete_service(service_id):
            raise _fail(404, "NOT_FOUND")
        changed(user, request, "service.deleted", str(service_id))

    @router.post("/services/order", status_code=204)
    def order_services(payload: Payload, request: Request) -> None:
        user = _admin(request)
        repository.reorder("services", _ids(payload))
        changed(user, request, "service.reordered")

    # -- gallery -------------------------------------------------------------------------

    @router.get("/media")
    def media(request: Request) -> dict:
        _admin(request)
        return {"media": [_media_row(row) for row in content.snapshot()["media"]]}

    @router.get("/media/file/{sha256}")
    def media_file(sha256: str, request: Request) -> Response:
        _admin(request)
        row = repository.media_bytes(sha256.lower(), published_only=False) \
            if len(sha256) == 64 and sha256.isalnum() else None
        if not row:
            raise _fail(404, "NOT_FOUND")
        return Response(bytes(row["data"]), media_type=row["content_type"])

    @router.post("/media", status_code=201)
    async def upload(request: Request) -> dict:
        user = _admin(request)
        described = site_media.describe(await request.body())
        if not described:
            raise _fail(422, "INVALID_IMAGE")
        row = await anyio.to_thread.run_sync(
            lambda: repository.add_media(**described, actor_id=user["user_id"]))
        await anyio.to_thread.run_sync(
            lambda: changed(user, request, "media.uploaded", described["sha256"][:16]))
        return _media_row(row)

    @router.patch("/media/{media_id}")
    def update_media(media_id: int, payload: Payload, request: Request) -> dict:
        user = _admin(request)
        fields: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name in ("alt_ka", "alt_en"):
            if name in payload:
                value = payload[name]
                if not isinstance(value, str):
                    errors[name] = "INVALID"
                elif len(value) > 160:
                    errors[name] = "TOO_LONG"
                else:
                    fields[name] = " ".join(value.split())
        if "is_published" in payload:
            if not isinstance(payload["is_published"], bool):
                errors["is_published"] = "INVALID"
            fields["is_published"] = payload["is_published"]
        if errors or not fields:
            raise _fail(422, "INVALID_FIELDS", errors or {"": "REQUIRED"})
        row = repository.update_media(media_id, fields)
        if row is None:
            raise _fail(404, "NOT_FOUND")
        changed(user, request, "media.saved", str(media_id))
        return _media_row(row)

    @router.delete("/media/{media_id}", status_code=204)
    def delete_media(media_id: int, request: Request) -> None:
        user = _admin(request)
        if not repository.delete_media(media_id):
            raise _fail(404, "NOT_FOUND")
        changed(user, request, "media.deleted", str(media_id))

    @router.post("/media/order", status_code=204)
    def order_media(payload: Payload, request: Request) -> None:
        user = _admin(request)
        repository.reorder("media", _ids(payload))
        changed(user, request, "media.reordered")

    # -- accounts ------------------------------------------------------------------------

    @router.get("/users")
    def users(request: Request) -> dict:
        _admin(request)
        return {"users": repository.list_users()}

    @router.post("/users", status_code=201)
    def create_user(payload: Payload, request: Request) -> dict:
        user = _admin(request)
        name = str(payload.get("username") or "").strip().lower()
        role, password = payload.get("role"), payload.get("password")
        display = payload.get("display_name", "")
        errors = {}
        if not USERNAME.fullmatch(name):
            errors["username"] = "INVALID"
        if role not in ROLES:
            errors["role"] = "INVALID"
        if not isinstance(display, str) or len(display) > 80:
            errors["display_name"] = "INVALID"
        problem = password_problem(password, name) if isinstance(password, str) else "REQUIRED"
        if problem:
            errors["password"] = problem
        if errors:
            raise _fail(422, "INVALID_FIELDS", errors)
        try:
            row = repository.create_user(name, " ".join(display.split()), role,
                                         hash_password(password))
        except Conflict:
            raise _fail(409, "USERNAME_TAKEN", {"username": "TAKEN"}) from None
        changed(user, request, "user.created", name, {"role": role})
        return row

    @router.patch("/users/{user_id}")
    def update_user(user_id: int, payload: Payload, request: Request) -> dict:
        user = _admin(request)
        fields: dict[str, Any] = {}
        errors = {}
        if "display_name" in payload:
            value = payload["display_name"]
            if not isinstance(value, str) or len(value) > 80:
                errors["display_name"] = "INVALID"
            else:
                fields["display_name"] = " ".join(value.split())
        if "role" in payload:
            if payload["role"] not in ROLES:
                errors["role"] = "INVALID"
            fields["role"] = payload["role"]
        if "is_active" in payload:
            if not isinstance(payload["is_active"], bool):
                errors["is_active"] = "INVALID"
            fields["is_active"] = payload["is_active"]
        if errors or not fields:
            raise _fail(422, "INVALID_FIELDS", errors or {"": "REQUIRED"})
        try:
            row = repository.update_user(user_id, fields)
        except LastAdmin:
            raise _fail(409, "LAST_ADMIN") from None
        if row is None:
            raise _fail(404, "NOT_FOUND")
        auth.forget(user_id=user_id)
        changed(user, request, "user.saved", row["username"],
                {key: fields[key] for key in ("role", "is_active") if key in fields})
        return row

    @router.post("/users/{user_id}/password", status_code=204)
    def set_password(user_id: int, payload: Payload, request: Request) -> None:
        user = _admin(request)
        target = next((row for row in repository.list_users()
                       if row["user_id"] == user_id), None)
        if target is None:
            raise _fail(404, "NOT_FOUND")
        password = payload.get("password")
        problem = (password_problem(password, target["username"])
                   if isinstance(password, str) else "REQUIRED")
        if problem:
            raise _fail(422, "INVALID_FIELDS", {"password": problem})
        repository.set_password(user_id, hash_password(password))
        auth.forget(user_id=user_id)
        changed(user, request, "user.password_reset", target["username"])

    @router.post("/users/{user_id}/sessions/revoke", status_code=204)
    def revoke_sessions(user_id: int, request: Request) -> None:
        user = _admin(request)
        repository.delete_user_sessions(user_id)
        auth.forget(user_id=user_id)
        name = next((row["username"] for row in repository.list_users()
                     if row["user_id"] == user_id), str(user_id))
        changed(user, request, "user.sessions_revoked", name)

    # -- prompts (every text sent to a language model; the live bot's are editable) ----------

    def prompt_rows() -> tuple[dict, str]:
        """The stored edits and the store's state: ready, unavailable (migration 0019 not
        applied or the database down: the packaged texts apply) or off (no login)."""
        if prompts is None or not getattr(prompts, "configured", False):
            return {}, "off"
        try:
            return prompts.rows(), "ready"
        except psycopg.Error:
            return {}, "unavailable"

    def known(key: str) -> prompt_library.Prompt:
        item = prompt_library.BY_KEY.get(key)
        if item is None:
            raise _fail(404, "NOT_FOUND")
        return item

    def writable(key: str) -> prompt_library.Prompt:
        item = known(key)
        if not item.editable:
            raise _fail(409, "PROMPT_READ_ONLY")
        if prompts is None or not getattr(prompts, "configured", False):
            raise _fail(503, "PROMPTS_UNAVAILABLE")
        return item

    def prompt_changed(user: dict, request: Request, action: str, key: str,
                       detail: dict | None = None) -> None:
        repository.audit(user, action, key, detail, _address(request))

    @router.get("/prompts")
    def list_prompts(request: Request) -> dict:
        _admin(request)
        rows, state = prompt_rows()
        return {"store": state, "max_chars": prompt_library.MAX_CHARS,
                "groups": [{"key": key, **group} for key, group in prompt_library.GROUPS.items()],
                "prompts": [_prompt_meta(item, rows.get(item.key) if item.editable else None)
                            for item in prompt_library.PROMPTS]}

    @router.get("/prompts/{key}")
    def read_prompt(key: str, request: Request) -> dict:
        _admin(request)
        item = known(key)
        rows, state = prompt_rows()
        row = rows.get(key) if item.editable else None
        default = prompt_library.default_text(key)
        history: list[dict] = []
        if item.editable and state == "ready":
            with contextlib.suppress(psycopg.Error):
                history = prompts.history(key)
        return {**_prompt_meta(item, row), "store": state,
                "max_chars": prompt_library.MAX_CHARS, "default": default,
                "text": row["body"] if row else default, "history": history}

    @router.put("/prompts/{key}")
    def save_prompt(key: str, payload: Payload, request: Request) -> dict:
        user = _admin(request)
        writable(key)
        version = _version(payload)
        text, problem = prompt_library.clean_text(payload.get("text"))
        if problem:
            raise _fail(422, "INVALID_FIELDS", {"text": problem})
        try:
            if text == prompt_library.default_text(key):
                # The packaged text again: no edit is kept, so a later release's text applies.
                if version and prompts.reset(key, version, user["username"]):
                    prompt_changed(user, request, "prompt.reset", key)
                return {"version": 0, "edited": False, "text": text}
            saved = prompts.save(key, text, version, user["username"])
        except prompt_library.PromptConflict:
            raise _fail(409, "VERSION_CONFLICT") from None
        except psycopg.Error:
            raise _fail(503, "PROMPTS_UNAVAILABLE") from None
        prompt_changed(user, request, "prompt.saved", key, {"chars": len(text)})
        return {"version": saved, "edited": True, "text": text}

    @router.post("/prompts/{key}/reset")
    def reset_prompt(key: str, payload: Payload, request: Request) -> dict:
        user = _admin(request)
        writable(key)
        version = _version(payload)
        try:
            if prompts.reset(key, version, user["username"]):
                prompt_changed(user, request, "prompt.reset", key)
        except prompt_library.PromptConflict:
            raise _fail(409, "VERSION_CONFLICT") from None
        except psycopg.Error:
            raise _fail(503, "PROMPTS_UNAVAILABLE") from None
        return {"version": 0, "edited": False, "text": prompt_library.default_text(key)}

    return router
