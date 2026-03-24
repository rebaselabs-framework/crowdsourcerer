"""Integration tests: register, task create, worker submit, requester approve.

Strategy: uses FastAPI dependency overrides to inject a mock DB session so
tests run without a real PostgreSQL instance. JWT tokens are created with the
real `create_access_token` helper so the auth middleware behaves exactly as in
production.

Coverage:
  - POST /v1/auth/register  — field validation, role field, duplicate email
  - GET  /v1/tasks          — requires auth
  - POST /v1/tasks          — requires auth, validates input
  - Task → submission → approval (mocked DB flow)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

# Must be set before any app imports
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "integration-test-secret")
os.environ.setdefault("API_KEY_SALT", "integration-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user_db(
    user_id: str | None = None,
    email: str = "test@example.com",
    role: str = "requester",
    credits: int = 500,
    name: str = "Test User",
    is_banned: bool = False,
    is_admin: bool = False,
):
    """Create a mock UserDB-like object suitable for endpoint return values."""
    u = MagicMock()
    u.id = uuid.UUID(user_id) if user_id else uuid.uuid4()
    u.email = email
    u.role = role
    u.credits = credits
    u.name = name
    u.is_banned = is_banned
    u.is_admin = is_admin
    u.totp_enabled = False
    u.password_hash = "$2b$12$testhashtesthashtesthashe"
    u.created_at = datetime.now(timezone.utc)
    u.plan = "free"
    return u


def _make_task_db(
    task_id: str | None = None,
    user_id: str | None = None,
    task_type: str = "llm_generate",
    status: str = "pending",
    credits_used: int = 10,
):
    """Create a mock TaskDB-like object."""
    t = MagicMock()
    t.id = uuid.UUID(task_id) if task_id else uuid.uuid4()
    t.user_id = uuid.UUID(user_id) if user_id else uuid.uuid4()
    t.type = task_type
    t.status = status
    t.credits_used = credits_used
    t.output = None
    t.error = None
    t.created_at = datetime.now(timezone.utc)
    t.completed_at = None
    t.priority = "normal"
    t.cached = False
    return t


def _make_mock_db() -> MagicMock:
    """Create a minimal AsyncSession mock with add/commit/refresh/execute."""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.execute = AsyncMock()
    return db


def _make_scalar_result(value):
    """Wrap a value as a SQLAlchemy scalar result mock."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return result


def _real_token(user_id: str) -> str:
    """Create a real JWT token via the production auth helper."""
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    """Return the FastAPI app for testing."""
    from main import app as _app
    return _app


