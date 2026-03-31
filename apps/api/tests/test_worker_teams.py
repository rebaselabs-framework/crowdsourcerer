"""Tests for the worker_teams router.

Covers:
  1.  list_teams — no memberships → empty PaginatedWorkerTeams
  2.  list_teams — bulk member counts are correctly mapped per team
  3.  list_teams — my_role is correctly assigned per team
  4.  accept_invite — invite not found → 404
  5.  accept_invite — non-pending invite → 400
  6.  accept_invite — team at 20-member capacity → 400
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "wt-test-secret")
os.environ.setdefault("API_KEY_SALT", "wt-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from fastapi import HTTPException


# ── IDs ───────────────────────────────────────────────────────────────────────

WORKER_ID = str(uuid.uuid4())
OWNER_ID  = str(uuid.uuid4())
TEAM1_ID  = str(uuid.uuid4())
TEAM2_ID  = str(uuid.uuid4())
INVITE_ID = str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _make_db() -> MagicMock:
    db = MagicMock()
    db.add      = MagicMock()
    db.flush    = AsyncMock()
    db.commit   = AsyncMock()
    db.rollback = AsyncMock()
    db.close    = AsyncMock()
    db.execute  = AsyncMock()
    db.scalar   = AsyncMock(return_value=0)
    db.delete   = AsyncMock()

    async def _refresh(obj):
        pass
    db.refresh = _refresh
    return db


def _make_worker_user(user_id: str = WORKER_ID) -> MagicMock:
    u = MagicMock()
    u.id                     = uuid.UUID(user_id)
    u.email                  = f"{user_id[:8]}@example.com"
    u.name                   = "Test Worker"
    u.role                   = "worker"
    u.worker_tasks_completed = 10
    u.worker_xp              = 500
    u.worker_level           = 3
    u.token_version          = 0
    return u


def _make_team(team_id: str, creator_id: str = OWNER_ID) -> MagicMock:
    t = MagicMock()
    t.id          = uuid.UUID(team_id)
    t.name        = f"Team {team_id[:8]}"
    t.description = "A test team"
    t.avatar_emoji = "👥"
    t.created_by  = uuid.UUID(creator_id)
    t.created_at  = _now()
    t.updated_at  = _now()
    return t


def _make_membership(team_id: uuid.UUID, user_id: uuid.UUID, role: str = "member") -> MagicMock:
    m = MagicMock()
    m.team_id  = team_id
    m.user_id  = user_id
    m.role     = role
    m.joined_at = _now()
    return m


def _make_invite(
    invite_id: str = INVITE_ID,
    team_id: str   = TEAM1_ID,
    invitee_id: str = WORKER_ID,
    status: str = "pending",
) -> MagicMock:
    inv = MagicMock()
    inv.id         = uuid.UUID(invite_id)
    inv.team_id    = uuid.UUID(team_id)
    inv.invitee_id = uuid.UUID(invitee_id)
    inv.invited_by = uuid.UUID(OWNER_ID)
    inv.status     = status
    inv.message    = None
    inv.created_at = _now()
    inv.expires_at = None   # no expiry
    return inv


# ── Result-wrapper helpers ────────────────────────────────────────────────────

def _scalar_one_result(value) -> MagicMock:
    """Result whose scalar_one_or_none() returns value."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.fetchall = MagicMock(return_value=[])
    r.all      = MagicMock(return_value=[])
    return r


def _fetchall_result(rows: list) -> MagicMock:
    """Result whose fetchall() returns a list of (id,) tuples."""
    r = MagicMock()
    r.fetchall           = MagicMock(return_value=rows)
    r.scalar_one_or_none = MagicMock(return_value=None)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all     = MagicMock(return_value=[])
    return r


def _scalars_all_result(items: list) -> MagicMock:
    """Result whose scalars().all() returns items."""
    r = MagicMock()
    r.scalars            = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.fetchall = MagicMock(return_value=[])
    r.all      = MagicMock(return_value=[])
    return r


def _all_result(rows: list) -> MagicMock:
    """Result whose .all() returns rows (used for GROUP BY queries)."""
    r = MagicMock()
    r.all                = MagicMock(return_value=rows)
    r.scalar_one_or_none = MagicMock(return_value=None)
    r.fetchall = MagicMock(return_value=rows)
    r.scalars  = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


