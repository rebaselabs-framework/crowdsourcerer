"""Tests for core/observability.py — Sentry init + PII filter."""

from unittest.mock import MagicMock, patch

import pytest

from core.observability import _before_send, _SILENCED_PATHS, init_sentry


def _settings(dsn: str = "") -> MagicMock:
    s = MagicMock()
    s.sentry_dsn = dsn
    s.sentry_environment = "test"
    s.sentry_traces_sample_rate = 0.0
    s.sentry_profiles_sample_rate = 0.0
    s.app_version = "0.1.0"
    return s


class TestInitSentry:
    def test_noop_when_dsn_empty(self):
        """Empty DSN → no SDK import, no init call, returns False."""
        with patch("sentry_sdk.init") as mock_init:
            assert init_sentry(_settings("")) is False
            mock_init.assert_not_called()

    def test_initialises_with_dsn(self):
        """Non-empty DSN → sentry_sdk.init called with the configured fields."""
        with patch("sentry_sdk.init") as mock_init:
            assert init_sentry(_settings("https://public@sentry.invalid/1")) is True
            assert mock_init.called
            kwargs = mock_init.call_args.kwargs
            assert kwargs["dsn"] == "https://public@sentry.invalid/1"
            assert kwargs["environment"] == "test"
            assert kwargs["release"] == "crowdsourcerer@0.1.0"
            assert kwargs["send_default_pii"] is False

    def test_passes_sample_rates(self):
        with patch("sentry_sdk.init") as mock_init:
            s = _settings("https://x@s.invalid/1")
            s.sentry_traces_sample_rate = 0.25
            s.sentry_profiles_sample_rate = 0.10
            init_sentry(s)
            kwargs = mock_init.call_args.kwargs
            assert kwargs["traces_sample_rate"] == 0.25
            assert kwargs["profiles_sample_rate"] == 0.10


class TestBeforeSendFilter:
    """_before_send drops events from high-volume health-probe routes."""

    @pytest.mark.parametrize("path", sorted(_SILENCED_PATHS))
    def test_drops_silenced_paths(self, path):
        event = {"request": {"url": f"https://crowdsourcerer.rebaselabs.online{path}"}}
        assert _before_send(event, {}) is None

    def test_keeps_regular_routes(self):
        event = {"request": {"url": "https://crowdsourcerer.rebaselabs.online/v1/tasks"}}
        assert _before_send(event, {}) is event

    def test_keeps_events_without_request(self):
        """Background worker exceptions have no HTTP request context."""
        event = {"level": "error", "message": "worker.dead"}
        assert _before_send(event, {}) is event

    def test_keeps_events_with_empty_url(self):
        event = {"request": {}}
        assert _before_send(event, {}) is event