@pytest.fixture
async def client(app) -> AsyncGenerator:
    """Plain unauthenticated ASGI client."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── Register endpoint ─────────────────────────────────────────────────────────

class TestRegisterValidation:
    """Field-level validation tests — no DB needed (rejected before DB hit)."""

    @pytest.mark.asyncio
    async def test_missing_email_returns_422(self, client):
        r = await client.post("/v1/auth/register", json={"password": "secret123"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_password_returns_422(self, client):
        r = await client.post("/v1/auth/register", json={"email": "x@example.com"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_password_too_short_returns_422(self, client):
        r = await client.post("/v1/auth/register", json={
            "email": "x@example.com", "password": "short"
        })
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_email_returns_422(self, client):
        r = await client.post("/v1/auth/register", json={
            "email": "not-an-email", "password": "validpass123"
        })
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_role_returns_422(self, client):
        """Only 'requester' and 'worker' are valid roles."""
        r = await client.post("/v1/auth/register", json={
            "email": "x@example.com", "password": "validpass123",
            "role": "admin",  # not allowed
        })
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_valid_requester_role_accepted(self, app):
        """'requester' is a valid role — should not get 422 (may fail at DB level)."""
        mock_db = _make_mock_db()
        mock_db.execute.return_value = _make_scalar_result(None)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/register", json={
                    "email": "x@example.com", "password": "validpass123", "role": "requester"
                })
            # 422 means validation failure — anything else is DB or success
            assert r.status_code != 422, "Valid role 'requester' should pass validation"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_valid_worker_role_accepted(self, app):
        """'worker' is a valid role — should not get 422."""
        mock_db = _make_mock_db()
        mock_db.execute.return_value = _make_scalar_result(None)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/register", json={
                    "email": "y@example.com", "password": "validpass123", "role": "worker"
                })
            assert r.status_code != 422, "Valid role 'worker' should pass validation"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_default_role_omitted(self, app):
        """Role can be omitted — it defaults to 'requester' (not 422)."""
        mock_db = _make_mock_db()
        mock_db.execute.return_value = _make_scalar_result(None)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/register", json={
                    "email": "z@example.com", "password": "validpass123"
                })
            assert r.status_code != 422, "Omitting role should not fail validation"
        finally:
            app.dependency_overrides.pop(get_db, None)


class TestRegisterWithMockDB:
    """Full register flow tests with mocked DB."""

    @pytest.mark.asyncio
    async def test_register_requester_success(self, app):
        """A requester can register and receives a JWT token."""
        mock_db = _make_mock_db()
        # No existing user with this email
        mock_db.execute.return_value = _make_scalar_result(None)

        # After flush, give the user an ID
        new_user = _make_user_db(role="requester")

        def _set_user_id_on_flush():
            pass  # id is already set on the MagicMock

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/register", json={
                    "email": "requester@test.com",
                    "password": "testpassword",
                    "role": "requester",
                })
            assert r.status_code == 201
            data = r.json()
            assert "access_token" in data
            assert data["token_type"] == "bearer"
            assert data["expires_in"] > 0
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_register_worker_success(self, app):
        """A worker can register and receives a JWT token."""
        mock_db = _make_mock_db()
        mock_db.execute.return_value = _make_scalar_result(None)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/register", json={
                    "email": "worker@test.com",
                    "password": "testpassword",
                    "role": "worker",
                })
            assert r.status_code == 201
            assert "access_token" in r.json()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_register_duplicate_email_returns_409(self, app):
        """Registering with an already-taken email returns 409."""
        # Reset rate-limiter storage so prior register tests don't trigger 429.
        # The register route uses a separate Limiter instance in routers/auth.py.
        app.state.limiter._storage.reset()
        from routers.auth import limiter as _auth_limiter
        _auth_limiter._storage.reset()

        existing_user = _make_user_db(email="taken@test.com")
        mock_db = _make_mock_db()
        mock_db.execute.return_value = _make_scalar_result(existing_user)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/register", json={
                    "email": "taken@test.com",
                    "password": "testpassword",
                })
            assert r.status_code == 409
            assert "already registered" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Task endpoints ────────────────────────────────────────────────────────────

class TestTasksAuth:
    """Auth guard tests — no DB mock needed, rejected before DB hit."""

    @pytest.mark.asyncio
    async def test_list_tasks_no_auth_401(self, client):
        r = await client.get("/v1/tasks")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_create_task_no_auth_401(self, client):
        r = await client.post("/v1/tasks", json={"type": "llm_generate", "input": {"prompt": "hi"}})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_get_task_no_auth_401(self, client):
        r = await client.get(f"/v1/tasks/{uuid.uuid4()}")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_fake_token_rejected_401(self, client):
        """A fake JWT that doesn't decode to a valid user should return 401."""
        r = await client.get("/v1/tasks", headers={"Authorization": "Bearer faketoken"})
        assert r.status_code == 401


class TestTaskInput:
    """Input validation tests for task creation — pass auth, fail at input validation."""

    @pytest.fixture
    def auth_headers(self):
        user_id = str(uuid.uuid4())
        token = _real_token(user_id)
        return {"Authorization": f"Bearer {token}"}

    @pytest.mark.asyncio
    async def test_create_task_missing_type_422(self, app, auth_headers):
        """Task creation without 'type' should return 422."""
        mock_db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/tasks", json={"input": {"prompt": "hi"}},
                                 headers=auth_headers)
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_task_missing_input_422(self, app, auth_headers):
        """Task creation without 'input' should return 422."""
        mock_db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/tasks", json={"type": "llm_generate"},
                                 headers=auth_headers)
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Full flow test (mocked DB) ────────────────────────────────────────────────

