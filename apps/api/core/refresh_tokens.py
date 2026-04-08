"""Refresh token lifecycle: create, rotate, revoke.

Refresh tokens are opaque strings (``csrt_`` + 64 random chars) stored
hashed (SHA-256) in the ``refresh_tokens`` table.  Each token belongs to a
*family* — all tokens descended from a single login share the same
``family_id``.  On rotation the old token is revoked and a new one issued.

**Replay detection:** if a *revoked* token is presented, the entire family
is revoked.  This catches the case where an attacker stole an older token
and tries to use it after the legitimate user already rotated.
"""

import hashlib
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings

logger = structlog.get_logger()

_ALPHABET = string.ascii_letters + string.digits
_TOKEN_PREFIX = "csrt_"
_TOKEN_LENGTH = 64  # characters after the prefix


def _generate_raw_token() -> str:
    return _TOKEN_PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_LENGTH))


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_refresh_token(
    user_id: str,
    db: AsyncSession,
    family_id: uuid.UUID | None = None,
) -> tuple[str, datetime]:
    """Create a new refresh token for *user_id*.

    Returns ``(raw_token, expires_at)``.  The raw token is returned to the
    caller (sent to the client); only its hash is stored.

    If *family_id* is ``None`` a new family is started (fresh login).
    """
    from models.db import RefreshTokenDB

    settings = get_settings()
    raw = _generate_raw_token()
    token_hash = _hash_token(raw)
    fam = family_id or uuid.uuid4()
    expires = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)

    # Convert user_id to UUID — accept UUID, str, or any object with str() repr
    if isinstance(user_id, uuid.UUID):
        uid = user_id
    else:
        uid = uuid.UUID(str(user_id))

    rec = RefreshTokenDB(
        user_id=uid,
        token_hash=token_hash,
        family_id=fam,
        expires_at=expires,
    )
    db.add(rec)
    await db.flush()  # get rec.id without full commit (caller commits)

    return raw, expires


async def rotate_refresh_token(
    raw_token: str,
    db: AsyncSession,
) -> tuple[str, str, datetime, str] | None:
    """Rotate *raw_token*: revoke it and issue a new one in the same family.

    Returns ``(new_access_token, new_raw_refresh, refresh_expires_at, user_id)``
    on success, or ``None`` if the token is invalid/expired.

    **Replay detection:** if the token is already revoked, the entire family
    is killed and ``None`` is returned.
    """
    from models.db import RefreshTokenDB, UserDB
    from core.auth import create_access_token

    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(RefreshTokenDB).where(RefreshTokenDB.token_hash == token_hash)
    )
    rec = result.scalar_one_or_none()

    if not rec:
        return None  # unknown token

    # ── Replay detection ──────────────────────────────────────────────────
    if rec.revoked_at is not None:
        # Token was already revoked — someone replayed it.
        # Revoke entire family to protect the user.
        logger.warning(
            "refresh_token_replay_detected",
            family_id=str(rec.family_id),
            user_id=str(rec.user_id),
        )
        await db.execute(
            update(RefreshTokenDB)
            .where(RefreshTokenDB.family_id == rec.family_id)
            .where(RefreshTokenDB.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.commit()
        return None

    # ── Expired check ─────────────────────────────────────────────────────
    expires = rec.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        return None  # expired

    # ── Fetch user (need token_version for new JWT) ───────────────────────
    user_result = await db.execute(
        select(UserDB).where(UserDB.id == rec.user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user or not user.is_active:
        return None

    # ── Revoke old token ──────────────────────────────────────────────────
    rec.revoked_at = now

    # ── Issue new tokens ──────────────────────────────────────────────────
    new_raw, new_expires = await create_refresh_token(
        str(rec.user_id), db, family_id=rec.family_id,
    )

    # Point old token at new one (for audit trail)
    # (need to flush to get the new token's DB id)
    new_result = await db.execute(
        select(RefreshTokenDB).where(
            RefreshTokenDB.token_hash == _hash_token(new_raw)
        )
    )
    new_rec = new_result.scalar_one_or_none()
    if new_rec:
        rec.replaced_by = new_rec.id

    # New access token
    access_token = create_access_token(
        str(user.id), token_version=user.token_version or 0,
    )

    await db.commit()

    return access_token, new_raw, new_expires, str(user.id)


async def revoke_refresh_token(raw_token: str, db: AsyncSession) -> bool:
    """Revoke a single refresh token.  Returns True if found and revoked."""
    from models.db import RefreshTokenDB

    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(RefreshTokenDB).where(RefreshTokenDB.token_hash == token_hash)
    )
    rec = result.scalar_one_or_none()
    if not rec or rec.revoked_at is not None:
        return False

    rec.revoked_at = now
    await db.commit()
    return True


async def revoke_all_user_tokens(user_id: str, db: AsyncSession) -> int:
    """Revoke ALL active refresh tokens for *user_id*.

    Called on password change/reset to invalidate all sessions.
    Returns count of tokens revoked.
    """
    from models.db import RefreshTokenDB

    now = datetime.now(timezone.utc)
    uid = uuid.UUID(user_id) if isinstance(user_id, str) else user_id

    result = await db.execute(
        update(RefreshTokenDB)
        .where(RefreshTokenDB.user_id == uid)
        .where(RefreshTokenDB.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.commit()
    return result.rowcount  # type: ignore[return-value]


async def cleanup_expired_tokens(db: AsyncSession, *, days_past: int = 7) -> int:
    """Delete refresh tokens that expired more than *days_past* ago.

    Call from sweeper to keep the table lean.
    """
    from models.db import RefreshTokenDB
    from sqlalchemy import delete

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_past)
    result = await db.execute(
        delete(RefreshTokenDB).where(RefreshTokenDB.expires_at < cutoff)
    )
    await db.commit()
    return result.rowcount  # type: ignore[return-value]
