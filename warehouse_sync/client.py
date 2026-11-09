"""Read-only, bounded HTTP client for the warehouse products endpoint."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Mapping

import requests

from warehouse_sync.config import Settings
from warehouse_sync.errors import AuthenticationError, SourceProtocolError

LOGGER = logging.getLogger(__name__)


class WarehouseClient:
    """Fetches product-list pages; it has no source-side write capability."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "text/html, */*; q=0.01",
                "User-Agent": "warehouse-sync/1.0 (+read-only)",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        if settings.cookie_header:
            self._session.headers["Cookie"] = settings.cookie_header
        elif settings.source_username and settings.source_password:
            self._session.auth = (settings.source_username, settings.source_password)

    def close(self) -> None:
        self._session.close()

    def fetch_products_page(self, page: int) -> str:
        """Return one positive page, rejecting redirects and excessive response bodies."""
        if page < 1:
            raise ValueError("page must be at least 1")

        params: Mapping[str, str | int] = {
            "SearchProductName": "",
            "SearchProductModel": "",
            "SearchProductCode": "",
            "SearchProductGroupCode": "",
            "SearchProductDubCode": "",
            "SearchProductBrand": "",
            "orderby": "id DESC",
            "page": page,
        }
        last_error: requests.RequestException | None = None
        for attempt in range(1, self._settings.max_retries + 1):
            retry_after = None
            try:
                with self._session.get(
                    self._settings.products_url,
                    params=params,
                    timeout=self._settings.timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        raise AuthenticationError("Source authentication was rejected.")
                    if response.status_code in {401, 403}:
                        raise AuthenticationError("Source authentication was rejected.")
                    if response.status_code in {408, 429, 500, 502, 503, 504}:
                        retry_after = response.headers.get("Retry-After")
                        raise requests.ConnectionError("Retryable source response")
                    if response.status_code != 200:
                        raise SourceProtocolError("Source returned an unexpected status.")
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "text/html" not in content_type:
                        raise SourceProtocolError("Source returned an unexpected content type.")
                    declared_size = response.headers.get("Content-Length")
                    if declared_size and int(declared_size) > self._settings.max_response_bytes:
                        raise SourceProtocolError(
                            "Source response exceeds the configured limit."
                        )
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=65_536):
                        body.extend(chunk)
                        if len(body) > self._settings.max_response_bytes:
                            raise SourceProtocolError(
                                "Source response exceeds the configured limit."
                            )
                    try:
                        return bytes(body).decode(response.encoding or "utf-8", errors="strict")
                    except UnicodeDecodeError as exc:
                        raise SourceProtocolError("Source response is not valid text.") from exc
            except (AuthenticationError, SourceProtocolError):
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < self._settings.max_retries:
                    delay = min(30.0, 2 ** (attempt - 1) + random.uniform(0, 0.5))  # nosec B311
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, min(float(retry_after), 60.0))
                    LOGGER.warning(
                        "Source request failed on page %s; retrying attempt %s/%s in %.1fs",
                        page, attempt + 1, self._settings.max_retries, delay,
                    )
                    time.sleep(delay)
                    continue
                break

        raise SourceProtocolError(
            f"Unable to fetch source page {page} after retries."
        ) from last_error
