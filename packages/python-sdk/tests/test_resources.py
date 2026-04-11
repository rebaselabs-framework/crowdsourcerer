"""Tests for marketplace, worker, and webhook resources in the sync client."""
import pytest
import respx
import httpx

from crowdsourcerer import CrowdSorcerer
from crowdsourcerer.errors import CrowdSorcererError, NotFoundError

BASE = "https://crowdsourcerer.rebaselabs.online"


def make_client() -> CrowdSorcerer:
    return CrowdSorcerer(api_key="cs_test_key", base_url=BASE)


# ─── Marketplace ──────────────────────────────────────────────────────────


class TestMarketplace:
    @respx.mock
    def test_list_templates(self):
        respx.get(f"{BASE}/v1/marketplace/templates").mock(
            return_value=httpx.Response(200, json={
                "items": [{"id": "tmpl-1", "name": "Web Summary"}],
                "total": 1, "page": 1, "page_size": 24,
            })
        )
        c = make_client()
        result = c.marketplace.list(task_type="web_research", sort="popular")
        assert result["total"] == 1
        assert result["items"][0]["name"] == "Web Summary"

    @respx.mock
    def test_list_templates_with_search(self):
        respx.get(f"{BASE}/v1/marketplace/templates").mock(
            return_value=httpx.Response(200, json={
                "items": [], "total": 0, "page": 1, "page_size": 24,
            })
        )
        c = make_client()
        result = c.marketplace.list(search="summarise", category="research")
        assert result["total"] == 0

    @respx.mock
    def test_list_my_templates(self):
        respx.get(f"{BASE}/v1/marketplace/templates").mock(
            return_value=httpx.Response(200, json={
                "items": [], "total": 0, "page": 1, "page_size": 24,
            })
        )
        c = make_client()
        result = c.marketplace.list(my_own=True)
        assert result["total"] == 0

    @respx.mock
    def test_get_template(self):
        respx.get(f"{BASE}/v1/marketplace/templates/tmpl-1").mock(
            return_value=httpx.Response(200, json={
                "id": "tmpl-1", "name": "Web Summary", "task_type": "web_research",
                "execution_mode": "ai", "use_count": 42,
            })
        )
        c = make_client()
        tmpl = c.marketplace.get("tmpl-1")
        assert tmpl["name"] == "Web Summary"
        assert tmpl["use_count"] == 42

    @respx.mock
    def test_create_template(self):
        respx.post(f"{BASE}/v1/marketplace/templates").mock(
            return_value=httpx.Response(200, json={
                "id": "tmpl-2", "name": "PII Scanner", "task_type": "pii_detect",
            })
        )
        c = make_client()
        result = c.marketplace.create(
            name="PII Scanner",
            task_type="pii_detect",
            description="Scan text for PII",
            tags=["privacy", "pii"],
        )
        assert result["name"] == "PII Scanner"

    @respx.mock
    def test_use_template(self):
        respx.post(f"{BASE}/v1/marketplace/templates/tmpl-1/use").mock(
            return_value=httpx.Response(200, json={
                "template_id": "tmpl-1", "task_type": "web_research",
                "execution_mode": "ai", "task_config": {},
                "example_input": {"url": "https://example.com"},
            })
        )
        c = make_client()
        result = c.marketplace.use("tmpl-1")
        assert result["template_id"] == "tmpl-1"
        assert result["example_input"]["url"] == "https://example.com"

    @respx.mock
    def test_rate_template(self):
        respx.post(f"{BASE}/v1/marketplace/templates/tmpl-1/rate").mock(
            return_value=httpx.Response(200, json={
                "template_id": "tmpl-1", "your_rating": 5,
                "new_avg": 4.5, "total_ratings": 10,
            })
        )
        c = make_client()
        result = c.marketplace.rate("tmpl-1", 5)
        assert result["your_rating"] == 5

    def test_rate_template_validates_range(self):
        c = make_client()
        with pytest.raises(ValueError, match="between 1 and 5"):
            c.marketplace.rate("tmpl-1", 0)
        with pytest.raises(ValueError, match="between 1 and 5"):
            c.marketplace.rate("tmpl-1", 6)

    @respx.mock
    def test_categories(self):
        respx.get(f"{BASE}/v1/marketplace/categories").mock(
            return_value=httpx.Response(200, json=[
                {"category": "research", "count": 10},
                {"category": "data", "count": 5},
            ])
        )
        c = make_client()
        cats = c.marketplace.categories()
        assert len(cats) == 2
        assert cats[0]["category"] == "research"

    @respx.mock
    def test_quota(self):
        respx.get(f"{BASE}/v1/users/quota").mock(
            return_value=httpx.Response(200, json={
                "plan": "free",
                "tasks": {"used": 5, "limit": 100},
                "batch_task_size": 50,
            })
        )
        c = make_client()
        q = c.marketplace.quota()
        assert q["plan"] == "free"


