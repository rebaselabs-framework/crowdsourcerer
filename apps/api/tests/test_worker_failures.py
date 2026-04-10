"""Tests for RebaseKit worker failure handling — the most critical untested path.

Covers:
  - RebaseKitClient.post() error handling: HTTP errors, timeouts, connection errors, missing API key
  - _run_task() WorkerError catch block: task status, credit refund, notifications, webhooks
  - _run_task() unexpected exception catch block: same checks
  - _run_task() guards: non-queued tasks skipped, missing tasks skipped
  - Credit refund routing (org vs user)
  - execute_task() failure propagation through all 10 task types
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("REBASEKIT_API_KEY", "test-key")
os.environ.setdefault("REBASEKIT_BASE_URL", "https://api.test.local")

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from workers.base import RebaseKitClient, WorkerError


# Disable retries and ensure API key is set for all tests by default.
# Retry-specific tests override MAX_RETRIES explicitly.
@pytest.fixture(autouse=True)
def no_retries_and_valid_key():
    with (
        patch("workers.base.MAX_RETRIES", 0),
        patch("workers.base.settings") as mock_settings,
    ):
        mock_settings.rebasekit_api_key = "test-key"
        mock_settings.rebasekit_base_url = "https://api.test.local"
        yield


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _make_mock_task(
    task_id=None, user_id=None, status="queued", task_type="web_research",
    org_id=None, webhook_url=None,
):
    """Create a mock TaskDB for _run_task tests."""
    t = MagicMock()
    t.id = task_id or str(uuid4())
    t.user_id = user_id or str(uuid4())
    t.type = task_type
    t.status = status
    t.execution_mode = "ai"
    t.input = {"url": "https://example.com"}
    t.output = None
    t.error = None
    t.started_at = None
    t.completed_at = None
    t.duration_ms = None
    t.credits_used = None
    t.cached = False
    t.webhook_url = webhook_url
    t.webhook_events = []
    t.org_id = org_id
    t.priority = "normal"
    return t


def _make_mock_user(user_id=None, credits=1000):
    """Create a mock UserDB."""
    u = MagicMock()
    u.id = user_id or str(uuid4())
    u.email = "test@example.com"
    u.credits = credits
    u.name = "Test User"
    return u


def _scalar_result(value):
    """Wrap value for SQLAlchemy scalar_one_or_none()."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _make_run_task_mocks(task, user, execute_side_effect):
    """Build the mock DB and patch context for _run_task tests.

    Returns (mock_db, patches_context_manager, named_mocks).

    The mock DB simulates the execute call sequence:
      1. select(TaskDB) FOR UPDATE → task  (main try)
      2. select(TaskDB) → task  (error handler re-fetch)
      3. select(UserDB/OrgDB) FOR UPDATE → user  (_refund_task_credits)
      4. select(UserDB) → user  (for email notification)
    """
    mock_db = AsyncMock()
    call_count = [0]

    async def mock_execute(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:
            return _scalar_result(task)
        return _scalar_result(user)

    mock_db.execute = AsyncMock(side_effect=mock_execute)
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

    named_mocks = {
        "create_notification": AsyncMock(),
        "safe_create_task": MagicMock(),
        "notify_task_failed": AsyncMock(),
    }

    @contextmanager
    def patches():
        with (
            # Lazy imports — patch at source module
            patch("core.database.AsyncSessionLocal", return_value=mock_session_ctx),
            patch("core.result_cache.cache_lookup", AsyncMock(return_value=None)),
            patch("core.notify.create_notification", named_mocks["create_notification"]),
            patch("core.email.notify_task_failed", named_mocks["notify_task_failed"]),
            patch("core.email.notify_task_completed", AsyncMock()),
            # Module-level imports — patch in routers.tasks namespace
            patch("routers.tasks.execute_task", AsyncMock(side_effect=execute_side_effect)),
            patch("routers.tasks.get_rebasekit_client", return_value=MagicMock()),
            patch("routers.tasks.safe_create_task", named_mocks["safe_create_task"]),
            patch("routers.tasks.fire_webhook_for_task", AsyncMock()),
            patch("routers.tasks.fire_persistent_endpoints", AsyncMock()),
        ):
            yield

    return mock_db, patches, named_mocks


# ═══════════════════════════════════════════════════════════════════════════════
# 1. RebaseKitClient.post() FAILURE HANDLING
# ═══════════════════════════════════════════════════════════════════════════════


class TestRebaseKitClientPost:
    """Tests for RebaseKitClient.post() — the thin HTTP wrapper that
    converts httpx errors into WorkerError exceptions."""

    @pytest.mark.asyncio
    async def test_successful_post(self):
        """Happy path: RebaseKit returns 200 with JSON."""
        client = RebaseKitClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": "ok"}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.post("/test/api/health", {"key": "value"})
        assert result == {"result": "ok"}
        mock_http.post.assert_called_once_with("/test/api/health", json={"key": "value"})

    @pytest.mark.asyncio
    async def test_missing_api_key_raises_503(self):
        """When REBASEKIT_API_KEY is empty, post() raises WorkerError(503) immediately."""
        client = RebaseKitClient()
        with patch("workers.base.settings") as mock_settings:
            mock_settings.rebasekit_api_key = ""
            with pytest.raises(WorkerError) as exc_info:
                await client.post("/test", {"data": "test"})
            assert exc_info.value.status_code == 503
            assert "API key not configured" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_missing_api_key_none_raises_503(self):
        """When REBASEKIT_API_KEY is None, post() raises WorkerError(503)."""
        client = RebaseKitClient()
        with patch("workers.base.settings") as mock_settings:
            mock_settings.rebasekit_api_key = None
            with pytest.raises(WorkerError) as exc_info:
                await client.post("/test", {"data": "test"})
            assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_http_503_raises_worker_error_502(self):
        """When RebaseKit returns 503, post() raises WorkerError(502)."""
        client = RebaseKitClient()
        response = httpx.Response(503, text="Service Unavailable")
        error = httpx.HTTPStatusError("503", request=MagicMock(), response=response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=error)
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/webtask/api/task", {"url": "https://example.com"})
        assert exc_info.value.status_code == 502
        assert "503" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_http_500_raises_worker_error_502(self):
        """When RebaseKit returns 500, post() raises WorkerError(502) with details."""
        client = RebaseKitClient()
        response = httpx.Response(500, text="Internal Server Error")
        error = httpx.HTTPStatusError("500", request=MagicMock(), response=response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=error)
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/llm/v1/chat/completions", {"messages": []})
        assert exc_info.value.status_code == 502
        assert exc_info.value.details == "Internal Server Error"

    @pytest.mark.asyncio
    async def test_http_429_raises_worker_error_502(self):
        """Rate limiting (429) from RebaseKit is converted to WorkerError(502)."""
        client = RebaseKitClient()
        response = httpx.Response(429, text="Too Many Requests")
        error = httpx.HTTPStatusError("429", request=MagicMock(), response=response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=error)
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/pii/api/detect", {"text": "test"})
        assert exc_info.value.status_code == 502
        assert "429" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_timeout_raises_worker_error_504(self):
        """When RebaseKit times out, post() raises WorkerError(504)."""
        client = RebaseKitClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.TimeoutException("read timeout"))
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/webtask/api/task", {"url": "https://slow.example.com"})
        assert exc_info.value.status_code == 504
        assert "timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_connect_error_raises_worker_error_502(self):
        """When RebaseKit is unreachable, post() raises WorkerError(502)."""
        client = RebaseKitClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/enrich/api/enrich/company", {"company": "test"})
        assert exc_info.value.status_code == 502
        assert "ConnectError" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_local_protocol_error_raises_worker_error_502(self):
        """LocalProtocolError (e.g., bad header) raises WorkerError(502)."""
        client = RebaseKitClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx.LocalProtocolError("Illegal header value")
        )
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/test", {"data": "test"})
        assert exc_info.value.status_code == 502
        assert "LocalProtocolError" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_remote_protocol_error_raises_worker_error_502(self):
        """RemoteProtocolError raises WorkerError(502) with class name."""
        client = RebaseKitClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx.RemoteProtocolError("malformed response")
        )
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/test", {"data": "test"})
        assert exc_info.value.status_code == 502
        assert "RemoteProtocolError" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_worker_error_preserves_details(self):
        """WorkerError.details contains the response text for HTTP errors."""
        client = RebaseKitClient()
        error_body = '{"error": "Service overloaded", "retry_after": 30}'
        response = httpx.Response(503, text=error_body)
        error = httpx.HTTPStatusError("503", request=MagicMock(), response=response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=error)
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/test", {})
        assert exc_info.value.details == error_body

    @pytest.mark.asyncio
    async def test_timeout_error_has_no_details(self):
        """Timeout WorkerError has no details (no response to extract from)."""
        client = RebaseKitClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.TimeoutException(""))
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/test", {})
        assert exc_info.value.details is None

    @pytest.mark.asyncio
    async def test_connect_error_has_no_details(self):
        """Connection error WorkerError has no details."""
        client = RebaseKitClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_http.is_closed = False
        client._client = mock_http

        with pytest.raises(WorkerError) as exc_info:
            await client.post("/test", {})
        assert exc_info.value.details is None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _run_task() ERROR HANDLING — WorkerError path
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunTaskWorkerError:
    """Tests for the _run_task() function's WorkerError catch block.

    When execute_task raises WorkerError (RebaseKit failure), _run_task must:
    1. Set task.status = "failed"
    2. Set task.error to the error message
    3. Refund credits via _refund_task_credits
    4. Create an in-app notification
    5. Fire webhook/email
    """

    @pytest.mark.asyncio
    async def test_worker_error_sets_task_failed(self):
        """WorkerError sets task.status to 'failed' and records error message."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id)
        user = _make_mock_user(user_id=user_id)
        error = WorkerError("RebaseKit API error: 503", status_code=502)

        _, patches, _ = _make_run_task_mocks(task, user, error)

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        assert task.status == "failed"
        assert task.error == "RebaseKit API error: 503"
        assert task.completed_at is not None

    @pytest.mark.asyncio
    async def test_worker_error_refunds_credits(self):
        """WorkerError triggers credit refund to the user (web_research = 10 credits)."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id, task_type="web_research")
        user = _make_mock_user(user_id=user_id, credits=990)
        error = WorkerError("RebaseKit API timed out", status_code=504)

        _, patches, _ = _make_run_task_mocks(task, user, error)

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        assert user.credits == 1000  # 990 + 10 refund

    @pytest.mark.asyncio
    async def test_worker_error_creates_notification(self):
        """WorkerError creates an in-app notification mentioning refund."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id, task_type="llm_generate")
        user = _make_mock_user(user_id=user_id)
        error = WorkerError("RebaseKit connection error: ConnectError", status_code=502)

        _, patches, mocks = _make_run_task_mocks(task, user, error)

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        assert mocks["create_notification"].call_count >= 1
        # Find the failure notification call
        for c in mocks["create_notification"].call_args_list:
            body = str(c)
            if "failed" in body.lower():
                assert "refunded" in body.lower() or "credits" in body.lower()
                break

    @pytest.mark.asyncio
    async def test_worker_error_fires_webhook(self):
        """WorkerError fires task.failed webhook when webhook_url is set."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(
            task_id=task_id, user_id=user_id,
            webhook_url="https://hooks.example.com/callback",
        )
        user = _make_mock_user(user_id=user_id)
        error = WorkerError("RebaseKit API error: 503", status_code=502)

        _, patches, mocks = _make_run_task_mocks(task, user, error)

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        # safe_create_task called for: webhook + persistent endpoints + email = 3+
        assert mocks["safe_create_task"].call_count >= 2

    @pytest.mark.asyncio
    async def test_worker_error_sends_email(self):
        """WorkerError triggers email notification via safe_create_task."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id)
        user = _make_mock_user(user_id=user_id)
        error = WorkerError("RebaseKit API error: 503", status_code=502)

        _, patches, mocks = _make_run_task_mocks(task, user, error)

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        # Email is fired via safe_create_task(notify_task_failed(...))
        assert mocks["safe_create_task"].call_count >= 1

    @pytest.mark.asyncio
    async def test_timeout_error_refunds_correct_amount(self):
        """Timeout on audio_transcribe refunds 8 credits."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id, task_type="audio_transcribe")
        user = _make_mock_user(user_id=user_id, credits=92)
        error = WorkerError("RebaseKit API timed out", status_code=504)

        _, patches, _ = _make_run_task_mocks(task, user, error)

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        assert user.credits == 100  # 92 + 8 refund

    @pytest.mark.asyncio
    async def test_connect_error_refunds_correct_amount(self):
        """Connection error on llm_generate refunds 1 credit."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id, task_type="llm_generate")
        user = _make_mock_user(user_id=user_id, credits=99)
        error = WorkerError("RebaseKit connection error: ConnectError", status_code=502)

        _, patches, _ = _make_run_task_mocks(task, user, error)

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        assert user.credits == 100  # 99 + 1 refund


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _run_task() ERROR HANDLING — Unexpected exception path
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunTaskUnexpectedError:
    """Tests for the _run_task() catch-all Exception handler."""

    @pytest.mark.asyncio
    async def test_unexpected_error_sets_task_failed(self):
        """Non-WorkerError exception sets task.status='failed'."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id)
        user = _make_mock_user(user_id=user_id)

        _, patches, _ = _make_run_task_mocks(task, user, RuntimeError("unexpected crash"))

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        assert task.status == "failed"
        assert "RuntimeError" in task.error

    @pytest.mark.asyncio
    async def test_unexpected_error_refunds_credits(self):
        """Non-WorkerError exception refunds credits (pii_detect = 2)."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id, task_type="pii_detect")
        user = _make_mock_user(user_id=user_id, credits=98)

        _, patches, _ = _make_run_task_mocks(task, user, KeyError("missing field"))

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        assert user.credits == 100  # 98 + 2

    @pytest.mark.asyncio
    async def test_unexpected_error_includes_class_name(self):
        """Error message includes the exception class name."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        task = _make_mock_task(task_id=task_id, user_id=user_id)
        user = _make_mock_user(user_id=user_id)

        _, patches, _ = _make_run_task_mocks(task, user, ValueError("bad value"))

        with patches():
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        assert "ValueError" in task.error
        assert "Unexpected error" in task.error


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _run_task() GUARD CONDITIONS
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunTaskGuards:
    """Tests for _run_task() guard conditions — skipping non-queued/missing tasks."""

    def _make_guard_patches(self, mock_session_ctx):
        """Minimal patches for guard tests (no execute_task needed)."""
        @contextmanager
        def patches():
            with (
                patch("core.database.AsyncSessionLocal", return_value=mock_session_ctx),
            ):
                yield
        return patches

    @pytest.mark.asyncio
    async def test_missing_task_silently_returns(self):
        """If task_id doesn't exist, _run_task returns without error."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=_scalar_result(None))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

        with self._make_guard_patches(mock_session_ctx)():
            from routers.tasks import _run_task
            await _run_task(str(uuid4()), str(uuid4()))

    @pytest.mark.asyncio
    async def test_non_queued_task_skipped(self):
        """'running' task is not re-executed."""
        task = _make_mock_task(status="running")

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=_scalar_result(task))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_execute = AsyncMock()
        with self._make_guard_patches(mock_session_ctx)():
            with patch("routers.tasks.execute_task", mock_execute):
                from routers.tasks import _run_task
                await _run_task(str(task.id), str(task.user_id))

        mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_task_not_reexecuted(self):
        """'completed' task is not re-executed."""
        task = _make_mock_task(status="completed")

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=_scalar_result(task))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_execute = AsyncMock()
        with self._make_guard_patches(mock_session_ctx)():
            with patch("routers.tasks.execute_task", mock_execute):
                from routers.tasks import _run_task
                await _run_task(str(task.id), str(task.user_id))

        mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_task_not_reexecuted(self):
        """'failed' task is not re-executed."""
        task = _make_mock_task(status="failed")

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=_scalar_result(task))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_execute = AsyncMock()
        with self._make_guard_patches(mock_session_ctx)():
            with patch("routers.tasks.execute_task", mock_execute):
                from routers.tasks import _run_task
                await _run_task(str(task.id), str(task.user_id))

        mock_execute.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CREDIT REFUND ROUTING — org vs user
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkerErrorCreditRouting:
    """Test that credit refunds on worker failure go to the right target."""

    @pytest.mark.asyncio
    async def test_org_scoped_task_refunds_to_org(self):
        """When a task has org_id, credits are refunded to the organization."""
        task_id = str(uuid4())
        user_id = str(uuid4())
        org_id = str(uuid4())
        task = _make_mock_task(
            task_id=task_id, user_id=user_id, org_id=org_id, task_type="screenshot",
        )
        org = MagicMock()
        org.id = org_id
        org.credits = 490
        user = _make_mock_user(user_id=user_id)

        mock_db = AsyncMock()
        call_count = [0]

        async def mock_execute(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                return _scalar_result(task)
            if call_count[0] == 3:
                return _scalar_result(org)  # _refund_task_credits finds org
            return _scalar_result(user)

        mock_db.execute = AsyncMock(side_effect=mock_execute)
        mock_db.commit = AsyncMock()
        mock_db.add = MagicMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

        error = WorkerError("RebaseKit API error: 503", status_code=502)

        with (
            patch("core.database.AsyncSessionLocal", return_value=mock_session_ctx),
            patch("core.result_cache.cache_lookup", AsyncMock(return_value=None)),
            patch("core.notify.create_notification", AsyncMock()),
            patch("core.email.notify_task_failed", AsyncMock()),
            patch("core.email.notify_task_completed", AsyncMock()),
            patch("routers.tasks.execute_task", AsyncMock(side_effect=error)),
            patch("routers.tasks.get_rebasekit_client", return_value=MagicMock()),
            patch("routers.tasks.safe_create_task", MagicMock()),
            patch("routers.tasks.fire_webhook_for_task", AsyncMock()),
            patch("routers.tasks.fire_persistent_endpoints", AsyncMock()),
        ):
            from routers.tasks import _run_task
            await _run_task(task_id, user_id)

        # screenshot costs 2 credits — org should get refund
        assert org.credits == 492  # 490 + 2


# ═══════════════════════════════════════════════════════════════════════════════
# 6. WORKER ERROR ATTRIBUTES
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkerErrorClass:
    """Tests for the WorkerError exception class itself."""

    def test_default_status_code_is_500(self):
        err = WorkerError("something broke")
        assert err.status_code == 500
        assert err.details is None

    def test_custom_status_code(self):
        err = WorkerError("not found", status_code=404)
        assert err.status_code == 404

    def test_with_details(self):
        err = WorkerError("bad request", status_code=400, details={"field": "url"})
        assert err.details == {"field": "url"}

    def test_message_is_string(self):
        err = WorkerError("RebaseKit API error: 503")
        assert str(err) == "RebaseKit API error: 503"

    def test_inherits_from_exception(self):
        err = WorkerError("test")
        assert isinstance(err, Exception)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. EACH TASK TYPE FAILURE — credit amounts
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailureRefundAmounts:
    """Verify correct refund amounts for each AI task type when worker fails."""

    @pytest.mark.parametrize("task_type,expected_cost", [
        ("web_research", 10),
        ("entity_lookup", 5),
        ("document_parse", 3),
        ("data_transform", 2),
        ("llm_generate", 1),
        ("screenshot", 2),
        ("audio_transcribe", 8),
        ("pii_detect", 2),
        ("code_execute", 3),
        ("web_intel", 5),
    ])
    def test_refund_matches_task_cost(self, task_type, expected_cost):
        """Each task type's refund amount matches its credit cost."""
        from routers.tasks import _compute_task_cost
        task = _make_mock_task(task_type=task_type)
        assert _compute_task_cost(task) == expected_cost


