"""AES-256-GCM for the Facebook access tokens the dashboard stores.

Stored form: ``v1.<nonce>.<ciphertext+tag>``, both parts unpadded base64url. The token's purpose
(``page``, ``user``, ``send_page``, ``ads_user``) is the associated data, so a stored page token
cannot be passed off as a login token or the other way round. The 32-byte key comes from
``META_TOKEN_ENCRYPTION_KEY`` and never reaches the database.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PURPOSES = frozenset({"page", "user", "send_page", "ads_user"})
_PREFIX = "v1"
_MAX_TOKEN = 2048


class TokenCipherError(ValueError):
    """Encryption is not configured, or a stored value was altered or belongs elsewhere."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_key_hex() -> str:
    """A fresh key for META_TOKEN_ENCRYPTION_KEY."""
    return os.urandom(32).hex()


class TokenCipher:
    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise TokenCipherError("META_TOKEN_ENCRYPTION_KEY must hold 32 bytes")
        self._aead = AESGCM(key)

    @staticmethod
    def _associated(purpose: str) -> bytes:
        if purpose not in PURPOSES:
            raise TokenCipherError("Unknown token purpose")
        return f"garage-meta-token:{purpose}".encode("ascii")

    def encrypt(self, token: str, *, purpose: str) -> str:
        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN:
            raise TokenCipherError("A token must be a non-empty string")
        nonce = os.urandom(12)
        sealed = self._aead.encrypt(nonce, token.encode("utf-8"), self._associated(purpose))
        return f"{_PREFIX}.{_b64(nonce)}.{_b64(sealed)}"

    def decrypt(self, stored: str, *, purpose: str) -> str:
        associated = self._associated(purpose)
        try:
            prefix, nonce_text, sealed_text = stored.split(".")
            if prefix != _PREFIX:
                raise ValueError
            nonce, sealed = _unb64(nonce_text), _unb64(sealed_text)
            if len(nonce) != 12:
                raise ValueError
            return self._aead.decrypt(nonce, sealed, associated).decode("utf-8")
        except (ValueError, InvalidTag, UnicodeDecodeError, AttributeError):
            raise TokenCipherError("The stored token cannot be decrypted with this key and "
                                   "purpose") from None
