"""Base RebaseKit worker client."""
import asyncio
import time
from typing import Any
import httpx
import structlog
from core.config import get_settings

logger = structlog.get_logger()
settings = get_settings()

# Transient HTTP status codes worth retrying
_RETRYABLE_STATUS_CODES = {502, 503, 504, 429}

# Default retry configuration
MAX_RETRIES = 2          # total attempts = MAX_RETRIES + 1
RETRY_BASE_DELAY = 1.0   # seconds; actual delay = base * 2^attempt (capped at 8s)
RETRY_MAX_DELAY = 8.0


class WorkerError(Exception):
    def __init__(self, message: str, status_code: int = 500, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class RebaseKitClient:
    """Thin async HTTP client for RebaseKit APIs with automatic retry."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            api_key = settings.rebasekit_api_key
            headers: dict[str, str] = {"X-Client": "crowdsourcerer/0.1.0"}
            if api_key:
                # Only set Authorization header when key is present.
                # An empty key produces "Bearer " (trailing space) which h11
                # rejects as an illegal header value → LocalProtocolError.
                headers["Authorization"] = f"Bearer {api_key}"
            self._client = httpx.AsyncClient(
                base_url=settings.rebasekit_base_url,
                headers=headers,
                timeout=120.0,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        # Fail fast with a clear error if the API key is not configured.
        if not settings.rebasekit_api_key:
            raise WorkerError(
                "RebaseKit API key not configured. Set REBASEKIT_API_KEY in environment.",
                status_code=503,
            )
        t0 = time.perf_counter()
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                r = await self.client.post(path, json=json)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in _RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    logger.warning(
                        "rebasekit_retry",
                        path=path,
                        status=status,
                        attempt=attempt + 1,
                        delay_s=delay,
                    )
                    last_exc = e
                    await asyncio.sleep(delay)
                    continue
                logger.error("rebasekit_error", path=path, status=status)
                raise WorkerError(
                    f"RebaseKit API error: {status}",
                    status_code=502,
                    details=e.response.text,
                )
            except httpx.TimeoutException:
                if attempt < MAX_RETRIES:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    logger.warning(
                        "rebasekit_retry",
                        path=path,
                        reason="timeout",
                        attempt=attempt + 1,
                        delay_s=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise WorkerError("RebaseKit API timed out", status_code=504)
            except httpx.RequestError as e:
                if attempt < MAX_RETRIES:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    logger.warning(
                        "rebasekit_retry",
                        path=path,
                        reason=type(e).__name__,
                        attempt=attempt + 1,
                        delay_s=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                # Covers LocalProtocolError, ConnectError, RemoteProtocolError, etc.
                logger.error("rebasekit_request_error", path=path, error=type(e).__name__)
                raise WorkerError(
                    f"RebaseKit connection error: {type(e).__name__}",
                    status_code=502,
                )
            finally:
                if attempt == MAX_RETRIES or last_exc is None:
                    elapsed = (time.perf_counter() - t0) * 1000
                    logger.debug("rebasekit_call", path=path, duration_ms=round(elapsed))

        # Should be unreachable, but just in case
        raise WorkerError("RebaseKit API call failed after retries", status_code=502)  # pragma: no cover


# Global client instance
_client = RebaseKitClient()


def get_rebasekit_client() -> RebaseKitClient:
    return _client
