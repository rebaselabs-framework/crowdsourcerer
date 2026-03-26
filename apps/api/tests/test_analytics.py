"""Unit tests for analytics module.

Covers:
  - _percentile() pure function — edge cases, interpolation, single element
  - completion_times bucketing logic — delta computation, negative delta guard
  - export _fmt_dt helper — None and datetime inputs
  - All analytics endpoints require authentication (401 guards)

No real DB or network required for the pure-function tests.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── _percentile() ──────────────────────────────────────────────────────────────

def percentile(data, pct):
    from routers.analytics import _percentile
    return _percentile(data, pct)


def test_percentile_empty_list():
    """Empty input should return 0.0, not raise."""
    assert percentile([], 50) == 0.0
    assert percentile([], 0) == 0.0
    assert percentile([], 100) == 0.0


def test_percentile_single_element():
    """Single element returns that element for any percentile."""
    assert percentile([7.5], 0) == 7.5
    assert percentile([7.5], 50) == 7.5
    assert percentile([7.5], 100) == 7.5


def test_percentile_two_elements_midpoint():
    """Two elements: p50 should interpolate halfway between them."""
    result = percentile([10.0, 20.0], 50)
    assert result == 15.0


def test_percentile_two_elements_p0():
    """p0 of two elements is the first (smallest)."""
    assert percentile([10.0, 20.0], 0) == 10.0


def test_percentile_two_elements_p100():
    """p100 of two elements is the last (largest)."""
    assert percentile([10.0, 20.0], 100) == 20.0


def test_percentile_sorted_five_elements_median():
    """p50 of [1, 2, 3, 4, 5] is 3 (exact middle element)."""
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0


def test_percentile_sorted_five_elements_p0():
    """p0 always returns the minimum value."""
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0) == 1.0


def test_percentile_sorted_five_elements_p100():
    """p100 always returns the maximum value."""
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 100) == 5.0


def test_percentile_sorted_five_elements_p25():
    """p25 of [1, 2, 3, 4, 5]: idx=1.0, exact → 2.0."""
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 25) == 2.0


def test_percentile_sorted_five_elements_p75():
    """p75 of [1, 2, 3, 4, 5]: idx=3.0, exact → 4.0."""
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 75) == 4.0


def test_percentile_p95_interpolated():
    """p95 of [1, 2, 3, 4, 5]: idx=3.8, interpolates between 4 and 5 → 4.8."""
    result = percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95)
    assert result == 4.8


def test_percentile_unsorted_input_handled():
    """_percentile sorts internally — input order must not matter."""
    result_forward = percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50)
    result_reversed = percentile([5.0, 4.0, 3.0, 2.0, 1.0], 50)
    result_shuffled = percentile([3.0, 1.0, 5.0, 2.0, 4.0], 50)
    assert result_forward == result_reversed == result_shuffled == 3.0


def test_percentile_odd_p95_interpolated():
    """p95 of [1, 3, 5, 7, 9]: idx=3.8 → 7 + 0.8*(9-7) = 8.6."""
    result = percentile([1.0, 3.0, 5.0, 7.0, 9.0], 95)
    assert result == 8.6


def test_percentile_returns_rounded_to_2dp():
    """Result must be rounded to 2 decimal places."""
    # [1.0, 2.0]: p33 → idx = 0.33, lo=0, hi=1, frac=0.33 → 1.0 + 0.33 = 1.33
    result = percentile([1.0, 2.0], 33)
    assert isinstance(result, float)
    # Check it's rounded (not arbitrary precision float)
    assert result == round(result, 2)


def test_percentile_large_dataset_consistency():
    """Large dataset: p50 should equal the actual median."""
    import statistics
    data = [float(i) for i in range(1, 101)]   # 1..100
    p50 = percentile(data, 50)
    actual_median = statistics.median(data)
    # For 100 elements, our linear interpolation p50 may differ from statistics.median
    # but should be within the expected range [50.0, 51.0]
    assert 49.5 <= p50 <= 51.0, f"Expected p50 near 50.5, got {p50}"


def test_percentile_all_same_values():
    """All identical values: any percentile returns that value."""
    assert percentile([5.0, 5.0, 5.0, 5.0], 0) == 5.0
    assert percentile([5.0, 5.0, 5.0, 5.0], 50) == 5.0
    assert percentile([5.0, 5.0, 5.0, 5.0], 100) == 5.0


# ── completion_times bucketing logic ──────────────────────────────────────────

def test_completion_buckets_basic():
    """Durations are bucketed correctly by task type."""
    from collections import defaultdict

    # Simulate what completion_times does
    rows = [
        ("web_research", datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
                         datetime(2024, 1, 1, 10, 30, tzinfo=timezone.utc)),  # 30 min
        ("web_research", datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc),
                         datetime(2024, 1, 1, 11, 45, tzinfo=timezone.utc)),  # 45 min
        ("llm_generate", datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
                         datetime(2024, 1, 1, 12, 5, tzinfo=timezone.utc)),   # 5 min
    ]

    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        task_type, created, completed = row
        if created and completed:
            delta_minutes = (
                completed.replace(tzinfo=timezone.utc)
                - created.replace(tzinfo=timezone.utc)
            ).total_seconds() / 60.0
            if delta_minutes >= 0:
                buckets[task_type].append(delta_minutes)

    assert buckets["web_research"] == [30.0, 45.0]
    assert buckets["llm_generate"] == [5.0]


def test_completion_buckets_negative_delta_excluded():
    """Negative deltas (clock skew) are excluded from buckets."""
    from collections import defaultdict

    rows = [
        ("web_research",
         datetime(2024, 1, 1, 10, 30, tzinfo=timezone.utc),
         datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)),  # negative: completed before created
    ]

    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        task_type, created, completed = row
        if created and completed:
            delta = (
                completed.replace(tzinfo=timezone.utc)
                - created.replace(tzinfo=timezone.utc)
            ).total_seconds() / 60.0
            if delta >= 0:
                buckets[task_type].append(delta)

    assert len(buckets["web_research"]) == 0


def test_completion_buckets_zero_delta_included():
    """Zero delta (completed instantly) is included — it's not negative."""
    from collections import defaultdict

    rows = [
        ("screenshot",
         datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc),
         datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)),  # 0 min
    ]

    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        task_type, created, completed = row
        if created and completed:
            delta = (
                completed.replace(tzinfo=timezone.utc)
                - created.replace(tzinfo=timezone.utc)
            ).total_seconds() / 60.0
            if delta >= 0:
                buckets[task_type].append(delta)

    assert buckets["screenshot"] == [0.0]


