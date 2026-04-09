"""Tests for the reputation router (worker reputation, admin moderation).

Covers:
  1.  GET  /v1/reputation/me                         — unauthenticated → 401
  2.  GET  /v1/reputation/me                         — happy path
  3.  GET  /v1/reputation/me                         — user not found → 404
  4.  GET  /v1/reputation/me                         — with active strikes
  5.  GET  /v1/admin/reputation/workers              — unauthenticated → 401
  6.  GET  /v1/admin/reputation/workers              — non-admin → 403
  7.  GET  /v1/admin/reputation/workers              — happy path
  8.  GET  /v1/admin/reputation/workers              — empty list
  9.  GET  /v1/admin/reputation/workers              — filter is_banned
  10. GET  /v1/admin/reputation/workers              — search filter
  11. GET  /v1/admin/reputation/workers/{id}         — unauthenticated → 401
  12. GET  /v1/admin/reputation/workers/{id}         — non-admin → 403
  13. GET  /v1/admin/reputation/workers/{id}         — happy path
  14. GET  /v1/admin/reputation/workers/{id}         — not found → 404
  15. GET  /v1/admin/reputation/workers/{id}         — not a worker → 400
  16. POST /v1/admin/reputation/workers/{id}/strikes — unauthenticated → 401
  17. POST /v1/admin/reputation/workers/{id}/strikes — non-admin → 403
  18. POST /v1/admin/reputation/workers/{id}/strikes — happy path → 201
  19. POST /v1/admin/reputation/workers/{id}/strikes — worker not found → 404
  20. POST /v1/admin/reputation/workers/{id}/strikes — not a worker → 400
  21. DELETE /v1/admin/reputation/strikes/{id}       — unauthenticated → 401
  22. DELETE /v1/admin/reputation/strikes/{id}       — non-admin → 403
  23. DELETE /v1/admin/reputation/strikes/{id}       — happy path
  24. DELETE /v1/admin/reputation/strikes/{id}       — not found → 404
  25. POST /v1/admin/reputation/workers/{id}/ban     — unauthenticated → 401
  26. POST /v1/admin/reputation/workers/{id}/ban     — non-admin → 403
  27. POST /v1/admin/reputation/workers/{id}/ban     — happy path
  28. POST /v1/admin/reputation/workers/{id}/ban     — already banned → 400
  29. POST /v1/admin/reputation/workers/{id}/ban     — not found → 404
  30. POST /v1/admin/reputation/workers/{id}/unban   — unauthenticated → 401
  31. POST /v1/admin/reputation/workers/{id}/unban   — non-admin → 403
  32. POST /v1/admin/reputation/workers/{id}/unban   — happy path
  33. POST /v1/admin/reputation/workers/{id}/unban   — not banned → 400
  34. POST /v1/admin/reputation/workers/{id}/unban   — not found → 404
  35. POST /v1/admin/reputation/recalculate          — unauthenticated → 401
  36. POST /v1/admin/reputation/recalculate          — non-admin → 403
  37. POST /v1/admin/reputation/recalculate          — happy path
  38. POST /v1/admin/reputation/recalculate          — no workers
"""
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

# ── Fixed IDs ────────────────────────────────────────────────────────────────

ADMIN_ID = str(uuid.uuid4())
WORKER_ID = str(uuid.uuid4())
NON_ADMIN_ID = str(uuid.uuid4())
STRIKE_ID = str(uuid.uuid4())


# ── Helpers ──────────────────────────────────────────────────────────────────

def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


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
        pass
    db.refresh = _refresh

    async def _delete(obj):
        pass
    db.delete = _delete
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _scalar(value):
    """Mock an execute() result where .scalar_one_or_none() returns value."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    r.scalar = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _scalars_result(items):
    """Mock an execute() result where .scalars().all() returns items."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalar_one = MagicMock(return_value=len(items))
    r.scalar = MagicMock(return_value=len(items))
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.all = MagicMock(return_value=items)
    return r


def _make_admin(admin_id=None):
    u = MagicMock()
    u.id = uuid.UUID(admin_id or ADMIN_ID)
    u.is_admin = True
    u.role = "requester"
    u.credits = 0
    u.token_version = 0
    u.is_active = True
    u.is_banned = False
    return u


