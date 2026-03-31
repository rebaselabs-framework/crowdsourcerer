"""Tests for org deletion credit handling.

Verifies that when an organization is deleted:
1. Active tasks billed to the org are cancelled and credits refunded to the org pool.
2. Remaining org credits (including refunds) are transferred to the owner's personal balance.
3. CreditTransactionDB records are created for refunds and transfers.
4. Org with zero credits works correctly (no-op transfer).
5. Org with no active tasks works correctly (direct transfer).
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_dummy")

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────────


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


def _scalars_list_result(items):
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    return r


def _make_org(credits: int = 500, org_id=None) -> MagicMock:
    org = MagicMock()
    org.id = org_id or uuid4()
    org.name = "Test Org"
    org.credits = credits
    return org


def _make_user(credits: int = 100, user_id=None) -> MagicMock:
    u = MagicMock()
    u.id = user_id or uuid4()
    u.credits = credits
    return u


def _make_member(user_id, org_id, role="owner") -> MagicMock:
    m = MagicMock()
    m.user_id = user_id
    m.org_id = org_id
    m.role = role
    return m


def _make_task(task_type="web_research", status="pending", org_id=None,
               user_id=None, worker_reward_credits=None,
               assignments_required=1) -> MagicMock:
    t = MagicMock()
    t.id = uuid4()
    t.type = task_type
    t.status = status
    t.org_id = org_id
    t.user_id = user_id
    t.worker_reward_credits = worker_reward_credits
    t.assignments_required = assignments_required
    return t


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestOrgDeletionCredits:
    """Tests for delete_org credit transfer."""

    @pytest.mark.asyncio
    async def test_org_credits_transferred_to_owner(self):
        """When org has credits but no active tasks, credits go to owner."""
        from routers.orgs import delete_org

        org_id = uuid4()
        user_id = uuid4()
        org = _make_org(credits=500, org_id=org_id)
        user = _make_user(credits=100, user_id=user_id)
        member = _make_member(user_id, org_id, role="owner")

        call_idx = 0

        async def _exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                # _get_org_and_require_role: select org
                return _scalar_result(org)
            if call_idx == 2:
                # _get_org_and_require_role: select member
                return _scalar_result(member)
            if call_idx == 3:
                # Lock org row (with_for_update)
                return _scalar_result(org)
            if call_idx == 4:
                # Active tasks query
                return _scalars_list_result([])
            if call_idx == 5:
                # User lookup for credit transfer
                return _scalar_result(user)
            return _scalar_result(None)

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_exec)
        db.add = MagicMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_org(org_id, db=db, user_id=str(user_id))

        # Owner gets org's credits
        assert user.credits == 600  # 100 + 500
        # Org credits zeroed out
        assert org.credits == 0
        # Transaction record created for transfer
        added_items = [c.args[0] for c in db.add.call_args_list]
        from models.db import CreditTransactionDB
        transfer_txns = [
            item for item in added_items
            if isinstance(item, CreditTransactionDB) and item.type == "transfer"
        ]
        assert len(transfer_txns) == 1
        assert transfer_txns[0].amount == 500

    @pytest.mark.asyncio
    async def test_active_tasks_cancelled_and_refunded_before_transfer(self):
        """Active tasks are cancelled and refunded to org pool, then transferred to owner."""
        from routers.orgs import delete_org

        org_id = uuid4()
        user_id = uuid4()
        org = _make_org(credits=200, org_id=org_id)
        user = _make_user(credits=50, user_id=user_id)
        member = _make_member(user_id, org_id, role="owner")

        # Two active AI tasks: web_research (10cr) + entity_lookup (5cr)
        task1 = _make_task("web_research", "pending", org_id, user_id)
        task2 = _make_task("entity_lookup", "queued", org_id, user_id)

        call_idx = 0

        async def _exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _scalar_result(org)
            if call_idx == 2:
                return _scalar_result(member)
            if call_idx == 3:
                return _scalar_result(org)
            if call_idx == 4:
                return _scalars_list_result([task1, task2])
            if call_idx == 5:
                return _scalar_result(user)
            return _scalar_result(None)

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_exec)
        db.add = MagicMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_org(org_id, db=db, user_id=str(user_id))

        # Tasks should be cancelled
        assert task1.status == "cancelled"
        assert task2.status == "cancelled"

        # Org credits: started at 200, refunded 10+5=15, total 215, then zeroed
        # User credits: started at 50, received 215 from org = 265
        assert user.credits == 265
        assert org.credits == 0

    @pytest.mark.asyncio
    async def test_human_tasks_refunded_correctly(self):
        """Human task refund computation matches charge formula."""
        from routers.orgs import delete_org

        org_id = uuid4()
        user_id = uuid4()
        org = _make_org(credits=100, org_id=org_id)
        user = _make_user(credits=0, user_id=user_id)
        member = _make_member(user_id, org_id, role="owner")

        # Human task: label_image with 5 credits reward, 3 assignments
        # Cost = 5 * 3 + max(1, int(5*3*0.2)) = 15 + 3 = 18
        task = _make_task(
            "label_image", "open", org_id, user_id,
            worker_reward_credits=5, assignments_required=3,
        )

        call_idx = 0

        async def _exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _scalar_result(org)
            if call_idx == 2:
                return _scalar_result(member)
            if call_idx == 3:
                return _scalar_result(org)
            if call_idx == 4:
                return _scalars_list_result([task])
            if call_idx == 5:
                return _scalar_result(user)
            return _scalar_result(None)

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_exec)
        db.add = MagicMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_org(org_id, db=db, user_id=str(user_id))

        # Org: 100 + 18 refund = 118, transferred to user
        assert user.credits == 118
        assert task.status == "cancelled"

    @pytest.mark.asyncio
    async def test_zero_credits_org_no_transfer(self):
        """Org with 0 credits and no tasks: nothing to transfer."""
        from routers.orgs import delete_org

        org_id = uuid4()
        user_id = uuid4()
        org = _make_org(credits=0, org_id=org_id)
        user = _make_user(credits=50, user_id=user_id)
        member = _make_member(user_id, org_id, role="owner")

        call_idx = 0

        async def _exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _scalar_result(org)
            if call_idx == 2:
                return _scalar_result(member)
            if call_idx == 3:
                return _scalar_result(org)
            if call_idx == 4:
                return _scalars_list_result([])
            return _scalar_result(None)

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_exec)
        db.add = MagicMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_org(org_id, db=db, user_id=str(user_id))

        # User credits unchanged
        assert user.credits == 50
        # No transfer transaction created (but db.add may be called for refund txns)
        added_items = [c.args[0] for c in db.add.call_args_list]
        from models.db import CreditTransactionDB
        transfer_txns = [
            item for item in added_items
            if isinstance(item, CreditTransactionDB) and item.type == "transfer"
        ]
        assert len(transfer_txns) == 0

    @pytest.mark.asyncio
    async def test_non_cancellable_tasks_ignored(self):
        """Tasks in completed/cancelled/failed status aren't selected for cancellation."""
        from routers.orgs import delete_org

        org_id = uuid4()
        user_id = uuid4()
        org = _make_org(credits=300, org_id=org_id)
        user = _make_user(credits=10, user_id=user_id)
        member = _make_member(user_id, org_id, role="owner")

        # Only pending task is returned — completed ones are filtered by the query
        pending_task = _make_task("screenshot", "pending", org_id, user_id)

        call_idx = 0

        async def _exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _scalar_result(org)
            if call_idx == 2:
                return _scalar_result(member)
            if call_idx == 3:
                return _scalar_result(org)
            if call_idx == 4:
                # Query only returns cancellable tasks
                return _scalars_list_result([pending_task])
            if call_idx == 5:
                return _scalar_result(user)
            return _scalar_result(None)

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_exec)
        db.add = MagicMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_org(org_id, db=db, user_id=str(user_id))

        # screenshot task cost is 2 credits
        # Org: 300 + 2 = 302, transferred to user
        # User: 10 + 302 = 312
        assert pending_task.status == "cancelled"
        assert user.credits == 312

    @pytest.mark.asyncio
    async def test_refund_transactions_created_per_task(self):
        """Each cancelled task gets its own CreditTransactionDB refund record."""
        from routers.orgs import delete_org

        org_id = uuid4()
        user_id = uuid4()
        org = _make_org(credits=100, org_id=org_id)
        user = _make_user(credits=0, user_id=user_id)
        member = _make_member(user_id, org_id, role="owner")

        tasks = [
            _make_task("web_research", "pending", org_id, user_id),
            _make_task("pii_detect", "queued", org_id, user_id),
            _make_task("llm_generate", "open", org_id, user_id),
        ]

        call_idx = 0

        async def _exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _scalar_result(org)
            if call_idx == 2:
                return _scalar_result(member)
            if call_idx == 3:
                return _scalar_result(org)
            if call_idx == 4:
                return _scalars_list_result(tasks)
            if call_idx == 5:
                return _scalar_result(user)
            return _scalar_result(None)

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_exec)
        db.add = MagicMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_org(org_id, db=db, user_id=str(user_id))

        # 3 refund records + 1 transfer record
        added_items = [c.args[0] for c in db.add.call_args_list]
        from models.db import CreditTransactionDB
        txns = [item for item in added_items if isinstance(item, CreditTransactionDB)]
        refund_txns = [t for t in txns if t.type == "refund"]
        transfer_txns = [t for t in txns if t.type == "transfer"]
        assert len(refund_txns) == 3
        assert len(transfer_txns) == 1

        # Verify each refund amount
        refund_amounts = sorted([t.amount for t in refund_txns])
        # web_research=10, pii_detect=2, llm_generate=1
        assert refund_amounts == [1, 2, 10]

    @pytest.mark.asyncio
    async def test_org_is_deleted_after_credit_handling(self):
        """db.delete(org) and db.commit() are called."""
        from routers.orgs import delete_org

        org_id = uuid4()
        user_id = uuid4()
        org = _make_org(credits=50, org_id=org_id)
        user = _make_user(credits=0, user_id=user_id)
        member = _make_member(user_id, org_id, role="owner")

        call_idx = 0

        async def _exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _scalar_result(org)
            if call_idx == 2:
                return _scalar_result(member)
            if call_idx == 3:
                return _scalar_result(org)
            if call_idx == 4:
                return _scalars_list_result([])
            if call_idx == 5:
                return _scalar_result(user)
            return _scalar_result(None)

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_exec)
        db.add = MagicMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        await delete_org(org_id, db=db, user_id=str(user_id))

        db.delete.assert_called_once_with(org)
        db.commit.assert_called_once()