# ═══════════════════════════════════════════════════════════════════════════════
# list_teams tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestListTeams:
    """Tests for the list_teams endpoint called directly (no HTTP layer)."""

    @pytest.mark.asyncio
    async def test_no_memberships_returns_empty(self):
        """If worker has no team memberships, returns empty PaginatedWorkerTeams."""
        from routers.worker_teams import list_teams

        user = _make_worker_user(WORKER_ID)
        db   = _make_db()
        call_num = [0]

        def _side_effect(stmt):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar_one_result(user)      # _require_worker
            if call_num[0] == 2:
                return _fetchall_result([])           # no team memberships
            return _scalar_one_result(None)

        db.execute.side_effect = _side_effect

        result = await list_teams(page=1, page_size=20, db=db, user_id=WORKER_ID)

        assert result.total == 0
        assert result.items == []

    @pytest.mark.asyncio
    async def test_bulk_member_counts_correctly_mapped(self):
        """list_teams uses one GROUP BY query and maps counts accurately to each team.

        This verifies the bulk-count optimisation: team1 gets 5 members, team2 gets 2.
        If the bulk mapping were broken (e.g. defaulting everything to 0), both
        counts would be 0 and the assertions would fail.
        """
        from routers.worker_teams import list_teams

        user  = _make_worker_user(WORKER_ID)
        team1 = _make_team(TEAM1_ID)
        team2 = _make_team(TEAM2_ID)
        uid   = uuid.UUID(WORKER_ID)

        # GROUP BY rows
        row1 = MagicMock(); row1.team_id = team1.id; row1.cnt = 5
        row2 = MagicMock(); row2.team_id = team2.id; row2.cnt = 2

        # My membership rows
        my1 = _make_membership(team1.id, uid, role="owner")
        my2 = _make_membership(team2.id, uid, role="member")

        db = _make_db()
        call_num = [0]

        def _side_effect(stmt):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar_one_result(user)                    # _require_worker
            if call_num[0] == 2:
                return _fetchall_result([(team1.id,), (team2.id,)])  # membership query
            if call_num[0] == 3:
                return _scalars_all_result([team1, team2])           # teams page
            if call_num[0] == 4:
                return _all_result([row1, row2])                     # GROUP BY counts
            if call_num[0] == 5:
                return _scalars_all_result([my1, my2])               # my roles
            return _scalar_one_result(None)

        db.execute.side_effect = _side_effect

        result = await list_teams(page=1, page_size=20, db=db, user_id=WORKER_ID)

        assert result.total == 2
        assert len(result.items) == 2

        out_by_id = {item.id: item for item in result.items}
        assert out_by_id[str(team1.id)].member_count == 5
        assert out_by_id[str(team2.id)].member_count == 2

    @pytest.mark.asyncio
    async def test_my_role_correctly_assigned(self):
        """list_teams assigns my_role per-team from the bulk roles query.

        worker is owner of team1 and member of team2.
        """
        from routers.worker_teams import list_teams

        user  = _make_worker_user(WORKER_ID)
        team1 = _make_team(TEAM1_ID)
        team2 = _make_team(TEAM2_ID)
        uid   = uuid.UUID(WORKER_ID)

        row1 = MagicMock(); row1.team_id = team1.id; row1.cnt = 3
        row2 = MagicMock(); row2.team_id = team2.id; row2.cnt = 7

        my1 = _make_membership(team1.id, uid, role="owner")
        my2 = _make_membership(team2.id, uid, role="member")

        db = _make_db()
        call_num = [0]

        def _side_effect(stmt):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar_one_result(user)
            if call_num[0] == 2:
                return _fetchall_result([(team1.id,), (team2.id,)])
            if call_num[0] == 3:
                return _scalars_all_result([team1, team2])
            if call_num[0] == 4:
                return _all_result([row1, row2])
            if call_num[0] == 5:
                return _scalars_all_result([my1, my2])
            return _scalar_one_result(None)

        db.execute.side_effect = _side_effect

        result = await list_teams(page=1, page_size=20, db=db, user_id=WORKER_ID)

        out_by_id = {item.id: item for item in result.items}
        assert out_by_id[str(team1.id)].my_role == "owner"
        assert out_by_id[str(team2.id)].my_role == "member"


# ═══════════════════════════════════════════════════════════════════════════════
# accept_invite tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAcceptInvite:
    """Tests for the accept_invite endpoint's guard logic.

    Called directly (bypassing FastAPI routing) so dependencies are injected
    explicitly. Only tests the guard/error paths that execute before get_team
    is called at the end of the happy path.
    """

    def _db_for_accept(self, user, invite) -> MagicMock:
        """Mock DB returning user on call 1, invite on call 2."""
        db = _make_db()
        call_num = [0]

        def _side_effect(stmt):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar_one_result(user)    # _require_worker
            if call_num[0] == 2:
                return _scalar_one_result(invite)  # invite with_for_update
            return _scalar_one_result(None)

        db.execute.side_effect = _side_effect
        return db

    @pytest.mark.asyncio
    async def test_invite_not_found_raises_404(self):
        """accept_invite raises 404 when no invite record matches the caller."""
        from routers.worker_teams import accept_invite

        user = _make_worker_user(WORKER_ID)
        db   = self._db_for_accept(user, None)  # invite → None

        with pytest.raises(HTTPException) as exc:
            await accept_invite(
                invite_id=uuid.uuid4(),
                db=db,
                user_id=WORKER_ID,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_already_accepted_invite_raises_400(self):
        """accept_invite raises 400 when the invite status is not 'pending'."""
        from routers.worker_teams import accept_invite

        user   = _make_worker_user(WORKER_ID)
        invite = _make_invite(status="accepted")
        db     = self._db_for_accept(user, invite)

        with pytest.raises(HTTPException) as exc:
            await accept_invite(
                invite_id=uuid.UUID(INVITE_ID),
                db=db,
                user_id=WORKER_ID,
            )
        assert exc.value.status_code == 400
        assert "accepted" in exc.value.detail

    @pytest.mark.asyncio
    async def test_team_at_capacity_raises_400(self):
        """accept_invite raises 400 when the team already has 20 members.

        This is the race-condition guard: after acquiring the row lock on the
        invite, we re-check the member count under the lock so two concurrent
        callers cannot both pass the 20-member check simultaneously.
        """
        from routers.worker_teams import accept_invite

        user   = _make_worker_user(WORKER_ID)
        invite = _make_invite(status="pending")
        db     = self._db_for_accept(user, invite)
        db.scalar.return_value = 20  # team is at capacity

        with pytest.raises(HTTPException) as exc:
            await accept_invite(
                invite_id=uuid.UUID(INVITE_ID),
                db=db,
                user_id=WORKER_ID,
            )
        assert exc.value.status_code == 400
        assert "20" in exc.value.detail or "limit" in exc.value.detail.lower()
        # Must NOT have committed (no membership was created)
        db.commit.assert_not_awaited()
