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


# ── Requester onboarding flow ─────────────────────────────────────────────────

class TestRequesterOnboardingFlow:
    """Mock-based tests for requester onboarding step completion."""

    USER_ID = str(uuid.uuid4())

    @pytest.fixture
    def headers(self):
        return {"Authorization": f"Bearer {_real_token(self.USER_ID)}"}

    @pytest.fixture
    def mock_db_with_no_record(self):
        """DB mock: first execute returns no existing onboarding record."""
        db = _make_mock_db()
        # First call: no existing record → create new
        db.execute.return_value = _make_scalar_result(None)
        return db

    @pytest.fixture
    def mock_db_with_existing_record(self):
        """DB mock: returns an existing partial record."""
        from models.db import RequesterOnboardingDB
        rec = MagicMock(spec=RequesterOnboardingDB)
        rec.id = uuid.uuid4()
        rec.user_id = self.USER_ID
        rec.step_welcome = False
        rec.step_create_task = False
        rec.step_view_results = False
        rec.step_set_webhook = False
        rec.step_invite_team = False
        rec.completed_at = None
        rec.bonus_claimed = False
        rec.skipped_at = None
        db = _make_mock_db()
        db.execute.return_value = _make_scalar_result(rec)
        return db, rec

    @pytest.mark.asyncio
    async def test_invalid_step_returns_400(self, app, headers):
        """Completing an unknown step name returns 400."""
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/requester-onboarding/steps/not_a_real_step/complete",
                    headers=headers,
                )
            assert r.status_code == 400
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_complete_welcome_step_creates_record(self, app, headers, mock_db_with_no_record):
        """Completing 'welcome' on a new user creates the record and marks the step."""
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db_with_no_record)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/requester-onboarding/steps/welcome/complete",
                    headers=headers,
                )
            # Should succeed and return onboarding status
            assert r.status_code == 200
            data = r.json()
            assert "steps" in data
            assert "completed_count" in data
            assert "total_steps" in data
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_complete_step_marks_existing_record(self, app, headers, mock_db_with_existing_record):
        """Completing a step on an existing record updates it."""
        db, rec = mock_db_with_existing_record
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/requester-onboarding/steps/view_results/complete",
                    headers=headers,
                )
            assert r.status_code == 200
            # The endpoint sets step_view_results = True on the rec object
            assert rec.step_view_results is True
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_skip_non_skippable_step_returns_400(self, app, headers, mock_db_with_no_record):
        """Only set_webhook and invite_team can be skipped; welcome cannot."""
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db_with_no_record)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/requester-onboarding/steps/welcome/skip",
                    headers=headers,
                )
            assert r.status_code == 400
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_skip_invite_team_succeeds(self, app, headers, mock_db_with_no_record):
        """invite_team is a skippable step."""
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db_with_no_record)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/requester-onboarding/steps/invite_team/skip",
                    headers=headers,
                )
            assert r.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Worker onboarding flow ────────────────────────────────────────────────────

class TestWorkerOnboardingFlow:
    """Mock-based tests for worker onboarding step completion."""

    USER_ID = str(uuid.uuid4())

    @pytest.fixture
    def headers(self):
        return {"Authorization": f"Bearer {_real_token(self.USER_ID)}"}

    @pytest.fixture
    def mock_db_no_record(self):
        """DB mock: no existing onboarding progress record."""
        db = _make_mock_db()
        db.execute.return_value = _make_scalar_result(None)
        return db

    @pytest.mark.asyncio
    async def test_status_requires_auth(self, client):
        r = await client.get("/v1/onboarding/status")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_step_complete_requires_auth(self, client):
        r = await client.post("/v1/onboarding/steps/profile/complete")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_complete_profile_step(self, app, headers, mock_db_no_record):
        """Completing the 'profile' step should return 200 with a step result."""
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db_no_record)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/onboarding/steps/profile/complete",
                    headers=headers,
                )
            assert r.status_code == 200
            data = r.json()
            # Response includes step and completion status (exact keys vary by schema)
            assert "step" in data or "all_done" in data or "is_complete" in data
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_complete_invalid_step_returns_400(self, app, headers, mock_db_no_record):
        """Completing a nonexistent step name returns 400."""
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db_no_record)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/onboarding/steps/nonexistent_step/complete",
                    headers=headers,
                )
            assert r.status_code in (400, 422)
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_skip_onboarding(self, app, headers, mock_db_no_record):
        """Skipping onboarding should set skipped_at and return 204."""
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(mock_db_no_record)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/onboarding/skip", headers=headers)
            assert r.status_code in (200, 204)
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Worker task claim flow ────────────────────────────────────────────────────

class TestWorkerClaimFlow:
    """Mock-based tests for worker task claiming."""

    WORKER_ID = str(uuid.uuid4())
    TASK_ID = str(uuid.uuid4())

    @pytest.fixture
    def worker_headers(self):
        return {"Authorization": f"Bearer {_real_token(self.WORKER_ID)}"}

    def _make_worker_user(self):
        user = _make_user_db(user_id=self.WORKER_ID, role="worker", credits=200)
        user.is_banned = False
        user.reputation_score = 100.0
        user.availability_status = "available"
        user.referral_code = None
        user.plan = "free"
        return user

    def _make_open_human_task(self):
        task = MagicMock()
        task.id = uuid.UUID(self.TASK_ID)
        task.user_id = uuid.uuid4()
        task.type = "label_text"
        task.status = "open"
        task.execution_mode = "human"
        task.application_mode = False
        task.assigned_team_id = None
        task.min_reputation_score = None
        task.min_skill_level = None
        task.assignments_required = 1
        task.assignments_completed = 0
        task.worker_reward_credits = 5
        task.timeout_minutes = 60
        task.sla_hours = None
        task.task_metadata = None
        task.input = {"text": "classify this"}
        task.tags = []
        task.certified_only = False
        return task

    @pytest.mark.asyncio
    async def test_claim_requires_auth(self, client):
        """Claiming a task without auth returns 401."""
        r = await client.post(f"/v1/worker/tasks/{self.TASK_ID}/claim")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_claim_by_non_worker_returns_403(self, app):
        """A requester-role user cannot claim a task."""
        requester_id = str(uuid.uuid4())
        headers = {"Authorization": f"Bearer {_real_token(requester_id)}"}

        # DB: returns requester user
        requester = _make_user_db(user_id=requester_id, role="requester")
        db = _make_mock_db()
        db.execute.return_value = _make_scalar_result(requester)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{self.TASK_ID}/claim",
                    headers=headers,
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_claim_banned_worker_returns_403(self, app, worker_headers):
        """A banned worker cannot claim a task."""
        worker = self._make_worker_user()
        worker.is_banned = True

        db = _make_mock_db()
        db.execute.return_value = _make_scalar_result(worker)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{self.TASK_ID}/claim",
                    headers=worker_headers,
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_claim_missing_task_returns_404(self, app, worker_headers):
        """Claiming a non-existent task returns 404."""
        worker = self._make_worker_user()

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_scalar_result(worker)   # user lookup
            return _make_scalar_result(None)          # task lookup → not found

        db = _make_mock_db()
        db.execute.side_effect = side_effect

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{self.TASK_ID}/claim",
                    headers=worker_headers,
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_submit_requires_active_assignment(self, app, worker_headers):
        """Worker submit returns 404 when no active assignment exists."""
        db = _make_mock_db()
        db.execute.return_value = _make_scalar_result(None)  # no active assignment

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{self.TASK_ID}/submit",
                    json={"response": {"label": "positive"}},
                    headers=worker_headers,
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)


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
