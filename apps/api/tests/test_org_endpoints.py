"""Tests for the organizations router (CRUD, members, invites, credits, analytics).

Covers all 16 endpoints:
  1.  POST   /v1/orgs                                  — create org
  2.  GET    /v1/orgs                                  — list my orgs
  3.  GET    /v1/orgs/{org_id}                          — get org
  4.  PATCH  /v1/orgs/{org_id}                          — update org
  5.  DELETE /v1/orgs/{org_id}                          — delete org
  6.  GET    /v1/orgs/{org_id}/members                  — list members
  7.  PATCH  /v1/orgs/{org_id}/members/{member_user_id} — update member role
  8.  DELETE /v1/orgs/{org_id}/members/{member_user_id} — remove member
  9.  POST   /v1/orgs/{org_id}/invites                  — invite member
  10. GET    /v1/orgs/{org_id}/invites                  — list invites
  11. DELETE /v1/orgs/{org_id}/invites/{invite_id}      — cancel invite
  12. POST   /v1/orgs/join?token=...                    — accept invite
  13. POST   /v1/orgs/{org_id}/credits/transfer         — transfer credits
  14. POST   /v1/orgs/{org_id}/activate                 — set active org
  15. POST   /v1/orgs/deactivate                        — deactivate org
  16. GET    /v1/orgs/{org_id}/analytics                — org analytics
"""
import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

# ── Fixed IDs ────────────────────────────────────────────────────────────────

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_ID2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ORG_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
MEMBER_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
INVITE_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
NOW = datetime.now(timezone.utc)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _auth_header(user_id: str = USER_ID) -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


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
            obj.created_at = NOW
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = NOW
        if getattr(obj, "plan", None) is None:
            obj.plan = "free"

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
    r.scalar = MagicMock(return_value=value if not isinstance(value, MagicMock) else 0)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    r.first = MagicMock(return_value=None)
    r.one = MagicMock(return_value=value)
    r.one_or_none = MagicMock(return_value=value)
    return r


