"""CrowdSorcerer SDK exceptions."""
from __future__ import annotations

from typing import Optional


class CrowdSorcererError(Exception):
    """Base exception for all CrowdSorcerer SDK errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class AuthError(CrowdSorcererError):
    """Raised when the API key is missing, invalid, or expired."""


class RateLimitError(CrowdSorcererError):
    """Raised when the API rate limit is exceeded (429)."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: Optional[float] = None,
        **kwargs,
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class InsufficientCreditsError(CrowdSorcererError):
    """Raised when the account has insufficient credits for the requested task."""


class TaskError(CrowdSorcererError):
    """Raised when a task fails during execution."""

    def __init__(
        self,
        message: str,
        task_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(message, **kwargs)
        self.task_id = task_id


class NotFoundError(CrowdSorcererError):
    """Raised when the requested resource is not found (404)."""
