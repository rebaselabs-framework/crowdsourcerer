"""Tests for the synchronous CrowdSorcerer client."""
import pytest
import respx
import httpx

from crowdsourcerer import CrowdSorcerer
from crowdsourcerer.errors import AuthError, RateLimitError, InsufficientCreditsError


BASE = "https://crowdsourcerer.rebaselabs.online"


def make_client() -> CrowdSorcerer:
    return CrowdSorcerer(api_key="cs_test_key", base_url=BASE)


# ─── Init ─────────────────────────────────────────────────────────────────

def test_requires_api_key():
    with pytest.raises(AuthError):
        CrowdSorcerer(api_key="")


def test_init_resources():
    c = make_client()
    assert c.tasks is not None
    assert c.credits is not None
    assert c.users is not None
    assert c.api_keys is not None


# ─── Tasks ────────────────────────────────────────────────────────────────

@respx.mock
def test_tasks_create():
    respx.post(f"{BASE}/v1/tasks").mock(return_value=httpx.Response(
        200,
        json={
            "id": "00000000-0000-0000-0000-000000000001",
            "type": "llm_generate",
            "status": "queued",
            "credits_used": 1,
            "execution_mode": "ai",
            "created_at": "2026-03-23T00:00:00Z",
        },
    ))
    c = make_client()
    task = c.tasks.create(
        type="llm_generate",
        input={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert task.type == "llm_generate"
    assert task.credits_used == 1


@respx.mock
def test_tasks_get():
    task_id = "00000000-0000-0000-0000-000000000001"
    respx.get(f"{BASE}/v1/tasks/{task_id}").mock(return_value=httpx.Response(
        200,
        json={
            "id": task_id,
            "type": "pii_detect",
            "status": "completed",
            "priority": "normal",
            "execution_mode": "ai",
            "input": {"text": "John"},
            "output": {"entities": []},
            "created_at": "2026-03-23T00:00:00Z",
        },
    ))
    c = make_client()
    task = c.tasks.get(task_id)
    assert task.status == "completed"
    assert task.output == {"entities": []}


# ─── Error handling ───────────────────────────────────────────────────────

@respx.mock
def test_401_raises_auth_error():
    respx.get(f"{BASE}/v1/users/me").mock(return_value=httpx.Response(401, json={"detail": "Unauthorized"}))
    c = make_client()
    with pytest.raises(AuthError):
        c.users.me()


@respx.mock
def test_429_raises_rate_limit():
    respx.get(f"{BASE}/v1/credits/balance").mock(
        return_value=httpx.Response(429, headers={"retry-after": "10"}, json={"detail": "Too many requests"})
    )
    c = CrowdSorcerer(api_key="cs_test_key", base_url=BASE, max_retries=1)
    with pytest.raises(RateLimitError) as exc:
        c.credits.balance()
    assert exc.value.retry_after == 10.0


@respx.mock
def test_402_raises_insufficient_credits():
    respx.post(f"{BASE}/v1/tasks").mock(return_value=httpx.Response(402, json={"detail": "Insufficient credits"}))
    c = make_client()
    with pytest.raises(InsufficientCreditsError):
        c.tasks.create("web_research", {"url": "https://x.com"})


# ─── Credits ──────────────────────────────────────────────────────────────

@respx.mock
def test_credits_balance():
    respx.get(f"{BASE}/v1/credits/balance").mock(return_value=httpx.Response(
        200,
        json={"available": 500, "total_used": 100, "total_purchased": 600},
    ))
    c = make_client()
    bal = c.credits.balance()
    assert bal.available == 500
    assert bal.total_used == 100