def _scalars_result(items):
    """Mock an execute() result where .scalars().all() returns items."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalar_one = MagicMock(return_value=len(items))
    r.scalar = MagicMock(return_value=len(items))
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.all = MagicMock(return_value=items)
    r.first = MagicMock(return_value=None)
    return r


def _make_org(org_id: str = ORG_ID, owner_id: str = USER_ID, credits: int = 100):
    org = MagicMock()
    org.id = uuid.UUID(org_id)
    org.name = "Test Org"
    org.slug = "test-org"
    org.owner_id = uuid.UUID(owner_id)
    org.credits = credits
    org.plan = "free"
    org.description = "A test organization"
    org.avatar_url = None
    org.created_at = NOW
    org.updated_at = NOW
    return org


def _make_member(
    user_id: str = USER_ID,
    org_id: str = ORG_ID,
    role: str = "owner",
    member_id: str = MEMBER_ID,
):
    m = MagicMock()
    m.id = uuid.UUID(member_id)
    m.org_id = uuid.UUID(org_id)
    m.user_id = uuid.UUID(user_id) if isinstance(user_id, str) else user_id
    m.role = role
    m.joined_at = NOW
    return m


def _make_user(user_id: str = USER_ID, email: str = "test@example.com", credits: int = 500):
    u = MagicMock()
    u.id = uuid.UUID(user_id)
    u.name = "Test User"
    u.email = email
    u.credits = credits
    u.is_admin = False
    u.is_active = True
    u.is_banned = False
    u.plan = "free"
    u.active_org_id = None
    return u


def _make_invite(
    org_id: str = ORG_ID,
    email: str = "invited@example.com",
    role: str = "member",
    invite_id: str = INVITE_ID,
    token: str = "test-invite-token",
    expired: bool = False,
):
    inv = MagicMock()
    inv.id = uuid.UUID(invite_id)
    inv.org_id = uuid.UUID(org_id)
    inv.email = email
    inv.role = role
    inv.token = token
    inv.invited_by = uuid.UUID(USER_ID)
    inv.expires_at = NOW - timedelta(days=1) if expired else NOW + timedelta(days=7)
    inv.accepted_at = None
    inv.created_at = NOW
    return inv


def _setup_org_and_role(db, org=None, member=None, role="owner"):
    """Set up db.execute side_effect for _get_org_and_require_role (2 calls)."""
    if org is None:
        org = _make_org()
    if member is None:
        member = _make_member(role=role)
    return org, member


def _agg_row(total=10, completed=5, failed=1, running=2, credits_spent=50, avg_duration_ms=1500):
    """Mock a single row for analytics aggregate query."""
    r = MagicMock()
    r.total = total
    r.completed = completed
    r.failed = failed
    r.running = running
    r.credits_spent = credits_spent
    r.avg_duration_ms = avg_duration_ms
    return r


# ── Test classes ─────────────────────────────────────────────────────────────


class TestCreateOrg:
    """POST /v1/orgs"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/orgs", json={"name": "My Org", "slug": "my-org"})
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # db.scalar: slug uniqueness check -> 0 (no conflict)
        db.scalar = AsyncMock(side_effect=[0, 1])  # slug check, then member_count
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/orgs",
                    json={"name": "My Org", "slug": "my-org", "description": "Test"},
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["name"] == "My Org"
            assert body["slug"] == "my-org"
            assert body["description"] == "Test"
            assert body["credits"] == 0
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_slug_conflict(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # db.scalar: slug uniqueness check -> 1 (conflict!)
        db.scalar = AsyncMock(return_value=1)
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/orgs",
                    json={"name": "My Org", "slug": "my-org"},
                    headers=_auth_header(),
                )
            assert r.status_code == 409
            assert "Slug already taken" in r.json()["detail"]
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_slug_format(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/orgs",
                    json={"name": "My Org", "slug": "INVALID SLUG!"},
                    headers=_auth_header(),
                )
            assert r.status_code == 422  # Pydantic validation
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestListMyOrgs:
    """GET /v1/orgs"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/orgs")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path_empty(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # First execute: join query returns empty
        db.execute = AsyncMock(return_value=_scalars_result([]))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/orgs", headers=_auth_header())
            assert r.status_code == 200
            assert r.json() == []
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path_with_orgs(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        # First execute: join query returns orgs, second: member count bulk query
        mc_row = MagicMock()
        mc_row.org_id = org.id
        mc_row.cnt = 3
        mc_result = MagicMock()
        mc_result.__iter__ = MagicMock(return_value=iter([mc_row]))
        db.execute = AsyncMock(side_effect=[_scalars_result([org]), mc_result])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/orgs", headers=_auth_header())
            assert r.status_code == 200
            body = r.json()
            assert len(body) == 1
            assert body[0]["name"] == "Test Org"
            assert body[0]["member_count"] == 3
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestGetOrg:
    """GET /v1/orgs/{org_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="viewer")
        # _get_org_and_require_role: 1st execute=org, 2nd execute=member
        # _org_to_out: db.scalar=member_count
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        db.scalar = AsyncMock(return_value=5)
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}", headers=_auth_header())
            assert r.status_code == 200
            body = r.json()
            assert body["name"] == "Test Org"
            assert body["member_count"] == 5
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_org_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # org lookup returns None
        db.execute = AsyncMock(return_value=_scalar(None))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}", headers=_auth_header())
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_a_member(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        # org found, but membership returns None
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(None)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}", headers=_auth_header())
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestUpdateOrg:
    """PATCH /v1/orgs/{org_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(f"/v1/orgs/{ORG_ID}", json={"name": "Updated"})
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        db.scalar = AsyncMock(return_value=3)  # member_count

        async def _refresh(obj):
            obj.name = "Updated Name"
            if getattr(obj, "created_at", None) is None:
                obj.created_at = NOW

        db.refresh = _refresh
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/orgs/{ORG_ID}",
                    json={"name": "Updated Name"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            assert r.json()["name"] == "Updated Name"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_permission_denied_member_role(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="member")  # needs admin+
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/orgs/{ORG_ID}",
                    json={"name": "Updated"},
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_org_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(f"/v1/orgs/{ORG_ID}", json={"name": "Updated"}, headers=_auth_header())
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestDeleteOrg:
    """DELETE /v1/orgs/{org_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/orgs/{ORG_ID}")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path_no_tasks_no_credits(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org(credits=0)
        member = _make_member(role="owner")
        # 1: org lookup, 2: member lookup (require_role)
        # 3: org locked row, 4: active tasks query (empty)
        db.execute = AsyncMock(side_effect=[
            _scalar(org),       # org lookup
            _scalar(member),    # member lookup
            _scalar(org),       # lock org row (scalar_one)
            _scalars_result([]),  # active tasks (empty)
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/orgs/{ORG_ID}", headers=_auth_header())
            assert r.status_code == 204
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path_with_credits_transfer(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org(credits=50)
        member = _make_member(role="owner")
        owner_user = _make_user(credits=100)

        db.execute = AsyncMock(side_effect=[
            _scalar(org),            # org lookup
            _scalar(member),         # member lookup
            _scalar(org),            # lock org row
            _scalars_result([]),     # active tasks (empty)
            _scalar(owner_user),     # lock owner user row for credit transfer
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/orgs/{ORG_ID}", headers=_auth_header())
            assert r.status_code == 204
            # Owner should have received the org credits
            assert owner_user.credits == 150  # 100 + 50
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_permission_denied_admin(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")  # needs owner
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/orgs/{ORG_ID}", headers=_auth_header())
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_org_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/orgs/{ORG_ID}", headers=_auth_header())
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestListMembers:
    """GET /v1/orgs/{org_id}/members"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/members")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        caller_member = _make_member(role="viewer")
        # The member returned in the list is the owner
        listed_member = _make_member(role="owner")
        user = _make_user()

        # _get_org_and_require_role: 2 calls, then members query
        members_result = MagicMock()
        members_result.all = MagicMock(return_value=[(listed_member, user)])
        db.execute = AsyncMock(side_effect=[
            _scalar(org),
            _scalar(caller_member),
            members_result,
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/members", headers=_auth_header())
            assert r.status_code == 200
            body = r.json()
            assert len(body) == 1
            assert body[0]["role"] == "owner"
            assert body[0]["email"] == "test@example.com"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_org_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/members", headers=_auth_header())
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestUpdateMemberRole:
    """PATCH /v1/orgs/{org_id}/members/{member_user_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(f"/v1/orgs/{ORG_ID}/members/{USER_ID2}?role=member")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="owner")
        target_member = _make_member(user_id=USER_ID2, role="member", member_id=str(uuid.uuid4()))
        target_user = _make_user(user_id=USER_ID2, email="other@example.com")

        target_row = MagicMock()
        target_row.first = MagicMock(return_value=(target_member, target_user))

        db.execute = AsyncMock(side_effect=[
            _scalar(org),        # org lookup
            _scalar(my_member),  # my membership
            target_row,          # target member+user lookup
        ])

        async def _refresh(obj):
            pass

        db.refresh = _refresh

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}?role=admin",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["email"] == "other@example.com"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_role_value(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="admin")
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(my_member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}?role=superuser",
                    headers=_auth_header(),
                )
            assert r.status_code == 400
            assert "role must be" in r.json()["detail"]
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_permission_denied_member_role(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="member")  # needs admin+
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(my_member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}?role=viewer",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_cannot_change_owner_role(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="owner")
        target_member = _make_member(user_id=USER_ID2, role="owner", member_id=str(uuid.uuid4()))
        target_user = _make_user(user_id=USER_ID2)
        target_row = MagicMock()
        target_row.first = MagicMock(return_value=(target_member, target_user))
        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(my_member), target_row,
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}?role=member",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
            assert "owner" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_target_member_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="admin")
        target_row = MagicMock()
        target_row.first = MagicMock(return_value=None)
        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(my_member), target_row,
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}?role=viewer",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_only_owner_can_promote_to_admin(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="admin")  # admin, not owner
        target_member = _make_member(user_id=USER_ID2, role="member", member_id=str(uuid.uuid4()))
        target_user = _make_user(user_id=USER_ID2)
        target_row = MagicMock()
        target_row.first = MagicMock(return_value=(target_member, target_user))
        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(my_member), target_row,
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}?role=admin",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
            assert "owner" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestRemoveMember:
    """DELETE /v1/orgs/{org_id}/members/{member_user_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/orgs/{ORG_ID}/members/{USER_ID2}")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path_admin_removes_member(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="admin")
        target_member = _make_member(user_id=USER_ID2, role="member", member_id=str(uuid.uuid4()))
        db.execute = AsyncMock(side_effect=[
            _scalar(org),
            _scalar(my_member),
            _scalar(target_member),  # target lookup
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}",
                    headers=_auth_header(),
                )
            assert r.status_code == 204
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_self_removal(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="member")
        db.execute = AsyncMock(side_effect=[
            _scalar(org),
            _scalar(my_member),
            _scalar(my_member),  # target is self
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 204
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_member_cannot_remove_others(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="member")  # not admin
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(my_member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_cannot_remove_owner(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="owner")
        target_member = _make_member(user_id=USER_ID2, role="owner", member_id=str(uuid.uuid4()))
        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(my_member), _scalar(target_member),
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
            assert "owner" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_target_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        my_member = _make_member(role="admin")
        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(my_member), _scalar(None),
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/orgs/{ORG_ID}/members/{USER_ID2}",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestInviteMember:
    """POST /v1/orgs/{org_id}/invites"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/invites",
                    json={"email": "new@example.com"},
                )
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.orgs.safe_create_task")
    @patch("routers.orgs.create_notification", new_callable=AsyncMock)
    async def test_happy_path(self, mock_notif, mock_task):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")
        db.execute = AsyncMock(side_effect=[
            _scalar(org),    # org lookup
            _scalar(member), # member lookup
        ])
        # db.scalar: existing member check=0, pending invite check=0,
        #            then invited_user lookup=None
        db.scalar = AsyncMock(side_effect=[0, 0, None])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/invites",
                    json={"email": "new@example.com", "role": "member"},
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["email"] == "new@example.com"
            assert body["role"] == "member"
            assert "token" in body
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_already_a_member(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        # existing member check returns 1
        db.scalar = AsyncMock(return_value=1)
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/invites",
                    json={"email": "existing@example.com"},
                    headers=_auth_header(),
                )
            assert r.status_code == 409
            assert "already a member" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_pending_invite_exists(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        # existing member=0, pending invite=1
        db.scalar = AsyncMock(side_effect=[0, 1])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/invites",
                    json={"email": "pending@example.com"},
                    headers=_auth_header(),
                )
            assert r.status_code == 409
            assert "pending invite" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_permission_denied_member_role(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="member")  # needs admin+
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/invites",
                    json={"email": "new@example.com"},
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestListInvites:
    """GET /v1/orgs/{org_id}/invites"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/invites")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")
        invite = _make_invite()
        db.execute = AsyncMock(side_effect=[
            _scalar(org),
            _scalar(member),
            _scalars_result([invite]),  # invites query
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/invites", headers=_auth_header())
            assert r.status_code == 200
            body = r.json()
            assert len(body) == 1
            assert body[0]["email"] == "invited@example.com"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_empty_invites(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")
        db.execute = AsyncMock(side_effect=[
            _scalar(org),
            _scalar(member),
            _scalars_result([]),
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/invites", headers=_auth_header())
            assert r.status_code == 200
            assert r.json() == []
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_permission_denied_member_role(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="member")  # needs admin+
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/invites", headers=_auth_header())
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestCancelInvite:
    """DELETE /v1/orgs/{org_id}/invites/{invite_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/orgs/{ORG_ID}/invites/{INVITE_ID}")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")
        invite = _make_invite()
        db.execute = AsyncMock(side_effect=[
            _scalar(org),
            _scalar(member),
            _scalar(invite),  # invite lookup with for_update
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/orgs/{ORG_ID}/invites/{INVITE_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 204
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invite_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="admin")
        db.execute = AsyncMock(side_effect=[
            _scalar(org),
            _scalar(member),
            _scalar(None),  # invite not found
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/orgs/{ORG_ID}/invites/{INVITE_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_permission_denied(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="member")
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/orgs/{ORG_ID}/invites/{INVITE_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestAcceptInvite:
    """POST /v1/orgs/join?token=..."""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/orgs/join?token=abc123")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("routers.orgs.create_notification", new_callable=AsyncMock)
    async def test_happy_path(self, mock_notif):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        invite = _make_invite(email="test@example.com")
        user = _make_user(email="test@example.com")
        org = _make_org()

        db.execute = AsyncMock(side_effect=[
            _scalar(invite),  # invite lookup by token
            _scalar(user),    # user lookup
            _scalar(org),     # org lookup for notification
        ])
        # db.scalar: already member check -> 0
        # db.scalar after commit: member_count for _org_to_out
        db.scalar = AsyncMock(side_effect=[0, 3])

        async def _refresh(obj):
            if getattr(obj, "created_at", None) is None:
                obj.created_at = NOW

        db.refresh = _refresh
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/orgs/join?token=test-invite-token",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["name"] == "Test Org"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_token(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # invite lookup returns None (invalid/expired)
        db.execute = AsyncMock(return_value=_scalar(None))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/orgs/join?token=bad-token",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
            assert "expired" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_email_mismatch(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        invite = _make_invite(email="other@example.com")
        user = _make_user(email="test@example.com")  # different email
        db.execute = AsyncMock(side_effect=[
            _scalar(invite),
            _scalar(user),
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/orgs/join?token=test-invite-token",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
            assert "other@example.com" in r.json()["detail"]
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_already_a_member(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        invite = _make_invite(email="test@example.com")
        user = _make_user(email="test@example.com")
        db.execute = AsyncMock(side_effect=[
            _scalar(invite),
            _scalar(user),
        ])
        # already member check -> 1
        db.scalar = AsyncMock(return_value=1)
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/orgs/join?token=test-invite-token",
                    headers=_auth_header(),
                )
            assert r.status_code == 409
            assert "already a member" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_user_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        invite = _make_invite(email="test@example.com")
        db.execute = AsyncMock(side_effect=[
            _scalar(invite),
            _scalar(None),  # user not found
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/orgs/join?token=test-invite-token",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
            assert "user" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestTransferCredits:
    """POST /v1/orgs/{org_id}/credits/transfer"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/credits/transfer",
                    json={"amount": 10, "direction": "to_org"},
                )
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path_to_org(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org(credits=50)
        member = _make_member(role="admin")
        user = _make_user(credits=500)

        db.execute = AsyncMock(side_effect=[
            _scalar(org),     # _get_org_and_require_role: org
            _scalar(member),  # _get_org_and_require_role: member
            _scalar(org),     # lock org row
            _scalar(user),    # lock user row
        ])
        db.scalar = AsyncMock(return_value=2)  # member_count for _org_to_out

        async def _refresh(obj):
            if getattr(obj, "created_at", None) is None:
                obj.created_at = NOW

        db.refresh = _refresh
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/credits/transfer",
                    json={"amount": 100, "direction": "to_org"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            # User credits: 500 - 100 = 400
            assert user.credits == 400
            # Org credits: 50 + 100 = 150
            assert org.credits == 150
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path_from_org(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org(credits=200)
        member = _make_member(role="admin")
        user = _make_user(credits=100)

        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(member),
            _scalar(org), _scalar(user),
        ])
        db.scalar = AsyncMock(return_value=2)

        async def _refresh(obj):
            if getattr(obj, "created_at", None) is None:
                obj.created_at = NOW

        db.refresh = _refresh
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/credits/transfer",
                    json={"amount": 50, "direction": "from_org"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            assert user.credits == 150   # 100 + 50
            assert org.credits == 150    # 200 - 50
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_insufficient_user_credits_to_org(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org(credits=50)
        member = _make_member(role="admin")
        user = _make_user(credits=5)  # only 5 credits

        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(member),
            _scalar(org), _scalar(user),
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/credits/transfer",
                    json={"amount": 100, "direction": "to_org"},
                    headers=_auth_header(),
                )
            assert r.status_code == 402
            body = r.json()["detail"]
            assert body["error"] == "insufficient_credits"
            assert body["available"] == 5
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_insufficient_org_credits_from_org(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org(credits=10)
        member = _make_member(role="admin")
        user = _make_user(credits=500)

        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(member),
            _scalar(org), _scalar(user),
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/credits/transfer",
                    json={"amount": 100, "direction": "from_org"},
                    headers=_auth_header(),
                )
            assert r.status_code == 402
            body = r.json()["detail"]
            assert body["error"] == "insufficient_org_credits"
            assert body["available"] == 10
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_permission_denied_member_role(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="member")  # needs admin+
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/credits/transfer",
                    json={"amount": 10, "direction": "to_org"},
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_amount(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/orgs/{ORG_ID}/credits/transfer",
                    json={"amount": 0, "direction": "to_org"},
                    headers=_auth_header(),
                )
            assert r.status_code == 422  # Pydantic: ge=1
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestSetActiveOrg:
    """POST /v1/orgs/{org_id}/activate"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(f"/v1/orgs/{ORG_ID}/activate")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="viewer")
        user = _make_user()

        db.execute = AsyncMock(side_effect=[
            _scalar(org),     # _get_org_and_require_role: org
            _scalar(member),  # _get_org_and_require_role: member
            _scalar(user),    # user lookup
        ])
        db.scalar = AsyncMock(return_value=2)  # member_count

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(f"/v1/orgs/{ORG_ID}/activate", headers=_auth_header())
            assert r.status_code == 200
            body = r.json()
            assert body["name"] == "Test Org"
            # Verify user.active_org_id was set
            assert user.active_org_id == uuid.UUID(ORG_ID)
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_org_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(f"/v1/orgs/{ORG_ID}/activate", headers=_auth_header())
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestDeactivateOrg:
    """POST /v1/orgs/deactivate"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/orgs/deactivate")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        user = _make_user()
        user.active_org_id = uuid.UUID(ORG_ID)
        db.execute = AsyncMock(return_value=_scalar(user))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/orgs/deactivate", headers=_auth_header())
            assert r.status_code == 204
            assert user.active_org_id is None
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_user_not_found_still_204(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # user not found -- endpoint is idempotent, still returns 204
        db.execute = AsyncMock(return_value=_scalar(None))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/orgs/deactivate", headers=_auth_header())
            assert r.status_code == 204
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestOrgAnalytics:
    """GET /v1/orgs/{org_id}/analytics"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/analytics")
            assert r.status_code == 401
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="member")

        agg = _agg_row()
        agg_result = MagicMock()
        agg_result.one = MagicMock(return_value=agg)

        type_row = MagicMock()
        type_row.type = "web_research"
        type_row.count = 5
        type_row.credits = 50
        type_row.completed = 3
        type_result = MagicMock()
        type_result.all = MagicMock(return_value=[type_row])

        member_row = MagicMock()
        member_row.id = uuid.UUID(USER_ID)
        member_row.name = "Test User"
        member_row.email = "test@example.com"
        member_row.tasks_created = 10
        member_row.tasks_completed = 5
        member_row.credits_spent = 50
        member_result = MagicMock()
        member_result.all = MagicMock(return_value=[member_row])

        daily_row = MagicMock()
        daily_row.day = "2026-04-01"
        daily_row.count = 3
        daily_row.completed = 2
        daily_result = MagicMock()
        daily_result.all = MagicMock(return_value=[daily_row])

        db.execute = AsyncMock(side_effect=[
            _scalar(org),       # _get_org_and_require_role: org
            _scalar(member),    # _get_org_and_require_role: member
            agg_result,         # aggregate stats
            type_result,        # type breakdown
            member_result,      # member breakdown
            daily_result,       # daily series
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/analytics", headers=_auth_header())
            assert r.status_code == 200
            body = r.json()
            assert body["org_id"] == ORG_ID
            assert body["org_name"] == "Test Org"
            assert body["summary"]["total_tasks"] == 10
            assert body["summary"]["completed"] == 5
            assert body["summary"]["credits_spent"] == 50
            assert len(body["by_type"]) == 1
            assert body["by_type"][0]["type"] == "web_research"
            assert len(body["by_member"]) == 1
            assert len(body["daily"]) == 1
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_custom_days_parameter(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="member")

        agg = _agg_row(total=0, completed=0, failed=0, running=0, credits_spent=0, avg_duration_ms=None)
        agg_result = MagicMock()
        agg_result.one = MagicMock(return_value=agg)

        empty_result = MagicMock()
        empty_result.all = MagicMock(return_value=[])

        db.execute = AsyncMock(side_effect=[
            _scalar(org), _scalar(member),
            agg_result, empty_result, empty_result, empty_result,
        ])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/analytics?days=7", headers=_auth_header())
            assert r.status_code == 200
            assert r.json()["days"] == 7
            assert r.json()["summary"]["total_tasks"] == 0
            assert r.json()["summary"]["completion_rate"] == 0.0
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_org_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/analytics", headers=_auth_header())
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_permission_denied_viewer(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        org = _make_org()
        member = _make_member(role="viewer")  # needs member+
        db.execute = AsyncMock(side_effect=[_scalar(org), _scalar(member)])
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/orgs/{ORG_ID}/analytics", headers=_auth_header())
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)
