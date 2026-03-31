"""Tests for the endorsements router and availability slot validation.

Covers endorsements:
  1.  GET /v1/workers/{id}/endorsements — worker not found → 404
  2.  GET /v1/workers/{id}/endorsements — happy path: returns list + total
  3.  GET /v1/workers/{id}/endorsements — empty results → items=[], total=0
  4.  GET /v1/workers/{id}/endorsements/count — worker not found → 404
  5.  GET /v1/workers/{id}/endorsements/count — returns count field
  6.  POST /v1/workers/{id}/endorse — unauthenticated → 401/403
  7.  POST /v1/workers/{id}/endorse — worker not found → 404
  8.  POST /v1/workers/{id}/endorse — task not found → 404
  9.  POST /v1/workers/{id}/endorse — task not completed → 400
  10. POST /v1/workers/{id}/endorse — duplicate endorsement → 409

Covers availability slot validation (AvailabilitySlotIn Pydantic model):
  11. Valid slot: start_hour=9, end_hour=17 → OK
  12. end_hour == start_hour → ValidationError
  13. end_hour < start_hour → ValidationError
  14. day_of_week out of range (7) → ValidationError
  15. end_hour=24 is valid (midnight end)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── IDs ───────────────────────────────────────────────────────────────────────

REQUESTER_ID = str(uuid.uuid4())
WORKER_ID    = str(uuid.uuid4())
TASK_ID      = str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _make_worker(worker_id: str = WORKER_ID) -> MagicMock:
    w = MagicMock()
    w.id            = uuid.UUID(worker_id)
    w.name          = "Test Worker"
    w.role          = "worker"
    w.token_version = 0
    return w


def _make_task(task_id: str = TASK_ID, status: str = "completed") -> MagicMock:
    t = MagicMock()
    t.id      = uuid.UUID(task_id)
    t.user_id = uuid.UUID(REQUESTER_ID)
    t.status  = status
    return t


def _make_assignment(task_id: str = TASK_ID, worker_id: str = WORKER_ID) -> MagicMock:
    a = MagicMock()
    a.task_id   = uuid.UUID(task_id)
    a.worker_id = uuid.UUID(worker_id)
    a.status    = "approved"
    return a


def _make_endorsement(
    worker_id: str = WORKER_ID,
    skill_tag: str | None = "data_annotation",
    note: str | None = "Great work!",
) -> MagicMock:
    e = MagicMock(spec=["id", "skill_tag", "note", "created_at"])
    e.id         = uuid.uuid4()
    e.skill_tag  = skill_tag
    e.note       = note
    e.created_at = _now()
    return e


def _make_db() -> MagicMock:
    db          = MagicMock()
    db.add      = MagicMock()
    db.execute  = AsyncMock()
    db.commit   = AsyncMock()
    db.scalar   = AsyncMock(return_value=0)

    async def _refresh(obj):
        pass
    db.refresh = _refresh
    return db


def _scalar_result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars            = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[value] if value is not None else []))
    )
    r.fetchall = MagicMock(return_value=[(value,)] if value is not None else [])
    return r


def _scalars_result(items: list) -> MagicMock:
    r = MagicMock()
    r.scalar_one         = MagicMock(return_value=len(items))
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalars            = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=items))
    )
    return r


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Endorsements — GET endpoints (public) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_list_endorsements_worker_not_found():
    """GET /v1/workers/{id}/endorsements — unknown worker → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{uuid.uuid4()}/endorsements")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_endorsements_happy_path():
    """GET /v1/workers/{id}/endorsements — returns items list and total."""
    from main import app
    from core.database import get_db

    worker = _make_worker()
    e1     = _make_endorsement(skill_tag="nlp")
    e2     = _make_endorsement(skill_tag="vision")

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(worker)       # worker exists check
        if call_num[0] == 2:
            return _scalars_result([e1, e2])    # endorsements query
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    db.scalar.return_value = 2          # total count

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/endorsements")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 2
        assert len(body["items"]) == 2
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_endorsements_empty():
    """GET /v1/workers/{id}/endorsements — no endorsements → empty items, total=0."""
    from main import app
    from core.database import get_db

    worker = _make_worker()

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(worker)
        return _scalars_result([])

    db.execute.side_effect = _side_effect
    db.scalar.return_value = 0

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/endorsements")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["total"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_endorsement_count_worker_not_found():
    """GET /v1/workers/{id}/endorsements/count — unknown worker → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{uuid.uuid4()}/endorsements/count")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_endorsement_count_happy_path():
    """GET /v1/workers/{id}/endorsements/count — returns worker_id and count."""
    from main import app
    from core.database import get_db

    worker = _make_worker()
    db = _make_db()
    db.execute.return_value = _scalar_result(worker)
    db.scalar.return_value  = 7

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/endorsements/count")
        assert r.status_code == 200
        body = r.json()
        assert "count" in body
        assert body["count"] == 7
        assert "worker_id" in body
    finally:
        app.dependency_overrides.clear()


# ── Endorsements — POST (authenticated) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_create_endorsement_unauthenticated():
    """POST /v1/workers/{id}/endorse — no token → 401 or 403."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/workers/{WORKER_ID}/endorse",
            json={"task_id": str(uuid.uuid4())},
        )
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_create_endorsement_worker_not_found():
    """POST /v1/workers/{id}/endorse — worker not found → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/workers/{uuid.uuid4()}/endorse",
                json={"task_id": str(uuid.uuid4())},
                headers={"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 404
        assert "Worker not found" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_endorsement_task_not_found():
    """POST /v1/workers/{id}/endorse — task not found → 404."""
    from main import app
    from core.database import get_db

    worker = _make_worker()
    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(worker)   # worker exists
        return _scalar_result(None)         # task not found

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/workers/{WORKER_ID}/endorse",
                json={"task_id": str(uuid.uuid4())},
                headers={"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_endorsement_task_not_completed():
    """POST /v1/workers/{id}/endorse — task not completed → 400."""
    from main import app
    from core.database import get_db

    worker = _make_worker()
    task   = _make_task(status="open")   # not completed
    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(worker)
        return _scalar_result(task)

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/workers/{WORKER_ID}/endorse",
                json={"task_id": TASK_ID},
                headers={"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 400
        assert "completed" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_endorsement_duplicate_409():
    """POST /v1/workers/{id}/endorse — duplicate endorsement → 409."""
    from main import app
    from core.database import get_db

    worker     = _make_worker()
    task       = _make_task(status="completed")
    assignment = _make_assignment()
    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(worker)
        if call_num[0] == 2:
            return _scalar_result(task)
        return _scalar_result(assignment)

    db.execute.side_effect = _side_effect
    db.scalar.return_value = 1    # existing endorsement count → duplicate

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/workers/{WORKER_ID}/endorse",
                json={"task_id": TASK_ID},
                headers={"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_endorsement_integrity_error_returns_409():
    """POST /v1/workers/{id}/endorse — concurrent duplicate caught by IntegrityError → 409.

    This tests the race-condition path: two requests simultaneously pass the
    count=0 guard and both reach db.commit().  The DB unique constraint fires,
    raising IntegrityError; the handler must roll back and return 409 (not 500).
    """
    from main import app
    from core.database import get_db
    from sqlalchemy.exc import IntegrityError

    worker     = _make_worker()
    task       = _make_task(status="completed")
    assignment = _make_assignment()
    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(worker)
        if call_num[0] == 2:
            return _scalar_result(task)
        return _scalar_result(assignment)

    db.execute.side_effect = _side_effect
    db.scalar.return_value = 0   # count check passes — no existing endorsement yet
    db.rollback = AsyncMock()    # _make_db() doesn't set this as async; ensure it is

    # Simulate DB unique constraint violation on commit
    db.commit.side_effect = IntegrityError(
        "INSERT INTO worker_endorsements ...",
        {},
        Exception("duplicate key value violates unique constraint"),
    )

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/workers/{WORKER_ID}/endorse",
                json={"task_id": TASK_ID},
                headers={"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 409
        assert "already endorsed" in r.json()["detail"].lower()
        # Rollback must have been issued
        db.rollback.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


# ── AvailabilitySlotIn validation (pure Pydantic) ─────────────────────────────

def test_availability_slot_valid():
    """Valid slot: start_hour=9, end_hour=17 → passes validation."""
    from routers.availability import AvailabilitySlotIn
    slot = AvailabilitySlotIn(day_of_week=0, start_hour=9, end_hour=17)
    assert slot.start_hour == 9
    assert slot.end_hour   == 17


def test_availability_slot_end_equals_start_rejected():
    """end_hour == start_hour → ValidationError."""
    from routers.availability import AvailabilitySlotIn
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        AvailabilitySlotIn(day_of_week=0, start_hour=9, end_hour=9)


def test_availability_slot_end_before_start_rejected():
    """end_hour < start_hour → ValidationError."""
    from routers.availability import AvailabilitySlotIn
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        AvailabilitySlotIn(day_of_week=0, start_hour=17, end_hour=9)


def test_availability_slot_day_out_of_range():
    """day_of_week=7 → ValidationError (0–6 only)."""
    from routers.availability import AvailabilitySlotIn
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        AvailabilitySlotIn(day_of_week=7, start_hour=9, end_hour=17)


def test_availability_slot_end_hour_24_valid():
    """end_hour=24 is valid (midnight end-of-day)."""
    from routers.availability import AvailabilitySlotIn
    slot = AvailabilitySlotIn(day_of_week=6, start_hour=23, end_hour=24)
    assert slot.end_hour == 24


def test_build_slot_out_day_name():
    """_build_slot_out maps day_of_week correctly to Monday-Sunday."""
    from routers.availability import _build_slot_out, DAY_NAMES
    slot_db = MagicMock(spec=["id", "day_of_week", "start_hour", "end_hour"])
    slot_db.id          = uuid.uuid4()
    slot_db.day_of_week = 0   # Monday
    slot_db.start_hour  = 8
    slot_db.end_hour    = 16
    out = _build_slot_out(slot_db)
    assert out.day_name == "Monday"
    assert out.day_of_week == 0


def test_day_names_list_length():
    """DAY_NAMES has exactly 7 entries."""
    from routers.availability import DAY_NAMES
    assert len(DAY_NAMES) == 7
    assert DAY_NAMES[0] == "Monday"
    assert DAY_NAMES[6] == "Sunday"