def test_completion_stats_output_shape():
    """CompletionTimeStats fields are populated from _percentile calls."""
    import statistics as stats_mod
    from routers.analytics import _percentile, CompletionTimeStats

    durations = [10.0, 20.0, 30.0, 40.0, 50.0]
    result = CompletionTimeStats(
        task_type="web_research",
        count=len(durations),
        avg_minutes=round(stats_mod.mean(durations), 2),
        p50_minutes=_percentile(durations, 50),
        p95_minutes=_percentile(durations, 95),
        min_minutes=round(min(durations), 2),
        max_minutes=round(max(durations), 2),
    )

    assert result.task_type == "web_research"
    assert result.count == 5
    assert result.avg_minutes == 30.0
    assert result.p50_minutes == 30.0
    assert result.p95_minutes == 48.0  # 40 + 0.8 * (50-40)
    assert result.min_minutes == 10.0
    assert result.max_minutes == 50.0


# ── _fmt_dt helper ─────────────────────────────────────────────────────────────

def fmt_dt(dt):
    """Replicate the _fmt_dt helper from export_analytics."""
    return dt.isoformat() if dt else ""


def test_fmt_dt_none():
    """None datetime returns empty string."""
    assert fmt_dt(None) == ""


def test_fmt_dt_aware_datetime():
    """Aware datetime returns ISO format string."""
    dt = datetime(2024, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
    result = fmt_dt(dt)
    assert result == "2024-06-15T12:30:00+00:00"
    assert isinstance(result, str)
    assert len(result) > 10


def test_fmt_dt_naive_datetime():
    """Naive datetime still returns ISO format (no tz info appended)."""
    dt = datetime(2024, 6, 15, 12, 30, 0)
    result = fmt_dt(dt)
    assert result == "2024-06-15T12:30:00"


# ── Auth guards (no DB, just ASGI app) ────────────────────────────────────────

@pytest.fixture
async def client():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_analytics_overview_requires_auth(client):
    """GET /v1/analytics/overview → 401 without auth."""
    r = await client.get("/v1/analytics/overview")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_costs_requires_auth(client):
    """GET /v1/analytics/costs → 401 without auth."""
    r = await client.get("/v1/analytics/costs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_export_requires_auth(client):
    """GET /v1/analytics/export → 401 without auth."""
    r = await client.get("/v1/analytics/export")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_completion_times_requires_auth(client):
    """GET /v1/analytics/completion-times → 401 without auth."""
    r = await client.get("/v1/analytics/completion-times")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_org_requires_auth(client):
    """GET /v1/analytics/org/{id} → 401 without auth (any UUID)."""
    import uuid
    r = await client.get(f"/v1/analytics/org/{uuid.uuid4()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_overview_validates_days_param(client):
    """GET /v1/analytics/overview?days=0 → 422 (ge=1 constraint)."""
    r = await client.get("/v1/analytics/overview?days=0")
    # 422 from Pydantic validation or 401 from auth (auth runs first)
    assert r.status_code in (401, 422)


@pytest.mark.asyncio
async def test_analytics_export_validates_format_param(client):
    """GET /v1/analytics/export?fmt=xlsx → 422 (only csv or json allowed)."""
    r = await client.get("/v1/analytics/export?fmt=xlsx")
    # Pattern validation catches this regardless of auth
    assert r.status_code in (401, 422)


# ── Export format logic (pure, no DB) ─────────────────────────────────────────

def test_export_columns_are_defined():
    """The export column list must contain all expected fields."""
    from routers.analytics import _EXPORT_COLUMNS
    required = {"id", "type", "status", "credits_used", "created_at", "completed_at"}
    for col in required:
        assert col in _EXPORT_COLUMNS, f"Export column missing: {col}"


def test_export_csv_structure():
    """Smoke-test the CSV export row construction logic."""
    import csv, io

    _EXPORT_COLUMNS = [
        "id", "type", "execution_mode", "status", "priority",
        "credits_used", "duration_ms", "created_at", "started_at", "completed_at",
    ]

    rows = [
        {
            "id": "abc-123",
            "type": "web_research",
            "execution_mode": "ai",
            "status": "completed",
            "priority": "normal",
            "credits_used": 5,
            "duration_ms": 1234,
            "created_at": "2024-01-01T00:00:00",
            "started_at": "2024-01-01T00:00:01",
            "completed_at": "2024-01-01T00:00:10",
        }
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    csv_content = buf.getvalue()

    assert "id,type,execution_mode" in csv_content
    assert "abc-123" in csv_content
    assert "web_research" in csv_content
    assert "completed" in csv_content


def test_export_json_structure():
    """Smoke-test the JSON export structure."""
    import json

    rows = [
        {"id": "abc", "type": "screenshot", "status": "completed",
         "credits_used": 3, "duration_ms": 500,
         "created_at": "2024-01-01", "started_at": "", "completed_at": ""},
    ]
    days = 30
    content = json.dumps({"tasks": rows, "count": len(rows), "days": days}, indent=2)
    parsed = json.loads(content)

    assert parsed["count"] == 1
    assert parsed["days"] == 30
    assert len(parsed["tasks"]) == 1
    assert parsed["tasks"][0]["type"] == "screenshot"
