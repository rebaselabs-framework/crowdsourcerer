"""Tests for core/rebasekit_health.py — AI capability monitoring.

Despite the historical file name, this module now reports on:

- **Local handlers** (``document_parse``, ``pii_detect``, ``code_execute``)
  which have no external dependency and are always available.
- **LLM-backed handlers** (``llm_generate``, ``data_transform``,
  ``web_research``) which depend on ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.rebasekit_health import (
    ALL_SERVICES,
    ALL_TASK_TYPES,
    LLM_TASK_TYPES,
    LOCAL_TASK_TYPES,
    RebaseKitHealthCache,
    TASK_TO_SERVICE,
    AIHealthSummary,
    get_ai_health_status,
    get_ai_health_summary,
    get_available_task_types,
    get_cache,
    get_service_health,
    is_ai_available,
    warmup,
)


def _mock_settings(*, anthropic_key: str = "test-key") -> MagicMock:
    s = MagicMock()
    s.anthropic_api_key = anthropic_key
    # Explicitly null the other provider keys — a bare MagicMock returns
    # a truthy child mock for any attribute, which makes the
    # "all three empty" branch unreachable unless we pin them.
    s.gemini_api_key = ""
    s.openai_api_key = ""
    s.llm_provider = ""
    return s


# ── Capability model ──────────────────────────────────────────────────


class TestCapabilityModel:
    def test_all_task_types_union(self):
        assert ALL_TASK_TYPES == LOCAL_TASK_TYPES | LLM_TASK_TYPES
        assert len(ALL_TASK_TYPES) == 6

    def test_local_set_is_the_local_three(self):
        assert LOCAL_TASK_TYPES == frozenset(
            {"document_parse", "pii_detect", "code_execute"}
        )

    def test_llm_set_is_the_llm_three(self):
        assert LLM_TASK_TYPES == frozenset(
            {"llm_generate", "data_transform", "web_research"}
        )

    def test_buckets_are_disjoint(self):
        assert LOCAL_TASK_TYPES.isdisjoint(LLM_TASK_TYPES)

    def test_task_to_service_maps_to_buckets(self):
        for task, bucket in TASK_TO_SERVICE.items():
            assert bucket in {"local", "llm"}, (task, bucket)
        assert set(TASK_TO_SERVICE.keys()) == ALL_TASK_TYPES

    def test_all_services_is_local_and_llm(self):
        assert ALL_SERVICES == ["llm", "local"]


# ── Cache refresh semantics ──────────────────────────────────────────


class TestCacheRefresh:
    @pytest.mark.asyncio
    async def test_both_up_when_key_configured(self):
        cache = RebaseKitHealthCache()
        with patch(
            "core.rebasekit_health.get_settings",
            return_value=_mock_settings(anthropic_key="sk-live-xxx"),
        ):
            status = await cache.force_refresh()
        assert status == {"local": True, "llm": True}

    @pytest.mark.asyncio
    async def test_llm_down_when_key_missing(self):
        cache = RebaseKitHealthCache()
        with patch(
            "core.rebasekit_health.get_settings",
            return_value=_mock_settings(anthropic_key=""),
        ):
            status = await cache.force_refresh()
        assert status == {"local": True, "llm": False}

    @pytest.mark.asyncio
    async def test_local_is_unconditionally_up(self):
        """No failure mode inside ``_refresh`` should ever mark local down."""
        cache = RebaseKitHealthCache()
        with patch(
            "core.rebasekit_health.get_settings",
            return_value=_mock_settings(anthropic_key=""),
        ):
            status = await cache.force_refresh()
        assert status["local"] is True


# ── Module-level aggregates ──────────────────────────────────────────


class TestAggregates:
    @pytest.mark.asyncio
    async def test_summary_healthy_when_key_set(self):
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            with patch(
                "core.rebasekit_health.get_settings",
                return_value=_mock_settings(anthropic_key="sk"),
            ):
                summary = await get_ai_health_summary()
            assert summary.status == "healthy"
            assert summary.services_up == summary.services_total
            assert summary.is_available is True
        finally:
            cache._status, cache._last_check = old_status, old_last

    @pytest.mark.asyncio
    async def test_summary_degraded_when_llm_key_missing(self):
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            with patch(
                "core.rebasekit_health.get_settings",
                return_value=_mock_settings(anthropic_key=""),
            ):
                summary = await get_ai_health_summary()
            # local=True + llm=False → degraded (one of two buckets up)
            assert summary.status == "degraded"
            assert summary.services_up == 1
            assert summary.services_total == 2
            assert summary.is_available is True
        finally:
            cache._status, cache._last_check = old_status, old_last

    @pytest.mark.asyncio
    async def test_status_helper_matches_summary(self):
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            with patch(
                "core.rebasekit_health.get_settings",
                return_value=_mock_settings(anthropic_key="sk"),
            ):
                assert await get_ai_health_status() == "healthy"
        finally:
            cache._status, cache._last_check = old_status, old_last

    @pytest.mark.asyncio
    async def test_is_ai_available_true_when_any_bucket_up(self):
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            with patch(
                "core.rebasekit_health.get_settings",
                return_value=_mock_settings(anthropic_key=""),
            ):
                assert await is_ai_available() is True  # local still up
        finally:
            cache._status, cache._last_check = old_status, old_last

    @pytest.mark.asyncio
    async def test_service_health_returns_dict(self):
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            with patch(
                "core.rebasekit_health.get_settings",
                return_value=_mock_settings(anthropic_key="sk"),
            ):
                status = await get_service_health()
            assert set(status.keys()) == {"local", "llm"}
        finally:
            cache._status, cache._last_check = old_status, old_last


# ── Per-task availability map ────────────────────────────────────────


class TestAvailableTaskTypes:
    @pytest.mark.asyncio
    async def test_all_six_reported_when_key_set(self):
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            with patch(
                "core.rebasekit_health.get_settings",
                return_value=_mock_settings(anthropic_key="sk"),
            ):
                availability = await get_available_task_types()
            assert set(availability.keys()) == ALL_TASK_TYPES
            assert all(availability.values())
        finally:
            cache._status, cache._last_check = old_status, old_last

    @pytest.mark.asyncio
    async def test_only_local_reported_up_when_key_missing(self):
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            with patch(
                "core.rebasekit_health.get_settings",
                return_value=_mock_settings(anthropic_key=""),
            ):
                availability = await get_available_task_types()
            for task in LOCAL_TASK_TYPES:
                assert availability[task] is True, task
            for task in LLM_TASK_TYPES:
                assert availability[task] is False, task
        finally:
            cache._status, cache._last_check = old_status, old_last


# ── Warmup + frozen summary object ───────────────────────────────────


class TestWarmup:
    @pytest.mark.asyncio
    async def test_warmup_never_raises(self):
        # Even if somewhere inside _refresh explodes, warmup swallows it.
        with patch(
            "core.rebasekit_health.get_settings",
            side_effect=RuntimeError("boom"),
        ):
            await warmup()  # should not raise

    def test_summary_is_frozen(self):
        s = AIHealthSummary(
            status="healthy",
            services_up=2,
            services_total=2,
            services={"local": True, "llm": True},
        )
        with pytest.raises((AttributeError, Exception)):
            s.status = "unavailable"  # type: ignore[misc]


# ── /v1/health endpoint integration ──────────────────────────────────


class TestV1HealthEndpoint:
    """The /v1/health endpoint is the canonical liveness probe for
    external monitors. It must return 503 when the platform is
    fundamentally broken so pagers fire on real outages."""

    async def _hit_health(self, *, anthropic_key: str) -> "httpx.Response":
        from main import app
        from httpx import AsyncClient, ASGITransport

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, *a, **kw):
                return None

        cache = get_cache()
        with patch.object(cache, "_refresh", return_value=None), patch(
            "main.AsyncSessionLocal", _FakeSession
        ), patch(
            "core.rebasekit_health.get_settings",
            return_value=_mock_settings(anthropic_key=anthropic_key),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                cache._status = {"local": True, "llm": bool(anthropic_key)}
                import time as _t
                cache._last_check = _t.monotonic()
                return await client.get("/v1/health")

    @pytest.mark.asyncio
    async def test_returns_200_when_healthy(self):
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            r = await self._hit_health(anthropic_key="sk-live-xxx")
            assert r.status_code == 200
            data = r.json()
            assert data["ai_status"] == "healthy"
            assert data["status"] == "ok"
        finally:
            cache._status, cache._last_check = old_status, old_last

    @pytest.mark.asyncio
    async def test_returns_200_when_only_local_available(self):
        """LLM key missing → degraded, but /v1/health still returns 200 since
        local handlers are still serving traffic."""
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            r = await self._hit_health(anthropic_key="")
            assert r.status_code == 200
            data = r.json()
            assert data["ai_status"] == "degraded"
            assert data["ai_available"] is True
        finally:
            cache._status, cache._last_check = old_status, old_last
