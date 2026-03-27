"""Tests for requester feedback (requester_note / reviewed_at) on task submissions.

Covers:
  - approve_submission with a reason → requester_note + reviewed_at persisted
  - approve_submission without a reason → requester_note is None
  - reject_submission with a reason → requester_note + reviewed_at persisted
  - reject_submission without a reason → requester_note is None
  - SubmissionReviewRequest accepts optional `reason` field (422 on bad types, OK on None)
  - TaskAssignmentOut schema exposes requester_note + reviewed_at fields
  - SubmissionOut schema exposes requester_note + reviewed_at fields
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "feedback-test-secret")
os.environ.setdefault("API_KEY_SALT", "feedback-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ────────────────────────────────────────────────────────────────

REQUESTER_ID = str(uuid.uuid4())
WORKER_ID    = str(uuid.uuid4())
TASK_ID      = str(uuid.uuid4())
ASSIGN_ID    = str(uuid.uuid4())


# ── Background-task suppression ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _suppress_background():
    noop = AsyncMock()
    with (
        patch("routers.tasks.fire_persistent_endpoints", noop),
        patch("routers.tasks.fire_webhook_for_task",     noop),
    ):
        yield


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


def _make_mock_db() -> MagicMock:
    db = MagicMock()
    db.add      = MagicMock()
    db.flush    = AsyncMock()
    db.commit   = AsyncMock()
    db.rollback = AsyncMock()
    db.close    = AsyncMock()
    db.execute  = AsyncMock()
    db.scalar   = AsyncMock(return_value=0)

    async def _refresh(obj): pass
    db.refresh = _refresh
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_task() -> MagicMock:
    t = MagicMock()
    t.id             = uuid.UUID(TASK_ID)
    t.user_id        = uuid.UUID(REQUESTER_ID)
    t.type           = "label_text"
    t.status         = "open"
    t.execution_mode = "human"
    t.output         = None
    t.webhook_url    = None
    t.webhook_events = []
    return t


def _make_assignment(status: str = "submitted") -> MagicMock:
    a = MagicMock()
    a.id               = uuid.UUID(ASSIGN_ID)
    a.task_id          = uuid.UUID(TASK_ID)
    a.worker_id        = uuid.UUID(WORKER_ID)
    a.status           = status
    a.earnings_credits = 10
    a.xp_earned        = 20
    a.claimed_at       = datetime.now(timezone.utc)
    a.submitted_at     = datetime.now(timezone.utc)
    a.requester_note   = None
    a.reviewed_at      = None
    return a


def _make_requester_user(credits: int = 500) -> MagicMock:
    u = MagicMock()
    u.id      = uuid.UUID(REQUESTER_ID)
    u.credits = credits
    u.is_admin = False
    return u


def _db_for_review(task, assignment, requester=None) -> MagicMock:
    """Mock DB: call1=task, call2=assignment, call3=requester (for reject refund path)."""
    db = _make_mock_db()
    call_count = 0

    def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        if call_count == 2:
            return _scalar(assignment)
        if call_count == 3 and requester is not None:
            return _scalar(requester)
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        r.scalar_one         = MagicMock(return_value=None)
        r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return r

    db.execute.side_effect = _side
    return db


# ── approve tests ─────────────────────────────────────────────────────────────

class TestApproveWithNote:

    @pytest.mark.asyncio
    async def test_approve_with_reason_persists_note(self):
        """approve_submission with a reason sets requester_note on the assignment."""
        task       = _make_task()
        assignment = _make_assignment(status="submitted")
        db         = _db_for_review(task, assignment)

        from main import app
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={"reason": "Great work, very accurate!"},
                    headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
                )
            assert r.status_code == 200, r.text
            # requester_note was set on the assignment object
            assert assignment.requester_note == "Great work, very accurate!"
            # reviewed_at was set
            assert assignment.reviewed_at is not None
            # DB was committed
            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_approve_without_reason_note_is_none(self):
        """approve_submission with no reason leaves requester_note as None."""
        task       = _make_task()
        assignment = _make_assignment(status="submitted")
        db         = _db_for_review(task, assignment)

        from main import app
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
                )
            assert r.status_code == 200, r.text
            assert assignment.requester_note is None
            assert assignment.reviewed_at is not None
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_approve_with_empty_string_reason_note_is_none(self):
        """Empty string reason is treated as no-note (stored as None)."""
        task       = _make_task()
        assignment = _make_assignment(status="submitted")
        db         = _db_for_review(task, assignment)

        from main import app
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={"reason": ""},
                    headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
                )
            assert r.status_code == 200, r.text
            # Empty string → None (falsy check in `req.reason or None`)
            assert assignment.requester_note is None
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_approve_status_updated_to_approved(self):
        """approve_submission updates assignment.status to 'approved'."""
        task       = _make_task()
        assignment = _make_assignment(status="submitted")
        db         = _db_for_review(task, assignment)

        from main import app
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={"reason": "Excellent!"},
                    headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
                )
            assert r.status_code == 200, r.text
            assert assignment.status == "approved"
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── reject tests ──────────────────────────────────────────────────────────────

class TestRejectWithNote:

    @pytest.mark.asyncio
    async def test_reject_with_reason_persists_note(self):
        """reject_submission with a reason sets requester_note on the assignment."""
        task       = _make_task()
        assignment = _make_assignment(status="submitted")
        requester  = _make_requester_user(credits=500)
        db         = _db_for_review(task, assignment, requester)

        from main import app
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/reject",
                    json={"reason": "Response missed the key label."},
                    headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
                )
            assert r.status_code == 200, r.text
            assert assignment.requester_note == "Response missed the key label."
            assert assignment.reviewed_at is not None
            assert assignment.status == "rejected"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_reject_without_reason_note_is_none(self):
        """reject_submission with no reason leaves requester_note as None."""
        task       = _make_task()
        assignment = _make_assignment(status="submitted")
        requester  = _make_requester_user(credits=500)
        db         = _db_for_review(task, assignment, requester)

        from main import app
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/reject",
                    json={},
                    headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
                )
            assert r.status_code == 200, r.text
            assert assignment.requester_note is None
            assert assignment.reviewed_at is not None
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_reject_non_submitted_returns_409(self):
        """Rejecting an assignment not in 'submitted' status returns 409."""
        task       = _make_task()
        assignment = _make_assignment(status="approved")
        db         = _db_for_review(task, assignment)

        from main import app
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/reject",
                    json={"reason": "Late"},
                    headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
                )
            assert r.status_code == 409, r.text
            assert "Cannot reject" in r.json()["detail"]
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Schema unit tests ─────────────────────────────────────────────────────────

class TestSchemas:

    def test_submission_review_request_accepts_no_reason(self):
        """SubmissionReviewRequest is valid with no reason field."""
        from models.schemas import SubmissionReviewRequest
        req = SubmissionReviewRequest()
        assert req.reason is None

    def test_submission_review_request_accepts_reason(self):
        """SubmissionReviewRequest stores the reason string."""
        from models.schemas import SubmissionReviewRequest
        req = SubmissionReviewRequest(reason="Good job")
        assert req.reason == "Good job"

    def test_task_assignment_out_has_requester_note_field(self):
        """TaskAssignmentOut schema exposes requester_note and reviewed_at."""
        from models.schemas import TaskAssignmentOut
        fields = TaskAssignmentOut.model_fields
        assert "requester_note" in fields
        assert "reviewed_at" in fields

    def test_task_assignment_out_note_defaults_none(self):
        """TaskAssignmentOut requester_note defaults to None."""
        from models.schemas import TaskAssignmentOut
        field = TaskAssignmentOut.model_fields["requester_note"]
        assert field.default is None

    def test_submission_out_has_requester_note_field(self):
        """SubmissionOut schema (requester's view) also exposes requester_note."""
        from models.schemas import SubmissionOut
        fields = SubmissionOut.model_fields
        assert "requester_note" in fields
        assert "reviewed_at" in fields

    def test_submission_out_note_defaults_none(self):
        """SubmissionOut requester_note defaults to None."""
        from models.schemas import SubmissionOut
        field = SubmissionOut.model_fields["requester_note"]
        assert field.default is None
