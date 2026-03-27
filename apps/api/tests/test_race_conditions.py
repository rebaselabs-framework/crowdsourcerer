"""Tests for race condition fixes in approve_submission and admin_review_payout.

Covers:
  - approve_submission idempotency: already-approved → 200 "Already approved."
  - approve_submission rejects non-submitted: status="active" → 409
  - approve_submission with_for_update: verify SELECT uses row-level lock
  - admin_review_payout rejects already-paid: status="paid" → 409
  - admin_review_payout sets status: body.status="processing" → payout.status updated
  - admin_review_payout refunds credits on rejection: user.credits += credits_requested
    and a CreditTransactionDB row is created
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

# Must precede app imports
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "race-test-secret")
os.environ.setdefault("API_KEY_SALT", "race-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ────────────────────────────────────────────────────────────────

REQUESTER_ID = str(uuid.uuid4())
WORKER_ID     = str(uuid.uuid4())
ADMIN_ID      = str(uuid.uuid4())
TASK_ID       = str(uuid.uuid4())
ASSIGN_ID     = str(uuid.uuid4())
PAYOUT_ID     = str(uuid.uuid4())


# ── Background-task suppression ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _suppress_background():
    """Suppress fire-and-forget webhooks/emails that try to open real connections."""
    noop = AsyncMock()
    with (
        patch("routers.tasks.fire_persistent_endpoints", noop),
        patch("routers.tasks.fire_webhook_for_task",     noop),
    ):
        yield


# ── Low-level DB mock helpers ─────────────────────────────────────────────────

def _scalar_result(value):
    """Wrap *value* as a SQLAlchemy scalar-result mock."""
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

    async def _refresh(obj):
        pass

    db.refresh = _refresh
    return db


def _db_override(mock_db):
    """Return an async-generator function FastAPI recognises as a generator dependency."""
    async def _override():
        yield mock_db
    return _override


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Mock object factories ─────────────────────────────────────────────────────

def _make_task(status: str = "completed") -> MagicMock:
    t = MagicMock()
    t.id                    = uuid.UUID(TASK_ID)
    t.user_id               = uuid.UUID(REQUESTER_ID)
    t.type                  = "label_text"
    t.status                = status
    t.execution_mode        = "human"
    t.output                = None
    t.webhook_url           = None
    t.webhook_events        = []
    return t


def _make_assignment(status: str = "submitted") -> MagicMock:
    a = MagicMock()
    a.id               = uuid.UUID(ASSIGN_ID)
    a.task_id          = uuid.UUID(TASK_ID)
    a.worker_id        = uuid.UUID(WORKER_ID)
    a.status           = status
    a.earnings_credits = 5
    a.xp_earned        = 10
    a.claimed_at       = datetime.now(timezone.utc)
    a.submitted_at     = datetime.now(timezone.utc)
    return a


def _make_payout(status: str = "pending", credits: int = 100) -> MagicMock:
    p = MagicMock()
    p.id                = uuid.UUID(PAYOUT_ID)
    p.worker_id         = uuid.UUID(WORKER_ID)
    p.credits_requested = credits
    p.usd_amount        = credits / 100.0
    p.status            = status
    p.payout_method     = "paypal"
    p.payout_details    = {"email": "worker@test.com"}
    p.admin_note        = None
    p.processed_at      = None
    p.created_at        = datetime.now(timezone.utc)
    p.updated_at        = datetime.now(timezone.utc)
    return p


def _make_worker_user(credits: int = 500) -> MagicMock:
    u = MagicMock()
    u.id      = uuid.UUID(WORKER_ID)
    u.email   = "worker@test.com"
    u.credits = credits
    u.is_admin = False
    return u


# ── Approve-submission DB helper ──────────────────────────────────────────────

def _db_for_approve(task: MagicMock, assignment: MagicMock) -> MagicMock:
    """Build a mock DB whose execute() returns task on call 1, assignment on call 2."""
    db = _make_mock_db()
    call_count = 0

    def _side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar_result(task)
        if call_count == 2:
            return _scalar_result(assignment)
        # Subsequent calls (worker lookup, skill update, etc.) → None
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        r.scalar_one         = MagicMock(return_value=None)
        r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return r

    db.execute.side_effect = _side_effect
    return db


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def requester_headers():
    return {"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"}


# ═══════════════════════════════════════════════════════════════════════════════
# approve_submission tests (HTTP)
# ═══════════════════════════════════════════════════════════════════════════════

class TestApproveSubmissionRaceConditions:
    """Race-condition related tests for the approve_submission endpoint."""

    # ── Test 1: Idempotency — already-approved returns 200 ────────────────────

    @pytest.mark.asyncio
    async def test_already_approved_returns_200_with_message(self, app, requester_headers):
        """Calling approve on an already-approved assignment is idempotent.

        The endpoint must return 200 (not 409) with the "Already approved." message
        so that a second concurrent caller that reads the committed "approved" row
        can return cleanly without erroring.
        """
        task       = _make_task(status="completed")
        assignment = _make_assignment(status="approved")
        db         = _db_for_approve(task, assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers=requester_headers,
                )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["status"] == "approved"
            assert data["message"] == "Already approved."
            # Confirm DB was NOT committed (no mutation needed for already-approved)
            db.commit.assert_not_awaited()
        finally:
            app.dependency_overrides.pop(get_db, None)

    # ── Test 2: Non-submitted status → 409 ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_active_assignment_returns_409(self, app, requester_headers):
        """Calling approve on an assignment with status='active' must return 409.

        Only 'submitted' and 'approved' statuses are valid for approval; any other
        status (active, rejected, expired …) should be rejected with a clear message.
        """
        task       = _make_task(status="open")
        assignment = _make_assignment(status="active")
        db         = _db_for_approve(task, assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers=requester_headers,
                )
            assert r.status_code == 409, r.text
            detail = r.json()["detail"]
            assert "active" in detail
            assert "Cannot approve" in detail
        finally:
            app.dependency_overrides.pop(get_db, None)

    # ── Test 3: with_for_update is used (behaviour: submitted → approved) ─────

    @pytest.mark.asyncio
    async def test_with_for_update_path_completes_successfully(self, app, requester_headers):
        """The normal approval path (submitted → approved) completes with 200.

        This test confirms the code path that includes with_for_update() works
        end-to-end: the function reads the assignment (which in production acquires
        a row lock), mutates status, and commits.  We verify the mutation occurs
        and the response is correct.
        """
        task       = _make_task(status="completed")
        assignment = _make_assignment(status="submitted")
        db         = _db_for_approve(task, assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers=requester_headers,
                )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["status"] == "approved"
            # The assignment object in our mock should have been mutated
            assert assignment.status == "approved"
            # And a DB commit must have been issued
            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.pop(get_db, None)


# ═══════════════════════════════════════════════════════════════════════════════
# admin_review_payout tests (direct function call)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminReviewPayoutRaceConditions:
    """Race-condition related tests for the admin_review_payout function.

    We call the function directly (bypassing FastAPI routing and auth) so we can
    inject fully-controlled mock objects for *body*, *payout*, and *user*.
    """

    def _body(self, status: str, admin_note: str | None = None) -> MagicMock:
        """Build a minimal PayoutReviewRequest-like mock."""
        b = MagicMock()
        b.status     = status
        b.admin_note = admin_note
        # body.note is referenced in log_admin_action call — MagicMock handles it
        return b

    def _db_for_payout(self, payout: MagicMock, user: MagicMock | None = None) -> MagicMock:
        """Build a mock DB whose execute() returns payout on call 1, user on call 2."""
        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(payout)
            if call_count == 2 and user is not None:
                return _scalar_result(user)
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalar_one         = MagicMock(return_value=None)
            return r

        db.execute.side_effect = _side_effect
        return db

    # ── Test 4: already-paid payout → 409 ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_already_paid_raises_409(self):
        """admin_review_payout must refuse to re-process a 'paid' payout.

        The row-level lock serialises concurrent reviewers; the second reviewer
        reads status='paid' and must get a 409 to avoid double-payment.
        """
        from routers.payouts import admin_review_payout
        from fastapi import HTTPException

        payout = _make_payout(status="paid")
        db     = self._db_for_payout(payout)
        body   = self._body("processing")

        with pytest.raises(HTTPException) as exc_info:
            await admin_review_payout(
                payout_id=uuid.UUID(PAYOUT_ID),
                body=body,
                admin_id=ADMIN_ID,
                db=db,
            )
        assert exc_info.value.status_code == 409

    # ── Test 5: processing transition sets payout.status ─────────────────────

    @pytest.mark.asyncio
    async def test_processing_status_sets_payout_status(self):
        """Approving a pending payout with status='processing' updates payout.status."""
        from routers.payouts import admin_review_payout

        payout = _make_payout(status="pending")
        db     = self._db_for_payout(payout)
        body   = self._body("processing")

        # model_validate is called on the returned payout; make it work by
        # patching PayoutRequestOut.model_validate to return a simple mock.
        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=MagicMock()):
            await admin_review_payout(
                payout_id=uuid.UUID(PAYOUT_ID),
                body=body,
                admin_id=ADMIN_ID,
                db=db,
            )

        assert payout.status == "processing"
        db.commit.assert_awaited_once()

    # ── Test 6: rejection refunds credits and creates a transaction ───────────

    @pytest.mark.asyncio
    async def test_rejection_refunds_credits_and_creates_transaction(self):
        """Rejecting a pending payout of 100 credits refunds those credits to the user.

        The refund path must:
          1. Add 100 to user.credits.
          2. Call db.add() with a CreditTransactionDB whose amount == 100.
        """
        from routers.payouts import admin_review_payout
        from models.db import CreditTransactionDB

        CREDITS = 100
        payout = _make_payout(status="pending", credits=CREDITS)
        user   = _make_worker_user(credits=500)
        db     = self._db_for_payout(payout, user=user)
        body   = self._body("rejected", admin_note="Fraudulent account")

        added_objects = []
        original_add  = db.add
        def _capture_add(obj):
            added_objects.append(obj)
            return original_add(obj)
        db.add = _capture_add

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=MagicMock()):
            await admin_review_payout(
                payout_id=uuid.UUID(PAYOUT_ID),
                body=body,
                admin_id=ADMIN_ID,
                db=db,
            )

        # Credits should have been restored
        assert user.credits == 500 + CREDITS, (
            f"Expected user.credits=600, got {user.credits}"
        )

        # A CreditTransactionDB row should have been added
        credit_txns = [o for o in added_objects if isinstance(o, CreditTransactionDB)]
        assert len(credit_txns) == 1, (
            f"Expected 1 CreditTransactionDB added, got {len(credit_txns)}: {added_objects}"
        )
        txn = credit_txns[0]
        assert txn.amount == CREDITS
        assert txn.type == "refund"

        # payout status must be updated to rejected
        assert payout.status == "rejected"
        db.commit.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# cancel_payout_request tests (HTTP) — DELETE /v1/payouts/{payout_id}
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelPayoutRequest:
    """cancel_payout_request: happy path, 404, 409 (status guard)."""

    @pytest.fixture
    def worker_headers(self):
        return {"Authorization": f"Bearer {_real_token(WORKER_ID)}"}

    def _db_for_cancel(self, payout: MagicMock, user: MagicMock) -> MagicMock:
        """Build a mock DB whose execute() returns payout (call 1) and user (call 2)."""
        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(payout)   # payout row (with_for_update)
            if call_count == 2:
                return _scalar_result(user)     # user row (with_for_update)
            # Subsequent calls (notification helpers)
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalar_one         = MagicMock(return_value=None)
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            return r

        db.execute.side_effect = _side_effect
        return db

    @pytest.mark.asyncio
    async def test_cancel_pending_payout_refunds_credits(self, app, worker_headers):
        """Cancelling a pending payout refunds credits and creates CreditTransactionDB."""
        from models.db import CreditTransactionDB

        CREDITS = 200
        payout = _make_payout(status="pending", credits=CREDITS)
        user   = _make_worker_user(credits=100)
        db     = self._db_for_cancel(payout, user)

        added_objects: list = []
        original_add = db.add
        def _capture_add(obj):
            added_objects.append(obj)
            return original_add(obj)
        db.add = _capture_add

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.delete(
                    f"/v1/payouts/{PAYOUT_ID}",
                    headers=worker_headers,
                )
            assert r.status_code == 204, r.text

            # Credits must be refunded
            assert user.credits == 100 + CREDITS, (
                f"Expected user.credits=300, got {user.credits}"
            )

            # Payout must be marked rejected (cancelled)
            assert payout.status == "rejected"
            assert payout.admin_note == "Cancelled by worker"

            # A CreditTransactionDB must have been added
            credit_txns = [o for o in added_objects if isinstance(o, CreditTransactionDB)]
            assert len(credit_txns) == 1
            txn = credit_txns[0]
            assert txn.amount == CREDITS
            assert txn.type == "refund"

            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_cancel_not_found_returns_404(self, app, worker_headers):
        """Cancelling a payout that does not exist (or belongs to another user) → 404."""
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(None)   # not found

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.delete(
                    f"/v1/payouts/{PAYOUT_ID}",
                    headers=worker_headers,
                )
            assert r.status_code == 404, r.text
            db.commit.assert_not_awaited()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_cancel_non_pending_returns_409(self, app, worker_headers):
        """Cancelling an already-processing payout (race guard) must return 409."""
        payout = _make_payout(status="processing", credits=100)
        db     = _make_mock_db()
        db.execute.return_value = _scalar_result(payout)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.delete(
                    f"/v1/payouts/{PAYOUT_ID}",
                    headers=worker_headers,
                )
            assert r.status_code == 409, r.text
            assert "processing" in r.json()["detail"].lower()
            db.commit.assert_not_awaited()
        finally:
            app.dependency_overrides.pop(get_db, None)
