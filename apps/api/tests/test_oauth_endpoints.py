"""Comprehensive tests for the Google OAuth router (routers/oauth.py).

Tests cover:
  _sign_state / _verify_state pure helpers:
    1.  Round-trip sign → verify returns same payload keys
    2.  Tampered signature raises HTTPException 400
    3.  Expired state (ts > 600s ago) raises HTTPException 400
    4.  Malformed state (no dot separator) raises HTTPException 400
    5.  Invalid base64 encoding raises HTTPException 400

  GET /v1/auth/google:
    6.  Returns 503 when google_client_id is not configured
    7.  Redirects (307) when configured, URL contains Google auth endpoint
    8.  Redirect URL contains correct client_id
    9.  Role and next params are embedded in state
    10. Default role is "requester", default next is "/dashboard"

  GET /v1/auth/google/callback:
    11. Error from Google → redirects to /login?oauth_error=...
    12. Missing code → redirects with missing_params
    13. Missing state → redirects with missing_params
    14. Invalid state signature → redirects with error
    15. Token exchange failure (non-2xx) → redirects with token_exchange_failed
    16. No access_token in token response → redirects with no_access_token
    17. Userinfo fetch failure (non-2xx) → redirects with userinfo_failed
    18. Missing sub in userinfo → redirects with missing_user_info
    19. Missing email in userinfo → redirects with missing_user_info
    20. Network error (httpx raises) → redirects with network_error
    21. Disabled account (is_active=False) → redirects with account_disabled
    22. New user creation: happy path, correct role from state
    23. New user creation: invalid role in state falls back to "requester"
    24. Existing user by google_id: returning user flow
    25. Account linking: existing user found by email gets google_id set
    26. Email verification: Google-verified email updates user's email_verified
    27. Email not verified by Google: user's email_verified stays unchanged
    28. State next param is forwarded to final redirect
    29. Refresh token is included in redirect when available
    30. Refresh token failure is non-fatal (best-effort)
"""

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, ASGITransport


# ── Mock DB helpers ──────────────────────────────────────────────────────────

