"""Tests for the export router: GET /v1/tasks/export.

Covers CSV / JSON / XLSX formats, date filters, org membership gating,
include_submissions flag, and auth enforcement.

NOTE: The main app registers the tasks router (with /{task_id}) before the
export router, which shadows /v1/tasks/export at the routing layer.  To test
the export endpoint logic in isolation we mount only the export router on a
minimal FastAPI app.  The auth-401 test uses the full app since the 422 from
the shadowing route still proves unauthenticated access is rejected.
"""
import csv
import io
import json
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

# ── Fixed IDs ────────────────────────────────────────────────────────────────

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
NOW = datetime.now(timezone.utc)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _token(user_id: str = USER_ID) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _auth() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


def _make_mock_db():
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock(return_value=0)
    db.get = AsyncMock(return_value=None)
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _scalar(value):
    """Mock an execute() result where .scalar_one_or_none() returns value."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


def _scalars_result(items):
    """Mock an execute() result where .scalars().all() returns items."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    return r


def _make_task(
    task_id=None,
    status="completed",
    task_type="web_research",
    exec_mode="ai",
):
    t = MagicMock()
    t.id = uuid.UUID(task_id or str(uuid.uuid4()))
    t.type = task_type
    t.status = status
    t.execution_mode = exec_mode
    t.priority = "normal"
    t.consensus_strategy = None
    t.dispute_status = None
    t.created_at = NOW
    t.started_at = None
    t.completed_at = NOW if status == "completed" else None
    t.credits_used = 10
    t.duration_ms = 500
    t.assignments_required = 1
    t.assignments_completed = 1
    t.worker_reward_credits = 5
    t.is_gold_standard = False
    t.org_id = None
    t.input = {"query": "test"}
    t.output = {"result": "test result"}
    t.error = None
    t.task_metadata = None
    t.user_id = uuid.UUID(USER_ID)
    t.tags = []
    return t


def _make_assignment(task_id, worker_id=None, status="submitted"):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.task_id = uuid.UUID(task_id) if isinstance(task_id, str) else task_id
    a.worker_id = uuid.UUID(worker_id or str(uuid.uuid4()))
    a.status = status
    a.response = {"answer": "worker response"}
    a.worker_note = "looks good"
    a.earnings_credits = 5
    a.submitted_at = NOW
    return a


def _build_test_app(mock_db):
    """Build a minimal FastAPI app with only the export router for isolation.

    The main app's tasks router shadows /v1/tasks/export with its
    /{task_id} path parameter, so we mount the export router alone.
    """
    from fastapi import FastAPI
    from routers.export import router as export_router
    from core.database import get_db

    app = FastAPI()
    app.include_router(export_router)
    app.dependency_overrides[get_db] = _db_override(mock_db)
    return app


# ── Auth ─────────────────────────────────────────────────────────────────────


class TestExportAuth:
    """Authentication enforcement."""

    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self):
        """Request without Authorization header is rejected."""
        db = _make_mock_db()
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get("/v1/tasks/export")
        # FastAPI returns 403 when HTTPBearer auto_error is False and no cred,
        # or 401 when HTTPBearer raises. Accept either as auth-blocked.
        assert r.status_code in (401, 403)


# ── Format Validation ────────────────────────────────────────────────────────


class TestExportFormatValidation:
    """Query param format must be csv, json, or xlsx."""

    @pytest.mark.asyncio
    async def test_invalid_format_xml_returns_400(self):
        db = _make_mock_db()
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export", params={"format": "xml"}, headers=_auth()
            )
        assert r.status_code == 400
        assert "format must be one of" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_format_pdf_returns_400(self):
        db = _make_mock_db()
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export", params={"format": "pdf"}, headers=_auth()
            )
        assert r.status_code == 400
        assert "format must be one of" in r.json()["detail"]


# ── CSV Export ───────────────────────────────────────────────────────────────


class TestCSVExport:
    """CSV format export tests."""

    @pytest.mark.asyncio
    async def test_csv_happy_path(self):
        """Single task: correct content-type, Content-Disposition, and data."""
        db = _make_mock_db()
        task = _make_task()
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "csv"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "Content-Disposition" in r.headers
        assert "tasks_export_" in r.headers["content-disposition"]
        assert ".csv" in r.headers["content-disposition"]

        reader = csv.DictReader(io.StringIO(r.text))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["task_id"] == str(task.id)
        assert rows[0]["status"] == "completed"
        assert rows[0]["type"] == "web_research"

    @pytest.mark.asyncio
    async def test_csv_empty_results_has_headers(self):
        """No tasks: CSV should contain header row only, no data rows."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "csv"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]

        reader = csv.DictReader(io.StringIO(r.text))
        rows = list(reader)
        assert len(rows) == 0
        # Headers should still be present
        assert "task_id" in r.text
        assert "status" in r.text

    @pytest.mark.asyncio
    async def test_csv_multiple_tasks(self):
        """Multiple tasks produce the correct number of CSV rows."""
        db = _make_mock_db()
        tasks = [_make_task(status="completed") for _ in range(5)]
        db.execute = AsyncMock(return_value=_scalars_result(tasks))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "csv"},
                headers=_auth(),
            )
        assert r.status_code == 200
        reader = csv.DictReader(io.StringIO(r.text))
        rows = list(reader)
        assert len(rows) == 5

    @pytest.mark.asyncio
    async def test_csv_default_format(self):
        """No format param defaults to CSV."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([_make_task()]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get("/v1/tasks/export", headers=_auth())
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]


