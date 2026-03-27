"""Auth guard + input validation tests for worker_marketplace, pipelines,
disputes, saved_searches, endorsements, and certifications endpoints.

Pattern: 401 without credentials for auth-required endpoints.
         No 401 for public endpoints.
         422 for invalid input (Pydantic).
"""
from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_dummy")

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock


# ── Mock DB helpers ───────────────────────────────────────────────────────────

def _make_empty_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.delete = AsyncMock()
    db.get = AsyncMock(return_value=None)
    empty_result = MagicMock()
    empty_result.scalar_one_or_none = MagicMock(return_value=None)
    empty_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(return_value=empty_result)
    db.scalar = AsyncMock(return_value=0)
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def client():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def client_with_db():
    from main import app
    from core.database import get_db
    mock_db = _make_empty_db()
    app.dependency_overrides[get_db] = _db_override(mock_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_db, None)


# ── Token helper ──────────────────────────────────────────────────────────────

def _make_token(user_id: str | None = None) -> str:
    from jose import jwt as _jwt
    import time
    secret = os.environ.get("JWT_SECRET", "app-test-secret")
    return _jwt.encode(
        {"sub": user_id or str(uuid.uuid4()), "exp": int(time.time()) + 300},
        secret,
        algorithm="HS256",
    )


def _uid() -> str:
    return str(uuid.uuid4())


def _task_id() -> str:
    return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════════════════
