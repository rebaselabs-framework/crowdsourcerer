"""Webhook URL validation — prevents SSRF attacks.

Validates that webhook destination URLs:
1. Use HTTPS (or HTTP in debug mode only)
2. Don't resolve to private/internal IP ranges
3. Don't target cloud metadata services
4. Don't use non-standard ports commonly used for internal services
"""

import ipaddress
import socket
from urllib.parse import urlparse

from core.config import get_settings


class UnsafeURLError(ValueError):
    """Raised when a URL fails SSRF validation."""
    pass


# IP ranges that must never be targeted by outbound webhooks
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),        # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),      # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),     # RFC 1918
    ipaddress.ip_network("127.0.0.0/8"),        # Loopback
    ipaddress.ip_network("169.254.0.0/16"),     # Link-local / cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),          # "This" network
    ipaddress.ip_network("100.64.0.0/10"),      # Shared address space (CGN)
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),      # Benchmarking
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
    ipaddress.ip_network("::ffff:127.0.0.0/104"),  # IPv4-mapped loopback
    ipaddress.ip_network("::ffff:10.0.0.0/104"),   # IPv4-mapped RFC 1918
    ipaddress.ip_network("::ffff:172.16.0.0/108"), # IPv4-mapped RFC 1918
    ipaddress.ip_network("::ffff:192.168.0.0/112"),# IPv4-mapped RFC 1918
    ipaddress.ip_network("::ffff:169.254.0.0/112"),# IPv4-mapped link-local
]

# Hostnames that must be blocked regardless of resolution
_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.internal",
    "instance-data",
}


def validate_webhook_url(url: str) -> str:
    """Validate and return the URL if safe for outbound webhook delivery.

    Raises UnsafeURLError if the URL could be used for SSRF.
    Returns the normalized URL string on success.
    """
    settings = get_settings()

    # Parse URL
    try:
        parsed = urlparse(url)
    except Exception:
        raise UnsafeURLError("Invalid URL format")

    # Scheme check
    allowed_schemes = {"https"}
    if settings.debug:
        allowed_schemes.add("http")
    if parsed.scheme not in allowed_schemes:
        raise UnsafeURLError(
            f"URL scheme must be HTTPS"
            + (" (or HTTP in debug mode)" if settings.debug else "")
        )

    # Must have a hostname
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL must have a hostname")

    # Block known dangerous hostnames
    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTNAMES:
        raise UnsafeURLError("This hostname is not allowed for webhook endpoints")

    # Block cloud metadata IP directly in URL
    if hostname_lower in ("169.254.169.254", "metadata.google.internal"):
        raise UnsafeURLError("Cloud metadata endpoints are not allowed")

    # Resolve hostname to IP and check against blocked ranges
    try:
        # Resolve all addresses (IPv4 and IPv6)
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeURLError(f"Cannot resolve hostname: {hostname}")

    for family, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise UnsafeURLError(
                    "Webhook URL resolves to a private/internal IP address"
                )

    return url
