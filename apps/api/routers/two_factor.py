"""TOTP two-factor authentication endpoints."""
from __future__ import annotations
import hashlib
import os
import secrets
import time
from typing import Optional

import pyotp
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id, create_access_token
from core.config import get_settings
from core.database import get_db
from models.db import UserDB
from models.schemas import (
    TwoFASetupResponse,
    TwoFAEnableRequest,
    TwoFAEnableResponse,
    TwoFAVerifyRequest,
    TwoFADisableRequest,
    TwoFAStatusResponse,
    TokenResponse,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/auth/2fa", tags=["2fa"])
settings = get_settings()
_ISSUER = "CrowdSorcerer"
_BACKUP_CODE_COUNT = 8
_PENDING_2FA_EXPIRE = 300  # 5 minutes


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _hash_backup_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _create_pending_token(user_id: str) -> str:
    """Issue a short-lived JWT that marks the user as 'password verified, 2FA pending'."""
    expire = int(time.time()) + _PENDING_2FA_EXPIRE
    payload = {"sub": user_id, "typ": "2fa_pending", "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _verify_pending_token(token: str) -> Optional[str]:
    """Decode and validate the pending 2FA JWT. Returns user_id or None."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("typ") != "2fa_pending":
            return None
        return payload.get("sub")
    except JWTError:
        return None


# ─── GET /v1/auth/2fa/status ───────────────────────────────────────────────────

@router.get("/status", response_model=TwoFAStatusResponse)
async def get_2fa_status(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return whether 2FA is enabled and how many backup codes remain."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    remaining = 0
    if user.totp_backup_codes:
        remaining = len([c for c in user.totp_backup_codes if c is not None])

    return TwoFAStatusResponse(enabled=user.totp_enabled, backup_codes_remaining=remaining)


# ─── POST /v1/auth/2fa/setup ───────────────────────────────────────────────────

@router.post("/setup", response_model=TwoFASetupResponse)
async def setup_2fa(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Generate a new TOTP secret and return the provisioning URI.

    The secret is stored (unconfirmed) on the user record. Calling
    ``/enable`` with a valid TOTP code confirms it.

    Calling setup again resets the secret (before enable is called).
    """
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.totp_enabled:
        raise HTTPException(
            status_code=409,
            detail="2FA is already enabled. Disable it first.",
        )

    # Generate a fresh TOTP secret
    secret = pyotp.random_base32()
    user.totp_secret = secret
    await db.commit()

    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(
        name=user.email,
        issuer_name=_ISSUER,
    )

    return TwoFASetupResponse(totp_uri=uri, secret=secret, issuer=_ISSUER)


# ─── POST /v1/auth/2fa/enable ─────────────────────────────────────────────────

@router.post("/enable", response_model=TwoFAEnableResponse)
async def enable_2fa(
    req: TwoFAEnableRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Verify a TOTP code and activate 2FA.

    Returns 8 plaintext backup codes (shown once, store safely).
    """
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA already enabled")

    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="Call /setup first")

    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(req.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid TOTP code")

    # Generate backup codes
    plaintext_codes = [secrets.token_hex(4).upper() for _ in range(_BACKUP_CODE_COUNT)]
    hashed_codes = [_hash_backup_code(c) for c in plaintext_codes]

    user.totp_enabled = True
    user.totp_backup_codes = hashed_codes
    await db.commit()

    logger.info("2fa_enabled", user_id=str(user_id))
    return TwoFAEnableResponse(backup_codes=plaintext_codes)


# ─── POST /v1/auth/2fa/disable ────────────────────────────────────────────────

@router.post("/disable", status_code=204, response_model=None)
async def disable_2fa(
    req: TwoFADisableRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Disable 2FA. Requires a valid TOTP code or backup code."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is not enabled")

    verified = False

    # Try TOTP
    if len(req.code) == 6 and user.totp_secret:
        totp = pyotp.TOTP(user.totp_secret)
        verified = totp.verify(req.code, valid_window=1)

    # Try backup code
    if not verified and user.totp_backup_codes:
        code_hash = _hash_backup_code(req.code.upper())
        if code_hash in user.totp_backup_codes:
            verified = True
            # Consume the backup code
            user.totp_backup_codes = [
                c for c in user.totp_backup_codes if c != code_hash
            ]

    if not verified:
        raise HTTPException(status_code=400, detail="Invalid code")

    user.totp_secret = None
    user.totp_enabled = False
    user.totp_backup_codes = None
    await db.commit()
    logger.info("2fa_disabled", user_id=str(user_id))


# ─── POST /v1/auth/2fa/verify ─────────────────────────────────────────────────

@router.post("/verify", response_model=TokenResponse)
async def verify_2fa(
    req: TwoFAVerifyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a pending-2FA token + TOTP code for a full access token.

    Called after a successful password check when 2FA is enabled.
    The ``pending_token`` is issued by ``POST /v1/auth/login``.
    """
    user_id = _verify_pending_token(req.pending_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired 2FA session",
        )

    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or disabled")

    if not user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is not enabled for this account")

    verified = False

    # Try TOTP code
    if len(req.code) == 6 and user.totp_secret:
        totp = pyotp.TOTP(user.totp_secret)
        verified = totp.verify(req.code, valid_window=1)

    # Try backup code (8-char hex)
    if not verified and user.totp_backup_codes:
        code_hash = _hash_backup_code(req.code.upper())
        if code_hash in user.totp_backup_codes:
            verified = True
            # Consume the backup code (one-time use)
            user.totp_backup_codes = [
                c for c in user.totp_backup_codes if c != code_hash
            ]
            await db.commit()

    if not verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid 2FA code",
        )

    access_token = create_access_token(str(user.id), token_version=user.token_version or 0)
    logger.info("2fa_login_success", user_id=str(user.id))

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.jwt_expire_minutes * 60,
    )
