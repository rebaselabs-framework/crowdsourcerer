"""Authentication utilities: JWT tokens + API key hashing."""
import hashlib
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.config import get_settings
from core.database import get_db

settings = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)

_ALPHABET = string.ascii_letters + string.digits


def generate_api_key() -> tuple[str, str]:
    """Returns (plaintext_key, hashed_key). Store only the hash."""
    raw = "csk_" + "".join(secrets.choice(_ALPHABET) for _ in range(48))
    hashed = _hash_api_key(raw)
    return raw, hashed


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(
        f"{settings.api_key_salt}:{key}".encode()
    ).hexdigest()


def create_access_token(subject: str, expire_minutes: Optional[int] = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expire_minutes or settings.jwt_expire_minutes
    )
    return jwt.encode(
        {"sub": subject, "exp": expire},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return payload.get("sub")
    except JWTError:
        return None


async def get_current_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Accepts either a JWT token or an API key (csk_...)."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    token = credentials.credentials

    # API key path
    if token.startswith("csk_"):
        from models.db import ApiKeyDB, UserDB  # avoid circular import
        hashed = _hash_api_key(token)
        result = await db.execute(
            select(ApiKeyDB).where(ApiKeyDB.key_hash == hashed)
        )
        api_key = result.scalar_one_or_none()
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
            )

        # Fetch owning user to resolve plan for rate-limit defaults
        user_result = await db.execute(select(UserDB).where(UserDB.id == api_key.user_id))
        owner = user_result.scalar_one_or_none()
        user_plan = owner.plan if owner else "free"

        # Enforce per-key rate limits (RPM + daily)
        from core.api_key_rate_limit import check_and_record_api_key_rate_limit
        await check_and_record_api_key_rate_limit(db, api_key, user_plan)

        # update last_used_at
        api_key.last_used_at = datetime.now(timezone.utc)
        await db.commit()
        return str(api_key.user_id)

    # JWT path
    user_id = decode_access_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )
    return user_id


async def get_optional_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Optional[str]:
    """Like get_current_user_id but returns None instead of 401 when unauthenticated.

    Use for public endpoints that optionally personalize the response (e.g.
    highlighting the current user on a public leaderboard).
    """
    if not credentials:
        return None
    token = credentials.credentials
    if token.startswith("csk_"):
        from models.db import ApiKeyDB
        hashed = _hash_api_key(token)
        result = await db.execute(select(ApiKeyDB).where(ApiKeyDB.key_hash == hashed))
        api_key = result.scalar_one_or_none()
        return str(api_key.user_id) if api_key else None
    return decode_access_token(token)


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Dependency: returns the full UserDB object for the authenticated caller."""
    from models.db import UserDB  # avoid circular import at module level
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )
    return user


async def require_admin(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
) -> str:
    """Dependency: verify the caller is an admin (is_admin=True)."""
    from models.db import UserDB  # avoid circular import at module level
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user_id
