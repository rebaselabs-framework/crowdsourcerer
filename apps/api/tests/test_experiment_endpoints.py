"""Tests for the A/B experiments router endpoints.

NOTE: `get_current_user_id` with JWT tokens does NOT call db.execute
(it decodes the JWT in-memory). So the first db.execute call in each
test is the actual endpoint logic, not auth.

Covers:
  - POST /v1/experiments — create experiment
  - GET /v1/experiments — list experiments
  - GET /v1/experiments/{id} — get experiment detail
  - PATCH /v1/experiments/{id}/status — update status
  - DELETE /v1/experiments/{id} — delete experiment
  - POST /v1/experiments/{id}/enroll — enroll task
  - GET /v1/experiments/{id}/results — get results
  - POST /v1/experiments/{id}/record-outcome — record outcome
  - Pure function tests for chi-squared, winner picking, etc.
"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

USER_ID = str(uuid.uuid4())
EXP_ID = str(uuid.uuid4())
VARIANT_A_ID = str(uuid.uuid4())
VARIANT_B_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())


# ── Helpers ──────────────────────────────────────────────────────────────────

def _token(user_id: str = None) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id or USER_ID)


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
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async def _refresh(obj):
        if not getattr(obj, "created_at", None):
            obj.created_at = now
        if not getattr(obj, "updated_at", None):
            obj.updated_at = now
    db.refresh = _refresh
    db.delete = AsyncMock()
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _scalars_list(items):
    """Return a mock result where .scalars().all() returns items."""
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.scalar_one_or_none = MagicMock(return_value=None)
    return r


def _scalars_iter(items):
    """Return a mock result where for v in .scalars(): yields items."""
    r = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.__iter__ = MagicMock(return_value=iter(items))
    mock_scalars.all = MagicMock(return_value=items)
    r.scalars = MagicMock(return_value=mock_scalars)
    return r


def _make_variant(variant_id=None, experiment_id=None, name="A", is_control=True,
                  traffic_pct=50.0, participant_count=0, completion_count=0,
                  failure_count=0, total_accuracy=0.0, total_duration_ms=0,
                  total_credits_used=0, task_config=None):
    return SimpleNamespace(
        id=uuid.UUID(variant_id or str(uuid.uuid4())),
        experiment_id=uuid.UUID(experiment_id or EXP_ID),
        name=name,
        description=f"Variant {name}",
        traffic_pct=traffic_pct,
        task_config=task_config,
        is_control=is_control,
        participant_count=participant_count,
        completion_count=completion_count,
        failure_count=failure_count,
        total_accuracy=total_accuracy,
        total_duration_ms=total_duration_ms,
        total_credits_used=total_credits_used,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _make_experiment(exp_id=None, user_id=None, status="draft", variants=None):
    return SimpleNamespace(
        id=uuid.UUID(exp_id or EXP_ID),
        user_id=uuid.UUID(user_id or USER_ID),
        name="Test Experiment",
        description="Test A/B experiment",
        hypothesis="Variant B is better",
        status=status,
        task_type="web_research",
        primary_metric="completion_rate",
        started_at=None,
        ended_at=None,
        winner_variant_id=None,
        variants=variants or [],
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _make_participant(task_id=None, variant_id=None, experiment_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        experiment_id=uuid.UUID(experiment_id or EXP_ID),
        variant_id=uuid.UUID(variant_id or VARIANT_A_ID),
        task_id=uuid.UUID(task_id or TASK_ID),
        user_id=uuid.UUID(USER_ID),
        completed_at=None,
        outcome=None,
        accuracy=None,
        duration_ms=None,
        credits_used=None,
    )


def _make_task(task_id=None, user_id=None):
    return SimpleNamespace(
        id=uuid.UUID(task_id or TASK_ID),
        user_id=uuid.UUID(user_id or USER_ID),
        type="web_research",
        status="pending",
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def headers():
    return {"Authorization": f"Bearer {_token(USER_ID)}"}


# ── Auth Tests ───────────────────────────────────────────────────────────────

class TestExperimentAuth:

    @pytest.mark.asyncio
    async def test_create_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/experiments", json={
                "name": "Test", "variants": [
                    {"name": "A", "traffic_pct": 50, "is_control": True},
                    {"name": "B", "traffic_pct": 50},
                ]
            })
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/experiments")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_get_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/experiments/{EXP_ID}")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(f"/v1/experiments/{EXP_ID}")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_results_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/experiments/{EXP_ID}/results")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_enroll_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/experiments/{EXP_ID}/enroll", json={
                "task_id": TASK_ID, "experiment_id": EXP_ID,
            })
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_record_outcome_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/experiments/{EXP_ID}/record-outcome?task_id={TASK_ID}&outcome=completed")
        assert r.status_code == 401


# ── Create Experiment ────────────────────────────────────────────────────────

class TestCreateExperiment:

    @pytest.mark.asyncio
    async def test_create_success(self, app, headers):
        """POST /v1/experiments creates and returns 201."""
        db = _make_mock_db()
        # Variants need to be MagicMock (not SimpleNamespace) because
        # the endpoint creates a real ABExperimentDB and assigns variants
        # via SQLAlchemy relationship, which checks _sa_instance_state.
        def _make_mock_variant(vid, name, ctrl=True):
            v = MagicMock()
            v.id = uuid.UUID(vid)
            v.experiment_id = uuid.uuid4()
            v.name = name
            v.description = f"Variant {name}"
            v.traffic_pct = 50.0
            v.task_config = None
            v.is_control = ctrl
            v.participant_count = 0
            v.completion_count = 0
            v.failure_count = 0
            v.total_accuracy = 0.0
            v.total_duration_ms = 0
            v.total_credits_used = 0
            v.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            return v
        va = _make_mock_variant(VARIANT_A_ID, "A")
        vb = _make_mock_variant(VARIANT_B_ID, "B", False)
        db.execute = AsyncMock(return_value=_scalars_list([va, vb]))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/experiments", headers=headers, json={
                    "name": "Test Exp",
                    "description": "Testing",
                    "hypothesis": "B is better",
                    "task_type": "web_research",
                    "variants": [
                        {"name": "A", "traffic_pct": 50, "is_control": True},
                        {"name": "B", "traffic_pct": 50},
                    ]
                })
            assert r.status_code == 201
            data = r.json()
            assert "id" in data
            assert data["status"] == "draft"
            assert "variants" in data
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_traffic_pct_must_sum_to_100(self, app, headers):
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/experiments", headers=headers, json={
                    "name": "Bad Exp",
                    "variants": [
                        {"name": "A", "traffic_pct": 30},
                        {"name": "B", "traffic_pct": 30},
                    ]
                })
            assert r.status_code == 400
            assert "sum to 100" in r.json()["detail"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_requires_at_least_2_variants(self, app, headers):
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/experiments", headers=headers, json={
                    "name": "Single Variant",
                    "variants": [{"name": "A", "traffic_pct": 100}],
                })
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_max_5_variants(self, app, headers):
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/experiments", headers=headers, json={
                    "name": "Too Many",
                    "variants": [
                        {"name": f"V{i}", "traffic_pct": 100 / 6}
                        for i in range(6)
                    ],
                })
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── List Experiments ─────────────────────────────────────────────────────────

class TestListExperiments:

    @pytest.mark.asyncio
    async def test_list_empty(self, app, headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_list([]))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/experiments", headers=headers)
            assert r.status_code == 200
            assert r.json() == []
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_list_with_status_filter(self, app, headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_list([]))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/experiments?status=running", headers=headers)
            assert r.status_code == 200
            assert isinstance(r.json(), list)
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Get Experiment ───────────────────────────────────────────────────────────

class TestGetExperiment:

    @pytest.mark.asyncio
    async def test_get_success(self, app, headers):
        db = _make_mock_db()
        va = _make_variant(variant_id=VARIANT_A_ID, name="A",
                          participant_count=10, completion_count=7, failure_count=3,
                          total_accuracy=6.0, total_duration_ms=50000, total_credits_used=14)
        vb = _make_variant(variant_id=VARIANT_B_ID, name="B", is_control=False,
                          participant_count=10, completion_count=8, failure_count=2)
        exp = _make_experiment(variants=[va, vb])
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)  # _get_exp
            return _scalars_list([va, vb])  # variant loading
        db.execute = AsyncMock(side_effect=_side)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/experiments/{EXP_ID}", headers=headers)
            assert r.status_code == 200
            data = r.json()
            assert data["id"] == EXP_ID
            assert data["name"] == "Test Experiment"
            assert len(data["variants"]) == 2
            assert data["variants"][0]["name"] in ("A", "B")
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_get_not_found(self, app, headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/experiments/{EXP_ID}", headers=headers)
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Update Status ────────────────────────────────────────────────────────────

class TestUpdateStatus:

    @pytest.mark.asyncio
    async def test_start_experiment(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="draft")
        db.execute = AsyncMock(return_value=_scalar(exp))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/experiments/{EXP_ID}/status?status=running", headers=headers)
            assert r.status_code == 200
            assert r.json()["status"] == "running"
            assert exp.started_at is not None
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_complete_experiment(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        db.execute = AsyncMock(return_value=_scalar(exp))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/experiments/{EXP_ID}/status?status=completed", headers=headers)
            assert r.status_code == 200
            assert r.json()["status"] == "completed"
            assert exp.ended_at is not None
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_pause_experiment(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        db.execute = AsyncMock(return_value=_scalar(exp))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/experiments/{EXP_ID}/status?status=paused", headers=headers)
            assert r.status_code == 200
            assert r.json()["status"] == "paused"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(self, app, headers):
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/experiments/{EXP_ID}/status?status=invalid", headers=headers)
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_status_not_found(self, app, headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/experiments/{EXP_ID}/status?status=running", headers=headers)
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Delete Experiment ────────────────────────────────────────────────────────

class TestDeleteExperiment:

    @pytest.mark.asyncio
    async def test_delete_success(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment()
        db.execute = AsyncMock(return_value=_scalar(exp))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(f"/v1/experiments/{EXP_ID}", headers=headers)
            assert r.status_code == 204
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_delete_not_found(self, app, headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(f"/v1/experiments/{EXP_ID}", headers=headers)
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Enroll Task ──────────────────────────────────────────────────────────────

class TestEnrollTask:

    @pytest.mark.asyncio
    async def test_enroll_experiment_not_running(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="draft")
        db.execute = AsyncMock(return_value=_scalar(exp))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/experiments/{EXP_ID}/enroll", headers=headers, json={
                    "task_id": TASK_ID, "experiment_id": EXP_ID,
                })
            assert r.status_code == 400
            assert "running" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_enroll_task_not_found(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)  # _get_exp
            return _scalar(None)  # Task not found
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/experiments/{EXP_ID}/enroll", headers=headers, json={
                    "task_id": TASK_ID, "experiment_id": EXP_ID,
                })
            assert r.status_code == 404
            assert "Task not found" in r.json()["detail"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_enroll_task_already_enrolled(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        task = _make_task()
        participant = _make_participant()
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)
            if call_count == 2:
                return _scalar(task)  # Task found
            if call_count == 3:
                return _scalar(participant)  # Already enrolled
            return _scalar(None)
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/experiments/{EXP_ID}/enroll", headers=headers, json={
                    "task_id": TASK_ID, "experiment_id": EXP_ID,
                })
            assert r.status_code == 409
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_enroll_success(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        task = _make_task()
        va = _make_variant(variant_id=VARIANT_A_ID, name="A", traffic_pct=50)
        vb = _make_variant(variant_id=VARIANT_B_ID, name="B", traffic_pct=50, is_control=False)
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)
            if call_count == 2:
                return _scalar(task)
            if call_count == 3:
                return _scalar(None)  # Not already enrolled
            return _scalars_list([va, vb])  # Variants
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/experiments/{EXP_ID}/enroll", headers=headers, json={
                    "task_id": TASK_ID, "experiment_id": EXP_ID,
                })
            assert r.status_code == 200
            data = r.json()
            assert "participant_id" in data
            assert "variant_id" in data
            assert "variant_name" in data
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_enroll_no_variants(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        task = _make_task()
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)
            if call_count == 2:
                return _scalar(task)
            if call_count == 3:
                return _scalar(None)  # Not enrolled
            return _scalars_list([])  # No variants
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/experiments/{EXP_ID}/enroll", headers=headers, json={
                    "task_id": TASK_ID, "experiment_id": EXP_ID,
                })
            assert r.status_code == 400
            assert "no variants" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Get Results ──────────────────────────────────────────────────────────────

class TestGetResults:

    @pytest.mark.asyncio
    async def test_results_success(self, app, headers):
        db = _make_mock_db()
        va = _make_variant(variant_id=VARIANT_A_ID, name="Control",
                          participant_count=20, completion_count=15, failure_count=5,
                          total_accuracy=13.5, total_duration_ms=150000, total_credits_used=30)
        vb = _make_variant(variant_id=VARIANT_B_ID, name="Treatment", is_control=False,
                          participant_count=20, completion_count=18, failure_count=2,
                          total_accuracy=16.2, total_duration_ms=120000, total_credits_used=36)
        exp = _make_experiment(status="completed")
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)
            return _scalars_list([va, vb])
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/experiments/{EXP_ID}/results", headers=headers)
            assert r.status_code == 200
            data = r.json()
            assert data["experiment_id"] == EXP_ID
            assert data["total_participants"] == 40
            assert len(data["variants"]) == 2
            assert "statistical_significance" in data
            assert "winner_variant_id" in data
            assert "recommendation" in data
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_results_not_found(self, app, headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/experiments/{EXP_ID}/results", headers=headers)
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_results_insufficient_data(self, app, headers):
        db = _make_mock_db()
        va = _make_variant(variant_id=VARIANT_A_ID, name="A",
                          participant_count=3, completion_count=2, failure_count=1)
        vb = _make_variant(variant_id=VARIANT_B_ID, name="B", is_control=False,
                          participant_count=3, completion_count=1, failure_count=2)
        exp = _make_experiment(status="running")
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)
            return _scalars_list([va, vb])
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/experiments/{EXP_ID}/results", headers=headers)
            assert r.status_code == 200
            data = r.json()
            assert "insufficient" in data["recommendation"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Record Outcome ───────────────────────────────────────────────────────────

class TestRecordOutcome:

    @pytest.mark.asyncio
    async def test_record_completed(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        participant = _make_participant()
        variant = _make_variant(variant_id=VARIANT_A_ID,
                               completion_count=5, participant_count=10)
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)
            if call_count == 2:
                return _scalar(participant)
            if call_count == 3:
                return _scalar(variant)
            return _scalar(None)
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/experiments/{EXP_ID}/record-outcome"
                    f"?task_id={TASK_ID}&outcome=completed&accuracy=0.95&duration_ms=5000&credits_used=2",
                    headers=headers,
                )
            assert r.status_code == 200
            assert r.json()["status"] == "recorded"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_record_failed(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        participant = _make_participant()
        variant = _make_variant(variant_id=VARIANT_A_ID, failure_count=2)
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)
            if call_count == 2:
                return _scalar(participant)
            if call_count == 3:
                return _scalar(variant)
            return _scalar(None)
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/experiments/{EXP_ID}/record-outcome?task_id={TASK_ID}&outcome=failed",
                    headers=headers,
                )
            assert r.status_code == 200
            assert r.json()["status"] == "recorded"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_record_participant_not_found(self, app, headers):
        db = _make_mock_db()
        exp = _make_experiment(status="running")
        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(exp)
            return _scalar(None)  # No participant
        db.execute = AsyncMock(side_effect=_side)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/experiments/{EXP_ID}/record-outcome?task_id={TASK_ID}&outcome=completed",
                    headers=headers,
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_record_invalid_outcome(self, app, headers):
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/experiments/{EXP_ID}/record-outcome?task_id={TASK_ID}&outcome=invalid",
                    headers=headers,
                )
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Pure Function Tests ──────────────────────────────────────────────────────

class TestPureFunctions:

    def test_chi_squared_insufficient_data(self):
        from routers.experiments import _chi_squared_p
        assert _chi_squared_p([(2, 10), (3, 10)]) is None

    def test_chi_squared_sufficient_data(self):
        from routers.experiments import _chi_squared_p
        # Need min(c1, c2, n1-c1, n2-c2) >= 5
        result = _chi_squared_p([(30, 50), (40, 50)])
        assert result is not None
        assert 0 <= result <= 1

    def test_chi_squared_three_variants_returns_none(self):
        from routers.experiments import _chi_squared_p
        assert _chi_squared_p([(10, 20), (12, 20), (8, 20)]) is None

    def test_chi_squared_all_complete(self):
        from routers.experiments import _chi_squared_p
        assert _chi_squared_p([(20, 20), (20, 20)]) is None

    def test_make_recommendation_insufficient_data(self):
        from routers.experiments import _make_recommendation
        va = _make_variant(participant_count=5)
        vb = _make_variant(participant_count=5, is_control=False)
        result = _make_recommendation([va, vb], "completion_rate", None, None)
        assert "insufficient" in result.lower()

    def test_pick_winner_no_participants(self):
        from routers.experiments import _pick_winner
        va = _make_variant(participant_count=0, completion_count=0)
        vb = _make_variant(participant_count=0, completion_count=0, is_control=False)
        assert _pick_winner([va, vb], "completion_rate") is None

    def test_pick_winner_by_completion_rate(self):
        from routers.experiments import _pick_winner
        va = _make_variant(variant_id=VARIANT_A_ID, participant_count=20, completion_count=10)
        vb = _make_variant(variant_id=VARIANT_B_ID, participant_count=20, completion_count=15, is_control=False)
        winner = _pick_winner([va, vb], "completion_rate")
        assert str(winner) == VARIANT_B_ID

    def test_pick_winner_by_accuracy(self):
        from routers.experiments import _pick_winner
        va = _make_variant(variant_id=VARIANT_A_ID, participant_count=20, completion_count=10,
                          total_accuracy=8.0)
        vb = _make_variant(variant_id=VARIANT_B_ID, participant_count=20, completion_count=10,
                          total_accuracy=9.0, is_control=False)
        winner = _pick_winner([va, vb], "accuracy")
        assert str(winner) == VARIANT_B_ID

    def test_weighted_choice_returns_variant(self):
        from routers.experiments import _weighted_choice
        va = _make_variant(traffic_pct=50)
        vb = _make_variant(traffic_pct=50, is_control=False)
        result = _weighted_choice([va, vb])
        assert result in [va, vb]

    def test_variant_stats_completion_rate(self):
        from routers.experiments import _variant_stats
        v = _make_variant(participant_count=20, completion_count=15)
        out = _variant_stats(v)
        assert out.completion_rate == 75.0

    def test_variant_stats_avg_accuracy(self):
        from routers.experiments import _variant_stats
        v = _make_variant(participant_count=20, completion_count=10, total_accuracy=9.0)
        out = _variant_stats(v)
        assert out.avg_accuracy == 0.9
