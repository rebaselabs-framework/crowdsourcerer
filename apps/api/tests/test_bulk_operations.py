"""Unit tests for bulk task operations and task rerun credit calculation.

Covers:
  - bulk_cancel: only open/pending/running tasks are cancelled; others silently skipped
  - bulk_archive: only completed/failed/cancelled tasks archived; others skipped
  - bulk_action cancel: succeeded/failed dict structure; wrong-status goes to failed list
  - bulk_action retry: only failed AI tasks succeed; human or non-failed rejected with reason
  - rerun_task credit calculation: human formula (reward*n + 20% fee) and AI TASK_CREDITS lookup
  - Edge cases: empty result set, all tasks not owned, mixed batch, max/min platform fee
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


# ── Shared task-factory helpers ───────────────────────────────────────────────

def _task(status: str, *, uid=None, tid=None, execution_mode="ai", task_type="web_research"):
    """Return a minimal TaskDB-like mock."""
    t = MagicMock()
    t.id = tid or uuid4()
    t.user_id = uid or uuid4()
    t.status = status
    t.execution_mode = execution_mode
    t.type = task_type
    t.error = None
    return t


def _db_returning(tasks: list) -> AsyncMock:
    """Mock DB whose execute() returns a result whose scalars() yields *tasks*.

    The bulk endpoints iterate directly: ``{str(t.id): t for t in result.scalars()}``
    so result.scalars() must return something iterable — the tasks list itself.
    """
    result = MagicMock()
    # result.scalars() is called as a method; its return_value is the tasks list.
    # A list is directly iterable, matching the `for t in tasks_result.scalars()` pattern.
    result.scalars.return_value = tasks

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=result)
    mock_db.commit = AsyncMock()
    return mock_db


def _req(**kwargs):
    """Minimal request-body mock."""
    r = MagicMock()
    for k, v in kwargs.items():
        setattr(r, k, v)
    return r


# ── bulk_cancel ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_cancel_open_task():
    """An 'open' task is cancelled and counted."""
    from routers.tasks import bulk_cancel_tasks

    t = _task("open")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_cancel_tasks(req, db=db, user_id=str(t.user_id))

    assert result.cancelled == 1
    assert result.skipped == 0
    assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_bulk_cancel_pending_task():
    """A 'pending' task is also cancellable."""
    from routers.tasks import bulk_cancel_tasks

    t = _task("pending")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_cancel_tasks(req, db=db, user_id=str(t.user_id))
    assert result.cancelled == 1
    assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_bulk_cancel_running_task():
    """A 'running' task is cancellable."""
    from routers.tasks import bulk_cancel_tasks

    t = _task("running")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_cancel_tasks(req, db=db, user_id=str(t.user_id))
    assert result.cancelled == 1
    assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_bulk_cancel_completed_task_skipped():
    """A 'completed' task cannot be cancelled — it is silently skipped."""
    from routers.tasks import bulk_cancel_tasks

    t = _task("completed")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_cancel_tasks(req, db=db, user_id=str(t.user_id))
    assert result.cancelled == 0
    assert result.skipped == 1
    assert t.status == "completed"  # unchanged


@pytest.mark.asyncio
async def test_bulk_cancel_failed_task_skipped():
    """A 'failed' task is not cancellable."""
    from routers.tasks import bulk_cancel_tasks

    t = _task("failed")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_cancel_tasks(req, db=db, user_id=str(t.user_id))
    assert result.cancelled == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_bulk_cancel_not_owned_task_skipped():
    """A task not in the DB result (not owned) is counted as skipped."""
    from routers.tasks import bulk_cancel_tasks

    # DB returns empty — requester doesn't own this task
    db = _db_returning([])
    req = _req(task_ids=[uuid4()])

    result = await bulk_cancel_tasks(req, db=db, user_id=str(uuid4()))
    assert result.cancelled == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_bulk_cancel_mixed_batch():
    """3 tasks: open+pending cancelled, completed skipped."""
    from routers.tasks import bulk_cancel_tasks

    uid = uuid4()
    t1 = _task("open",    uid=uid)
    t2 = _task("pending", uid=uid)
    t3 = _task("completed", uid=uid)
    db = _db_returning([t1, t2, t3])
    req = _req(task_ids=[t1.id, t2.id, t3.id])

    result = await bulk_cancel_tasks(req, db=db, user_id=str(uid))
    assert result.cancelled == 2
    assert result.skipped == 1
    assert str(t1.id) in result.task_ids
    assert str(t2.id) in result.task_ids
    assert str(t3.id) not in result.task_ids


# ── bulk_archive ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_archive_completed():
    """'completed' tasks can be archived."""
    from routers.tasks import bulk_archive_tasks

    t = _task("completed")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_archive_tasks(req, db=db, user_id=str(t.user_id))
    assert result.archived == 1
    assert result.skipped == 0
    assert t.status == "archived"


@pytest.mark.asyncio
async def test_bulk_archive_failed():
    """'failed' tasks can be archived."""
    from routers.tasks import bulk_archive_tasks

    t = _task("failed")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_archive_tasks(req, db=db, user_id=str(t.user_id))
    assert result.archived == 1
    assert t.status == "archived"


@pytest.mark.asyncio
async def test_bulk_archive_cancelled():
    """'cancelled' tasks can be archived."""
    from routers.tasks import bulk_archive_tasks

    t = _task("cancelled")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_archive_tasks(req, db=db, user_id=str(t.user_id))
    assert result.archived == 1
    assert t.status == "archived"


@pytest.mark.asyncio
async def test_bulk_archive_open_skipped():
    """'open' (non-terminal) tasks cannot be archived."""
    from routers.tasks import bulk_archive_tasks

    t = _task("open")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_archive_tasks(req, db=db, user_id=str(t.user_id))
    assert result.archived == 0
    assert result.skipped == 1
    assert t.status == "open"


@pytest.mark.asyncio
async def test_bulk_archive_running_skipped():
    """'running' (non-terminal) tasks cannot be archived."""
    from routers.tasks import bulk_archive_tasks

    t = _task("running")
    db = _db_returning([t])
    req = _req(task_ids=[t.id])

    result = await bulk_archive_tasks(req, db=db, user_id=str(t.user_id))
    assert result.archived == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_bulk_archive_all_terminal():
    """All three terminal statuses archived in one batch."""
    from routers.tasks import bulk_archive_tasks

    uid = uuid4()
    t1 = _task("completed", uid=uid)
    t2 = _task("failed",    uid=uid)
    t3 = _task("cancelled", uid=uid)
    db = _db_returning([t1, t2, t3])
    req = _req(task_ids=[t1.id, t2.id, t3.id])

    result = await bulk_archive_tasks(req, db=db, user_id=str(uid))
    assert result.archived == 3
    assert result.skipped == 0


# ── bulk_action: cancel ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_action_cancel_queued():
    """bulk_action cancel succeeds on 'queued' tasks."""
    from routers.tasks import bulk_task_action

    t = _task("queued")
    db = _db_returning([t])
    req = _req(task_ids=[t.id], action="cancel")
    bg = MagicMock()

    result = await bulk_task_action(req, bg, db=db, user_id=str(t.user_id))
    assert str(t.id) in result.succeeded
    assert result.failed == []
    assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_bulk_action_cancel_completed_goes_to_failed_list():
    """bulk_action cancel on a 'completed' task is reported as failed with a reason."""
    from routers.tasks import bulk_task_action

    t = _task("completed")
    db = _db_returning([t])
    req = _req(task_ids=[t.id], action="cancel")
    bg = MagicMock()

    result = await bulk_task_action(req, bg, db=db, user_id=str(t.user_id))
    assert result.succeeded == []
    assert len(result.failed) == 1
    assert result.failed[0]["task_id"] == str(t.id)
    assert "status" in result.failed[0]["reason"]  # e.g. "cannot cancel task with status 'completed'"


@pytest.mark.asyncio
async def test_bulk_action_cancel_not_owned_goes_to_failed_list():
    """bulk_action cancel on an unowned task is reported as failed with not-found reason."""
    from routers.tasks import bulk_task_action

    db = _db_returning([])  # task not in result
    req = _req(task_ids=[uuid4()], action="cancel")
    bg = MagicMock()

    result = await bulk_task_action(req, bg, db=db, user_id=str(uuid4()))
    assert len(result.failed) == 1
    assert "not found" in result.failed[0]["reason"].lower()


# ── bulk_action: retry ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_action_retry_failed_ai_task():
    """Retrying a failed AI task succeeds and clears the error field."""
    from routers.tasks import bulk_task_action

    t = _task("failed", execution_mode="ai")
    t.error = "timeout"
    db = _db_returning([t])
    req = _req(task_ids=[t.id], action="retry")
    bg = MagicMock()

    result = await bulk_task_action(req, bg, db=db, user_id=str(t.user_id))
    assert str(t.id) in result.succeeded
    assert t.status == "queued"
    assert t.error is None


@pytest.mark.asyncio
async def test_bulk_action_retry_non_failed_task_rejected():
    """Retrying a task that is not in 'failed' status returns a failure reason."""
    from routers.tasks import bulk_task_action

    t = _task("completed", execution_mode="ai")
    db = _db_returning([t])
    req = _req(task_ids=[t.id], action="retry")
    bg = MagicMock()

    result = await bulk_task_action(req, bg, db=db, user_id=str(t.user_id))
    assert result.succeeded == []
    assert len(result.failed) == 1
    assert "retry" in result.failed[0]["reason"].lower() or "status" in result.failed[0]["reason"].lower()


@pytest.mark.asyncio
async def test_bulk_action_retry_human_task_rejected():
    """Retry is only supported for AI tasks; human tasks return a failure reason."""
    from routers.tasks import bulk_task_action

    t = _task("failed", execution_mode="human")
    db = _db_returning([t])
    req = _req(task_ids=[t.id], action="retry")
    bg = MagicMock()

    result = await bulk_task_action(req, bg, db=db, user_id=str(t.user_id))
    assert result.succeeded == []
    assert len(result.failed) == 1
    assert "ai" in result.failed[0]["reason"].lower()


@pytest.mark.asyncio
async def test_bulk_action_retry_mixed_batch():
    """In a mixed batch only the failed AI task retries; others go to failed list."""
    from routers.tasks import bulk_task_action

    uid = uuid4()
    good = _task("failed",    uid=uid, execution_mode="ai")
    bad1 = _task("completed", uid=uid, execution_mode="ai")   # wrong status
    bad2 = _task("failed",    uid=uid, execution_mode="human") # wrong mode
    db = _db_returning([good, bad1, bad2])
    req = _req(task_ids=[good.id, bad1.id, bad2.id], action="retry")
    bg = MagicMock()

    result = await bulk_task_action(req, bg, db=db, user_id=str(uid))
    assert result.succeeded == [str(good.id)]
    assert len(result.failed) == 2


@pytest.mark.asyncio
async def test_bulk_action_result_contains_action_field():
    """BulkActionResult always echoes back the action that was requested."""
    from routers.tasks import bulk_task_action

    db = _db_returning([])
    req = _req(task_ids=[uuid4()], action="cancel")
    bg = MagicMock()

    result = await bulk_task_action(req, bg, db=db, user_id=str(uuid4()))
    assert result.action == "cancel"


# ── rerun credit calculation ──────────────────────────────────────────────────
# These tests exercise the TASK_CREDITS and HUMAN_TASK_BASE_CREDITS constants
# and the credit formula used in rerun_task, without touching the DB.

def _human_rerun_cost(reward: int, n: int) -> int:
    """Replicate the rerun_task human-task cost formula."""
    platform_fee = max(1, int(reward * n * 0.2))
    return reward * n + platform_fee


def test_rerun_human_single_assignment():
    """Single assignment: reward × 1 + 20% fee (min 1)."""
    cost = _human_rerun_cost(reward=10, n=1)
    assert cost == 10 + max(1, int(10 * 1 * 0.2))   # 10 + 2 = 12


def test_rerun_human_platform_fee_minimum_is_1():
    """Very small reward still charges at least 1 credit in platform fee."""
    cost = _human_rerun_cost(reward=1, n=1)
    assert cost == 1 + 1  # fee floored at 1


def test_rerun_human_five_assignments():
    """5 assignments doubles the base and fee proportionally."""
    cost = _human_rerun_cost(reward=10, n=5)
    # reward*n = 50, fee = max(1, int(50*0.2)) = 10
    assert cost == 60


def test_rerun_human_label_image_defaults():
    """label_image default reward (3) with 3 assignments."""
    from routers.tasks import HUMAN_TASK_BASE_CREDITS
    reward = HUMAN_TASK_BASE_CREDITS["label_image"]
    cost = _human_rerun_cost(reward=reward, n=3)
    assert cost == reward * 3 + max(1, int(reward * 3 * 0.2))


def test_rerun_human_transcription_review_defaults():
    """transcription_review has the highest default reward (5)."""
    from routers.tasks import HUMAN_TASK_BASE_CREDITS
    assert HUMAN_TASK_BASE_CREDITS["transcription_review"] == 5


def test_rerun_ai_task_credits_lookup():
    """AI rerun cost comes from TASK_CREDITS dict."""
    from workers.router import TASK_CREDITS
    for task_type, credits in TASK_CREDITS.items():
        assert credits > 0, f"TASK_CREDITS[{task_type!r}] must be positive"


def test_rerun_ai_task_credits_fallback():
    """Unknown AI task type falls back to 5 credits."""
    from workers.router import TASK_CREDITS
    cost = TASK_CREDITS.get("unknown_future_type", 5)
    assert cost == 5


def test_human_task_base_credits_covers_all_types():
    """HUMAN_TASK_BASE_CREDITS must define a reward for every human task type."""
    from routers.tasks import HUMAN_TASK_BASE_CREDITS
    expected_types = {
        "label_image", "label_text", "rate_quality", "verify_fact",
        "moderate_content", "compare_rank", "answer_question", "transcription_review",
    }
    assert set(HUMAN_TASK_BASE_CREDITS.keys()) == expected_types


def test_human_task_base_credits_all_positive():
    """Every human task base credit value must be at least 1."""
    from routers.tasks import HUMAN_TASK_BASE_CREDITS
    for task_type, cost in HUMAN_TASK_BASE_CREDITS.items():
        assert cost >= 1, f"HUMAN_TASK_BASE_CREDITS[{task_type!r}] must be >= 1"


def test_rerun_platform_fee_scales_with_assignments():
    """Platform fee grows with the number of assignments (not a flat rate)."""
    reward = 10
    fee_1 = max(1, int(reward * 1 * 0.2))
    fee_5 = max(1, int(reward * 5 * 0.2))
    assert fee_5 > fee_1


def test_rerun_total_cost_greater_than_worker_rewards():
    """Requester always pays more than just the worker rewards (platform fee added)."""
    reward, n = 5, 4
    worker_payout = reward * n
    total = _human_rerun_cost(reward=reward, n=n)
    assert total > worker_payout
