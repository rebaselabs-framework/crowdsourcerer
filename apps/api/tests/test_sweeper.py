"""Tests for the assignment timeout sweeper."""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set env before imports
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")


@pytest.mark.asyncio
async def test_sweep_once_no_expired():
    """When there are no expired assignments, sweep returns zeros."""
    from core.sweeper import sweep_once

    # Build a fake session factory that returns an empty result
    mock_assignment_result = MagicMock()
    mock_assignment_result.scalars.return_value.all.return_value = []

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_assignment_result)
    mock_db.commit = AsyncMock()
    mock_db.rollback = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock()
    mock_factory.return_value = mock_db

    result = await sweep_once(mock_factory)

    assert result["timed_out"] == 0
    assert result["reopened"] == 0
    assert result["errors"] == 0


@pytest.mark.asyncio
async def test_sweep_once_expired_assignment():
    """Expired assignments are marked timed_out and tasks may be reopened."""
    from core.sweeper import sweep_once

    now = datetime.now(timezone.utc)

    # Fake expired assignment
    assignment = MagicMock()
    assignment.id = "test-assignment-id"
    assignment.task_id = "test-task-id"
    assignment.worker_id = "test-worker-id"
    assignment.timeout_at = now - timedelta(minutes=35)
    assignment.status = "active"

    # Fake worker
    worker = MagicMock()
    worker.worker_reliability = 1.0

    # Fake task (assigned, not enough active assignments)
    task = MagicMock()
    task.id = "test-task-id"
    task.type = "label_image"
    task.status = "assigned"
    task.assignments_required = 1

    # Mock DB calls
    mock_db = AsyncMock()
    call_count = [0]

    async def mock_execute(query):
        call_count[0] += 1
        result = MagicMock()
        if call_count[0] == 1:
            # First call: expired assignments
            result.scalars.return_value.all.return_value = [assignment]
        elif call_count[0] == 2:
            # Worker lookup
            result.scalar_one_or_none.return_value = worker
        elif call_count[0] == 3:
            # Task lookup
            result.scalar_one_or_none.return_value = task
        elif call_count[0] == 4:
            # Active count
            result = 0  # No active assignments left
        return result

    mock_db.execute = mock_execute
    mock_db.scalar = AsyncMock(return_value=0)
    mock_db.commit = AsyncMock()
    mock_db.rollback = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock()
    mock_factory.return_value = mock_db

    result = await sweep_once(mock_factory)

    # The assignment should have been marked timed_out
    assert assignment.status == "timed_out"
    # Worker reliability should be penalised
    assert worker.worker_reliability < 1.0


def test_start_stop_sweeper():
    """Sweeper can be started and cancelled without error."""
    import asyncio
    from core.sweeper import start_sweeper, stop_sweeper, get_sweeper_task

    async def _run():
        mock_factory = MagicMock()
        task = start_sweeper(mock_factory, interval=9999)
        assert get_sweeper_task() is task
        assert not task.done()
        stop_sweeper()
        # Give event loop a tick to process the cancellation
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()

    asyncio.run(_run())


def test_sweeper_config_defaults():
    """Sweeper interval constant is sensible (>= 60s)."""
    from core.sweeper import SWEEP_INTERVAL_SECONDS
    assert SWEEP_INTERVAL_SECONDS >= 60
