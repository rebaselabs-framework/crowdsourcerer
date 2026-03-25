"""Base RebaseKit worker client."""
from __future__ import annotations
import time
from typing import Any
import httpx
import structlog
from core.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


class WorkerError(Exception):
    def __init__(self, message: str, status_code: int = 500, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class RebaseKitClient:
    """Thin async HTTP client for RebaseKit APIs."""

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
        try:
            r = await self.client.post(path, json=json)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            logger.error("rebasekit_error", path=path, status=e.response.status_code)
            raise WorkerError(
                f"RebaseKit API error: {e.response.status_code}",
                status_code=502,
                details=e.response.text,
            )
        except httpx.TimeoutException:
            raise WorkerError("RebaseKit API timed out", status_code=504)
        except httpx.RequestError as e:
            # Covers LocalProtocolError, ConnectError, RemoteProtocolError, etc.
            logger.error("rebasekit_request_error", path=path, error=type(e).__name__)
            raise WorkerError(
                f"RebaseKit connection error: {type(e).__name__}",
                status_code=502,
            )
        finally:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.debug("rebasekit_call", path=path, duration_ms=round(elapsed))


# Global client instance
_client = RebaseKitClient()


def get_rebasekit_client() -> RebaseKitClient:
    return _client