# ─── Worker ───────────────────────────────────────────────────────────────


class TestWorker:
    @respx.mock
    def test_list_tasks(self):
        respx.get(f"{BASE}/v1/worker/tasks").mock(
            return_value=httpx.Response(200, json={
                "items": [
                    {"id": "task-1", "type": "label_image", "reward_credits": 3},
                ],
                "total": 1, "page": 1, "page_size": 20,
            })
        )
        c = make_client()
        result = c.worker.list_tasks(task_type="label_image")
        assert len(result["items"]) == 1

    @respx.mock
    def test_list_tasks_with_pagination(self):
        respx.get(f"{BASE}/v1/worker/tasks").mock(
            return_value=httpx.Response(200, json={
                "items": [], "total": 0, "page": 2, "page_size": 10,
            })
        )
        c = make_client()
        result = c.worker.list_tasks(page=2, page_size=10)
        assert result["page"] == 2

    @respx.mock
    def test_get_feed(self):
        respx.get(f"{BASE}/v1/worker/tasks/feed").mock(
            return_value=httpx.Response(200, json={
                "items": [
                    {"id": "task-1", "type": "label_image", "match_score": 0.95},
                ],
                "total": 1, "page": 1, "page_size": 20,
            })
        )
        c = make_client()
        result = c.worker.get_feed()
        assert result["items"][0]["match_score"] == 0.95

    @respx.mock
    def test_claim(self):
        respx.post(f"{BASE}/v1/worker/tasks/task-1/claim").mock(
            return_value=httpx.Response(200, json={
                "assignment_id": "assign-1", "task_id": "task-1",
                "status": "active", "timeout_at": "2026-01-01T01:00:00Z",
            })
        )
        c = make_client()
        result = c.worker.claim("task-1")
        assert result["status"] == "active"

    @respx.mock
    def test_submit(self):
        respx.post(f"{BASE}/v1/worker/tasks/task-1/submit").mock(
            return_value=httpx.Response(200, json={
                "assignment_id": "assign-1", "status": "submitted",
                "xp_earned": 10, "credits_earned": 3,
            })
        )
        c = make_client()
        result = c.worker.submit("task-1", {"labels": ["cat"]})
        assert result["status"] == "submitted"
        assert result["xp_earned"] == 10

    @respx.mock
    def test_release(self):
        respx.delete(f"{BASE}/v1/worker/tasks/task-1/release").mock(
            return_value=httpx.Response(204)
        )
        c = make_client()
        c.worker.release("task-1")

    @respx.mock
    def test_my_skills(self):
        respx.get(f"{BASE}/v1/workers/me/skills").mock(
            return_value=httpx.Response(200, json={
                "skills": [
                    {"task_type": "label_image", "level": 3, "tasks_completed": 42},
                ],
            })
        )
        c = make_client()
        result = c.worker.my_skills()
        assert len(result["skills"]) == 1
        assert result["skills"][0]["level"] == 3


# ─── Webhooks ─────────────────────────────────────────────────────────────


