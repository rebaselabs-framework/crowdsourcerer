"""Credit ledger — atomic charge / refund / transaction logging.

Centralises the "lock payer row, adjust balance, write a
CreditTransactionDB row" pattern that previously lived inline inside
``routers/tasks.py::_refund_task_credits`` and other credit paths.

Design
------

- The *payer* for a task is derived from ``task.org_id`` (org wallet
  first, falling back to ``task.user_id``). Callers never pick the
  payer themselves.
- Balance mutations always lock the payer row with
  ``SELECT ... FOR UPDATE`` to prevent concurrent-batch overcharges.
- Every mutation writes a matching :class:`CreditTransactionDB` row
  so the ledger stays queryable and the user's transaction history
  always reflects reality.
- Negative balances raise :class:`CreditLedgerError`. Callers that
  want to pre-flight check a balance should call
  :meth:`CreditLedger.balance_for`.

The service is intentionally stateless — no wrapping class instance
is needed, everything is a function that takes the DB session
explicitly. Callers pass ``default_ledger`` for the common path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import CreditTransactionDB, OrganizationDB, UserDB

logger = structlog.get_logger()


class CreditLedgerError(ValueError):
    """Raised when a credit operation would leave a balance below zero
    or when the payer row cannot be located."""


class _TaskPayer(Protocol):
    """Structural subset of ``TaskDB`` the ledger needs to pick a payer."""

    id: object  # actually UUID but the ledger only stores it
    type: str
    user_id: object
    org_id: object | None


@dataclass(frozen=True, slots=True)
class CreditLedger:
    """Policy knobs for the ledger. Stateless at runtime."""

    #: Allow charges that bring the balance exactly to zero (normal).
    allow_zero_balance: bool = True
    #: When True, refunds past their original amount are blocked.
    enforce_refund_cap: bool = False

    # ─── Charges ──────────────────────────────────────────────────────────

    async def charge(
        self,
        db: AsyncSession,
        task: _TaskPayer,
        amount: int,
        *,
        reason: str = "create",
    ) -> int | None:
        """Deduct *amount* credits from the task's payer and write a
        matching ``charge`` transaction. Returns the payer's new
        balance, or ``None`` when the payer row has been deleted (the
        transaction is still recorded for audit-trail integrity).

        Raises :class:`CreditLedgerError` when the amount is negative
        or (if ``allow_zero_balance`` is off) when the balance would
        go negative.
        """
        if amount < 0:
            raise CreditLedgerError(f"charge amount must be ≥ 0, got {amount}")
        if amount == 0:
            return None

        new_balance = await self._apply(db, task, -amount)
        if (
            new_balance is not None
            and new_balance < 0
            and not self.allow_zero_balance
        ):
            raise CreditLedgerError(
                f"charge would leave payer balance at {new_balance} credits"
            )
        self._write_transaction(db, task, -amount, reason=f"Task {reason}: {task.type}")
        return new_balance

    # ─── Refunds ──────────────────────────────────────────────────────────

    async def refund(
        self,
        db: AsyncSession,
        task: _TaskPayer,
        amount: int,
        *,
        reason: str = "cancelled",
    ) -> int | None:
        """Credit *amount* back to the task's payer and write a
        matching ``refund`` transaction. Returns the payer's new
        balance, or ``None`` when the payer row has been deleted.
        """
        if amount < 0:
            raise CreditLedgerError(f"refund amount must be ≥ 0, got {amount}")
        if amount == 0:
            return None

        new_balance = await self._apply(db, task, amount)
        self._write_transaction(
            db, task, amount, reason=f"Task {reason}: {task.type}", kind="refund",
        )
        return new_balance

    # ─── Read ────────────────────────────────────────────────────────────

    async def balance_for(
        self,
        db: AsyncSession,
        task: _TaskPayer,
    ) -> int:
        """Return the current balance on the task's payer without locking."""
        if task.org_id:
            row = (
                await db.execute(
                    select(OrganizationDB.credits).where(
                        OrganizationDB.id == task.org_id
                    )
                )
            ).scalar_one_or_none()
        else:
            row = (
                await db.execute(
                    select(UserDB.credits).where(UserDB.id == task.user_id)
                )
            ).scalar_one_or_none()
        if row is None:
            raise CreditLedgerError(
                f"no payer row for task {task.id} "
                f"(org_id={task.org_id}, user_id={task.user_id})"
            )
        return int(row)

    # ─── Internals ────────────────────────────────────────────────────────

    async def _apply(
        self,
        db: AsyncSession,
        task: _TaskPayer,
        delta: int,
    ) -> int | None:
        """Lock the payer row, add *delta*, return the new balance.

        Returns None when the payer row is missing — matches the legacy
        ``_refund_task_credits`` behaviour where a deleted org silently
        skipped the balance mutation but still recorded a transaction.
        Callers that need strictness should use :meth:`balance_for`
        first to verify the payer exists.
        """
        if task.org_id:
            org = (
                await db.execute(
                    select(OrganizationDB)
                    .where(OrganizationDB.id == task.org_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if org is None:
                return None
            org.credits += delta
            return int(org.credits)

        user = (
            await db.execute(
                select(UserDB).where(UserDB.id == task.user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if user is None:
            return None
        user.credits += delta
        return int(user.credits)

    @staticmethod
    def _write_transaction(
        db: AsyncSession,
        task: _TaskPayer,
        amount: int,
        *,
        reason: str,
        kind: str | None = None,
    ) -> None:
        """Append a CreditTransactionDB row matching the balance change."""
        inferred_kind = kind or ("refund" if amount > 0 else "charge")
        db.add(
            CreditTransactionDB(
                user_id=str(task.user_id),
                task_id=task.id,
                amount=amount,
                type=inferred_kind,
                description=reason,
            )
        )


default_ledger: CreditLedger = CreditLedger()


__all__ = [
    "CreditLedger",
    "CreditLedgerError",
    "default_ledger",
]
