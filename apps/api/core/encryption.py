"""Symmetric encryption helpers for secrets at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the ``cryptography`` library.
The encryption key is sourced from the ``WEBHOOK_ENCRYPTION_KEY`` setting.
If not configured, a deterministic key is derived from ``JWT_SECRET`` via
PBKDF2-SHA256 (sufficient for development, but a dedicated key is
recommended for production).

Encrypted values are prefixed with ``enc:`` so the code can distinguish
encrypted vs plaintext values during migration.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTED_PREFIX = "enc:"


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Return a cached Fernet instance.  Lazily initialised from settings."""
    from core.config import get_settings
    settings = get_settings()

    raw_key = settings.webhook_encryption_key
    if not raw_key:
        # Derive a deterministic Fernet key from jwt_secret via PBKDF2
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            settings.jwt_secret.encode(),
            b"crowdsorcerer-webhook-encryption",
            100_000,
            dklen=32,
        )
        raw_key = base64.urlsafe_b64encode(dk).decode()

    return Fernet(raw_key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext secret.  Returns ``enc:<ciphertext>``."""
    f = _get_fernet()
    ct = f.encrypt(plaintext.encode()).decode()
    return f"{ENCRYPTED_PREFIX}{ct}"


def decrypt_secret(value: str) -> str:
    """Decrypt a secret.  Handles both encrypted (``enc:...``) and legacy plaintext values."""
    if not value.startswith(ENCRYPTED_PREFIX):
        # Legacy plaintext — return as-is (will be encrypted on next rotation/migration)
        return value
    ct = value[len(ENCRYPTED_PREFIX):]
    try:
        f = _get_fernet()
        return f.decrypt(ct.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt webhook secret — check WEBHOOK_ENCRYPTION_KEY")
