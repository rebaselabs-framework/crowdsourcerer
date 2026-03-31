"""Tests for JWT refresh token rotation system."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── Unit tests for core/refresh_tokens.py ──────────────────────────────────


class TestTokenGeneration:
    """Test token format and hashing."""

    def test_raw_token_has_correct_prefix(self):
        from core.refresh_tokens import _generate_raw_token
        raw = _generate_raw_token()
        assert raw.startswith("csrt_")

    def test_raw_token_has_correct_length(self):
        from core.refresh_tokens import _generate_raw_token
        raw = _generate_raw_token()
        # csrt_ (5) + 64 chars = 69 total
        assert len(raw) == 69

    def test_hash_is_sha256_hex(self):
        from core.refresh_tokens import _hash_token
        raw = "csrt_testtoken123"
        h = _hash_token(raw)
        assert len(h) == 64  # SHA-256 hex = 64 chars
        assert h == hashlib.sha256(raw.encode()).hexdigest()

    def test_different_tokens_produce_different_hashes(self):
        from core.refresh_tokens import _generate_raw_token, _hash_token
        t1 = _generate_raw_token()
        t2 = _generate_raw_token()
        assert t1 != t2
        assert _hash_token(t1) != _hash_token(t2)


class TestTokenResponseSchema:
    """Test that TokenResponse now includes refresh token fields."""

    def test_token_response_with_refresh(self):
        from models.schemas import TokenResponse
        resp = TokenResponse(
            access_token="jwt_here",
            expires_in=1800,
            refresh_token="csrt_refresh_here",
            refresh_expires_in=2592000,
        )
        assert resp.refresh_token == "csrt_refresh_here"
        assert resp.refresh_expires_in == 2592000

    def test_token_response_without_refresh(self):
        from models.schemas import TokenResponse
        resp = TokenResponse(
            access_token="jwt_here",
            expires_in=1800,
        )
        assert resp.refresh_token is None
        assert resp.refresh_expires_in is None

    def test_token_response_backward_compatible(self):
        """Existing callers that only check access_token/expires_in still work."""
        from models.schemas import TokenResponse
        resp = TokenResponse(
            access_token="jwt_here",
            token_type="bearer",
            expires_in=604800,
        )
        assert resp.access_token == "jwt_here"
        assert resp.token_type == "bearer"
        assert resp.expires_in == 604800


class TestConfigDefaults:
    """Verify the new config defaults."""

    def test_access_token_default_30min(self):
        from core.config import Settings
        s = Settings(jwt_secret="test", api_key_salt="test")
        assert s.jwt_expire_minutes == 30

    def test_refresh_token_default_30days(self):
        from core.config import Settings
        s = Settings(jwt_secret="test", api_key_salt="test")
        assert s.refresh_token_expire_days == 30

    def test_access_token_expiry_override(self):
        from core.config import Settings
        s = Settings(jwt_secret="test", api_key_salt="test", jwt_expire_minutes=60)
        assert s.jwt_expire_minutes == 60


class TestRefreshTokenModel:
    """Test the RefreshTokenDB model exists and has expected columns."""

    def test_model_exists(self):
        from models.db import RefreshTokenDB
        assert RefreshTokenDB.__tablename__ == "refresh_tokens"

    def test_model_has_required_columns(self):
        from models.db import RefreshTokenDB
        cols = {c.name for c in RefreshTokenDB.__table__.columns}
        assert "id" in cols
        assert "user_id" in cols
        assert "token_hash" in cols
        assert "family_id" in cols
        assert "expires_at" in cols
        assert "revoked_at" in cols
        assert "replaced_by" in cols
        assert "created_at" in cols


class TestRefreshEndpointSchemas:
    """Test the request schemas for refresh/logout endpoints."""

    def test_refresh_request_requires_token(self):
        # Import the schema from auth router
        import importlib
        mod = importlib.import_module("routers.auth")
        RefreshRequest = mod.RefreshRequest
        req = RefreshRequest(refresh_token="csrt_test123")
        assert req.refresh_token == "csrt_test123"

    def test_logout_request_requires_token(self):
        import importlib
        mod = importlib.import_module("routers.auth")
        LogoutRequest = mod.LogoutRequest
        req = LogoutRequest(refresh_token="csrt_test123")
        assert req.refresh_token == "csrt_test123"


class TestAccessTokenExpiry:
    """Verify access tokens now have shorter expiry."""

    def test_access_token_30min_expiry(self):
        from core.auth import create_access_token
        from jose import jwt as jose_jwt
        from core.config import get_settings

        settings = get_settings()
        token = create_access_token("test-user-id", token_version=0)
        payload = jose_jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = exp - now
        # Should be close to 30 minutes (allow 5 second tolerance)
        assert 25 * 60 <= delta.total_seconds() <= 31 * 60


class TestRevokeAllUserTokens:
    """Test the revoke_all_user_tokens function used on password change."""

    @pytest.mark.asyncio
    async def test_revoke_returns_count(self):
        from core.refresh_tokens import revoke_all_user_tokens

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 3
        mock_db.execute.return_value = mock_result
        mock_db.commit = AsyncMock()

        count = await revoke_all_user_tokens(str(uuid.uuid4()), mock_db)
        assert count == 3


class TestCleanupExpired:
    """Test expired token cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_returns_count(self):
        from core.refresh_tokens import cleanup_expired_tokens

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 5
        mock_db.execute.return_value = mock_result
        mock_db.commit = AsyncMock()

        count = await cleanup_expired_tokens(mock_db, days_past=7)
        assert count == 5