# ── JSON Export ──────────────────────────────────────────────────────────────


class TestJSONExport:
    """JSON format export tests."""

    @pytest.mark.asyncio
    async def test_json_happy_path(self):
        """Correct content-type and response structure."""
        db = _make_mock_db()
        task = _make_task()
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert "application/json" in r.headers["content-type"]
        assert "Content-Disposition" in r.headers
        assert ".json" in r.headers["content-disposition"]

        body = r.json()
        assert "exported_at" in body
        assert body["count"] == 1
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["task_id"] == str(task.id)
        assert body["tasks"][0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_json_empty_results(self):
        """No tasks: count=0, empty tasks list."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["tasks"] == []

    @pytest.mark.asyncio
    async def test_json_task_fields_complete(self):
        """Every expected field is present in the exported task dict."""
        db = _make_mock_db()
        task = _make_task()
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        t = r.json()["tasks"][0]
        expected_keys = {
            "task_id", "type", "status", "execution_mode", "priority",
            "consensus_strategy", "dispute_status",
            "created_at", "started_at", "completed_at",
            "credits_used", "duration_ms",
            "assignments_required", "assignments_completed",
            "worker_reward_credits", "is_gold_standard",
            "org_id", "input", "output", "error", "metadata",
        }
        assert expected_keys.issubset(set(t.keys()))

    @pytest.mark.asyncio
    async def test_json_multiple_tasks_count_matches(self):
        """count field matches the actual number of tasks in the list."""
        db = _make_mock_db()
        tasks = [_make_task() for _ in range(3)]
        db.execute = AsyncMock(return_value=_scalars_result(tasks))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 3
        assert len(body["tasks"]) == 3


# ── XLSX Export ──────────────────────────────────────────────────────────────


class TestXLSXExport:
    """XLSX format export tests."""

    @pytest.mark.asyncio
    async def test_xlsx_happy_path(self):
        """Correct content-type and non-empty body for xlsx."""
        db = _make_mock_db()
        task = _make_task()
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "xlsx"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            in r.headers["content-type"]
        )
        assert ".xlsx" in r.headers["content-disposition"]
        assert len(r.content) > 0

    @pytest.mark.asyncio
    async def test_xlsx_empty_results(self):
        """Empty export still returns a valid xlsx file."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "xlsx"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            in r.headers["content-type"]
        )
        assert len(r.content) > 0


# ── Date Filters ─────────────────────────────────────────────────────────────


