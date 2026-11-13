"""What one Facebook operation needs: settings, the store, the Graph client and the cipher.

Built once per process: the dashboard with the site login, the worker with its own. Tokens are
read from the database on every call, never cached, so a reconnect or a disconnect made in the
dashboard reaches a running worker at its next send.

Which token a call uses is decided here, in one place:
- ``page_token()`` reads and acts for the page (always minted through our app);
- ``send_token()`` sends to customers: the optional send-only page token minted through another
  app (the fallback when Meta gates a new app) when one is stored, else the page token;
- ``user_token()`` / ``ads_token()`` read stats and ads: a separate ads app's login when stored,
  else the owner's long-lived login.
The two that may come from another app return a ``TokenChoice``, which tells the Graph client
whether ``appsecret_proof`` may be added (our secret would make Meta reject another app's token).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from automotive.meta_config import MetaSettings
from automotive.meta_crypto import TokenCipher
from automotive.meta_db import MetaDatabase
from automotive.meta_graph import GraphClient, TokenChoice
from automotive.meta_store import MetaStore

__all__ = ["MetaContext", "NotConnected", "TokenChoice", "build_context", "utc_now"]


class NotConnected(RuntimeError):
    """No page, or no token for this purpose, is connected yet."""


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class MetaContext:
    settings: MetaSettings
    store: MetaStore
    graph: GraphClient
    cipher: TokenCipher | None = None
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = utc_now

    @property
    def page_id(self) -> str:
        """The connected page's id, read fresh; NotConnected when there is none."""
        page = self.store.page()
        if not page:
            raise NotConnected("No Facebook page is connected")
        return page["page_id"]

    def _token(self, purpose: str) -> str:
        token = self.store.token(purpose)
        if not token:
            raise NotConnected(f"No {purpose} token is stored; connect the page first")
        return token

    def page_token(self) -> str:
        return self._token("page")

    def user_token(self) -> str:
        return self._token("user")

    def _choice(self, preferred: str, fallback: str) -> TokenChoice:
        rows = {row["purpose"]: row for row in self.store.token_rows()}
        purpose = preferred if preferred in rows else fallback
        if purpose not in rows:
            raise NotConnected(f"No {fallback} token is stored; connect the page first")
        return TokenChoice(self._token(purpose), rows[purpose]["app_id"] == self.settings.app_id,
                           purpose)

    def send_token(self) -> TokenChoice:
        """For the Send API: ``token, proof = ctx.send_token()``; ``.purpose`` names the row
        to mark broken when Meta reports the token dead."""
        return self._choice("send_page", "page")

    def ads_token(self) -> TokenChoice:
        """For ads reads; pass it straight to ``graph.get``/``graph.paged`` as the token."""
        return self._choice("ads_user", "user")


def build_context(settings: MetaSettings, database_url: str, *, graph: GraphClient | None = None,
                  application_name: str = "garage-meta") -> MetaContext:
    """The context for one login. Without META_TOKEN_ENCRYPTION_KEY the cipher is None and
    any token read or write raises TokenCipherError."""
    cipher = TokenCipher(settings.token_key) if settings.token_key else None
    store = MetaStore(MetaDatabase(database_url, application_name=application_name), cipher)
    return MetaContext(settings, store, graph if graph is not None else GraphClient(settings),
                       cipher)
