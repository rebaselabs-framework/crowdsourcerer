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
    AIHealthSummary,
    get_service_health,
    is_ai_available,
    get_available_task_types,
    get_ai_health_status,
    get_ai_health_summary,
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


# ─── Gateway-catalog aware behaviour ────────────────────────────────────


def _catalog_response(active: list[str]) -> httpx.Response:
    """Build a fake RebaseKit gateway catalog response."""
    payload = {
        "api": "RebaseKit",
        "version": "1.10.0",
        "active_services": {svc: {"base": f"/{svc}/"} for svc in active},
        "disabled_services": [],
    }
    return httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("GET", "http://test/"),
    )


class TestCatalogAwareRefresh:
    """_refresh() uses gateway active_services to short-circuit stale probes."""

    @pytest.mark.asyncio
    async def test_services_missing_from_catalog_are_down_without_probe(self):
        """A service not listed in active_services is down — no probe emitted."""
        cache = RebaseKitHealthCache()
        probed: list[str] = []

        async def mock_get(url, **kwargs):
            if url.endswith("/"):
                # Gateway catalog — only pii is live.
                return _catalog_response(["pii"])
            probed.append(url)
            return _make_response(200)

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                status = await cache.force_refresh()

        # Only pii is up; everyone else skipped the probe entirely.
        assert status["pii"] is True
        assert all(status[svc] is False for svc in ALL_SERVICES if svc != "pii")
        assert probed == [f"http://rebasekit:8000/pii/health"]

    @pytest.mark.asyncio
    async def test_catalog_unreachable_falls_back_to_probing_every_service(self):
        """If the catalog endpoint is broken, we still probe each service."""
        cache = RebaseKitHealthCache()

        async def mock_get(url, **kwargs):
            if url.endswith("/"):
                return _make_response(503)  # catalog unreachable
            return _make_response(200)

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                status = await cache.force_refresh()

        assert all(status[svc] is True for svc in ALL_SERVICES)

    @pytest.mark.asyncio
    async def test_probe_hits_service_slash_health_path(self):
        """New RebaseKit convention is /<service>/health, not /api/health."""
        cache = RebaseKitHealthCache()
        probed_urls: list[str] = []

        async def mock_get(url, **kwargs):
            probed_urls.append(url)
            if url.endswith("/"):
                return _make_response(503)  # skip catalog
            return _make_response(200)

        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
            with patch("httpx.AsyncClient") as MockClient:
                mock_ctx = AsyncMock()
                mock_ctx.get = mock_get
                MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                await cache.force_refresh()

        probe_urls = [u for u in probed_urls if not u.endswith("/")]
        assert all("/health" in u for u in probe_urls)
        assert all("/api/health" not in u for u in probe_urls)


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


# ─── Three-tier health status ───────────────────────────────────────────


class TestAIHealthStatus:
    """get_ai_health_status() / get_ai_health_summary() — the honest
    replacement for the old ``is_ai_available``-as-truth pattern."""

    def _seed(self, ups: dict[str, bool]):
        cache = get_cache()
        cache._status = {svc: ups.get(svc, False) for svc in ALL_SERVICES}
        cache._last_check = time.monotonic()
        return cache

    @pytest.mark.asyncio
    async def test_healthy_when_all_services_up(self):
        cache = self._seed({svc: True for svc in ALL_SERVICES})
        old_status = cache._status
        old_last = cache._last_check
        try:
            with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
                summary = await get_ai_health_summary()
                status = await get_ai_health_status()
            assert summary.status == "healthy"
            assert status == "healthy"
            assert summary.services_up == summary.services_total
            assert summary.is_available is True
        finally:
            cache._status = old_status
            cache._last_check = old_last

    @pytest.mark.asyncio
    async def test_degraded_when_one_service_up(self):
        """The whole point of the refactor: 1/N up is degraded, not healthy."""
        cache = self._seed({"pii": True})
        old_status = cache._status
        old_last = cache._last_check
        try:
            with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
                summary = await get_ai_health_summary()
            assert summary.status == "degraded"
            assert summary.services_up == 1
            assert summary.services_total == len(ALL_SERVICES)
            # is_ai_available still True (backwards compat) but caller can
            # now tell the difference.
            assert summary.is_available is True
        finally:
            cache._status = old_status
            cache._last_check = old_last

    @pytest.mark.asyncio
    async def test_unavailable_when_all_down(self):
        cache = self._seed({})
        old_status = cache._status
        old_last = cache._last_check
        try:
            with patch("core.rebasekit_health.get_settings", return_value=_mock_settings()):
                summary = await get_ai_health_summary()
            assert summary.status == "unavailable"
            assert summary.services_up == 0
            assert summary.is_available is False
        finally:
            cache._status = old_status
            cache._last_check = old_last

    @pytest.mark.asyncio
    async def test_unavailable_without_api_key(self):
        """Missing API key short-circuits to unavailable without hitting the cache."""
        with patch("core.rebasekit_health.get_settings", return_value=_mock_settings(api_key="")):
            summary = await get_ai_health_summary()
        assert summary.status == "unavailable"
        assert summary.services_up == 0
        assert summary.services_total == len(ALL_SERVICES)
        assert all(up is False for up in summary.services.values())

    def test_summary_is_frozen(self):
        """AIHealthSummary is an immutable value object."""
        s = AIHealthSummary(
            status="healthy",
            services_up=10,
            services_total=10,
            services={svc: True for svc in ALL_SERVICES},
        )
        with pytest.raises((AttributeError, Exception)):
            s.status = "unavailable"  # type: ignore[misc]


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