def _make_non_admin(user_id=None):
    u = MagicMock()
    u.id = uuid.UUID(user_id or NON_ADMIN_ID)
    u.is_admin = False
    u.role = "requester"
    u.credits = 100
    u.token_version = 0
    u.is_active = True
    u.is_banned = False
    return u


def _make_worker(worker_id=None, is_banned=False):
    u = MagicMock()
    u.id = uuid.UUID(worker_id or WORKER_ID)
    u.is_admin = False
    u.role = "worker"
    u.credits = 100
    u.token_version = 0
    u.is_active = True
    u.is_banned = is_banned
    u.ban_reason = "test" if is_banned else None
    u.ban_expires_at = None
    u.reputation_score = 75.0
    u.strike_count = 0
    u.worker_accuracy = 0.9
    u.worker_reliability = 0.85
    u.worker_tasks_completed = 50
    u.worker_level = 3
    u.worker_streak_days = 7
    u.worker_xp = 500
    u.email = "worker@test.com"
    u.name = "Test Worker"
    u.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return u


def _make_strike(strike_id=None, worker_id=None):
    s = MagicMock()
    s.id = uuid.UUID(strike_id or STRIKE_ID)
    s.worker_id = uuid.UUID(worker_id or WORKER_ID)
    s.severity = "minor"
    s.reason = "Test strike"
    s.is_active = True
    s.expires_at = None
    s.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return s


def _admin_header():
    return {"Authorization": f"Bearer {_token(ADMIN_ID)}"}


def _worker_header():
    return {"Authorization": f"Bearer {_token(WORKER_ID)}"}


def _non_admin_header():
    return {"Authorization": f"Bearer {_token(NON_ADMIN_ID)}"}


# ── Test classes ─────────────────────────────────────────────────────────────


