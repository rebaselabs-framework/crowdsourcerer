"""Unit tests for services/credit_ledger.py."""

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from services.credit_ledger import CreditLedger, CreditLedgerError, default_ledger


# ── Lightweight stand-ins (no DB, no ORM) ──────────────────────────────


@dataclass
class _FakeUser:
    credits: int = 0
    id: UUID = field(default_factory=uuid4)


@dataclass
class _FakeOrg:
    credits: int = 0
    id: UUID = field(default_factory=uuid4)


@dataclass
class _FakeTask:
    id: UUID = field(default_factory=uuid4)
    type: str = "web_research"
    user_id: UUID = field(default_factory=uuid4)
    org_id: UUID | None = None


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _mock_db(payer):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(payer))
    db.add = MagicMock()
    return db


# ── Refund ────────────────────────────────────────────────────────────


class TestRefund:
    @pytest.mark.asyncio
    async def test_refund_credits_user_when_no_org(self):
        user = _FakeUser(credits=100)
        task = _FakeTask(user_id=user.id)
        db = _mock_db(user)

        new_balance = await default_ledger.refund(db, task, 25)

        assert user.credits == 125
        assert new_balance == 125

    @pytest.mark.asyncio
    async def test_refund_credits_org_when_org_id_set(self):
        org = _FakeOrg(credits=500)
        task = _FakeTask(org_id=org.id)
        db = _mock_db(org)

        new_balance = await default_ledger.refund(db, task, 50)

        assert org.credits == 550
        assert new_balance == 550

    @pytest.mark.asyncio
    async def test_refund_writes_transaction_record(self):
        user = _FakeUser(credits=0)
        task = _FakeTask(user_id=user.id, type="llm_generate")
        db = _mock_db(user)

        await default_ledger.refund(db, task, 7)

        txn = db.add.call_args[0][0]
        assert txn.amount == 7
        assert txn.type == "refund"
        assert txn.description.endswith("llm_generate")
        assert txn.task_id == task.id
        assert txn.user_id == str(task.user_id)

    @pytest.mark.asyncio
    async def test_refund_reason_flows_into_description(self):
        task = _FakeTask(type="code_execute")
        db = _mock_db(_FakeUser())

        await default_ledger.refund(db, task, 5, reason="failed")

        txn = db.add.call_args[0][0]
        assert "failed" in txn.description

    @pytest.mark.asyncio
    async def test_refund_missing_payer_still_writes_txn_and_returns_none(self):
        """Legacy semantics: if the payer row is gone, we record the txn
        for audit trail integrity but can't actually credit anything."""
        task = _FakeTask(org_id=uuid4())
        db = _mock_db(None)  # org row is gone

        balance = await default_ledger.refund(db, task, 10)

        assert balance is None
        assert db.add.called  # txn still recorded

    @pytest.mark.asyncio
    async def test_refund_zero_is_noop(self):
        task = _FakeTask()
        db = _mock_db(_FakeUser())

        balance = await default_ledger.refund(db, task, 0)

        assert balance is None
        assert not db.add.called

    @pytest.mark.asyncio
    async def test_refund_negative_raises(self):
        task = _FakeTask()
        db = _mock_db(_FakeUser())

        with pytest.raises(CreditLedgerError):
            await default_ledger.refund(db, task, -5)


# ── Charge ────────────────────────────────────────────────────────────


class TestCharge:
    @pytest.mark.asyncio
    async def test_charge_deducts_from_user(self):
        user = _FakeUser(credits=100)
        task = _FakeTask(user_id=user.id)
        db = _mock_db(user)

        balance = await default_ledger.charge(db, task, 30)

        assert user.credits == 70
        assert balance == 70

    @pytest.mark.asyncio
    async def test_charge_deducts_from_org(self):
        org = _FakeOrg(credits=1000)
        task = _FakeTask(org_id=org.id)
        db = _mock_db(org)

        balance = await default_ledger.charge(db, task, 100)

        assert org.credits == 900
        assert balance == 900

    @pytest.mark.asyncio
    async def test_charge_writes_negative_transaction(self):
        user = _FakeUser(credits=50)
        task = _FakeTask(user_id=user.id)
        db = _mock_db(user)

        await default_ledger.charge(db, task, 10, reason="create")

        txn = db.add.call_args[0][0]
        assert txn.amount == -10
        assert txn.type == "charge"
        assert "create" in txn.description

    @pytest.mark.asyncio
    async def test_charge_strict_ledger_rejects_overdraft(self):
        strict = CreditLedger(allow_zero_balance=False)
        user = _FakeUser(credits=5)
        task = _FakeTask(user_id=user.id)
        db = _mock_db(user)

        with pytest.raises(CreditLedgerError):
            await strict.charge(db, task, 10)

    @pytest.mark.asyncio
    async def test_charge_negative_raises(self):
        task = _FakeTask()
        db = _mock_db(_FakeUser())
        with pytest.raises(CreditLedgerError):
            await default_ledger.charge(db, task, -1)


# ── balance_for ────────────────────────────────────────────────────────


class TestBalanceFor:
    @pytest.mark.asyncio
    async def test_balance_for_user(self):
        task = _FakeTask()
        db = _mock_db(42)
        assert await default_ledger.balance_for(db, task) == 42

    @pytest.mark.asyncio
    async def test_balance_for_org(self):
        task = _FakeTask(org_id=uuid4())
        db = _mock_db(777)
        assert await default_ledger.balance_for(db, task) == 777

    @pytest.mark.asyncio
    async def test_balance_for_missing_payer_raises(self):
        task = _FakeTask()
        db = _mock_db(None)
        with pytest.raises(CreditLedgerError):
            await default_ledger.balance_for(db, task)


# ── Value-object discipline ───────────────────────────────────────────


class TestPolicyKnobs:
    def test_ledger_is_frozen(self):
        with pytest.raises((AttributeError, Exception)):
            default_ledger.allow_zero_balance = False  # type: ignore[misc]

    def test_custom_ledger_overrides_policy(self):
        strict = CreditLedger(allow_zero_balance=False)
        assert strict.allow_zero_balance is False
        assert default_ledger.allow_zero_balance is True
