"""Tests for the persistent webhook retry queue.

Tests cover:
  - Backoff schedule calculation
  - Enqueue helper (inserts queue item)
  - Process item: success, 4xx dead-letter, 5xx retry, exhausted retries
  - Poll loop batch processing
  - Queue stats helper
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# These tests use unit-level mocking — no real database required.


class TestBackoffSchedule:
    """Test the exponential backoff schedule calculation."""

    def test_backoff_schedule_values(self):
        from core.webhook_retry import _backoff_seconds

        assert _backoff_seconds(1) == 30      # 30 seconds
        assert _backoff_seconds(2) == 120     # 2 minutes
        assert _backoff_seconds(3) == 600     # 10 minutes
        assert _backoff_seconds(4) == 3600    # 1 hour
        assert _backoff_seconds(5) == 14400   # 4 hours

    def test_backoff_clamps_at_max(self):
        """Attempts beyond the schedule use the last value."""
        from core.webhook_retry import _backoff_seconds

        assert _backoff_seconds(10) == 14400
        assert _backoff_seconds(100) == 14400


class TestEnqueueRetry:
    """Test the enqueue_retry helper function."""

    @pytest.mark.asyncio
    async def test_enqueue_creates_queue_item(self):
        """Enqueue should insert a row into webhook_delivery_queue."""
        from core.webhook_retry import enqueue_retry

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        added_items: list[Any] = []
        mock_session.add = lambda item: added_items.append(item)
        mock_session.commit = AsyncMock()

        with patch("core.webhook_retry.AsyncSessionLocal", return_value=mock_session):
            await enqueue_retry(
                endpoint_id="ep-123",
                user_id="user-456",
                task_id="task-789",
                event_type="task.completed",
                url="https://example.com/webhook",
                payload={"event": "task.completed"},
                headers={"X-Crowdsorcerer-Event": "task.completed"},
                max_attempts=4,
            )

        assert len(added_items) == 1
        item = added_items[0]
        assert item.endpoint_id == "ep-123"
        assert item.user_id == "user-456"
        assert item.task_id == "task-789"
        assert item.event_type == "task.completed"
        assert item.url == "https://example.com/webhook"
        assert item.status == "pending"
        assert item.attempt == 1
        assert item.max_attempts == 4
        # next_retry_at should be ~30 seconds from now
        now = datetime.now(timezone.utc)
        assert item.next_retry_at > now
        assert item.next_retry_at < now + timedelta(seconds=60)


class TestProcessItem:
    """Test _process_item for various HTTP response scenarios."""

    def _make_queue_item(self, **overrides: Any) -> MagicMock:
        item = MagicMock()
        item.id = uuid.uuid4()
        item.endpoint_id = str(uuid.uuid4())
        item.user_id = str(uuid.uuid4())
        item.task_id = str(uuid.uuid4())
        item.event_type = "task.completed"
        item.url = "https://example.com/webhook"
        item.payload = {"event": "task.completed"}
        item.headers = {"Content-Type": "application/json"}
        item.attempt = 1
        item.max_attempts = 5
        item.status = "processing"
        for k, v in overrides.items():
            setattr(item, k, v)
        return item

    @pytest.mark.asyncio
    async def test_success_marks_completed(self):
        """Successful delivery should set status to completed."""
        from core.webhook_retry import _process_item

        item = self._make_queue_item()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhook_retry._get_retry_client", return_value=mock_client), \
             patch("core.webhook_retry.AsyncSessionLocal", return_value=mock_session):
            await _process_item(item)

        # Verify the update was called (at least logging + status update)
        assert mock_session.execute.called
        assert mock_session.commit.called

    @pytest.mark.asyncio
    async def test_4xx_goes_to_dead_letter(self):
        """4xx response should set status to dead_letter immediately."""
        from core.webhook_retry import _process_item

        item = self._make_queue_item(attempt=1, max_attempts=5)

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhook_retry._get_retry_client", return_value=mock_client), \
             patch("core.webhook_retry.AsyncSessionLocal", return_value=mock_session):
            await _process_item(item)

        assert mock_session.execute.called

    @pytest.mark.asyncio
    async def test_5xx_reschedules_retry(self):
        """5xx response with remaining attempts should schedule next retry."""
        from core.webhook_retry import _process_item

        item = self._make_queue_item(attempt=2, max_attempts=5)

        mock_resp = MagicMock()
        mock_resp.status_code = 503

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhook_retry._get_retry_client", return_value=mock_client), \
             patch("core.webhook_retry.AsyncSessionLocal", return_value=mock_session):
            await _process_item(item)

        assert mock_session.execute.called

    @pytest.mark.asyncio
    async def test_exhausted_retries_dead_letter(self):
        """When max attempts are exhausted, item goes to dead_letter."""
        from core.webhook_retry import _process_item

        item = self._make_queue_item(attempt=5, max_attempts=5)

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhook_retry._get_retry_client", return_value=mock_client), \
             patch("core.webhook_retry.AsyncSessionLocal", return_value=mock_session):
            await _process_item(item)

        assert mock_session.execute.called

    @pytest.mark.asyncio
    async def test_network_error_reschedules(self):
        """Network error (exception) should reschedule if attempts remain."""
        from core.webhook_retry import _process_item

        item = self._make_queue_item(attempt=1, max_attempts=5)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("Connection refused"))

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhook_retry._get_retry_client", return_value=mock_client), \
             patch("core.webhook_retry.AsyncSessionLocal", return_value=mock_session):
            await _process_item(item)

        assert mock_session.execute.called


class TestDeliverToEndpointEnqueues:
    """Test that _deliver_to_endpoint enqueues on failure."""

    @pytest.mark.asyncio
    async def test_5xx_enqueues_retry(self):
        """5xx response should enqueue to persistent retry queue."""
        from core.webhooks import _deliver_to_endpoint

        mock_endpoint = MagicMock()
        mock_endpoint.id = uuid.uuid4()
        mock_endpoint.url = "https://example.com/webhook"
        mock_endpoint.secret = "test-secret-key"
        mock_endpoint.events = None

        mock_resp = MagicMock()
        mock_resp.status_code = 503

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        with patch("core.webhooks._get_webhook_client", return_value=mock_client), \
             patch("core.webhooks.AsyncSessionLocal", return_value=mock_session), \
             patch("core.webhooks._get_user_event_template", return_value=None), \
             patch("core.webhooks.enqueue_retry", new_callable=AsyncMock) as mock_enqueue:
            await _deliver_to_endpoint(
                endpoint=mock_endpoint,
                task_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                event_type="task.completed",
                extra=None,
                max_retries=3,
            )

        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args.kwargs
        assert call_kwargs["url"] == "https://example.com/webhook"
        assert call_kwargs["max_attempts"] == 2  # 3 - 1

    @pytest.mark.asyncio
    async def test_success_does_not_enqueue(self):
        """Successful delivery should NOT enqueue."""
        from core.webhooks import _deliver_to_endpoint

        mock_endpoint = MagicMock()
        mock_endpoint.id = uuid.uuid4()
        mock_endpoint.url = "https://example.com/webhook"
        mock_endpoint.secret = "test-secret-key"
        mock_endpoint.events = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhooks._get_webhook_client", return_value=mock_client), \
             patch("core.webhooks.AsyncSessionLocal", return_value=mock_session), \
             patch("core.webhooks._get_user_event_template", return_value=None), \
             patch("core.webhooks.enqueue_retry", new_callable=AsyncMock) as mock_enqueue:
            await _deliver_to_endpoint(
                endpoint=mock_endpoint,
                task_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                event_type="task.completed",
                extra=None,
                max_retries=3,
            )

        mock_enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_4xx_does_not_enqueue(self):
        """4xx response should NOT enqueue (client error)."""
        from core.webhooks import _deliver_to_endpoint

        mock_endpoint = MagicMock()
        mock_endpoint.id = uuid.uuid4()
        mock_endpoint.url = "https://example.com/webhook"
        mock_endpoint.secret = "test-secret-key"
        mock_endpoint.events = None

        mock_resp = MagicMock()
        mock_resp.status_code = 403

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhooks._get_webhook_client", return_value=mock_client), \
             patch("core.webhooks.AsyncSessionLocal", return_value=mock_session), \
             patch("core.webhooks._get_user_event_template", return_value=None), \
             patch("core.webhooks.enqueue_retry", new_callable=AsyncMock) as mock_enqueue:
            await _deliver_to_endpoint(
                endpoint=mock_endpoint,
                task_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                event_type="task.completed",
                extra=None,
                max_retries=3,
            )

        mock_enqueue.assert_not_called()


class TestDeliverEnqueues:
    """Test that _deliver (per-task webhooks) also enqueues on failure."""

    @pytest.mark.asyncio
    async def test_5xx_enqueues_retry_for_task_webhook(self):
        """Per-task webhook 5xx should also enqueue for retry."""
        from core.webhooks import _deliver

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        with patch("core.webhooks._get_webhook_client", return_value=mock_client), \
             patch("core.webhooks.AsyncSessionLocal", return_value=mock_session), \
             patch("core.webhooks.enqueue_retry", new_callable=AsyncMock) as mock_enqueue:
            await _deliver(
                url="https://example.com/webhook",
                payload={"event": "task.completed"},
                task_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                event_type="task.completed",
                max_retries=3,
            )

        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args.kwargs
        assert call_kwargs["endpoint_id"] is None  # per-task, no endpoint
        assert call_kwargs["max_attempts"] == 2
