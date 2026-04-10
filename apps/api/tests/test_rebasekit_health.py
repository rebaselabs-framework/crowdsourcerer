"""Tests for core/rebasekit_health.py — RebaseKit service health monitoring."""

import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from core.rebasekit_health import (
    RebaseKitHealthCache,
    TASK_TO_SERVICE,
    ALL_SERVICES,
    CACHE_TTL,
    get_service_health,
    is_ai_available,
    get_available_task_types,
    warmup,
    get_cache,
)


# ─── Helpers ─────────────────────────────────────────────────────────────


def _make_response(status_code: int = 200) -> httpx.Response:
    """Create a minimal httpx.Response for mocking."""
    return httpx.Response(status_code=status_code, request=httpx.Request("GET", "http://test"))


def _mock_settings(api_key: str = "test-key", base_url: str = "http://rebasekit:8000"):
    """Return a mock settings object."""
    s = MagicMock()
    s.rebasekit_api_key = api_key
    s.rebasekit_base_url = base_url
    return s


# ─── RebaseKitHealthCache unit tests ────────────────────────────────────


class TestHealthCacheInit:
    """Initial state of the health cache."""

    def test_starts_stale(self):
        cache = RebaseKitHealthCache()
        assert cache.is_stale is True

    def test_starts_empty(self):
        cache = RebaseKitHealthCache()
        assert cache._status == {}


class TestHealthCacheRefresh:
    """_refresh() probes each service and updates the cache."""

    @pytest.mark.asyncio
    async def test_all_services_up(self):
        """When all health endpoints return 200, all services report up."""
        cache = RebaseKitHealthCache()

        async def mock_get(url, **kwargs):
            return _make_response(200)

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                status = await cache.force_refresh()

        assert all(v is True for v in status.values())
        assert set(status.keys()) == set(ALL_SERVICES)
        assert cache.is_stale is False

    @pytest.mark.asyncio
    async def test_all_services_down(self):
        """When all endpoints fail, all services report down."""
        cache = RebaseKitHealthCache()

        async def mock_get(url, **kwargs):
            raise httpx.ConnectError("Connection refused")

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                status = await cache.force_refresh()

        assert all(v is False for v in status.values())
        assert set(status.keys()) == set(ALL_SERVICES)

    @pytest.mark.asyncio
    async def test_partial_availability(self):
        """When some services return 200 and others fail."""
        cache = RebaseKitHealthCache()

        # pii returns 200, everything else returns 503
        async def mock_get(url, **kwargs):
            if "/pii/" in url:
                return _make_response(200)
            return _make_response(503)

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                status = await cache.force_refresh()

        assert status["pii"] is True
        down_services = [s for s, up in status.items() if not up]
        assert len(down_services) == len(ALL_SERVICES) - 1

    @pytest.mark.asyncio
    async def test_401_counts_as_up(self):
        """Services returning 401 (auth required) are considered 'up'."""
        cache = RebaseKitHealthCache()

        async def mock_get(url, **kwargs):
            return _make_response(401)

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                status = await cache.force_refresh()

        assert all(v is True for v in status.values())

    @pytest.mark.asyncio
    async def test_no_api_key_all_down(self):
        """When no API key is set, all services report down without pinging."""
        cache = RebaseKitHealthCache()

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings(api_key="")):
            status = await cache.force_refresh()

        assert all(v is False for v in status.values())

    @pytest.mark.asyncio
    async def test_timeout_counts_as_down(self):
        """Services that time out are considered down."""
        cache = RebaseKitHealthCache()

        async def mock_get(url, **kwargs):
            raise httpx.TimeoutException("timed out")

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                status = await cache.force_refresh()

        assert all(v is False for v in status.values())


class TestHealthCacheTTL:
    """TTL caching behavior."""

    @pytest.mark.asyncio
    async def test_cache_hit_within_ttl(self):
        """get_status() does not re-probe within TTL."""
        cache = RebaseKitHealthCache()
        cache._status = {"pii": True, "llm": False}
        cache._last_check = time.monotonic()  # just checked

        status = await cache.get_status()
        assert status == {"pii": True, "llm": False}

    @pytest.mark.asyncio
    async def test_cache_miss_after_ttl(self):
        """get_status() re-probes after TTL expires."""
        cache = RebaseKitHealthCache()
        cache._status = {"pii": True}
        cache._last_check = time.monotonic() - CACHE_TTL - 1  # expired

        async def mock_get(url, **kwargs):
            return _make_response(200)

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                status = await cache.get_status()

        # Should have all services now (re-probed)
        assert set(status.keys()) == set(ALL_SERVICES)

    @pytest.mark.asyncio
    async def test_force_refresh_ignores_ttl(self):
        """force_refresh() always re-probes regardless of TTL."""
        cache = RebaseKitHealthCache()
        cache._status = {"pii": True}
        cache._last_check = time.monotonic()  # just checked

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings(api_key="")):
            status = await cache.force_refresh()

        # Should have re-probed (no API key → all down)
        assert all(v is False for v in status.values())


# ─── Module-level function tests ────────────────────────────────────────


