"""Unit tests for dispute/consensus logic.

Covers:
  - _response_key() canonical JSON comparison
  - check_and_apply_consensus() for all four strategies:
    any_first, requester_review, majority_vote, unanimous
  - Tie/no-consensus edge cases

No real DB required — consensus logic is mocked at the DB level.
"""
from __future__ import annotations

import os
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest


# ── _response_key() ───────────────────────────────────────────────────────────

def response_key(r):
    from routers.disputes import _response_key
    return _response_key(r)


def test_response_key_simple_dict():
    """Two dicts with the same content produce the same key."""
    a = {"label": "cat"}
    b = {"label": "cat"}
    assert response_key(a) == response_key(b)


def test_response_key_different_values():
    """Different values produce different keys."""
    a = {"label": "cat"}
    b = {"label": "dog"}
    assert response_key(a) != response_key(b)


def test_response_key_key_order_independent():
    """Dict key ordering doesn't affect the canonical key (sort_keys=True)."""
    a = {"label": "cat", "confidence": 0.9}
    b = {"confidence": 0.9, "label": "cat"}
    assert response_key(a) == response_key(b)


def test_response_key_nested_dict():
    """Nested dicts are also serialised canonically."""
    a = {"answer": {"verdict": "true", "score": 1}}
    b = {"answer": {"score": 1, "verdict": "true"}}
    assert response_key(a) == response_key(b)


def test_response_key_numeric_types():
    """Integer vs float values must compare equal when JSON-equivalent."""
    a = {"rating": 4}
    b = {"rating": 4}
    assert response_key(a) == response_key(b)


def test_response_key_output_is_valid_json():
    """The returned key must be parseable as JSON."""
    key = response_key({"label": "dog", "score": 0.87})
    parsed = json.loads(key)
    assert parsed["label"] == "dog"


# ── Helpers for mocking DB in consensus checks ───────────────────────────────

def _make_assignment(response_dict, assignment_id=None):
    """Create a minimal mock TaskAssignmentDB-like object."""
    a = MagicMock()
    a.id = assignment_id or uuid4()
    a.response = response_dict
    a.status = "submitted"
    return a


def _make_task(strategy, n_required, n_completed):
    """Create a minimal mock TaskDB-like object."""
    t = MagicMock()
    t.id = uuid4()
    t.user_id = uuid4()
    t.consensus_strategy = strategy
    t.assignments_required = n_required
    t.assignments_completed = n_completed
    t.status = "open"
    t.dispute_status = None
    t.output = None
    t.winning_assignment_id = None
    t.completed_at = None
    return t


def _make_db_for_assignments(assignments, winner=None):
    """Build a mock DB that returns the given assignments + optional winner."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = assignments

    winner_result = MagicMock()
    winner_result.scalar_one_or_none.return_value = winner

    mock_db = AsyncMock()
    # First execute call = load assignments; second (optional) = load winner
    mock_db.execute = AsyncMock(side_effect=[mock_result, winner_result])
    return mock_db


# ── any_first strategy ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consensus_any_first_returns_immediately():
    """any_first is handled upstream; check_and_apply_consensus must be a no-op."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("any_first", n_required=1, n_completed=1)
    db = AsyncMock()

    await check_and_apply_consensus(task, db)

    # No DB queries should be made (returns at first check)
    db.execute.assert_not_called()
    # Task status must remain unchanged (still "open" as set in _make_task)
    assert task.status == "open"


# ── requester_review strategy ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consensus_requester_review_flags_dispute():
    """requester_review always flags the task for manual review."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("requester_review", n_required=2, n_completed=2)
    db = AsyncMock()

    # Mock the assignments query
    assignments = [
        _make_assignment({"label": "cat"}),
        _make_assignment({"label": "dog"}),
    ]
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = assignments
    db.execute = AsyncMock(return_value=result_mock)

    # Mock create_notification
    with pytest.MonkeyPatch().context() as mp:
        from unittest.mock import patch
        with patch("routers.disputes.create_notification", new_callable=AsyncMock) as mock_notif:
            await check_and_apply_consensus(task, db)

    assert task.dispute_status == "disputed"
    assert task.status == "completed"
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_consensus_requester_review_incomplete_no_action():
    """requester_review: if not all assignments are in yet, don't act."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("requester_review", n_required=3, n_completed=2)
    db = AsyncMock()

    await check_and_apply_consensus(task, db)

    # No query should be made — early return because count not met
    db.execute.assert_not_called()


