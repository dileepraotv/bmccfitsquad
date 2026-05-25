"""Encryption helpers for sensitive fields stored in the database.

All Strava OAuth tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
before being written to the `users` table and decrypted on read.

Usage
-----
    from app.crypto import encrypt, decrypt

    # Before writing to DB:
    user.strava_access_token  = encrypt(raw_access_token)
    user.strava_refresh_token = encrypt(raw_refresh_token)

    # After reading from DB:
    raw_token = decrypt(user.strava_access_token)
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Return the shared Fernet instance, built once from ENCRYPTION_KEY.

    The key must be a URL-safe base64-encoded 32-byte value produced by
    ``Fernet.generate_key()``.  Store it in the ENCRYPTION_KEY env var.
    """
    key = get_settings().encryption_key
    # Fernet.generate_key() returns bytes; we store it as a str in the env.
    return Fernet(key.encode() if isinstance(key, str) else key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* and return a URL-safe base64 ciphertext string.

    The returned value is safe to store in a TEXT database column.

    Args:
        plaintext: The raw string to encrypt (e.g. a Strava access token).

    Returns:
        Fernet token as a UTF-8 string.
    """
    if not plaintext:
        raise ValueError("Cannot encrypt an empty or None value.")
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet token produced by :func:`encrypt`.

    Args:
        ciphertext: The encrypted string as stored in the database.

    Returns:
        The original plaintext string.

    Raises:
        cryptography.fernet.InvalidToken: If the ciphertext is tampered with
            or was encrypted with a different key.
        ValueError: If *ciphertext* is empty or None.
    """
    if not ciphertext:
        raise ValueError("Cannot decrypt an empty or None value.")
    return _fernet().decrypt(ciphertext.encode()).decode()


def decrypt_or_none(ciphertext: str | None) -> str | None:
    """Decrypt *ciphertext* and return the plaintext, or ``None`` if the
    input is falsy.  Convenient for nullable token columns.

    Args:
        ciphertext: Encrypted value from the database, or None.

    Returns:
        Decrypted string, or None.
    """
    if not ciphertext:
        return None
    return decrypt(ciphertext)


def is_valid_encryption_key(key: str) -> bool:
    """Return True if *key* is a syntactically valid Fernet key.

    Useful in startup health-checks to fail fast rather than discovering a
    bad key at the first token write.
    """
    try:
        Fernet(key.encode() if isinstance(key, str) else key)
        return True
    except Exception:
        return False