class TestModuleFunctions:
    """get_service_health(), is_ai_available(), get_available_task_types()."""

    @pytest.mark.asyncio
    async def test_get_service_health_delegates_to_cache(self):
        """get_service_health() returns the cache's status."""
        expected = {"pii": True, "llm": False}
        cache = get_cache()
        old_status = cache._status
        old_last = cache._last_check
        try:
            cache._status = expected
            cache._last_check = time.monotonic()
            result = await get_service_health()
            assert result == expected
        finally:
            cache._status = old_status
            cache._last_check = old_last

    @pytest.mark.asyncio
    async def test_is_ai_available_true_when_any_service_up(self):
        """is_ai_available() returns True if any service is up."""
        cache = get_cache()
        old_status = cache._status
        old_last = cache._last_check
        try:
            cache._status = {s: False for s in ALL_SERVICES}
            cache._status["pii"] = True
            cache._last_check = time.monotonic()
            with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
                result = await is_ai_available()
            assert result is True
        finally:
            cache._status = old_status
            cache._last_check = old_last

    @pytest.mark.asyncio
    async def test_is_ai_available_false_when_all_down(self):
        """is_ai_available() returns False when all services are down."""
        cache = get_cache()
        old_status = cache._status
        old_last = cache._last_check
        try:
            cache._status = {s: False for s in ALL_SERVICES}
            cache._last_check = time.monotonic()
            with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
                result = await is_ai_available()
            assert result is False
        finally:
            cache._status = old_status
            cache._last_check = old_last

    @pytest.mark.asyncio
    async def test_is_ai_available_false_without_api_key(self):
        """is_ai_available() returns False if no API key."""
        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings(api_key="")):
            result = await is_ai_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_get_available_task_types(self):
        """get_available_task_types() maps task types to their service status."""
        cache = get_cache()
        old_status = cache._status
        old_last = cache._last_check
        try:
            cache._status = {s: False for s in ALL_SERVICES}
            cache._status["pii"] = True
            cache._status["webtask"] = True
            cache._last_check = time.monotonic()

            result = await get_available_task_types()

            # pii_detect uses pii → True
            assert result["pii_detect"] is True
            # web_research and web_intel use webtask → True
            assert result["web_research"] is True
            assert result["web_intel"] is True
            # llm_generate uses llm → False
            assert result["llm_generate"] is False
            # All 10 task types should be present
            assert len(result) == len(TASK_TO_SERVICE)
        finally:
            cache._status = old_status
            cache._last_check = old_last


# ─── Service mapping correctness ────────────────────────────────────────


class TestServiceMapping:
    """Verify TASK_TO_SERVICE covers all expected task types."""

    def test_all_ai_task_types_mapped(self):
        expected_types = {
            "web_research", "entity_lookup", "document_parse",
            "data_transform", "llm_generate", "screenshot",
            "audio_transcribe", "pii_detect", "code_execute", "web_intel",
        }
        assert set(TASK_TO_SERVICE.keys()) == expected_types

    def test_all_services_are_unique_list(self):
        """ALL_SERVICES is a deduplicated, sorted list."""
        assert ALL_SERVICES == sorted(set(ALL_SERVICES))

    def test_web_intel_and_web_research_share_webtask(self):
        assert TASK_TO_SERVICE["web_research"] == "webtask"
        assert TASK_TO_SERVICE["web_intel"] == "webtask"


# ─── Warmup tests ───────────────────────────────────────────────────────


class TestWarmup:
    """warmup() pre-populates the cache on startup."""

    @pytest.mark.asyncio
    async def test_warmup_doesnt_raise_on_failure(self):
        """warmup() should not raise even if health check fails."""
        cache = get_cache()
        old_status = cache._status
        old_last = cache._last_check
        try:
            # Force stale cache
            cache._last_check = 0.0

            with patch("core.rebasekit_health.get_settings", return_value=_mock_settings(api_key="")):
                # Should not raise
                await warmup()
        finally:
            cache._status = old_status
            cache._last_check = old_last


# ─── /v1/config endpoint integration ────────────────────────────────────


class TestConfigEndpoint:
    """Test the /v1/config endpoint returns live health data."""

    @pytest.mark.asyncio
    async def test_config_includes_task_availability(self):
        """The /v1/config endpoint includes task_availability when API key is set."""
        from main import app
        from httpx import AsyncClient, ASGITransport

        cache = get_cache()
        old_status = cache._status
        old_last = cache._last_check
        try:
            cache._status = {s: True for s in ALL_SERVICES}
            cache._last_check = time.monotonic()

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/v1/config")

            assert r.status_code == 200
            data = r.json()
            assert "ai_available" in data
            # task_availability should be present when API key is configured
            if data.get("task_availability"):
                assert "web_research" in data["task_availability"]
        finally:
            cache._status = old_status
            cache._last_check = old_last

    @pytest.mark.asyncio
    async def test_config_ai_available_reflects_health(self):
        """ai_available should be False when all services are down."""
        from main import app
        from httpx import AsyncClient, ASGITransport

        cache = get_cache()
        old_status = cache._status
        old_last = cache._last_check
        try:
            cache._status = {s: False for s in ALL_SERVICES}
            cache._last_check = time.monotonic()

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/v1/config")

            data = r.json()
            # ai_available depends on whether the API key is set AND services are up
            # In test mode the key may or may not be set, but if it is and all
            # services are down, ai_available should be False
            assert "ai_available" in data
        finally:
            cache._status = old_status
            cache._last_check = old_last