def _make_mock_db():
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock(return_value=0)
    db.get = AsyncMock(return_value=None)

    async def _refresh(obj):
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "token_version", None) is None:
            obj.token_version = 0
        # SQLAlchemy column defaults are server-side; simulate them for unmapped instances
        if getattr(obj, "is_active", None) is None:
            obj.is_active = True
    db.refresh = _refresh
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _scalar(value):
    """Create a mock result whose scalar_one_or_none() returns value."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


# ── UserDB patch helper ─────────────────────────────────────────────────────
# SQLAlchemy column defaults (like is_active=True) are only applied on flush/
# commit. When testing without a real DB, a raw UserDB() instance has
# is_active=None which is falsy. This wrapper ensures the column default is
# present so the endpoint's `if not user.is_active` check behaves correctly.

def _patched_user_db_class():
    """Return a wrapper around UserDB that sets is_active=True on new instances."""
    import warnings
    from models.db import UserDB as RealUserDB

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        warnings.filterwarnings("ignore", message=".*string-lookup table.*")

        class PatchedUserDB(RealUserDB):
            def __init__(self, **kwargs):
                kwargs.setdefault("is_active", True)
                super().__init__(**kwargs)

        PatchedUserDB.__tablename__ = RealUserDB.__tablename__
    return PatchedUserDB


# ── Mock settings helper ────────────────────────────────────────────────────

def _make_settings(**overrides):
    """Return a MagicMock that behaves like core.config.Settings."""
    defaults = {
        "jwt_secret": "test-secret",
        "jwt_algorithm": "HS256",
        "jwt_expire_minutes": 30,
        "google_client_id": "",
        "google_client_secret": "",
        "free_tier_credits": 1000,
        "api_key_salt": "test-salt",
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


# ── Mock httpx helpers ───────────────────────────────────────────────────────

def _mock_httpx_client(token_response=None, info_response=None, raise_exc=None):
    """Build a mock httpx.AsyncClient context manager.

    Args:
        token_response: dict with keys is_success (bool), json (callable)
        info_response: dict with keys is_success (bool), json (callable)
        raise_exc: if set, client.post() will raise this exception
    """
    mock_client = AsyncMock()

    if raise_exc:
        mock_client.post = AsyncMock(side_effect=raise_exc)
        mock_client.get = AsyncMock(side_effect=raise_exc)
    else:
        if token_response:
            tok_res = MagicMock()
            tok_res.is_success = token_response.get("is_success", True)
            tok_res.json = MagicMock(return_value=token_response.get("json", {}))
            mock_client.post = AsyncMock(return_value=tok_res)
        if info_response:
            info_res = MagicMock()
            info_res.is_success = info_response.get("is_success", True)
            info_res.json = MagicMock(return_value=info_response.get("json", {}))
            mock_client.get = AsyncMock(return_value=info_res)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx, mock_client


# ── Mock user helper ─────────────────────────────────────────────────────────

def _make_user(
    user_id=None,
    email="test@example.com",
    google_id="google-123",
    is_active=True,
    email_verified=False,
    token_version=0,
    name="Test User",
    role="requester",
):
    user = MagicMock()
    user.id = user_id or uuid.uuid4()
    user.email = email
    user.google_id = google_id
    user.is_active = is_active
    user.email_verified = email_verified
    user.email_verification_token_hash = "some-hash" if not email_verified else None
    user.token_version = token_version
    user.name = name
    user.role = role
    user.created_at = datetime.now(timezone.utc)
    return user


# ═════════════════════════════════════════════════════════════════════════════
# _sign_state / _verify_state pure helper tests
# ═════════════════════════════════════════════════════════════════════════════

class TestSignVerifyState:
    """Tests for the _sign_state and _verify_state HMAC helpers."""

    def test_round_trip(self):
        """sign then verify returns same payload keys."""
        import time as _time
        from routers.oauth import _sign_state, _verify_state
        ts = int(_time.time())
        payload = {"nonce": "abc123", "ts": ts, "role": "worker", "next": "/tasks"}
        state = _sign_state(payload)
        result = _verify_state(state)
        assert result["role"] == "worker"
        assert result["next"] == "/tasks"
        assert result["nonce"] == "abc123"
        assert result["ts"] == ts

    def test_tampered_signature_raises(self):
        """Changing the signature should raise HTTPException 400."""
        from routers.oauth import _sign_state, _verify_state
        import time as _time
        payload = {"nonce": "x", "ts": int(_time.time()), "role": "requester", "next": "/"}
        state = _sign_state(payload)
        encoded, sig = state.rsplit(".", 1)
        tampered = f"{encoded}.{'a' * len(sig)}"
        with pytest.raises(HTTPException) as exc_info:
            _verify_state(tampered)
        assert exc_info.value.status_code == 400
        assert "signature mismatch" in exc_info.value.detail.lower() or "CSRF" in exc_info.value.detail

    def test_expired_state_raises(self):
        """State older than 600 seconds should raise HTTPException 400."""
        from routers.oauth import _sign_state, _verify_state
        import time as _time
        payload = {"nonce": "x", "ts": int(_time.time()) - 700, "role": "requester", "next": "/"}
        state = _sign_state(payload)
        with pytest.raises(HTTPException) as exc_info:
            _verify_state(state)
        assert exc_info.value.status_code == 400
        assert "expired" in exc_info.value.detail.lower()

    def test_malformed_state_no_dot(self):
        """State with no dot separator should raise HTTPException 400."""
        from routers.oauth import _verify_state
        with pytest.raises(HTTPException) as exc_info:
            _verify_state("nodothere")
        assert exc_info.value.status_code == 400

    def test_invalid_base64_encoding(self):
        """Valid HMAC but garbled base64 payload should raise HTTPException 400."""
        import hashlib
        import hmac as _hmac
        from routers.oauth import _verify_state
        from core.config import get_settings
        settings = get_settings()
        bad_encoded = "not!!!valid!!!base64"
        sig = _hmac.new(
            settings.jwt_secret.encode(), bad_encoded.encode(), hashlib.sha256
        ).hexdigest()
        state = f"{bad_encoded}.{sig}"
        with pytest.raises(HTTPException) as exc_info:
            _verify_state(state)
        assert exc_info.value.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# GET /v1/auth/google
# ═════════════════════════════════════════════════════════════════════════════

class TestGoogleLogin:
    """Tests for GET /v1/auth/google."""

    @pytest.mark.asyncio
    async def test_returns_503_when_not_configured(self):
        """Returns 503 when google_client_id is empty."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        mock_settings = _make_settings(google_client_id="")

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.get_settings", return_value=mock_settings):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get("/v1/auth/google")
                    assert r.status_code == 503
                    assert "not configured" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_redirects_when_configured(self):
        """Returns 307 redirect to Google when client_id is set."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        mock_settings = _make_settings(
            google_client_id="test-client-id",
            google_client_secret="test-secret",
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.get_settings", return_value=mock_settings):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get("/v1/auth/google")
                    assert r.status_code == 307
                    location = r.headers["location"]
                    assert "accounts.google.com" in location
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_redirect_contains_client_id(self):
        """Redirect URL contains the configured client_id."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        mock_settings = _make_settings(
            google_client_id="my-gcp-client-id",
            google_client_secret="my-secret",
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.get_settings", return_value=mock_settings):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get("/v1/auth/google")
                    location = r.headers["location"]
                    parsed = parse_qs(urlparse(location).query)
                    assert parsed["client_id"] == ["my-gcp-client-id"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_role_and_next_embedded_in_state(self):
        """Custom role and next params appear in the signed state."""
        from main import app
        from core.database import get_db
        from routers.oauth import _verify_state

        mock_db = _make_mock_db()
        mock_settings = _make_settings(
            google_client_id="cid",
            google_client_secret="csecret",
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.get_settings", return_value=mock_settings):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get("/v1/auth/google?role=worker&next=/my-tasks")
                    location = r.headers["location"]
                    parsed = parse_qs(urlparse(location).query)
                    state_str = parsed["state"][0]
                    payload = _verify_state(state_str)
                    assert payload["role"] == "worker"
                    assert payload["next"] == "/my-tasks"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_defaults_role_requester_and_next_dashboard(self):
        """Without role/next params, defaults are requester and /dashboard."""
        from main import app
        from core.database import get_db
        from routers.oauth import _verify_state

        mock_db = _make_mock_db()
        mock_settings = _make_settings(
            google_client_id="cid",
            google_client_secret="csecret",
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.get_settings", return_value=mock_settings):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get("/v1/auth/google")
                    location = r.headers["location"]
                    parsed = parse_qs(urlparse(location).query)
                    state_str = parsed["state"][0]
                    payload = _verify_state(state_str)
                    assert payload["role"] == "requester"
                    assert payload["next"] == "/dashboard"
        finally:
            app.dependency_overrides.pop(get_db, None)


# ═════════════════════════════════════════════════════════════════════════════
# GET /v1/auth/google/callback
# ═════════════════════════════════════════════════════════════════════════════

def _valid_state(role="requester", next_path="/dashboard"):
    """Generate a properly signed state string for testing."""
    import time as _time
    from routers.oauth import _sign_state
    return _sign_state({
        "nonce": os.urandom(8).hex(),
        "ts": int(_time.time()),
        "role": role,
        "next": next_path,
    })


def _good_token_response():
    return {
        "is_success": True,
        "json": {"access_token": "google-access-token-abc", "token_type": "Bearer"},
    }


def _good_userinfo(sub="google-sub-123", email="user@gmail.com", name="Jane Doe", email_verified=True):
    return {
        "is_success": True,
        "json": {
            "sub": sub,
            "email": email,
            "name": name,
            "email_verified": email_verified,
        },
    }


class TestGoogleCallback:
    """Tests for GET /v1/auth/google/callback."""

    @pytest.mark.asyncio
    async def test_error_from_google_redirects(self):
        """When Google returns ?error=access_denied, redirect to /login with error."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                r = await client.get("/v1/auth/google/callback?error=access_denied")
                assert r.status_code == 307
                assert "/login" in r.headers["location"]
                assert "oauth_error=access_denied" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_missing_code_redirects(self):
        """Missing code param redirects with missing_params."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                r = await client.get("/v1/auth/google/callback?state=something")
                assert r.status_code == 307
                assert "missing_params" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_missing_state_redirects(self):
        """Missing state param redirects with missing_params."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                r = await client.get("/v1/auth/google/callback?code=abc")
                assert r.status_code == 307
                assert "missing_params" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_state_signature_redirects(self):
        """Tampered state redirects with error message."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                r = await client.get(
                    "/v1/auth/google/callback?code=abc&state=bad.signature"
                )
                assert r.status_code == 307
                assert "oauth_error" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_token_exchange_failure(self):
        """Non-2xx from Google token endpoint redirects with token_exchange_failed."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()
        mock_ctx, _ = _mock_httpx_client(
            token_response={"is_success": False, "json": {"error": "invalid_grant"}},
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "token_exchange_failed" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_no_access_token_in_response(self):
        """Token response without access_token redirects with no_access_token."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()
        mock_ctx, _ = _mock_httpx_client(
            token_response={"is_success": True, "json": {"token_type": "Bearer"}},
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "no_access_token" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_userinfo_fetch_failure(self):
        """Non-2xx from Google userinfo endpoint redirects with userinfo_failed."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()
        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response={"is_success": False, "json": {}},
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "userinfo_failed" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_missing_sub_in_userinfo(self):
        """Userinfo without 'sub' redirects with missing_user_info."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()
        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response={
                "is_success": True,
                "json": {"email": "a@b.com", "name": "X"},
            },
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "missing_user_info" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_missing_email_in_userinfo(self):
        """Userinfo without 'email' redirects with missing_user_info."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()
        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response={
                "is_success": True,
                "json": {"sub": "123", "name": "X"},
            },
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "missing_user_info" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_network_error_redirects(self):
        """httpx network exception redirects with network_error."""
        from main import app
        from core.database import get_db
        import httpx as _httpx

        mock_db = _make_mock_db()
        state = _valid_state()
        mock_ctx, _ = _mock_httpx_client(raise_exc=_httpx.ConnectError("connection refused"))

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "network_error" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_disabled_account_redirects(self):
        """User with is_active=False redirects with account_disabled."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()

        disabled_user = _make_user(is_active=False, google_id="google-sub-123")
        mock_db.execute = AsyncMock(return_value=_scalar(disabled_user))

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(sub="google-sub-123"),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "account_disabled" in r.headers["location"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_new_user_creation_happy_path(self):
        """Brand-new user: creates user with correct role and redirects with JWT."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state(role="worker", next_path="/my-tasks")

        # Both queries return None → new user
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(None),  # by google_id
            _scalar(None),  # by email
        ])

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(sub="new-google-sub", email="new@gmail.com", name="New User"),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.UserDB", _patched_user_db_class()), \
                 patch("routers.oauth.create_access_token", return_value="jwt-token-xyz") as mock_jwt, \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, return_value=("refresh-tok", datetime.now(timezone.utc))):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    location = r.headers["location"]
                    assert "/auth/google-success" in location
                    assert "token=jwt-token-xyz" in location
                    assert "next=" in location

                    # Verify db.add was called (new user created)
                    mock_db.add.assert_called_once()
                    added_user = mock_db.add.call_args[0][0]
                    assert added_user.email == "new@gmail.com"
                    assert added_user.role == "worker"
                    assert added_user.google_id == "new-google-sub"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_new_user_invalid_role_falls_back_to_requester(self):
        """Invalid role in state falls back to 'requester'."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state(role="admin")  # invalid role

        mock_db.execute = AsyncMock(side_effect=[
            _scalar(None),  # by google_id
            _scalar(None),  # by email
        ])

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(sub="new-sub", email="new2@gmail.com"),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.UserDB", _patched_user_db_class()), \
                 patch("routers.oauth.create_access_token", return_value="jwt-tok"), \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, return_value=("rt", datetime.now(timezone.utc))):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    location = r.headers["location"]
                    assert "/auth/google-success" in location
                    mock_db.add.assert_called_once()
                    added_user = mock_db.add.call_args[0][0]
                    assert added_user.role == "requester"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_existing_user_by_google_id(self):
        """Returning user found by google_id skips creation, issues JWT."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()

        existing = _make_user(google_id="google-sub-123", email="existing@gmail.com")
        mock_db.execute = AsyncMock(return_value=_scalar(existing))

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(sub="google-sub-123", email="existing@gmail.com"),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.create_access_token", return_value="jwt-returning") as mock_jwt, \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, return_value=("rt", datetime.now(timezone.utc))):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "jwt-returning" in r.headers["location"]
                    # db.add should NOT have been called (existing user)
                    mock_db.add.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_account_linking_by_email(self):
        """Existing user found by email gets google_id linked."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()

        existing = _make_user(google_id=None, email="linked@gmail.com", email_verified=False)
        # First query (by google_id) returns None, second (by email) finds the user
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(None),      # by google_id
            _scalar(existing),  # by email
        ])

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(
                sub="new-google-sub",
                email="linked@gmail.com",
                email_verified=True,
            ),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.create_access_token", return_value="jwt-linked"), \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, return_value=("rt", datetime.now(timezone.utc))):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    assert "jwt-linked" in r.headers["location"]
                    # google_id should now be set on the existing user
                    assert existing.google_id == "new-google-sub"
                    # db.add should NOT have been called (linked, not created)
                    mock_db.add.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_email_verification_from_google(self):
        """Google-verified email updates user's email_verified to True."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()

        existing = _make_user(
            google_id=None,
            email="verify@gmail.com",
            email_verified=False,
        )
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(None),      # by google_id
            _scalar(existing),  # by email
        ])

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(
                sub="g-sub-456",
                email="verify@gmail.com",
                email_verified=True,
            ),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.create_access_token", return_value="jwt-v"), \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, return_value=("rt", datetime.now(timezone.utc))):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert existing.email_verified is True
                    assert existing.email_verification_token_hash is None
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_email_not_verified_by_google_stays_unchanged(self):
        """If Google says email_verified=False, user's email_verified is not changed."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()

        existing = _make_user(
            google_id=None,
            email="unverified@gmail.com",
            email_verified=False,
        )
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(None),      # by google_id
            _scalar(existing),  # by email
        ])

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(
                sub="g-sub-789",
                email="unverified@gmail.com",
                email_verified=False,
            ),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.create_access_token", return_value="jwt-uv"), \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, return_value=("rt", datetime.now(timezone.utc))):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    # Should NOT have been set to True
                    assert existing.email_verified is False
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_state_next_param_forwarded_to_redirect(self):
        """The next param from state appears in the final redirect URL."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state(next_path="/custom/path")

        mock_db.execute = AsyncMock(side_effect=[
            _scalar(None),
            _scalar(None),
        ])

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.UserDB", _patched_user_db_class()), \
                 patch("routers.oauth.create_access_token", return_value="jwt-next"), \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, return_value=("rt", datetime.now(timezone.utc))):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    location = r.headers["location"]
                    assert "/auth/google-success" in location
                    # URL-decode the next param and verify
                    assert "/custom/path" in location
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_refresh_token_included_in_redirect(self):
        """When refresh token creation succeeds, it appears in the redirect."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()

        mock_db.execute = AsyncMock(side_effect=[
            _scalar(None),
            _scalar(None),
        ])

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.UserDB", _patched_user_db_class()), \
                 patch("routers.oauth.create_access_token", return_value="jwt-rt"), \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, return_value=("csrt_refresh_token_value", datetime.now(timezone.utc))):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    location = r.headers["location"]
                    assert "/auth/google-success" in location
                    assert "refresh=csrt_refresh_token_value" in location
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_refresh_token_failure_is_nonfatal(self):
        """If refresh token creation fails, the redirect still works (best-effort)."""
        from main import app
        from core.database import get_db

        mock_db = _make_mock_db()
        state = _valid_state()

        mock_db.execute = AsyncMock(side_effect=[
            _scalar(None),
            _scalar(None),
        ])

        mock_ctx, _ = _mock_httpx_client(
            token_response=_good_token_response(),
            info_response=_good_userinfo(),
        )

        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            with patch("routers.oauth.httpx.AsyncClient", return_value=mock_ctx), \
                 patch("routers.oauth.UserDB", _patched_user_db_class()), \
                 patch("routers.oauth.create_access_token", return_value="jwt-no-rt"), \
                 patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock, side_effect=Exception("DB error")):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    r = await client.get(
                        f"/v1/auth/google/callback?code=abc&state={state}"
                    )
                    assert r.status_code == 307
                    location = r.headers["location"]
                    assert "/auth/google-success" in location
                    assert "token=jwt-no-rt" in location
                    # refresh param should NOT be present
                    assert "refresh=" not in location
        finally:
            app.dependency_overrides.pop(get_db, None)