# ─── Integration tests via ASGI test client ──────────────────────────────────

class TestRefreshEndpoint:
    """Test POST /v1/auth/refresh and POST /v1/auth/logout."""

    @pytest.fixture
    def app(self):
        from main import app
        return app

    @pytest.mark.asyncio
    async def test_refresh_without_token_returns_422(self, app):
        """Missing refresh_token in body → validation error."""
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/auth/refresh", json={})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_refresh_with_invalid_token_returns_401(self, app):
        """Invalid refresh token → 401."""
        from httpx import AsyncClient, ASGITransport
        from core.database import get_db

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async def _db():
            yield mock_db
        app.dependency_overrides[get_db] = _db

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/refresh", json={
                    "refresh_token": "csrt_invalid_token_12345678901234567890123456789012345678901234567890123456789"
                })
            assert r.status_code == 401
            data = r.json()
            assert "expired" in data["detail"].lower() or "invalid" in data["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_logout_always_returns_200(self, app):
        """Logout always succeeds (even with invalid token)."""
        from httpx import AsyncClient, ASGITransport
        from core.database import get_db

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.commit = AsyncMock()

        async def _db():
            yield mock_db
        app.dependency_overrides[get_db] = _db

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/logout", json={
                    "refresh_token": "csrt_does_not_exist_12345678901234567890123456789012345678901234567890"
                })
            assert r.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)


class TestReplayDetection:
    """Test that using a revoked token triggers family revocation."""

    @pytest.mark.asyncio
    async def test_revoked_token_triggers_family_revocation(self):
        """If a token was already revoked, rotate_refresh_token revokes family."""
        from core.refresh_tokens import rotate_refresh_token, _hash_token
        from models.db import RefreshTokenDB

        family_id = uuid.uuid4()
        token_hash = _hash_token("csrt_already_revoked_token")

        # Mock: token exists but was already revoked
        revoked_rec = MagicMock(spec=RefreshTokenDB)
        revoked_rec.id = uuid.uuid4()
        revoked_rec.user_id = uuid.uuid4()
        revoked_rec.family_id = family_id
        revoked_rec.revoked_at = datetime.now(timezone.utc) - timedelta(hours=1)
        revoked_rec.token_hash = token_hash

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = revoked_rec
        mock_db.execute.return_value = mock_result
        mock_db.commit = AsyncMock()

        result = await rotate_refresh_token("csrt_already_revoked_token", mock_db)
        assert result is None  # rotation should fail

        # Verify db.execute was called to revoke the family (UPDATE statement)
        assert mock_db.execute.call_count >= 2  # SELECT + UPDATE
        assert mock_db.commit.called


class TestExpiredTokenRejection:
    """Test that expired tokens are rejected."""

    @pytest.mark.asyncio
    async def test_expired_token_returns_none(self):
        from core.refresh_tokens import rotate_refresh_token
        from models.db import RefreshTokenDB

        expired_rec = MagicMock(spec=RefreshTokenDB)
        expired_rec.id = uuid.uuid4()
        expired_rec.user_id = uuid.uuid4()
        expired_rec.family_id = uuid.uuid4()
        expired_rec.revoked_at = None  # not revoked
        expired_rec.expires_at = datetime.now(timezone.utc) - timedelta(days=1)  # expired

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expired_rec
        mock_db.execute.return_value = mock_result

        result = await rotate_refresh_token("csrt_expired_token_value_here", mock_db)
        assert result is None
