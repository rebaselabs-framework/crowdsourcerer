"""Auth endpoints: register, login."""
from datetime import timedelta
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from passlib.context import CryptContext
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.auth import create_access_token
from core.config import get_settings
from core.database import get_db
from models.db import UserDB
from models.schemas import (
    LoginRequest, RegisterRequest, TokenResponse,
    LoginWith2FAResponse,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
settings = get_settings()
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

    user = UserDB(
        email=req.email,
        name=req.name,
        password_hash=pwd_context.hash(req.password),
        credits=settings.free_tier_credits,
    )
    db.add(user)
    await db.flush()  # get user.id without committing

    # Apply referral bonus if a valid code was provided
    if ref:
        from routers.referrals import apply_referral_on_signup
        await apply_referral_on_signup(user.id, ref, db)

    await db.commit()
    await db.refresh(user)

    token = create_access_token(str(user.id))
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
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

    if not user or not pwd_context.verify(req.password, user.password_hash or ""):
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

    token = create_access_token(str(user.id))
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
    )
