"""Synchronous CrowdSorcerer API client."""
from __future__ import annotations

import time
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
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    BatchTaskCreateRequest,
    BatchTaskCreateResponse,
    CreditBalance,
    CreditTransaction,
    PaginatedTasks,
    Task,
    TaskCreateRequest,
    TaskCreateResponse,
    User,
)

DEFAULT_BASE_URL = "https://crowdsourcerer.rebaselabs.online"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
SDK_VERSION = "1.0.0"


class CrowdSorcerer:
    """Synchronous client for the CrowdSorcerer API.

    Example::

        from crowdsourcerer import CrowdSorcerer

        client = CrowdSorcerer(api_key="cs_live_...")

        task = client.tasks.create(
            type="web_research",
            input={"url": "https://example.com", "instruction": "Summarise this page"},
        )
        result = client.tasks.wait(task.id)
        print(result.output)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        if not api_key:
            raise AuthError("api_key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._http = http_client or httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client": f"crowdsourcerer-python/{SDK_VERSION}",
            },
            timeout=timeout,
        )
        self.tasks = _TasksResource(self)
        self.credits = _CreditsResource(self)
        self.users = _UsersResource(self)
        self.api_keys = _ApiKeysResource(self)
        self.marketplace = _MarketplaceResource(self)
        self.worker = _WorkerResource(self)
        self.webhooks = _WebhooksResource(self)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Make an authenticated HTTP request with retries."""
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                resp = self._http.request(method, path, **kwargs)
                return self._handle_response(resp)
            except httpx.TimeoutException as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
            except RateLimitError as e:
                last_exc = e
                retry_after = e.retry_after or (2 ** attempt)
                if attempt < self._max_retries - 1:
                    time.sleep(retry_after)
            except (AuthError, NotFoundError, InsufficientCreditsError, TaskError):
                raise  # Don't retry client errors
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

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ─── Resource classes ─────────────────────────────────────────────────────

class _TasksResource:
    def __init__(self, client: CrowdSorcerer) -> None:
        self._c = client

    def create(
        self,
        type: str,
        input: Dict[str, Any],
        priority: str = "normal",
        webhook_url: Optional[str] = None,
        worker_reward_credits: Optional[int] = None,
        assignments_required: Optional[int] = None,
        task_instructions: Optional[str] = None,
        claim_timeout_minutes: Optional[int] = None,
    ) -> TaskCreateResponse:
        """Create a new task."""
        payload: Dict[str, Any] = {"type": type, "input": input, "priority": priority}
        if webhook_url:
            payload["webhook_url"] = webhook_url
        if worker_reward_credits is not None:
            payload["worker_reward_credits"] = worker_reward_credits
        if assignments_required is not None:
            payload["assignments_required"] = assignments_required
        if task_instructions:
            payload["task_instructions"] = task_instructions
        if claim_timeout_minutes is not None:
            payload["claim_timeout_minutes"] = claim_timeout_minutes
        data = self._c._request("POST", "/v1/tasks", json=payload)
        return TaskCreateResponse(**data)

    def create_batch(
        self,
        tasks: List[Dict[str, Any]],
    ) -> BatchTaskCreateResponse:
        """Create up to 50 tasks in a single atomic request."""
        data = self._c._request("POST", "/v1/tasks/batch", json={"tasks": tasks})
        return BatchTaskCreateResponse(**data)

    def get(self, task_id: Union[str, UUID]) -> Task:
        """Get a task by ID."""
        data = self._c._request("GET", f"/v1/tasks/{task_id}")
        return Task(**data)

    def list(
        self,
        limit: int = 20,
        offset: int = 0,
        status: Optional[str] = None,
        type: Optional[str] = None,
    ) -> PaginatedTasks:
        """List tasks with optional filters."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if type:
            params["type"] = type
        data = self._c._request("GET", "/v1/tasks", params=params)
        return PaginatedTasks(**data)

    def wait(
        self,
        task_id: Union[str, UUID],
        poll_interval: float = 2.0,
        timeout: float = 300.0,
    ) -> Task:
        """Poll until the task reaches a terminal state (completed or failed).

        Args:
            task_id: Task ID to wait for.
            poll_interval: Seconds between polls (default 2s).
            timeout: Maximum total wait time in seconds (default 300s).

        Returns:
            The completed Task object.

        Raises:
            TaskError: If the task fails or times out.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.get(task_id)
            if task.status in ("completed", "failed", "cancelled"):
                if task.status == "failed":
                    raise TaskError(
                        task.error or "Task failed",
                        task_id=str(task.id),
                    )
                return task
            time.sleep(poll_interval)
        raise TaskError(f"Task {task_id} did not complete within {timeout}s", task_id=str(task_id))

    def cancel(self, task_id: Union[str, UUID]) -> None:
        """Cancel a task."""
        self._c._request("DELETE", f"/v1/tasks/{task_id}")

    # ─── Convenience helpers ──────────────────────────────────────────────

    def web_research(self, url: str, instruction: str, **kwargs) -> TaskCreateResponse:
        return self.create("web_research", {"url": url, "instruction": instruction}, **kwargs)

    def entity_lookup(self, entity_type: str, name: str, **kwargs) -> TaskCreateResponse:
        return self.create("entity_lookup", {"entity_type": entity_type, "name": name}, **kwargs)

    def document_parse(self, url: str, **kwargs) -> TaskCreateResponse:
        return self.create("document_parse", {"url": url}, **kwargs)

    def data_transform(self, data: Any, transform: str, **kwargs) -> TaskCreateResponse:
        return self.create("data_transform", {"data": data, "transform": transform}, **kwargs)

    def llm_generate(self, messages: List[Dict], model: str = "claude-3-5-haiku-20241022", **kwargs) -> TaskCreateResponse:
        return self.create("llm_generate", {"messages": messages, "model": model}, **kwargs)

    def screenshot(self, url: str, **kwargs) -> TaskCreateResponse:
        return self.create("screenshot", {"url": url}, **kwargs)

    def audio_transcribe(self, url: str, language: str = "en", **kwargs) -> TaskCreateResponse:
        return self.create("audio_transcribe", {"url": url, "language": language}, **kwargs)

    def pii_detect(self, text: str, mask: bool = True, **kwargs) -> TaskCreateResponse:
        return self.create("pii_detect", {"text": text, "mask": mask}, **kwargs)

    def code_execute(self, code: str, language: str = "python", **kwargs) -> TaskCreateResponse:
        return self.create("code_execute", {"code": code, "language": language}, **kwargs)

    def web_intel(self, query: str, **kwargs) -> TaskCreateResponse:
        return self.create("web_intel", {"query": query}, **kwargs)