class TestGetMyReputation:
    """GET /v1/reputation/me"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/reputation/me")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        worker = _make_worker()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                # get_current_user_id does not call db.execute for JWT path
                # First real call: select UserDB for the me endpoint
                return _scalar(worker)
            if call_num[0] == 2:
                # Second: select active strikes
                return _scalars_result([])
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/reputation/me", headers=_worker_header())
            assert r.status_code == 200
            body = r.json()
            assert body["user_id"] == WORKER_ID
            assert body["reputation_score"] == 75.0
            assert body["tier"] == "Expert"  # 75.0 >= 75
            assert body["tasks_completed"] == 50
            assert body["level"] == 3
            assert body["streak_days"] == 7
            assert body["is_banned"] is False
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_user_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/reputation/me", headers=_worker_header())
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_with_active_strikes(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        worker = _make_worker()
        worker.strike_count = 2
        strike1 = _make_strike()
        strike2 = _make_strike(strike_id=str(uuid.uuid4()))
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(worker)
            if call_num[0] == 2:
                return _scalars_result([strike1, strike2])
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/reputation/me", headers=_worker_header())
            assert r.status_code == 200
            body = r.json()
            assert body["strike_count"] == 2
            assert len(body["active_strikes"]) == 2
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestListWorkersReputation:
    """GET /v1/admin/reputation/workers"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/admin/reputation/workers")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        non_admin = _make_non_admin()
        db.execute = AsyncMock(return_value=_scalar(non_admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/admin/reputation/workers",
                    headers=_non_admin_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        worker = _make_worker()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                # require_admin: select user
                return _scalar(admin)
            if call_num[0] == 2:
                # list workers query
                return _scalars_result([worker])
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/admin/reputation/workers",
                    headers=_admin_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert isinstance(body, list)
            assert len(body) == 1
            assert body[0]["id"] == WORKER_ID
            assert body[0]["reputation_score"] == 75.0
            assert body[0]["tier"] == "Expert"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_empty_list(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalars_result([])
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/admin/reputation/workers",
                    headers=_admin_header(),
                )
            assert r.status_code == 200
            assert r.json() == []
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_filter_is_banned(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalars_result([])
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/admin/reputation/workers",
                    params={"is_banned": "true"},
                    headers=_admin_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_search_filter(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalars_result([])
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/admin/reputation/workers",
                    params={"search": "test@example"},
                    headers=_admin_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestGetWorkerReputation:
    """GET /v1/admin/reputation/workers/{worker_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/admin/reputation/workers/{WORKER_ID}")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        non_admin = _make_non_admin()
        db.execute = AsyncMock(return_value=_scalar(non_admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/admin/reputation/workers/{WORKER_ID}",
                    headers=_non_admin_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        worker = _make_worker()
        strike = _make_strike()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                # require_admin: select user
                return _scalar(admin)
            if call_num[0] == 2:
                # _get_worker_or_404: select worker
                return _scalar(worker)
            if call_num[0] == 3:
                # select strikes
                return _scalars_result([strike])
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/admin/reputation/workers/{WORKER_ID}",
                    headers=_admin_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["user_id"] == WORKER_ID
            assert body["reputation_score"] == 75.0
            assert body["tier"] == "Expert"
            assert body["tasks_completed"] == 50
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(None)  # Worker not found
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/admin/reputation/workers/{uuid.uuid4()}",
                    headers=_admin_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_a_worker(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        requester = _make_non_admin()  # role="requester", not "worker"
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(requester)  # exists but not a worker
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/admin/reputation/workers/{NON_ADMIN_ID}",
                    headers=_admin_header(),
                )
            assert r.status_code == 400
            assert "not a worker" in r.json()["detail"]
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestIssueStrike:
    """POST /v1/admin/reputation/workers/{worker_id}/strikes"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/strikes",
                    json={"severity": "minor", "reason": "Test reason for strike"},
                )
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        non_admin = _make_non_admin()
        db.execute = AsyncMock(return_value=_scalar(non_admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/strikes",
                    json={"severity": "minor", "reason": "Test reason for strike"},
                    headers=_non_admin_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    @patch("routers.reputation.refresh_worker_reputation", new_callable=AsyncMock, return_value=60.0)
    async def test_happy_path(self, mock_refresh, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        worker = _make_worker()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                # require_admin: select user (admin)
                return _scalar(admin)
            if call_num[0] == 2:
                # _get_worker_or_404: select worker
                return _scalar(worker)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/strikes",
                    json={"severity": "minor", "reason": "Test reason for strike"},
                    headers=_admin_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["message"] == "Strike issued"
            assert body["new_reputation_score"] == 60.0
            mock_refresh.assert_awaited_once()
            mock_notify.assert_awaited_once()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    @patch("routers.reputation.refresh_worker_reputation", new_callable=AsyncMock, return_value=50.0)
    async def test_worker_not_found(self, mock_refresh, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(None)  # Worker not found
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{uuid.uuid4()}/strikes",
                    json={"severity": "minor", "reason": "Test reason for strike"},
                    headers=_admin_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    @patch("routers.reputation.refresh_worker_reputation", new_callable=AsyncMock, return_value=50.0)
    async def test_not_a_worker(self, mock_refresh, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        requester = _make_non_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(requester)  # Not a worker
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{NON_ADMIN_ID}/strikes",
                    json={"severity": "minor", "reason": "Test reason for strike"},
                    headers=_admin_header(),
                )
            assert r.status_code == 400
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_severity(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        db.execute = AsyncMock(return_value=_scalar(admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/strikes",
                    json={"severity": "invalid_value", "reason": "Test reason for strike"},
                    headers=_admin_header(),
                )
            assert r.status_code == 422  # Pydantic validation
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestPardonStrike:
    """DELETE /v1/admin/reputation/strikes/{strike_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/admin/reputation/strikes/{STRIKE_ID}")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        non_admin = _make_non_admin()
        db.execute = AsyncMock(return_value=_scalar(non_admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/admin/reputation/strikes/{STRIKE_ID}",
                    headers=_non_admin_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.refresh_worker_reputation", new_callable=AsyncMock, return_value=80.0)
    async def test_happy_path(self, mock_refresh):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        strike = _make_strike()
        worker = _make_worker()
        worker.strike_count = 1
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                # require_admin
                return _scalar(admin)
            if call_num[0] == 2:
                # select strike
                return _scalar(strike)
            if call_num[0] == 3:
                # select worker (to reduce strike_count)
                return _scalar(worker)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/admin/reputation/strikes/{STRIKE_ID}",
                    headers=_admin_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["message"] == "Strike pardoned"
            assert body["new_reputation_score"] == 80.0
            mock_refresh.assert_awaited_once()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(None)  # Strike not found
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/admin/reputation/strikes/{uuid.uuid4()}",
                    headers=_admin_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestBanWorker:
    """POST /v1/admin/reputation/workers/{worker_id}/ban"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/ban",
                    json={"reason": "Test ban reason here"},
                )
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        non_admin = _make_non_admin()
        db.execute = AsyncMock(return_value=_scalar(non_admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/ban",
                    json={"reason": "Test ban reason here"},
                    headers=_non_admin_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    async def test_happy_path(self, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        worker = _make_worker(is_banned=False)
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(worker)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/ban",
                    json={"reason": "Test ban reason here"},
                    headers=_admin_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["message"] == "Worker banned"
            mock_notify.assert_awaited_once()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    async def test_already_banned(self, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        worker = _make_worker(is_banned=True)
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(worker)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/ban",
                    json={"reason": "Test ban reason here"},
                    headers=_admin_header(),
                )
            assert r.status_code == 400
            assert "already banned" in r.json()["detail"]
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    async def test_not_found(self, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(None)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{uuid.uuid4()}/ban",
                    json={"reason": "Test ban reason here"},
                    headers=_admin_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_short_reason_validation(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        db.execute = AsyncMock(return_value=_scalar(admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/ban",
                    json={"reason": "ab"},  # Too short (min 5)
                    headers=_admin_header(),
                )
            assert r.status_code == 422
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestUnbanWorker:
    """POST /v1/admin/reputation/workers/{worker_id}/unban"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(f"/v1/admin/reputation/workers/{WORKER_ID}/unban")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        non_admin = _make_non_admin()
        db.execute = AsyncMock(return_value=_scalar(non_admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/unban",
                    headers=_non_admin_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    @patch("routers.reputation.refresh_worker_reputation", new_callable=AsyncMock, return_value=70.0)
    async def test_happy_path(self, mock_refresh, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        worker = _make_worker(is_banned=True)
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(worker)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/unban",
                    headers=_admin_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["message"] == "Worker unbanned"
            assert body["new_reputation_score"] == 70.0
            mock_refresh.assert_awaited_once()
            mock_notify.assert_awaited_once()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    @patch("routers.reputation.refresh_worker_reputation", new_callable=AsyncMock, return_value=70.0)
    async def test_not_banned(self, mock_refresh, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        worker = _make_worker(is_banned=False)
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(worker)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{WORKER_ID}/unban",
                    headers=_admin_header(),
                )
            assert r.status_code == 400
            assert "not banned" in r.json()["detail"]
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.create_notification", new_callable=AsyncMock)
    @patch("routers.reputation.refresh_worker_reputation", new_callable=AsyncMock, return_value=70.0)
    async def test_not_found(self, mock_refresh, mock_notify):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalar(None)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/admin/reputation/workers/{uuid.uuid4()}/unban",
                    headers=_admin_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestRecalculateAll:
    """POST /v1/admin/reputation/recalculate"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/admin/reputation/recalculate")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        non_admin = _make_non_admin()
        db.execute = AsyncMock(return_value=_scalar(non_admin))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/admin/reputation/recalculate",
                    headers=_non_admin_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.reputation.compute_reputation", new_callable=AsyncMock, return_value=80.0)
    async def test_happy_path(self, mock_compute):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        worker1 = _make_worker()
        worker2 = _make_worker(worker_id=str(uuid.uuid4()))
        call_num = [0]

        # cert_res and strike_res return iterable results
        cert_result = MagicMock()
        cert_result.__iter__ = MagicMock(return_value=iter([]))
        strike_result = MagicMock()
        strike_result.__iter__ = MagicMock(return_value=iter([]))

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                # require_admin
                return _scalar(admin)
            if call_num[0] == 2:
                # select all workers
                return _scalars_result([worker1, worker2])
            if call_num[0] == 3:
                # cert counts query
                return cert_result
            if call_num[0] == 4:
                # strike severities query
                return strike_result
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/admin/reputation/recalculate",
                    headers=_admin_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert "Recalculated 2 worker" in body["message"]
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_no_workers(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        admin = _make_admin()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(admin)
            if call_num[0] == 2:
                return _scalars_result([])  # No workers
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/admin/reputation/recalculate",
                    headers=_admin_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert "0 worker" in body["message"]
        finally:
            _app.dependency_overrides.pop(get_db, None)