# worker_marketplace.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_browse_workers_requires_auth(client):
    r = await client.get("/v1/workers/browse")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_task_invite_requires_auth(client):
    r = await client.post(
        f"/v1/tasks/{_task_id()}/invite",
        json={"worker_id": _uid()},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_bulk_invite_requires_auth(client):
    r = await client.post(
        f"/v1/tasks/{_task_id()}/bulk-invite",
        json={"worker_ids": [_uid()]},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_task_invites_requires_auth(client):
    r = await client.get(f"/v1/tasks/{_task_id()}/invites")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_worker_invites_requires_auth(client):
    r = await client.get("/v1/worker/invites")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_respond_invite_requires_auth(client):
    r = await client.post(
        f"/v1/worker/invites/{_uid()}/respond",
        json={"action": "accept"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_add_watchlist_requires_auth(client):
    r = await client.post(f"/v1/worker/watchlist/{_task_id()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_remove_watchlist_requires_auth(client):
    r = await client.delete(f"/v1/worker/watchlist/{_task_id()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_watchlist_requires_auth(client):
    r = await client.get("/v1/worker/watchlist")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_check_watchlist_requires_auth(client):
    r = await client.get(f"/v1/worker/watchlist/check/{_task_id()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_invite_missing_worker_id_is_422(client):
    """worker_id is required in InviteRequest."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/invite",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_invite_message_too_long_is_422(client):
    """message max_length=500 in InviteRequest."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/invite",
        json={"worker_id": _uid(), "message": "x" * 501},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_respond_invite_invalid_action_is_rejected(client):
    """action must be 'accept' or 'decline' — returns 400 (manual) or 422 (Pydantic)."""
    token = _make_token()
    r = await client.post(
        f"/v1/worker/invites/{_uid()}/respond",
        json={"action": "ignore"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (400, 422)


# ═══════════════════════════════════════════════════════════════════════════════
# pipelines.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_pipeline_requires_auth(client):
    r = await client.post("/v1/pipelines", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_pipelines_requires_auth(client):
    r = await client.get("/v1/pipelines")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_pipeline_requires_auth(client):
    r = await client.get(f"/v1/pipelines/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_pipeline_requires_auth(client):
    r = await client.delete(f"/v1/pipelines/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_run_pipeline_requires_auth(client):
    r = await client.post(f"/v1/pipelines/{_uid()}/run", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_pipeline_runs_requires_auth(client):
    r = await client.get(f"/v1/pipelines/{_uid()}/runs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_pipeline_run_requires_auth(client):
    r = await client.get(f"/v1/pipelines/runs/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_retry_pipeline_run_requires_auth(client):
    r = await client.post(f"/v1/pipelines/runs/{_uid()}/retry", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_cancel_pipeline_run_requires_auth(client):
    r = await client.post(f"/v1/pipelines/runs/{_uid()}/cancel")
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# disputes.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_dispute_tasks_requires_auth(client):
    r = await client.get("/v1/disputes/tasks")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_consensus_requires_auth(client):
    r = await client.get(f"/v1/disputes/tasks/{_task_id()}/consensus")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_resolve_dispute_requires_auth(client):
    r = await client.post(
        f"/v1/disputes/tasks/{_task_id()}/resolve",
        json={"winning_assignment_id": _uid()},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_evidence_requires_auth(client):
    r = await client.get(f"/v1/disputes/tasks/{_task_id()}/evidence")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_evidence_requires_auth(client):
    r = await client.post(
        f"/v1/disputes/tasks/{_task_id()}/evidence",
        json={"evidence_type": "text", "content": "some evidence"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_dispute_timeline_requires_auth(client):
    r = await client.get(f"/v1/disputes/tasks/{_task_id()}/timeline")
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# saved_searches.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_saved_searches_requires_auth(client):
    r = await client.get("/v1/worker/saved-searches")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_saved_search_requires_auth(client):
    r = await client.post("/v1/worker/saved-searches", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_saved_search_requires_auth(client):
    r = await client.get(f"/v1/worker/saved-searches/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_update_saved_search_requires_auth(client):
    r = await client.patch(f"/v1/worker/saved-searches/{_uid()}", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_saved_search_requires_auth(client):
    r = await client.delete(f"/v1/worker/saved-searches/{_uid()}")
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# endorsements.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_endorse_worker_requires_auth(client):
    r = await client.post(
        f"/v1/workers/{_uid()}/endorse",
        json={"task_id": _task_id()},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_endorsements_no_auth_needed(client_with_db):
    """Endorsement list is public — should NOT return 401."""
    r = await client_with_db.get(f"/v1/workers/{_uid()}/endorsements")
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_endorsement_count_no_auth_needed(client_with_db):
    """Endorsement count is public — should NOT return 401."""
    r = await client_with_db.get(f"/v1/workers/{_uid()}/endorsements/count")
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_endorse_note_too_long_is_422(client):
    """note max_length=500 in EndorseRequest."""
    token = _make_token()
    r = await client.post(
        f"/v1/workers/{_uid()}/endorse",
        json={"task_id": _task_id(), "note": "x" * 501},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_endorse_skill_tag_too_long_is_422(client):
    """skill_tag max_length=100 in EndorseRequest."""
    token = _make_token()
    r = await client.post(
        f"/v1/workers/{_uid()}/endorse",
        json={"task_id": _task_id(), "skill_tag": "x" * 101},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_endorse_missing_task_id_is_422(client):
    """task_id is required in EndorseRequest."""
    token = _make_token()
    r = await client.post(
        f"/v1/workers/{_uid()}/endorse",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# certifications.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_certifications_requires_auth(client):
    r = await client.get("/v1/certifications")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_certification_requires_auth(client):
    r = await client.get("/v1/certifications/data_labeling")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_attempt_certification_requires_auth(client):
    r = await client.post(
        "/v1/certifications/data_labeling/attempt",
        json={"answers": []},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_my_earned_certifications_requires_auth(client):
    r = await client.get("/v1/certifications/me/earned")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_attempt_certification_empty_answers_passes_pydantic(client_with_db):
    """Empty answers list passes Pydantic; hits DB logic (will 404/400 due to mock)."""
    token = _make_token()
    r = await client_with_db.post(
        "/v1/certifications/data_labeling/attempt",
        json={"answers": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Should NOT be 422 (Pydantic validation passes)
    assert r.status_code != 422
