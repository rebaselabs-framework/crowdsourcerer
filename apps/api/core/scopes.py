"""API key scope constants and enforcement dependency factory.

Scopes follow the pattern  <resource>:<action>.
When a key is created with an empty scopes list it is granted **full access**
(backward-compatible default).  Once the list is non-empty only the listed
scopes are allowed.

JWT-authenticated requests (browser sessions) bypass scope checks entirely —
all routes are accessible.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.auth import _hash_api_key, decode_access_token
from core.config import get_settings
from core.database import get_db

settings = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)

# ── Canonical scope names ─────────────────────────────────────────────────

SCOPE_TASKS_READ = "tasks:read"
SCOPE_TASKS_WRITE = "tasks:write"
SCOPE_PIPELINES_READ = "pipelines:read"
SCOPE_PIPELINES_WRITE = "pipelines:write"
SCOPE_CREDITS_READ = "credits:read"
SCOPE_CREDITS_WRITE = "credits:write"
SCOPE_WORKERS_READ = "workers:read"
SCOPE_ANALYTICS_READ = "analytics:read"
SCOPE_WEBHOOKS_READ = "webhooks:read"
SCOPE_WEBHOOKS_WRITE = "webhooks:write"
SCOPE_MARKETPLACE_READ = "marketplace:read"
SCOPE_MARKETPLACE_WRITE = "marketplace:write"

# All valid scope strings
ALL_SCOPES: list[str] = [
    SCOPE_TASKS_READ,
    SCOPE_TASKS_WRITE,
    SCOPE_PIPELINES_READ,
    SCOPE_PIPELINES_WRITE,
    SCOPE_CREDITS_READ,
    SCOPE_CREDITS_WRITE,
    SCOPE_WORKERS_READ,
    SCOPE_ANALYTICS_READ,
    SCOPE_WEBHOOKS_READ,
    SCOPE_WEBHOOKS_WRITE,
    SCOPE_MARKETPLACE_READ,
    SCOPE_MARKETPLACE_WRITE,
]

SCOPE_DESCRIPTIONS: dict[str, str] = {
    SCOPE_TASKS_READ: "List and view tasks",
    SCOPE_TASKS_WRITE: "Create, cancel, and manage tasks",
    SCOPE_PIPELINES_READ: "List and view pipelines and runs",
    SCOPE_PIPELINES_WRITE: "Create, delete, and trigger pipelines",
    SCOPE_CREDITS_READ: "Check credit balance and transactions",
    SCOPE_CREDITS_WRITE: "Purchase credits (Stripe checkout)",
    SCOPE_WORKERS_READ: "View worker feed and marketplace",
    SCOPE_ANALYTICS_READ: "Access analytics and usage reports",
    SCOPE_WEBHOOKS_READ: "List webhook endpoints and delivery logs",
    SCOPE_WEBHOOKS_WRITE: "Create, update, and delete webhook endpoints",
    SCOPE_MARKETPLACE_READ: "Browse task template marketplace",
    SCOPE_MARKETPLACE_WRITE: "Create and publish task templates",
}

# ── Dependency factory ────────────────────────────────────────────────────

def require_scope(scope: str):
    """Return a FastAPI dependency that enforces the given scope.

    Usage in route::

        @router.post("/tasks")
        async def create_task(
            ...,
            user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
        ):
            ...
    """
    async def _dependency(
        credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
        db: AsyncSession = Depends(get_db),
    ) -> str:
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        token = credentials.credentials

        # ── API key path ────────────────────────────────────────────────
        if token.startswith("csk_"):
            from models.db import ApiKeyDB, UserDB
            from core.api_key_rate_limit import check_and_record_api_key_rate_limit
            from datetime import datetime, timezone

            hashed = _hash_api_key(token)
            result = await db.execute(
                select(ApiKeyDB).where(ApiKeyDB.key_hash == hashed)
            )
            api_key = result.scalar_one_or_none()
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid API key",
                )

            # Scope check: empty list = full access (legacy keys)
            key_scopes: list[str] = api_key.scopes or []
            if key_scopes and scope not in key_scopes:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"API key is missing required scope: '{scope}'",
                )

            # Rate limiting
            user_result = await db.execute(
                select(UserDB).where(UserDB.id == api_key.user_id)
            )
            owner = user_result.scalar_one_or_none()
            user_plan = owner.plan if owner else "free"
            await check_and_record_api_key_rate_limit(db, api_key, user_plan)

            # Stamp last_used_at
            api_key.last_used_at = datetime.now(timezone.utc)
            await db.commit()
            return str(api_key.user_id)

        # ── JWT path — bypass scope check ─────────────────────────────
        user_id = decode_access_token(token)
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )
        return user_id

    # Give the inner function a unique name so FastAPI doesn't confuse
    # dependencies with the same scope string.
    _dependency.__name__ = f"require_scope_{scope.replace(':', '_')}"
    return _dependency
