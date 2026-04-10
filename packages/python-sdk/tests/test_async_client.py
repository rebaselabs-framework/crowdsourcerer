"""Tests for the async CrowdSorcerer client."""
import pytest
import respx
import httpx

from crowdsourcerer import AsyncCrowdSorcerer
from crowdsourcerer.errors import (
    AuthError,
    CrowdSorcererError,
    InsufficientCreditsError,
    NotFoundError,
    RateLimitError,
    TaskError,
)

BASE = "https://crowdsourcerer.rebaselabs.online"

TASK_RESPONSE = {
    "id": "00000000-0000-0000-0000-000000000001",
    "type": "llm_generate",
    "status": "completed",
    "priority": "normal",
    "execution_mode": "ai",
    "input": {"messages": [{"role": "user", "content": "Hello"}]},
    "output": {"raw": "Hi!"},
    "created_at": "2026-01-01T00:00:00Z",
    "credits_used": 1,
}


# ─── Init ─────────────────────────────────────────────────────────────────


def test_async_requires_api_key():
    with pytest.raises(AuthError):
        AsyncCrowdSorcerer(api_key="")


def test_async_init_resources():
    c = AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE)
    assert c.tasks is not None
    assert c.credits is not None
    assert c.users is not None
    assert c.api_keys is not None


# ─── Context manager ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_context_manager():
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        assert c.tasks is not None


