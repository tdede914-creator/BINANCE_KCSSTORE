"""Security utilities: symmetric encryption for API keys + JWT for sessions."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt

from app.core.config import settings

# =========================================================================
# Fernet symmetric encryption for Binance API keys at rest.
# =========================================================================
#
# We store Binance API key + secret encrypted in the database. Fernet is
# AES-128-CBC + HMAC-SHA256 with rotating IVs — safe for at-rest storage
# of small secrets like API keys.


def _load_fernet() -> Fernet:
    key = settings.ENCRYPTION_KEY.strip()
    if not key:
        # Auto-generate one for dev if none provided. Warn loudly in logs.
        # NOTE: in production always set ENCRYPTION_KEY explicitly, otherwise
        # keys will not decrypt across restarts.
        key_path = "/tmp/kcs_dev_fernet.key"  # noqa: S108
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                key = f.read().decode()
        else:
            key = Fernet.generate_key().decode()
            with open(key_path, "w") as f:
                f.write(key)
    return Fernet(key.encode())


_fernet = _load_fernet()


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string. Returns a base64 token string."""
    if not plaintext:
        return ""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt_secret`.

    Raises ValueError if the token is invalid or was encrypted with a
    different key.
    """
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Invalid or wrong-key encrypted token") from e


def mask_key(key: str, visible: int = 4) -> str:
    """Return a masked representation of an API key for display: ``abcd...wxyz``."""
    if not key:
        return ""
    if len(key) <= visible * 2:
        return "*" * len(key)
    return f"{key[:visible]}...{key[-visible:]}"


# =========================================================================
# JWT session tokens for the frontend.
# =========================================================================


def create_access_token(subject: str | int, extra: dict[str, Any] | None = None) -> str:
    """Create a signed JWT token.

    :param subject: Usually the user id.
    :param extra: Additional custom claims.
    """
    now = datetime.now(tz=timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.JWT_EXPIRES_MINUTES)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode + verify a JWT. Raises ValueError on failure."""
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except JWTError as e:
        raise ValueError(f"Invalid JWT: {e}") from e
