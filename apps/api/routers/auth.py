"""Auth endpoints: register, login, forgot/reset password, email verification."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
import hashlib
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
import bcrypt as _bcrypt
from pydantic import BaseModel, EmailStr, Field, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.auth import create_access_token, get_current_user_id
from core.background import safe_create_task
from core.config import get_settings
from core.database import get_db
from models.db import UserDB, PasswordResetTokenDB
from models.schemas import (
    LoginRequest, RegisterRequest, TokenResponse,
    LoginWith2FAResponse,
)

_RESET_TOKEN_TTL_MINUTES = 30
_VERIFY_TOKEN_TTL_HOURS = 24


def _make_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hex). Store the hash, email the raw token."""
    raw = os.urandom(32).hex()  # 64 hex chars = 256 bits of entropy
    h = hashlib.sha256(raw.encode()).hexdigest()
    return raw, h


# Keep old name as alias for backward-compat within this module
_make_reset_token = _make_token

router = APIRouter(prefix="/v1/auth", tags=["auth"])
settings = get_settings()


def _hash_password_sync(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_password_sync(password: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


async def _hash_password(password: str) -> str:
    """Hash password off the event loop — bcrypt is intentionally slow (~300ms)."""
    return await asyncio.to_thread(_hash_password_sync, password)


async def _verify_password(password: str, hashed: str) -> bool:
    """Verify password off the event loop — bcrypt is intentionally slow (~300ms)."""
    return await asyncio.to_thread(_verify_password_sync, password, hashed)
limiter = Limiter(key_func=get_remote_address)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(
    request: Request,
    req: RegisterRequest,
    ref: Optional[str] = Query(None, description="Referral code"),
    db: AsyncSession = Depends(get_db),
):
    # Check email uniqueness
    existing = await db.execute(select(UserDB).where(UserDB.email == req.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    # Generate email verification token
    raw_verify_token, verify_token_hash = _make_token()

    user = UserDB(
        email=req.email,
        name=req.name,
        password_hash=await _hash_password(req.password),
        role=req.role,
        credits=settings.free_tier_credits,
        email_verified=False,
        email_verification_token_hash=verify_token_hash,
    )
    db.add(user)
    await db.flush()  # get user.id without committing

    # Apply referral bonus if a valid code was provided
    if ref:
        from routers.referrals import apply_referral_on_signup
        await apply_referral_on_signup(user.id, ref, db)

    await db.commit()
    await db.refresh(user)

    # Send verification email (fire-and-forget — don't block signup on email failure)
    from core.email import send_email_verification
    verify_url = f"{settings.public_site_url}/verify-email?token={raw_verify_token}"
    safe_create_task(
        send_email_verification(user.email, verify_url, user.name),
        name="email.verification",
    )

    token = create_access_token(str(user.id), token_version=user.token_version or 0)

    # Issue refresh token (best-effort — don't fail registration if this errors)
    raw_refresh = None
    refresh_expires_in = None
    try:
        from core.refresh_tokens import create_refresh_token
        raw_refresh, refresh_expires = await create_refresh_token(str(user.id), db)
        await db.commit()  # persist the refresh token (flush alone is rolled back)
        refresh_expires_in = int((refresh_expires - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        pass  # access token alone is enough to proceed

    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        refresh_token=raw_refresh,
        refresh_expires_in=refresh_expires_in,
    )


@router.post("/login")
@limiter.limit("10/minute")
async def login(
    request: Request,
    req: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with email + password.

    If 2FA is enabled for the account, returns a ``LoginWith2FAResponse``
    (HTTP 200 with ``requires_2fa: true``) instead of a full token.
    The client should then call ``POST /v1/auth/2fa/verify`` with the
    ``pending_token`` and a TOTP code.
    """
    result = await db.execute(select(UserDB).where(UserDB.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not await _verify_password(req.password, user.password_hash or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    # ── 2FA gate ───────────────────────────────────────────────────────────────
    if user.totp_enabled:
        # Issue short-lived pending token
        from routers.two_factor import _create_pending_token
        pending = _create_pending_token(str(user.id))
        return LoginWith2FAResponse(pending_token=pending)

    token = create_access_token(str(user.id), token_version=user.token_version or 0)

    # Issue refresh token (best-effort — don't fail login if this errors)
    raw_refresh = None
    refresh_expires_in = None
    try:
        from core.refresh_tokens import create_refresh_token
        raw_refresh, refresh_expires = await create_refresh_token(str(user.id), db)
        await db.commit()  # persist the refresh token (flush alone is rolled back)
        refresh_expires_in = int((refresh_expires - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        pass  # access token alone is enough to proceed

    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        refresh_token=raw_refresh,
        refresh_expires_in=refresh_expires_in,
    )


# ─── Password reset ──────────────────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(max_length=512)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


@router.post("/forgot-password", status_code=200)
@limiter.limit("5/minute")
async def forgot_password(
    request: Request,
    req: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Initiate a password reset. Always returns 200 to avoid user enumeration.

    If the email exists we create a short-lived token and email the reset link.
    If not, we silently succeed — the response is indistinguishable either way.
    """
    result = await db.execute(select(UserDB).where(UserDB.email == req.email))
    user = result.scalar_one_or_none()

    if user and user.is_active:
        raw_token, token_hash = _make_reset_token()
        expires = datetime.now(timezone.utc) + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES)

        reset_rec = PasswordResetTokenDB(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires,
            used=False,
        )
        db.add(reset_rec)
        await db.commit()

        reset_url = f"{settings.public_site_url}/reset-password?token={raw_token}"
        # Send email fire-and-forget — don't block on email failures
        from core.email import send_password_reset
        safe_create_task(
            send_password_reset(user.email, reset_url, user.name),
            name="email.password_reset",
        )

    # Always return success to prevent email enumeration
    return {"message": "If an account exists for that email, a reset link has been sent."}


@router.post("/reset-password", status_code=200)
@limiter.limit("10/minute")
async def reset_password(
    request: Request,
    req: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Complete a password reset using a token from the reset email."""
    token_hash = hashlib.sha256(req.token.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(PasswordResetTokenDB).where(PasswordResetTokenDB.token_hash == token_hash)
    )
    rec = result.scalar_one_or_none()

    if not rec or rec.used or rec.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset link. Please request a new one.",
        )

    # Update password
    user_result = await db.execute(select(UserDB).where(UserDB.id == rec.user_id))
    user = user_result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="Account not found or disabled.")

    user.password_hash = await _hash_password(req.new_password)
    user.token_version = (user.token_version or 0) + 1
    rec.used = True
    await db.commit()

    # Revoke all refresh tokens — force re-login on all devices
    from core.refresh_tokens import revoke_all_user_tokens
    await revoke_all_user_tokens(str(user.id), db)

    return {"message": "Password updated successfully. You can now log in with your new password."}


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def new_password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


@router.post("/change-password", status_code=200)
@limiter.limit("10/minute")
async def change_password(
    request: Request,
    req: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Change password for the currently authenticated user.

    Requires the current password to prevent session-hijack password changes.
    """
    from uuid import UUID
    result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not await _verify_password(req.current_password, user.password_hash or ""):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.password_hash = await _hash_password(req.new_password)
    user.token_version = (user.token_version or 0) + 1
    await db.commit()

    # Revoke all refresh tokens — force re-login on all devices
    from core.refresh_tokens import revoke_all_user_tokens
    await revoke_all_user_tokens(user_id, db)

    return {"message": "Password changed successfully."}


# ─── Email verification ───────────────────────────────────────────────────────

@router.get("/verify-email", status_code=200)
@limiter.limit("10/minute")
async def verify_email(
    request: Request,
    token: str = Query(..., description="Email verification token from link"),
    db: AsyncSession = Depends(get_db),
):
    """Verify a user's email address using the token sent at signup.

    Returns 200 on success (including already-verified), 400 for bad/expired token.
    The token is single-use: cleared from DB after successful verification.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    result = await db.execute(
        select(UserDB).where(UserDB.email_verification_token_hash == token_hash)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired verification link. Please request a new one.",
        )

    if user.email_verified:
        # Already verified — idempotent success
        return {"message": "Email already verified.", "already_verified": True}

    user.email_verified = True
    user.email_verification_token_hash = None  # single-use; clear it
    await db.commit()

    return {"message": "Email verified successfully.", "already_verified": False}


@router.post("/resend-verification", status_code=200)
@limiter.limit("3/minute")
async def resend_verification(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Resend the email verification link. Requires authentication.

    Rate-limited to 3/minute to prevent abuse. Safe to call even if already verified.
    """
    from uuid import UUID
    result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.email_verified:
        return {"message": "Email is already verified."}

    # Generate a fresh token
    raw_token, token_hash = _make_token()
    user.email_verification_token_hash = token_hash
    await db.commit()

    from core.email import send_email_verification
    verify_url = f"{settings.public_site_url}/verify-email?token={raw_token}"
    safe_create_task(
        send_email_verification(user.email, verify_url, user.name),
        name="email.verification",
    )

    return {"message": "Verification email sent. Please check your inbox."}


# ─── Token refresh / logout ──────────────────────────────────────────────────

class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("30/minute")
async def refresh_token(
    request: Request,
    req: RefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a valid refresh token for a new access + refresh token pair.

    The old refresh token is revoked and a new one issued in the same family.
    If the refresh token was already revoked (replay attack), the entire token
    family is invalidated for security.
    """
    from core.refresh_tokens import rotate_refresh_token as _rotate

    result = await _rotate(req.refresh_token, db)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    access_token, new_refresh, refresh_expires, _user_id = result
    refresh_expires_in = int((refresh_expires - datetime.now(timezone.utc)).total_seconds())

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.jwt_expire_minutes * 60,
        refresh_token=new_refresh,
        refresh_expires_in=refresh_expires_in,
    )


class LogoutRequest(BaseModel):
    refresh_token: str


@router.post("/logout", status_code=200)
async def logout(
    req: LogoutRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke a refresh token (log out the session).

    Always returns 200 regardless of whether the token existed — prevent
    oracle attacks that enumerate valid tokens.
    """
    from core.refresh_tokens import revoke_refresh_token

    await revoke_refresh_token(req.refresh_token, db)
    return {"message": "Logged out successfully."}