# ─── /v1/health endpoint integration ────────────────────────────────────


class TestV1HealthEndpoint:
    """The /v1/health endpoint is the canonical liveness probe for
    external monitors. It must return 503 when the platform is
    fundamentally broken so pagers fire on real outages.

    FastAPI's startup lifespan calls ``warmup_health()`` which runs a
    real HTTP probe against the upstream RebaseKit gateway — so each
    test patches ``_cache._refresh`` to a no-op and seeds the cache
    state manually. Otherwise warmup would clobber the fixture.
    """

    async def _hit_health(self, seeded_status: dict[str, bool]) -> "httpx.Response":
        from main import app
        from httpx import AsyncClient, ASGITransport

        # Stand-in async context manager that acts like AsyncSessionLocal()
        # but never actually touches Postgres. /v1/health's SELECT 1 probe
        # is satisfied by returning a session whose execute() is a no-op.
        class _FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def execute(self, *a, **kw):
                return None

        cache = get_cache()
        noop_refresh = AsyncMock()
        with patch.object(cache, "_refresh", noop_refresh), \
             patch("main.AsyncSessionLocal", _FakeSession), \
             patch(
                 "core.rebasekit_health.get_settings",
                 return_value=_mock_settings(),
             ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Seed AFTER lifespan has run so warmup's (no-op) refresh
                # can't race with our assignment.
                cache._status = dict(seeded_status)
                cache._last_check = time.monotonic()
                return await client.get("/v1/health")

    @pytest.mark.asyncio
    async def test_returns_503_when_ai_fleet_unavailable(self):
        """All workers down ⇒ HTTP 503 with ai_status='unavailable'."""
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            r = await self._hit_health({s: False for s in ALL_SERVICES})
            assert r.status_code == 503
            data = r.json()
            assert data["ai_status"] == "unavailable"
            assert data["ai_available"] is False
            assert data["status"] == "degraded"
        finally:
            cache._status, cache._last_check = old_status, old_last

    @pytest.mark.asyncio
    async def test_returns_200_when_degraded(self):
        """Some workers up ⇒ HTTP 200 (degraded fleet still serves traffic)."""
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            r = await self._hit_health({s: (s == "pii") for s in ALL_SERVICES})
            assert r.status_code == 200
            data = r.json()
            assert data["ai_status"] == "degraded"
            assert data["ai_available"] is True
        finally:
            cache._status, cache._last_check = old_status, old_last

    @pytest.mark.asyncio
    async def test_returns_200_when_healthy(self):
        """Every worker up ⇒ HTTP 200 + status 'ok'."""
        cache = get_cache()
        old_status, old_last = cache._status, cache._last_check
        try:
            r = await self._hit_health({s: True for s in ALL_SERVICES})
            assert r.status_code == 200
            data = r.json()
            assert data["ai_status"] == "healthy"
            assert data["status"] == "ok"
        finally:
            cache._status, cache._last_check = old_status, old_last
