"""Async CrowdSorcerer API client (httpx-based)."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

import httpx

from .errors import (
    AuthError,
    CrowdSorcererError,
    InsufficientCreditsError,
    NotFoundError,
    RateLimitError,
    TaskError,
)
from .types import (
    ApiKey,
    ApiKeyCreateResponse,
    BatchTaskCreateResponse,
    CreditBalance,
    PaginatedTasks,
    Task,
    TaskCreateResponse,
    User,
)

DEFAULT_BASE_URL = "https://crowdsourcerer.rebaselabs.online"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
SDK_VERSION = "1.0.0"


class AsyncCrowdSorcerer:
    """Async client for the CrowdSorcerer API.

    Example::

        import asyncio
        from crowdsourcerer import AsyncCrowdSorcerer

        async def main():
            async with AsyncCrowdSorcerer(api_key="csk_...") as client:
                task = await client.tasks.create(
                    type="llm_generate",
                    input={"messages": [{"role": "user", "content": "Write a haiku"}]},
                )
                result = await client.tasks.wait(task.id)
                print(result.output)

        asyncio.run(main())
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not api_key:
            raise AuthError("api_key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client": f"crowdsourcerer-python/{SDK_VERSION}",
            },
            timeout=timeout,
        )
        self.tasks = _AsyncTasksResource(self)
        self.credits = _AsyncCreditsResource(self)
        self.users = _AsyncUsersResource(self)
        self.api_keys = _AsyncApiKeysResource(self)

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._http.request(method, path, **kwargs)
                return self._handle_response(resp)
            except httpx.TimeoutException as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
            except RateLimitError as e:
                last_exc = e
                retry_after = e.retry_after or (2 ** attempt)
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(retry_after)
            except (AuthError, NotFoundError, InsufficientCreditsError, TaskError):
                raise
        if last_exc:
            raise last_exc
        raise CrowdSorcererError("Request failed after retries")

    def _handle_response(self, resp: httpx.Response) -> Any:
        request_id = resp.headers.get("x-request-id")
        if resp.status_code == 401:
            raise AuthError("Invalid API key", status_code=401, request_id=request_id)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise RateLimitError(retry_after=retry_after, status_code=429, request_id=request_id)
        if resp.status_code == 404:
            raise NotFoundError("Resource not found", status_code=404, request_id=request_id)
        if resp.status_code == 402:
            raise InsufficientCreditsError("Insufficient credits", status_code=402, request_id=request_id)
        if not resp.is_success:
            body = {}
            try:
                body = resp.json()
            except Exception:
                pass
            message = body.get("detail") or body.get("message") or f"HTTP {resp.status_code}"
            raise CrowdSorcererError(message, status_code=resp.status_code, request_id=request_id)
        if resp.status_code == 204:
            return None
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class _AsyncTasksResource:
    def __init__(self, client: AsyncCrowdSorcerer) -> None:
        self._c = client

    async def create(
        self,
        type: str,
        input: Dict[str, Any],
        priority: str = "normal",
        webhook_url: Optional[str] = None,
        **kwargs,
    ) -> TaskCreateResponse:
        payload: Dict[str, Any] = {"type": type, "input": input, "priority": priority}
        if webhook_url:
            payload["webhook_url"] = webhook_url
        payload.update(kwargs)
        data = await self._c._request("POST", "/v1/tasks", json=payload)
        return TaskCreateResponse(**data)

    async def create_batch(self, tasks: List[Dict[str, Any]]) -> BatchTaskCreateResponse:
        data = await self._c._request("POST", "/v1/tasks/batch", json={"tasks": tasks})
        return BatchTaskCreateResponse(**data)

    async def get(self, task_id: Union[str, UUID]) -> Task:
        data = await self._c._request("GET", f"/v1/tasks/{task_id}")
        return Task(**data)

    async def list(
        self,
        limit: int = 20,
        offset: int = 0,
        status: Optional[str] = None,
        type: Optional[str] = None,
    ) -> PaginatedTasks:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if type:
            params["type"] = type
        data = await self._c._request("GET", "/v1/tasks", params=params)
        return PaginatedTasks(**data)

    async def wait(
        self,
        task_id: Union[str, UUID],
        poll_interval: float = 2.0,
        timeout: float = 300.0,
    ) -> Task:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = await self.get(task_id)
            if task.status in ("completed", "failed", "cancelled"):
                if task.status == "failed":
                    raise TaskError(task.error or "Task failed", task_id=str(task.id))
                return task
            await asyncio.sleep(poll_interval)
        raise TaskError(f"Task {task_id} did not complete within {timeout}s", task_id=str(task_id))

    async def cancel(self, task_id: Union[str, UUID]) -> None:
        await self._c._request("DELETE", f"/v1/tasks/{task_id}")

    # Typed task-type helpers were dropped when AI task types became
    # pipeline-only primitives. Use ``create(type, input)`` directly.


class _AsyncCreditsResource:
    def __init__(self, client: AsyncCrowdSorcerer) -> None:
        self._c = client

    async def balance(self) -> CreditBalance:
        data = await self._c._request("GET", "/v1/credits/balance")
        return CreditBalance(**data)


class _AsyncUsersResource:
    def __init__(self, client: AsyncCrowdSorcerer) -> None:
        self._c = client

    async def me(self) -> User:
        data = await self._c._request("GET", "/v1/users/me")
        return User(**data)


class _AsyncApiKeysResource:
    def __init__(self, client: AsyncCrowdSorcerer) -> None:
        self._c = client

    async def list(self) -> List[ApiKey]:
        data = await self._c._request("GET", "/v1/users/me/api-keys")
        return [ApiKey(**k) for k in data]

    async def create(self, name: str, scopes: Optional[List[str]] = None) -> ApiKeyCreateResponse:
        payload = {"name": name, "scopes": scopes or []}
        data = await self._c._request("POST", "/v1/users/me/api-keys", json=payload)
        return ApiKeyCreateResponse(**data)

    async def delete(self, key_id: Union[str, UUID]) -> None:
        await self._c._request("DELETE", f"/v1/users/me/api-keys/{key_id}")
