"""CrowdSorcerer Python SDK."""

__version__ = "1.0.0"
__all__ = ["CrowdSorcerer", "AsyncCrowdSorcerer", "CrowdSorcererError", "AuthError", "RateLimitError", "InsufficientCreditsError", "TaskError"]

from .client import CrowdSorcerer
from .async_client import AsyncCrowdSorcerer
from .errors import (
    CrowdSorcererError,
    AuthError,
    RateLimitError,
    InsufficientCreditsError,
    TaskError,
)
