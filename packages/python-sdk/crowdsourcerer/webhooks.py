"""Webhook signature verification utilities.

Every webhook delivery from CrowdSorcerer includes an
``X-Crowdsorcerer-Signature`` header in the format ``t=TIMESTAMP,v1=HMAC_HEX``.
Use :func:`verify_webhook` to validate that a delivery is authentic and recent.

Example::

    from crowdsourcerer import verify_webhook

    # In your Flask / FastAPI handler:
    sig = request.headers["X-Crowdsorcerer-Signature"]
    body = request.get_data()  # raw bytes
    if not verify_webhook(body, YOUR_ENDPOINT_SECRET, sig):
        abort(401, "Invalid signature")
"""

from __future__ import annotations

import hashlib
import hmac
import time


def verify_webhook(
    payload: bytes,
    secret: str,
    signature_header: str,
    *,
    tolerance: int = 300,
) -> bool:
    """Verify a CrowdSorcerer webhook signature.

    Args:
        payload: The raw request body as bytes.
        secret: Your webhook endpoint signing secret (shown once on creation).
        signature_header: Value of the ``X-Crowdsorcerer-Signature`` header.
        tolerance: Maximum age of the delivery in seconds (default: 300).
            Set to ``0`` to skip timestamp checking (not recommended).

    Returns:
        ``True`` if the signature is valid and the timestamp is within
        *tolerance* seconds of the current time.

    Raises:
        ValueError: If *payload* is not ``bytes`` or *secret* is empty.

    Example::

        from crowdsourcerer import verify_webhook

        is_valid = verify_webhook(
            payload=request.get_data(),
            secret="your_endpoint_secret",
            signature_header=request.headers["X-Crowdsorcerer-Signature"],
        )
    """
    if not isinstance(payload, bytes):
        raise ValueError("payload must be bytes — use request.get_data() or similar")
    if not secret:
        raise ValueError("secret must not be empty")

    # Parse "t=TIMESTAMP,v1=SIGNATURE[,v0=OLD_SIGNATURE]"
    parts: dict[str, str] = {}
    for segment in signature_header.split(","):
        eq_idx = segment.find("=")
        if eq_idx == -1:
            continue
        key = segment[:eq_idx].strip()
        value = segment[eq_idx + 1 :].strip()
        parts[key] = value

    timestamp = parts.get("t")
    v1_sig = parts.get("v1")
    if not timestamp or not v1_sig:
        return False

    # Replay protection: reject old deliveries
    if tolerance > 0:
        try:
            ts_int = int(timestamp)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > tolerance:
            return False

    # Reconstruct the signed payload: "{timestamp}.{body}"
    sig_input = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), sig_input, hashlib.sha256).hexdigest()

    # Constant-time comparison
    return hmac.compare_digest(expected, v1_sig)


def verify_webhook_with_rotation(
    payload: bytes,
    current_secret: str,
    previous_secret: str | None,
    signature_header: str,
    *,
    tolerance: int = 300,
) -> bool:
    """Verify a webhook during a secret rotation window.

    During rotation, CrowdSorcerer sends both ``v1`` (new secret) and
    ``v0`` (old secret) signatures for 24 hours. This function checks
    the current secret first, then falls back to the previous secret.

    Args:
        payload: The raw request body as bytes.
        current_secret: The new (current) webhook signing secret.
        previous_secret: The previous secret (if rotating), or ``None``.
        signature_header: Value of the ``X-Crowdsorcerer-Signature`` header.
        tolerance: Maximum age in seconds (default: 300).

    Returns:
        ``True`` if either the ``v1`` or ``v0`` signature is valid.
    """
    # Try current secret against v1
    if verify_webhook(payload, current_secret, signature_header, tolerance=tolerance):
        return True

    # During rotation, try previous secret against v0
    if previous_secret:
        parts: dict[str, str] = {}
        for segment in signature_header.split(","):
            eq_idx = segment.find("=")
            if eq_idx == -1:
                continue
            key = segment[:eq_idx].strip()
            value = segment[eq_idx + 1 :].strip()
            parts[key] = value

        v0_sig = parts.get("v0")
        timestamp = parts.get("t")
        if v0_sig and timestamp:
            if tolerance > 0:
                try:
                    ts_int = int(timestamp)
                except ValueError:
                    return False
                if abs(time.time() - ts_int) > tolerance:
                    return False

            sig_input = f"{timestamp}.".encode() + payload
            expected = hmac.new(
                previous_secret.encode(), sig_input, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, v0_sig)

    return False