# ─── Tasks ────────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_async_tasks_create():
    respx.post(f"{BASE}/v1/tasks").mock(return_value=httpx.Response(
        200,
        json={
            "id": "00000000-0000-0000-0000-000000000001",
            "type": "llm_generate",
            "status": "queued",
            "credits_used": 1,
            "execution_mode": "ai",
            "created_at": "2026-01-01T00:00:00Z",
        },
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        task = await c.tasks.create(
            type="llm_generate",
            input={"messages": [{"role": "user", "content": "Hello"}]},
        )
        assert task.type == "llm_generate"
        assert task.credits_used == 1


@respx.mock
@pytest.mark.asyncio
async def test_async_tasks_get():
    task_id = "00000000-0000-0000-0000-000000000001"
    respx.get(f"{BASE}/v1/tasks/{task_id}").mock(
        return_value=httpx.Response(200, json=TASK_RESPONSE)
    )
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        task = await c.tasks.get(task_id)
        assert task.status == "completed"


@respx.mock
@pytest.mark.asyncio
async def test_async_tasks_list():
    respx.get(f"{BASE}/v1/tasks").mock(return_value=httpx.Response(
        200,
        json={"items": [TASK_RESPONSE], "total": 1, "limit": 20, "offset": 0},
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        result = await c.tasks.list(status="completed")
        assert len(result.items) == 1
        assert result.total == 1


@respx.mock
@pytest.mark.asyncio
async def test_async_tasks_cancel():
    task_id = "00000000-0000-0000-0000-000000000001"
    respx.delete(f"{BASE}/v1/tasks/{task_id}").mock(
        return_value=httpx.Response(204)
    )
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        await c.tasks.cancel(task_id)


@respx.mock
@pytest.mark.asyncio
async def test_async_tasks_batch():
    respx.post(f"{BASE}/v1/tasks/batch").mock(return_value=httpx.Response(
        200,
        json={
            "created": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "type": "llm_generate",
                    "status": "queued",
                    "credits_used": 1,
                    "execution_mode": "ai",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ],
            "errors": [],
            "summary": {"total": 1, "created": 1, "failed": 0},
        },
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        result = await c.tasks.create_batch([
            {"type": "llm_generate", "input": {"messages": [{"role": "user", "content": "Hi"}]}},
        ])
        assert len(result.created) == 1
        assert result.summary["created"] == 1


# ─── Convenience helpers ─────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_async_web_research():
    respx.post(f"{BASE}/v1/tasks").mock(return_value=httpx.Response(
        200,
        json={
            "id": "00000000-0000-0000-0000-000000000002",
            "type": "web_research",
            "status": "queued",
            "credits_used": 10,
            "execution_mode": "ai",
            "created_at": "2026-01-01T00:00:00Z",
        },
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        task = await c.tasks.web_research("https://example.com", "Summarise")
        assert task.type == "web_research"


@respx.mock
@pytest.mark.asyncio
async def test_async_pii_detect():
    respx.post(f"{BASE}/v1/tasks").mock(return_value=httpx.Response(
        200,
        json={
            "id": "00000000-0000-0000-0000-000000000003",
            "type": "pii_detect",
            "status": "queued",
            "credits_used": 2,
            "execution_mode": "ai",
            "created_at": "2026-01-01T00:00:00Z",
        },
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        task = await c.tasks.pii_detect("John Doe lives at 123 Main St")
        assert task.type == "pii_detect"


# ─── Error handling ──────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_async_401_raises_auth_error():
    respx.get(f"{BASE}/v1/users/me").mock(
        return_value=httpx.Response(401, json={"detail": "Unauthorized"})
    )
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE, max_retries=1) as c:
        with pytest.raises(AuthError):
            await c.users.me()


@respx.mock
@pytest.mark.asyncio
async def test_async_404_raises_not_found():
    respx.get(f"{BASE}/v1/tasks/nonexistent").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE, max_retries=1) as c:
        with pytest.raises(NotFoundError):
            await c.tasks.get("nonexistent")


@respx.mock
@pytest.mark.asyncio
async def test_async_402_raises_insufficient_credits():
    respx.post(f"{BASE}/v1/tasks").mock(
        return_value=httpx.Response(402, json={"detail": "Insufficient credits"})
    )
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE, max_retries=1) as c:
        with pytest.raises(InsufficientCreditsError):
            await c.tasks.create("web_research", {"url": "https://example.com"})


@respx.mock
@pytest.mark.asyncio
async def test_async_429_raises_rate_limit():
    respx.get(f"{BASE}/v1/credits/balance").mock(
        return_value=httpx.Response(
            429,
            headers={"retry-after": "15"},
            json={"detail": "Too many requests"},
        )
    )
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE, max_retries=1) as c:
        with pytest.raises(RateLimitError) as exc:
            await c.credits.balance()
        assert exc.value.retry_after == 15.0


@respx.mock
@pytest.mark.asyncio
async def test_async_500_raises_generic_error():
    respx.get(f"{BASE}/v1/users/me").mock(
        return_value=httpx.Response(500, json={"detail": "Server error"})
    )
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE, max_retries=1) as c:
        with pytest.raises(CrowdSorcererError):
            await c.users.me()


# ─── Credits ─────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_async_credits_balance():
    respx.get(f"{BASE}/v1/credits/balance").mock(return_value=httpx.Response(
        200,
        json={"available": 800, "total_used": 200, "total_purchased": 1000},
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        bal = await c.credits.balance()
        assert bal.available == 800
        assert bal.total_purchased == 1000


# ─── Users ───────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_async_users_me():
    respx.get(f"{BASE}/v1/users/me").mock(return_value=httpx.Response(
        200,
        json={
            "id": "00000000-0000-0000-0000-000000000099",
            "email": "test@example.com",
            "name": "Test User",
            "plan": "free",
            "role": "requester",
            "credits": 1000,
            "created_at": "2026-01-01T00:00:00Z",
        },
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        user = await c.users.me()
        assert user.email == "test@example.com"
        assert user.credits == 1000


# ─── API Keys ────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_async_api_keys_list():
    respx.get(f"{BASE}/v1/users/me/api-keys").mock(return_value=httpx.Response(
        200,
        json=[{
            "id": "00000000-0000-0000-0000-000000000010",
            "name": "test-key",
            "key_prefix": "cs_",
            "scopes": ["tasks:read"],
            "created_at": "2026-01-01T00:00:00Z",
        }],
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        keys = await c.api_keys.list()
        assert len(keys) == 1
        assert keys[0].name == "test-key"


@respx.mock
@pytest.mark.asyncio
async def test_async_api_keys_create():
    respx.post(f"{BASE}/v1/users/me/api-keys").mock(return_value=httpx.Response(
        200,
        json={
            "id": "00000000-0000-0000-0000-000000000011",
            "name": "new-key",
            "key": "cs_live_abc123",
            "key_prefix": "cs_live_",
            "scopes": [],
            "created_at": "2026-01-01T00:00:00Z",
        },
    ))
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        result = await c.api_keys.create("new-key")
        assert result.key == "cs_live_abc123"


@respx.mock
@pytest.mark.asyncio
async def test_async_api_keys_delete():
    key_id = "00000000-0000-0000-0000-000000000010"
    respx.delete(f"{BASE}/v1/users/me/api-keys/{key_id}").mock(
        return_value=httpx.Response(204)
    )
    async with AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
        await c.api_keys.delete(key_id)


# ─── SDK version in User-Agent ───────────────────────────────────────────


def test_async_sdk_version_header():
    """Verify the X-Client header uses version 1.0.0."""
    c = AsyncCrowdSorcerer(api_key="cs_test", base_url=BASE)
    headers = c._http.headers
    assert "crowdsourcerer-python/1.0.0" in headers.get("x-client", "")
