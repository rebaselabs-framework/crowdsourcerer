"""Tests for webhook URL SSRF validation (core/url_validation.py).

Verifies that:
1. HTTPS URLs to public hosts are allowed.
2. HTTP URLs are blocked in non-debug mode.
3. Private/internal IPs are blocked (127.x, 10.x, 172.16.x, 192.168.x).
4. Cloud metadata endpoints are blocked (169.254.169.254).
5. Known dangerous hostnames are blocked.
6. Invalid URLs (missing scheme, no host) are rejected.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

from unittest.mock import patch
import pytest

from core.url_validation import validate_webhook_url, UnsafeURLError


# ── Helper to mock DNS resolution ─────────────────────────────────────────────

def _mock_getaddrinfo(host_to_ip_map):
    """Return a mock getaddrinfo that maps hostnames to IPs."""
    import socket

    def _getaddrinfo(hostname, port, *args, **kwargs):
        ip = host_to_ip_map.get(hostname)
        if ip is None:
            raise socket.gaierror(f"Name resolution failed: {hostname}")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 443))]

    return _getaddrinfo


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestURLValidation:

    def test_https_public_host_allowed(self):
        """HTTPS URL to a public IP should be allowed."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"example.com": "93.184.216.34"})):
            result = validate_webhook_url("https://example.com/webhook")
            assert result == "https://example.com/webhook"

    def test_http_allowed_in_debug_mode(self):
        """HTTP is allowed when DEBUG=true."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"example.com": "93.184.216.34"})):
            result = validate_webhook_url("http://example.com/webhook")
            assert result == "http://example.com/webhook"

    def test_http_blocked_in_production(self):
        """HTTP is blocked when not in debug mode."""
        from core.config import Settings
        prod_settings = Settings(debug=False, jwt_secret="prod-secret", api_key_salt="prod-salt")
        with patch("core.url_validation.get_settings", return_value=prod_settings):
            with pytest.raises(UnsafeURLError, match="HTTPS"):
                validate_webhook_url("http://example.com/webhook")

    def test_loopback_blocked(self):
        """127.0.0.1 and other loopback addresses are blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"localhost": "127.0.0.1"})):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://localhost/webhook")

    def test_loopback_ip_blocked(self):
        """Direct 127.x.x.x IP in URL is blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"127.0.0.1": "127.0.0.1"})):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://127.0.0.1/webhook")

    def test_private_10_network_blocked(self):
        """10.0.0.0/8 addresses are blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"internal.corp": "10.0.1.50"})):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://internal.corp/webhook")

    def test_private_172_network_blocked(self):
        """172.16.0.0/12 addresses are blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"docker.local": "172.17.0.1"})):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://docker.local/webhook")

    def test_private_192_168_blocked(self):
        """192.168.0.0/16 addresses are blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"router.local": "192.168.1.1"})):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://router.local/webhook")

    def test_cloud_metadata_ip_blocked(self):
        """169.254.169.254 (cloud metadata) is blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"169.254.169.254": "169.254.169.254"})):
            with pytest.raises(UnsafeURLError, match="[Mm]etadata|private"):
                validate_webhook_url("https://169.254.169.254/latest/meta-data/")

    def test_cloud_metadata_hostname_blocked(self):
        """metadata.google.internal is blocked."""
        with pytest.raises(UnsafeURLError, match="not allowed"):
            validate_webhook_url("https://metadata.google.internal/computeMetadata/")

    def test_link_local_blocked(self):
        """169.254.x.x (link-local) addresses are blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"local.weird": "169.254.1.1"})):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://local.weird/webhook")

    def test_ftp_scheme_blocked(self):
        """Non-HTTP(S) schemes are blocked."""
        with pytest.raises(UnsafeURLError, match="HTTPS"):
            validate_webhook_url("ftp://example.com/webhook")

    def test_no_scheme_blocked(self):
        """URLs without a scheme are blocked."""
        with pytest.raises(UnsafeURLError, match="HTTPS"):
            validate_webhook_url("example.com/webhook")

    def test_no_hostname_blocked(self):
        """URLs with no hostname are blocked."""
        with pytest.raises(UnsafeURLError):
            validate_webhook_url("https:///path")

    def test_dns_failure_blocked(self):
        """URLs that fail DNS resolution are blocked."""
        import socket
        with patch("core.url_validation.socket.getaddrinfo",
                   side_effect=socket.gaierror("nxdomain")):
            with pytest.raises(UnsafeURLError, match="resolve"):
                validate_webhook_url("https://nonexistent.invalid/webhook")

    def test_shared_address_space_blocked(self):
        """100.64.0.0/10 (CGN) addresses are blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"cgn.isp": "100.64.0.1"})):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://cgn.isp/webhook")

    def test_zero_network_blocked(self):
        """0.0.0.0/8 addresses are blocked."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"zero": "0.0.0.1"})):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://zero/webhook")

    def test_multiple_ips_all_checked(self):
        """If DNS returns multiple IPs, all must be public."""
        import socket

        def _multi_resolve(hostname, port, *args, **kwargs):
            # First IP is public, second is private
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443)),
            ]

        with patch("core.url_validation.socket.getaddrinfo", _multi_resolve):
            with pytest.raises(UnsafeURLError, match="private"):
                validate_webhook_url("https://dual.example.com/webhook")

    def test_public_ip_direct_allowed(self):
        """Direct public IP in URL should be allowed."""
        with patch("core.url_validation.socket.getaddrinfo",
                   _mock_getaddrinfo({"8.8.8.8": "8.8.8.8"})):
            result = validate_webhook_url("https://8.8.8.8/webhook")
            assert result == "https://8.8.8.8/webhook"