# ═══════════════════════════════════════════════════════════════════════════════
# 8. execute_task() ROUTING + FAILURE PROPAGATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecuteTaskFailurePropagation:
    """Test that WorkerErrors from RebaseKitClient propagate through execute_task."""

    @pytest.mark.asyncio
    async def test_web_research_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("RebaseKit API error: 503", status_code=502))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("web_research", {"url": "https://example.com"}, client)
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_llm_generate_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("RebaseKit API timed out", status_code=504))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("llm_generate", {"messages": [{"role": "user", "content": "hi"}]}, client)
        assert exc_info.value.status_code == 504

    @pytest.mark.asyncio
    async def test_pii_detect_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("ConnectError", status_code=502))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("pii_detect", {"text": "My SSN is 123-45-6789"}, client)
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_screenshot_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("500", status_code=502))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("screenshot", {"url": "https://example.com"}, client)
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_entity_lookup_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("503", status_code=502))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("entity_lookup", {"name": "Acme Inc"}, client)
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_audio_transcribe_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("timed out", status_code=504))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("audio_transcribe", {"url": "https://audio.example.com/file.mp3"}, client)
        assert exc_info.value.status_code == 504

    @pytest.mark.asyncio
    async def test_code_execute_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("ConnectError", status_code=502))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("code_execute", {"code": "print('hello')"}, client)
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_data_transform_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("503", status_code=502))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("data_transform", {"data": '{"key": "value"}'}, client)
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_document_parse_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("502", status_code=502))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("document_parse", {"url": "https://example.com/doc.pdf"}, client)
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_web_intel_propagates_worker_error(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock(side_effect=WorkerError("timed out", status_code=504))
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("web_intel", {"query": "latest AI news"}, client)
        assert exc_info.value.status_code == 504

    @pytest.mark.asyncio
    async def test_missing_required_field_raises_422(self):
        """Missing required fields raise WorkerError(422) before network call."""
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock()
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("web_research", {}, client)
        assert exc_info.value.status_code == 422
        assert "url" in str(exc_info.value).lower()
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_pii_detect_missing_text_raises_422(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock()
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("pii_detect", {}, client)
        assert exc_info.value.status_code == 422
        assert "text" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_code_execute_missing_code_raises_422(self):
        from workers.router import execute_task
        client = MagicMock()
        client.post = AsyncMock()
        with pytest.raises(WorkerError) as exc_info:
            await execute_task("code_execute", {}, client)
        assert exc_info.value.status_code == 422
        assert "code" in str(exc_info.value).lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 9. RETRY WITH BACKOFF
# ═══════════════════════════════════════════════════════════════════════════════


class TestRebaseKitClientRetry:
    """Tests for the automatic retry logic in RebaseKitClient.post()."""

    @pytest.mark.asyncio
    async def test_retry_on_503_then_success(self):
        """503 triggers retry; success on second attempt returns the result."""
        client = RebaseKitClient()

        ok_response = MagicMock()
        ok_response.json.return_value = {"result": "ok"}
        ok_response.raise_for_status = MagicMock()

        err_response = httpx.Response(503, text="Service Unavailable")
        err = httpx.HTTPStatusError("503", request=MagicMock(), response=err_response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=[err, ok_response])
        mock_http.is_closed = False
        client._client = mock_http

        # Override autouse fixture's MAX_RETRIES=0 to enable retries
        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", AsyncMock()),
        ):
            result = await client.post("/test", {"key": "value"})

        assert result == {"result": "ok"}
        assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_timeout_then_success(self):
        """Timeout triggers retry; success on second attempt returns the result."""
        client = RebaseKitClient()

        ok_response = MagicMock()
        ok_response.json.return_value = {"data": "here"}
        ok_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=[httpx.TimeoutException("read timeout"), ok_response]
        )
        mock_http.is_closed = False
        client._client = mock_http

        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", AsyncMock()),
        ):
            result = await client.post("/test", {})

        assert result == {"data": "here"}
        assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_connect_error_then_success(self):
        """Connection error triggers retry; success on retry."""
        client = RebaseKitClient()

        ok_response = MagicMock()
        ok_response.json.return_value = {"ok": True}
        ok_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=[httpx.ConnectError("refused"), ok_response]
        )
        mock_http.is_closed = False
        client._client = mock_http

        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", AsyncMock()),
        ):
            result = await client.post("/test", {})

        assert result == {"ok": True}
        assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises_worker_error(self):
        """After all retries are exhausted, WorkerError is raised."""
        client = RebaseKitClient()

        err_response = httpx.Response(503, text="Still down")
        err = httpx.HTTPStatusError("503", request=MagicMock(), response=err_response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=err)
        mock_http.is_closed = False
        client._client = mock_http

        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", AsyncMock()),
        ):
            with pytest.raises(WorkerError) as exc_info:
                await client.post("/test", {})

        assert exc_info.value.status_code == 502
        assert "503" in str(exc_info.value)
        assert mock_http.post.call_count == 3  # 1 original + 2 retries

    @pytest.mark.asyncio
    async def test_no_retry_on_400(self):
        """400 errors (client errors) are NOT retried."""
        client = RebaseKitClient()

        err_response = httpx.Response(400, text="Bad Request")
        err = httpx.HTTPStatusError("400", request=MagicMock(), response=err_response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=err)
        mock_http.is_closed = False
        client._client = mock_http

        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", AsyncMock()),
        ):
            with pytest.raises(WorkerError) as exc_info:
                await client.post("/test", {})

        assert "400" in str(exc_info.value)
        assert mock_http.post.call_count == 1  # No retries for 400

    @pytest.mark.asyncio
    async def test_no_retry_on_404(self):
        """404 errors are NOT retried."""
        client = RebaseKitClient()

        err_response = httpx.Response(404, text="Not Found")
        err = httpx.HTTPStatusError("404", request=MagicMock(), response=err_response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=err)
        mock_http.is_closed = False
        client._client = mock_http

        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", AsyncMock()),
        ):
            with pytest.raises(WorkerError):
                await client.post("/test", {})

        assert mock_http.post.call_count == 1  # No retries for 404

    @pytest.mark.asyncio
    async def test_no_retry_on_500(self):
        """500 errors (unexpected server errors) are NOT retried — only 502/503/504/429."""
        client = RebaseKitClient()

        err_response = httpx.Response(500, text="Internal Server Error")
        err = httpx.HTTPStatusError("500", request=MagicMock(), response=err_response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=err)
        mock_http.is_closed = False
        client._client = mock_http

        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", AsyncMock()),
        ):
            with pytest.raises(WorkerError):
                await client.post("/test", {})

        assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_429_rate_limit(self):
        """429 (rate limiting) triggers retry."""
        client = RebaseKitClient()

        ok_response = MagicMock()
        ok_response.json.return_value = {"result": "ok"}
        ok_response.raise_for_status = MagicMock()

        err_response = httpx.Response(429, text="Too Many Requests")
        err = httpx.HTTPStatusError("429", request=MagicMock(), response=err_response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=[err, ok_response])
        mock_http.is_closed = False
        client._client = mock_http

        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", AsyncMock()),
        ):
            result = await client.post("/test", {})

        assert result == {"result": "ok"}
        assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self):
        """Retry delays follow exponential backoff: 1s, 2s."""
        client = RebaseKitClient()

        err_response = httpx.Response(503, text="Down")
        err = httpx.HTTPStatusError("503", request=MagicMock(), response=err_response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=err)
        mock_http.is_closed = False
        client._client = mock_http

        mock_sleep = AsyncMock()

        with (
            patch("workers.base.MAX_RETRIES", 2),
            patch("workers.base.asyncio.sleep", mock_sleep),
        ):
            with pytest.raises(WorkerError):
                await client.post("/test", {})

        # Two retries → two sleeps: 1.0s (2^0), 2.0s (2^1)
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0].args[0] == 1.0
        assert mock_sleep.call_args_list[1].args[0] == 2.0

    @pytest.mark.asyncio
    async def test_backoff_capped_at_max_delay(self):
        """Backoff delay is capped at RETRY_MAX_DELAY (8 seconds)."""
        client = RebaseKitClient()

        err_response = httpx.Response(503, text="Down")
        err = httpx.HTTPStatusError("503", request=MagicMock(), response=err_response)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=err)
        mock_http.is_closed = False
        client._client = mock_http

        mock_sleep = AsyncMock()

        with (
            patch("workers.base.MAX_RETRIES", 5),
            patch("workers.base.asyncio.sleep", mock_sleep),
        ):
            with pytest.raises(WorkerError):
                await client.post("/test", {})

        # Delays: 1, 2, 4, 8, 8 (capped)
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]