class _CreditsResource:
    def __init__(self, client: CrowdSorcerer) -> None:
        self._c = client

    def balance(self) -> CreditBalance:
        """Get current credit balance."""
        data = self._c._request("GET", "/v1/credits/balance")
        return CreditBalance(**data)

    def transactions(self, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        """List credit transactions."""
        return self._c._request("GET", "/v1/credits/transactions", params={"limit": limit, "offset": offset})


class _UsersResource:
    def __init__(self, client: CrowdSorcerer) -> None:
        self._c = client

    def me(self) -> User:
        """Get the current authenticated user."""
        data = self._c._request("GET", "/v1/users/me")
        return User(**data)


class _ApiKeysResource:
    def __init__(self, client: CrowdSorcerer) -> None:
        self._c = client

    def list(self) -> List[ApiKey]:
        """List all API keys."""
        data = self._c._request("GET", "/v1/users/me/api-keys")
        return [ApiKey(**k) for k in data]

    def create(self, name: str, scopes: Optional[List[str]] = None) -> ApiKeyCreateResponse:
        """Create a new API key."""
        payload = {"name": name, "scopes": scopes or []}
        data = self._c._request("POST", "/v1/users/me/api-keys", json=payload)
        return ApiKeyCreateResponse(**data)

    def delete(self, key_id: Union[str, UUID]) -> None:
        """Delete an API key."""
        self._c._request("DELETE", f"/v1/users/me/api-keys/{key_id}")


class _MarketplaceResource:
    """Access the CrowdSorcerer template marketplace."""

    def __init__(self, client: CrowdSorcerer) -> None:
        self._c = client

    def list(
        self,
        task_type: Optional[str] = None,
        category: Optional[str] = None,
        execution_mode: Optional[str] = None,
        search: Optional[str] = None,
        sort: str = "featured",
        my_own: bool = False,
        page: int = 1,
        page_size: int = 24,
    ) -> Dict[str, Any]:
        """Browse the template marketplace."""
        params: Dict[str, Any] = {"page": page, "page_size": page_size, "sort": sort}
        if task_type:
            params["task_type"] = task_type
        if category:
            params["category"] = category
        if execution_mode:
            params["execution_mode"] = execution_mode
        if search:
            params["search"] = search
        if my_own:
            params["my_own"] = "true"
        return self._c._request("GET", "/v1/marketplace/templates", params=params)

    def get(self, template_id: Union[str, UUID]) -> Dict[str, Any]:
        """Get a single template."""
        return self._c._request("GET", f"/v1/marketplace/templates/{template_id}")

    def create(
        self,
        name: str,
        task_type: str,
        task_config: Optional[Dict[str, Any]] = None,
        description: Optional[str] = None,
        execution_mode: str = "ai",
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        example_input: Optional[Dict[str, Any]] = None,
        is_public: bool = True,
    ) -> Dict[str, Any]:
        """Save a task configuration as a reusable marketplace template."""
        payload = {
            "name": name,
            "task_type": task_type,
            "task_config": task_config or {},
            "description": description,
            "execution_mode": execution_mode,
            "category": category,
            "tags": tags,
            "example_input": example_input,
            "is_public": is_public,
        }
        return self._c._request("POST", "/v1/marketplace/templates", json=payload)

    def use(self, template_id: Union[str, UUID]) -> Dict[str, Any]:
        """Mark a template as used and get its config for pre-filling a task."""
        return self._c._request("POST", f"/v1/marketplace/templates/{template_id}/use")

    def rate(self, template_id: Union[str, UUID], rating: int) -> Dict[str, Any]:
        """Rate a template 1–5 stars."""
        if not (1 <= rating <= 5):
            raise ValueError("Rating must be between 1 and 5")
        return self._c._request(
            "POST",
            f"/v1/marketplace/templates/{template_id}/rate",
            json={"rating": rating},
        )

    def categories(self) -> List[Dict[str, Any]]:
        """List all template categories with counts."""
        return self._c._request("GET", "/v1/marketplace/categories")

    def quota(self) -> Dict[str, Any]:
        """Get current user's plan quota usage and limits."""
        return self._c._request("GET", "/v1/users/quota")


class _WorkerResource:
    """Worker marketplace and skill-ranked feed."""

    def __init__(self, client: CrowdSorcerer) -> None:
        self._c = client

    def list_tasks(
        self,
        task_type: Optional[str] = None,
        priority: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """Browse open human tasks (chronological)."""
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if task_type:
            params["type"] = task_type
        if priority:
            params["priority"] = priority
        return self._c._request("GET", "/v1/worker/tasks", params=params)

    def get_feed(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Get a skill-ranked personalised task feed.

        Each item includes a ``match_score`` (0.0–1.0) field.
        """
        return self._c._request(
            "GET", "/v1/worker/tasks/feed",
            params={"page": page, "page_size": page_size},
        )

    def claim(self, task_id: Union[str, UUID]) -> Dict[str, Any]:
        """Claim a human task from the marketplace."""
        return self._c._request("POST", f"/v1/worker/tasks/{task_id}/claim")

    def submit(self, task_id: Union[str, UUID], response: Any) -> Dict[str, Any]:
        """Submit your work for a claimed task."""
        return self._c._request(
            "POST", f"/v1/worker/tasks/{task_id}/submit",
            json={"response": response},
        )

    def release(self, task_id: Union[str, UUID]) -> None:
        """Release a claimed task back to the marketplace."""
        self._c._request("DELETE", f"/v1/worker/tasks/{task_id}/release")

    def my_skills(self) -> Dict[str, Any]:
        """Get the authenticated worker's skill proficiency profile."""
        return self._c._request("GET", "/v1/workers/me/skills")


class _WebhooksResource:
    """Webhook delivery logs and event type catalogue."""

    def __init__(self, client: CrowdSorcerer) -> None:
        self._c = client

    def events(self) -> Dict[str, Any]:
        """List all supported webhook event types with descriptions."""
        return self._c._request("GET", "/v1/webhooks/events")

    def stats(self) -> Dict[str, Any]:
        """Get webhook delivery stats (success rate, by event type, etc.)."""
        return self._c._request("GET", "/v1/webhooks/stats")

    def logs(
        self,
        task_id: Optional[Union[str, UUID]] = None,
        event_type: Optional[str] = None,
        success: Optional[bool] = None,
        page: int = 1,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        """List webhook delivery logs."""
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if task_id:
            params["task_id"] = str(task_id)
        if event_type:
            params["event_type"] = event_type
        if success is not None:
            params["success"] = str(success).lower()
        return self._c._request("GET", "/v1/webhooks/logs", params=params)