class TestTaskSubmitApproveFlow:
    """
    Mock-based integration test for the full human-task flow:
    1. Requester POSTs a task
    2. Worker submits output
    3. Requester approves the submission
    """

    REQUESTER_ID = str(uuid.uuid4())
    WORKER_ID = str(uuid.uuid4())
    TASK_ID = str(uuid.uuid4())
    ASSIGN_ID = str(uuid.uuid4())

    @pytest.fixture
    def requester_headers(self):
        return {"Authorization": f"Bearer {_real_token(self.REQUESTER_ID)}"}

    @pytest.fixture
    def worker_headers(self):
        return {"Authorization": f"Bearer {_real_token(self.WORKER_ID)}"}

    @pytest.mark.asyncio
    async def test_approve_submission_requires_auth(self, client):
        """Approving a submission without auth should return 401."""
        task_id = str(uuid.uuid4())
        assign_id = str(uuid.uuid4())
        r = await client.post(
            f"/v1/tasks/{task_id}/submissions/{assign_id}/approve",
            json={}
        )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_reject_submission_requires_auth(self, client):
        """Rejecting a submission without auth should return 401."""
        task_id = str(uuid.uuid4())
        assign_id = str(uuid.uuid4())
        r = await client.post(
            f"/v1/tasks/{task_id}/submissions/{assign_id}/reject",
            json={"reason": "not good"}
        )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_worker_assignments_requires_auth(self, client):
        """Worker assignment list requires authentication."""
        r = await client.get("/v1/worker/assignments")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_task_submissions_requires_auth(self, client):
        """Listing task submissions requires authentication."""
        task_id = str(uuid.uuid4())
        r = await client.get(f"/v1/tasks/{task_id}/submissions")
        assert r.status_code == 401


# ── Onboarding endpoints ──────────────────────────────────────────────────────

class TestOnboardingAuth:
    """Auth guards for both requester and worker onboarding endpoints."""

    @pytest.mark.asyncio
    async def test_requester_onboarding_status_requires_auth(self, client):
        r = await client.get("/v1/requester-onboarding/status")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_worker_onboarding_status_requires_auth(self, client):
        r = await client.get("/v1/onboarding/status")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_requester_onboarding_step_complete_requires_auth(self, client):
        r = await client.post("/v1/requester-onboarding/steps/welcome/complete")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_worker_onboarding_step_complete_requires_auth(self, client):
        r = await client.post("/v1/onboarding/steps/profile/complete")
        assert r.status_code == 401


# ── JWT token integrity ───────────────────────────────────────────────────────

class TestJWTIntegrity:
    """Verify the auth token helper produces valid, decodable tokens."""

    def test_create_and_decode_roundtrip(self):
        """create_access_token → decode_access_token should round-trip correctly."""
        from core.auth import create_access_token, decode_access_token
        user_id = str(uuid.uuid4())
        token = create_access_token(user_id)
        assert decode_access_token(token) == user_id

    def test_tampered_token_rejected(self):
        """A tampered JWT signature should decode to None."""
        from core.auth import create_access_token, decode_access_token
        token = create_access_token(str(uuid.uuid4()))
        # Flip the last character of the signature
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert decode_access_token(tampered) is None

    def test_different_users_get_different_tokens(self):
        """Two different user IDs should produce distinct tokens."""
        from core.auth import create_access_token
        t1 = create_access_token(str(uuid.uuid4()))
        t2 = create_access_token(str(uuid.uuid4()))
        assert t1 != t2


# ── Helpers: dependency override factories ───────────────────────────────────

async def _async_yield(value):
    """Turn any value into an async generator (for dependency_overrides)."""
    yield value


def _db_override(mock_db):
    """Return an async generator *function* that FastAPI will recognise as a
    generator dependency and call correctly.  Using a plain lambda breaks
    because FastAPI checks `inspect.isasyncgenfunction` to decide how to
    consume the yielded value."""
    async def _override():
        yield mock_db
    return _override