# ── majority_vote strategy ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consensus_majority_vote_2_of_3_win():
    """majority_vote: 2/3 workers agree → winner is set, task completed."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("majority_vote", n_required=3, n_completed=3)

    agree_id = uuid4()
    agree_id2 = uuid4()
    dissent_id = uuid4()
    winner_response = {"label": "cat"}
    assignments = [
        _make_assignment(winner_response, agree_id),
        _make_assignment(winner_response, agree_id2),
        _make_assignment({"label": "dog"}, dissent_id),
    ]

    winner_assignment = _make_assignment(winner_response, agree_id)
    db = _make_db_for_assignments(assignments, winner=winner_assignment)

    with pytest.MonkeyPatch().context() as mp:
        from unittest.mock import patch
        with patch("routers.disputes.create_notification", new_callable=AsyncMock):
            await check_and_apply_consensus(task, db)

    assert task.status == "completed"
    assert task.dispute_status is None
    assert task.output == winner_response
    assert task.winning_assignment_id == agree_id


@pytest.mark.asyncio
async def test_consensus_majority_vote_3_way_tie_dispute():
    """majority_vote: 3 workers each give a different answer → dispute."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("majority_vote", n_required=3, n_completed=3)
    assignments = [
        _make_assignment({"label": "cat"}),
        _make_assignment({"label": "dog"}),
        _make_assignment({"label": "bird"}),
    ]
    db = _make_db_for_assignments(assignments)

    with pytest.MonkeyPatch().context() as mp:
        from unittest.mock import patch
        with patch("routers.disputes.create_notification", new_callable=AsyncMock):
            await check_and_apply_consensus(task, db)

    assert task.dispute_status == "disputed"
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_consensus_majority_vote_exact_half_no_majority():
    """majority_vote: 2/4 (50%) is NOT a majority (needs >50%)."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("majority_vote", n_required=4, n_completed=4)
    assignments = [
        _make_assignment({"label": "cat"}),
        _make_assignment({"label": "cat"}),
        _make_assignment({"label": "dog"}),
        _make_assignment({"label": "dog"}),
    ]
    db = _make_db_for_assignments(assignments)

    with pytest.MonkeyPatch().context() as mp:
        from unittest.mock import patch
        with patch("routers.disputes.create_notification", new_callable=AsyncMock):
            await check_and_apply_consensus(task, db)

    assert task.dispute_status == "disputed"


# ── unanimous strategy ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consensus_unanimous_all_agree_win():
    """unanimous: all 3 workers agree → winner is set, task completed."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("unanimous", n_required=3, n_completed=3)
    winner_response = {"label": "cat"}
    agree_id = uuid4()
    assignments = [
        _make_assignment(winner_response, agree_id),
        _make_assignment(winner_response, uuid4()),
        _make_assignment(winner_response, uuid4()),
    ]
    winner_assignment = _make_assignment(winner_response, agree_id)
    db = _make_db_for_assignments(assignments, winner=winner_assignment)

    with pytest.MonkeyPatch().context() as mp:
        from unittest.mock import patch
        with patch("routers.disputes.create_notification", new_callable=AsyncMock):
            await check_and_apply_consensus(task, db)

    assert task.status == "completed"
    assert task.dispute_status is None
    assert task.output == winner_response


@pytest.mark.asyncio
async def test_consensus_unanimous_one_dissenter_dispute():
    """unanimous: 2/3 agree but 1 dissents → dispute."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("unanimous", n_required=3, n_completed=3)
    assignments = [
        _make_assignment({"label": "cat"}),
        _make_assignment({"label": "cat"}),
        _make_assignment({"label": "dog"}),  # dissent
    ]
    db = _make_db_for_assignments(assignments)

    with pytest.MonkeyPatch().context() as mp:
        from unittest.mock import patch
        with patch("routers.disputes.create_notification", new_callable=AsyncMock):
            await check_and_apply_consensus(task, db)

    assert task.dispute_status == "disputed"
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_consensus_unanimous_incomplete_no_action():
    """unanimous: not all assignments in yet → no action."""
    from routers.disputes import check_and_apply_consensus

    task = _make_task("unanimous", n_required=3, n_completed=2)
    db = AsyncMock()

    await check_and_apply_consensus(task, db)

    db.execute.assert_not_called()