class TestWebhooks:
    @respx.mock
    def test_events(self):
        respx.get(f"{BASE}/v1/webhooks/events").mock(
            return_value=httpx.Response(200, json={
                "events": [
                    {"type": "task.completed", "description": "Task done", "is_default": True},
                    {"type": "task.failed", "description": "Task failed", "is_default": False},
                ],
                "default_events": ["task.completed"],
            })
        )
        c = make_client()
        result = c.webhooks.events()
        assert len(result["events"]) == 2

    @respx.mock
    def test_stats(self):
        respx.get(f"{BASE}/v1/webhooks/stats").mock(
            return_value=httpx.Response(200, json={
                "total_deliveries": 100, "succeeded": 95, "failed": 5,
                "success_rate": 0.95, "avg_duration_ms": 250,
                "by_event_type": {"task.completed": 80},
            })
        )
        c = make_client()
        result = c.webhooks.stats()
        assert result["success_rate"] == 0.95

    @respx.mock
    def test_logs(self):
        respx.get(f"{BASE}/v1/webhooks/logs").mock(
            return_value=httpx.Response(200, json={
                "items": [
                    {"id": "log-1", "task_id": "task-1", "event_type": "task.completed",
                     "success": True, "status_code": 200},
                ],
                "total": 1, "page": 1, "page_size": 25,
            })
        )
        c = make_client()
        result = c.webhooks.logs(task_id="task-1", event_type="task.completed", success=True)
        assert len(result["items"]) == 1
        assert result["items"][0]["success"] is True

    @respx.mock
    def test_logs_without_filters(self):
        respx.get(f"{BASE}/v1/webhooks/logs").mock(
            return_value=httpx.Response(200, json={
                "items": [], "total": 0, "page": 1, "page_size": 25,
            })
        )
        c = make_client()
        result = c.webhooks.logs()
        assert result["total"] == 0


# ─── Sync client edge cases ──────────────────────────────────────────────


class TestSyncClientEdgeCases:
    @respx.mock
    def test_context_manager(self):
        respx.get(f"{BASE}/v1/users/me").mock(
            return_value=httpx.Response(200, json={
                "id": "00000000-0000-0000-0000-000000000001",
                "email": "test@example.com", "plan": "free",
                "role": "requester", "credits": 1000,
                "created_at": "2026-01-01T00:00:00Z",
            })
        )
        with CrowdSorcerer(api_key="cs_test", base_url=BASE) as c:
            user = c.users.me()
            assert user.email == "test@example.com"

    @respx.mock
    def test_204_response(self):
        respx.delete(f"{BASE}/v1/tasks/task-1").mock(
            return_value=httpx.Response(204)
        )
        c = make_client()
        result = c.tasks.cancel("task-1")
        assert result is None

    @respx.mock
    def test_generic_500_error(self):
        respx.get(f"{BASE}/v1/users/me").mock(
            return_value=httpx.Response(500, json={"detail": "Internal Server Error"})
        )
        c = CrowdSorcerer(api_key="cs_test", base_url=BASE, max_retries=1)
        with pytest.raises(CrowdSorcererError) as exc:
            c.users.me()
        assert exc.value.status_code == 500

    @respx.mock
    def test_not_found_error(self):
        respx.get(f"{BASE}/v1/tasks/nonexistent").mock(
            return_value=httpx.Response(404, json={"detail": "Not found"})
        )
        c = CrowdSorcerer(api_key="cs_test", base_url=BASE, max_retries=1)
        with pytest.raises(NotFoundError):
            c.tasks.get("nonexistent")

    def test_sdk_version_in_headers(self):
        """Verify the X-Client header uses version 1.0.0."""
        c = make_client()
        headers = c._http.headers
        assert "crowdsourcerer-python/1.0.0" in headers.get("x-client", "")

    @respx.mock
    def test_request_id_propagated(self):
        """Error includes x-request-id from response headers."""
        respx.get(f"{BASE}/v1/users/me").mock(
            return_value=httpx.Response(
                401,
                json={"detail": "Unauthorized"},
                headers={"x-request-id": "req-abc-123"},
            )
        )
        c = CrowdSorcerer(api_key="cs_test", base_url=BASE, max_retries=1)
        with pytest.raises(CrowdSorcererError) as exc:
            c.users.me()
        assert exc.value.request_id == "req-abc-123"