class TestDateFilters:
    """from_date / to_date validation and usage."""

    @pytest.mark.asyncio
    async def test_invalid_from_date_returns_400(self):
        db = _make_mock_db()
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"from_date": "not-a-date"},
                headers=_auth(),
            )
        assert r.status_code == 400
        assert "from_date" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_to_date_returns_400(self):
        db = _make_mock_db()
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"to_date": "yesterday"},
                headers=_auth(),
            )
        assert r.status_code == 400
        assert "to_date" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_valid_date_filters_succeed(self):
        """Valid ISO dates pass validation and return results."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={
                    "from_date": "2025-01-01",
                    "to_date": "2025-12-31",
                    "format": "json",
                },
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0

    @pytest.mark.asyncio
    async def test_valid_from_date_with_tasks(self):
        """Date-filtered export returns tasks normally."""
        db = _make_mock_db()
        task = _make_task()
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"from_date": "2025-01-01", "format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert r.json()["count"] == 1


# ── Org Export ───────────────────────────────────────────────────────────────


class TestOrgExport:
    """Org-scoped exports require membership check."""

    @pytest.mark.asyncio
    async def test_non_member_returns_403(self):
        """User who is not an org member gets 403."""
        db = _make_mock_db()
        # First execute: membership check -> None (not a member)
        db.execute = AsyncMock(return_value=_scalar(None))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"org_id": ORG_ID, "format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 403
        assert "Not a member" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_member_can_export(self):
        """Org member gets a successful export."""
        db = _make_mock_db()
        membership = MagicMock()  # truthy = member exists
        task = _make_task()
        task.org_id = uuid.UUID(ORG_ID)

        # First execute: membership check -> found
        # Second execute: task query -> one task
        db.execute = AsyncMock(
            side_effect=[_scalar(membership), _scalars_result([task])]
        )
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"org_id": ORG_ID, "format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["tasks"][0]["org_id"] == ORG_ID

    @pytest.mark.asyncio
    async def test_org_export_empty(self):
        """Org member with no tasks gets count=0."""
        db = _make_mock_db()
        membership = MagicMock()
        db.execute = AsyncMock(
            side_effect=[_scalar(membership), _scalars_result([])]
        )
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"org_id": ORG_ID, "format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert r.json()["count"] == 0


# ── include_submissions ──────────────────────────────────────────────────────


class TestIncludeSubmissions:
    """include_submissions loads worker assignment data for human tasks."""

    @pytest.mark.asyncio
    async def test_submissions_included_for_human_tasks(self):
        """Human task with submissions populates the submissions array."""
        db = _make_mock_db()
        task_id = "11111111-1111-1111-1111-111111111111"
        task = _make_task(task_id=task_id, exec_mode="human")
        assignment = _make_assignment(task_id=task_id)

        # First execute: task query
        # Second execute: assignment query
        db.execute = AsyncMock(
            side_effect=[_scalars_result([task]), _scalars_result([assignment])]
        )
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "json", "include_submissions": "true"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        submissions = body["tasks"][0]["submissions"]
        assert len(submissions) == 1
        assert submissions[0]["status"] == "submitted"
        assert submissions[0]["worker_note"] == "looks good"
        assert submissions[0]["earnings_credits"] == 5

    @pytest.mark.asyncio
    async def test_submissions_empty_for_ai_tasks(self):
        """AI tasks yield no assignment query; submissions list is empty."""
        db = _make_mock_db()
        task = _make_task(exec_mode="ai")
        # Only one execute: tasks query (task_ids is empty so no assignment query)
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "json", "include_submissions": "true"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["tasks"][0]["submissions"] == []

    @pytest.mark.asyncio
    async def test_submissions_not_included_by_default(self):
        """Without include_submissions the submissions key is absent."""
        db = _make_mock_db()
        task = _make_task(exec_mode="human")
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert "submissions" not in r.json()["tasks"][0]

    @pytest.mark.asyncio
    async def test_submissions_csv_excludes_submissions_column(self):
        """CSV export with include_submissions omits the submissions column."""
        db = _make_mock_db()
        task_id = "22222222-2222-2222-2222-222222222222"
        task = _make_task(task_id=task_id, exec_mode="human")
        assignment = _make_assignment(task_id=task_id)
        db.execute = AsyncMock(
            side_effect=[_scalars_result([task]), _scalars_result([assignment])]
        )
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"format": "csv", "include_submissions": "true"},
                headers=_auth(),
            )
        assert r.status_code == 200
        reader = csv.DictReader(io.StringIO(r.text))
        rows = list(reader)
        assert len(rows) == 1
        # "submissions" should not appear as a CSV column
        assert "submissions" not in reader.fieldnames


# ── Status / Type / Execution Mode Filters ───────────────────────────────────


class TestQueryFilters:
    """Filtering by status, type, and execution_mode."""

    @pytest.mark.asyncio
    async def test_status_filter(self):
        """Passing status=completed does not error; tasks returned."""
        db = _make_mock_db()
        task = _make_task(status="completed")
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"status": "completed", "format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert r.json()["count"] == 1

    @pytest.mark.asyncio
    async def test_status_all_returns_everything(self):
        """status=all should not add a status condition."""
        db = _make_mock_db()
        tasks = [
            _make_task(status="completed"),
            _make_task(status="failed"),
        ]
        db.execute = AsyncMock(return_value=_scalars_result(tasks))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"status": "all", "format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert r.json()["count"] == 2

    @pytest.mark.asyncio
    async def test_type_filter(self):
        """Passing type=web_research does not error."""
        db = _make_mock_db()
        task = _make_task(task_type="web_research")
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"type": "web_research", "format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert r.json()["count"] == 1

    @pytest.mark.asyncio
    async def test_execution_mode_filter(self):
        """Passing execution_mode=ai does not error."""
        db = _make_mock_db()
        task = _make_task(exec_mode="ai")
        db.execute = AsyncMock(return_value=_scalars_result([task]))
        app = _build_test_app(db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get(
                "/v1/tasks/export",
                params={"execution_mode": "ai", "format": "json"},
                headers=_auth(),
            )
        assert r.status_code == 200
        assert r.json()["count"] == 1
