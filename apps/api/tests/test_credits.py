"""Smoke tests for credits-related API endpoints.

All tests use the ASGI app directly — no real DB or Stripe needed.
Auth-protected endpoints are verified to return 401 without credentials.
Core logic units (formatDuration, credit math helpers) are tested in isolation.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_dummy")

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
async def client():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── Auth guards ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_credits_requires_auth(client):
    """GET /v1/credits must return 401 without credentials."""
    r = await client.get("/v1/credits")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_transactions_requires_auth(client):
    """GET /v1/credits/transactions must return 401 without credentials."""
    r = await client.get("/v1/credits/transactions")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_checkout_requires_auth(client):
    """POST /v1/credits/checkout must return 401 without credentials."""
    r = await client.post("/v1/credits/checkout", json={"credits": 2500})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_worker_stats_requires_auth(client):
    """GET /v1/worker/stats must return 401 without credentials."""
    r = await client.get("/v1/worker/stats")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_worker_assignments_requires_auth(client):
    """GET /v1/worker/assignments must return 401 without credentials."""
    r = await client.get("/v1/worker/assignments")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_worker_activity_calendar_requires_auth(client):
    """GET /v1/worker/activity/calendar must return 401 without credentials."""
    r = await client.get("/v1/worker/activity/calendar")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_cache_stats_requires_auth(client):
    """GET /v1/admin/cache/stats must return 401 without credentials."""
    r = await client.get("/v1/admin/cache/stats")
    assert r.status_code == 401


# ── Stripe webhook must reject missing signature ──────────────────────────────

@pytest.mark.asyncio
async def test_stripe_webhook_rejects_missing_signature(client):
    """POST /v1/webhooks/stripe without Stripe-Signature must return 400 or 422.

    NOTE: get_settings() is lru_cache'd and may have been populated by an
    earlier test before STRIPE_WEBHOOK_SECRET was set in this module.  We
    force-refresh the cache here so the endpoint sees the correct secret.
    """
    import os
    from core.config import get_settings
    # Force the secret into the environment and reset the cached settings so
    # the Stripe handler sees a non-empty stripe_webhook_secret.
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_dummy"
    get_settings.cache_clear()
    try:
        r = await client.post(
            "/v1/webhooks/stripe",
            content=b'{"type": "checkout.session.completed"}',
            headers={"Content-Type": "application/json"},
            # No Stripe-Signature header → should reject with 400
        )
        # Stripe signature verification fails → 400 or 422
        assert r.status_code in (400, 422)
    finally:
        # Restore original environment and cache so other tests aren't affected
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        get_settings.cache_clear()


# ── Credit bonus logic (pure unit test, no DB/HTTP) ───────────────────────────

def test_credit_bonus_table_is_consistent():
    """The CREDIT_BONUSES dict must have all bundles with positive bonuses."""
    # Import the dict directly so we test the source of truth
    from routers.credits import _CREDIT_BONUSES
    assert len(_CREDIT_BONUSES) > 0, "Credit bonus table is empty"
    for credits_amount, bonus in _CREDIT_BONUSES.items():
        assert credits_amount > 0, "Bundle amount must be positive"
        assert bonus > 0, "Bonus must be positive"
        assert bonus < credits_amount, "Bonus should be less than the bundle itself"


def test_credit_bonus_larger_bundle_gets_more():
    """Bigger purchases should get a proportionally equal or better bonus rate."""
    from routers.credits import _CREDIT_BONUSES
    sorted_bundles = sorted(_CREDIT_BONUSES.items())
    prev_rate = 0.0
    for amount, bonus in sorted_bundles:
        rate = bonus / amount
        # Each tier's rate must be >= previous (better deal for larger purchase)
        assert rate >= prev_rate * 0.99, (
            f"Bundle {amount}: rate {rate:.3f} worse than previous {prev_rate:.3f}"
        )
        prev_rate = rate


# ── Task credit cost table ─────────────────────────────────────────────────────

def test_task_credit_costs_defined():
    """All known task types must have a credit cost defined."""
    from workers.router import TASK_CREDITS
    EXPECTED_TYPES = [
        "web_research", "entity_lookup", "document_parse", "data_transform",
        "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
        "code_execute", "web_intel",
    ]
    for t in EXPECTED_TYPES:
        assert t in TASK_CREDITS, f"Missing credit cost for task type: {t}"
        assert TASK_CREDITS[t] > 0, f"Credit cost for {t} must be positive"


def test_task_credit_costs_are_integers():
    """Credit costs must be whole numbers (no fractional credits)."""
    from workers.router import TASK_CREDITS
    for task_type, cost in TASK_CREDITS.items():
        assert isinstance(cost, int), f"Credit cost for {task_type} is not int: {cost!r}"
