"""Google OAuth 2.0 login/signup.

Endpoints:
  GET /v1/auth/google          — redirect user to Google consent screen
  GET /v1/auth/google/callback — exchange code for tokens; create or log in user

Configuration required (env vars):
  GOOGLE_CLIENT_ID     — from Google Cloud Console → Credentials → OAuth 2.0 Client
  GOOGLE_CLIENT_SECRET — same OAuth 2.0 Client
  Authorised redirect URI must be:
    https://crowdsourcerer.rebaselabs.online/v1/auth/google/callback
  (or http://localhost:8100/v1/auth/google/callback for local dev)

Flow:
  1. Client visits /v1/auth/google (optional ?role=worker&next=/dashboard)
  2. Server generates signed state param and redirects to Google
  3. Google redirects back to /callback with code + state
  4. Server verifies state, exchanges code for access_token, fetches user info
  5. Finds existing user by google_id or email; creates new one if needed
  6. Issues JWT and redirects to the web UI with ?token=<jwt> in fragment or
     returns JSON (for API callers that pass ?format=json)
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from typing import Optional

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import create_access_token
from core.config import get_settings
from core.database import get_db
from models.db import UserDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/auth", tags=["auth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_STATE_TTL_SECONDS = 600  # 10 minutes
_BASE_URL = "https://crowdsourcerer.rebaselabs.online"


# ─── State helpers (stateless HMAC-signed state) ─────────────────────────────

def _sign_state(payload: dict) -> str:
    """Encode and HMAC-sign a state dict. Returns a URL-safe base64 string."""
    settings = get_settings()
    # JWT secret doubles as HMAC key; if it's default, OAuth is effectively disabled
    raw_json = json.dumps(payload, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw_json).rstrip(b"=").decode()
    sig = hmac.new(settings.jwt_secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


def _verify_state(state: str) -> dict:
    """Verify state HMAC and TTL. Returns payload dict or raises HTTPException."""
    try:
        encoded, sig = state.rsplit(".", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    settings = get_settings()
    expected = hmac.new(settings.jwt_secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=400, detail="OAuth state signature mismatch — possible CSRF")

    # Re-pad and decode
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid OAuth state encoding")

    if time.time() - payload.get("ts", 0) > _STATE_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="OAuth state expired. Please try again.")

    return payload


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/google")
async def google_login(
    request: Request,
    role: Optional[str] = Query(None, description="Intended role: requester or worker"),
    next: Optional[str] = Query(None, description="Post-login redirect path"),
):
    """Redirect to Google OAuth consent screen.

    If GOOGLE_CLIENT_ID is not configured returns 503.
    """
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured on this server.",
        )

    state_payload = {
        "nonce": os.urandom(16).hex(),
        "ts": int(time.time()),
        "role": role or "requester",
        "next": next or "/dashboard",
    }
    state = _sign_state(state_payload)

    redirect_uri = f"{_BASE_URL}/v1/auth/google/callback"
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    auth_url = f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=auth_url)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Handle Google's redirect back after user consents (or denies)."""
    settings = get_settings()

    # User denied or error from Google
    if error:
        return RedirectResponse(url=f"/login?oauth_error={urllib.parse.quote(error)}")

    if not code or not state:
        return RedirectResponse(url="/login?oauth_error=missing_params")

    # Verify state
    try:
        state_payload = _verify_state(state)
    except HTTPException as e:
        return RedirectResponse(url=f"/login?oauth_error={urllib.parse.quote(str(e.detail))}")

    # Exchange code for tokens
    redirect_uri = f"{_BASE_URL}/v1/auth/google/callback"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_res = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if not token_res.is_success:
                return RedirectResponse(url="/login?oauth_error=token_exchange_failed")

            token_data = token_res.json()
            access_token = token_data.get("access_token")
            if not access_token:
                return RedirectResponse(url="/login?oauth_error=no_access_token")

            # Fetch user info
            info_res = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if not info_res.is_success:
                return RedirectResponse(url="/login?oauth_error=userinfo_failed")

            user_info = info_res.json()
    except Exception:
        return RedirectResponse(url="/login?oauth_error=network_error")

    google_id = user_info.get("sub")
    email = user_info.get("email")
    name = user_info.get("name")
    email_verified_by_google = user_info.get("email_verified", False)

    if not google_id or not email:
        return RedirectResponse(url="/login?oauth_error=missing_user_info")

    # ── Find or create user ───────────────────────────────────────────────────
    # 1. Try by google_id first (returning OAuth user)
    result = await db.execute(select(UserDB).where(UserDB.google_id == google_id))
    user = result.scalar_one_or_none()

    if not user:
        # 2. Try by email (account linking: existing user signs in with Google)
        result = await db.execute(select(UserDB).where(UserDB.email == email))
        user = result.scalar_one_or_none()
        if user:
            # Link Google account to existing user
            user.google_id = google_id
            if email_verified_by_google and not user.email_verified:
                user.email_verified = True
                user.email_verification_token_hash = None

    if not user:
        # 3. Brand-new user via Google
        intended_role = state_payload.get("role", "requester")
        if intended_role not in ("requester", "worker", "both"):
            intended_role = "requester"

        user = UserDB(
            email=email,
            name=name,
            google_id=google_id,
            role=intended_role,
            credits=settings.free_tier_credits,
            email_verified=email_verified_by_google,
            email_verification_token_hash=None,
        )
        db.add(user)

    if not user.is_active:
        return RedirectResponse(url="/login?oauth_error=account_disabled")

    await db.commit()
    await db.refresh(user)

    # Issue JWT and refresh token, then redirect to web UI
    jwt_token = create_access_token(str(user.id), token_version=user.token_version or 0)

    # Issue refresh token (best-effort — OAuth still works without it)
    raw_refresh = ""
    try:
        from core.refresh_tokens import create_refresh_token
        raw_refresh, _refresh_expires = await create_refresh_token(str(user.id), db)
        await db.commit()  # persist the refresh token
    except Exception:
        logger.exception("oauth_refresh_token_error", user_id=str(user.id))

    next_path = state_payload.get("next", "/dashboard")
    # Redirect to web's /auth/google-success which picks up the tokens from the query string
    # and sets httpOnly cookies (server-side via Astro route)
    refresh_param = f"&refresh={urllib.parse.quote(raw_refresh)}" if raw_refresh else ""
    return RedirectResponse(
        url=(
            f"/auth/google-success"
            f"?token={urllib.parse.quote(jwt_token)}"
            f"{refresh_param}"
            f"&next={urllib.parse.quote(next_path)}"
        )
    )
