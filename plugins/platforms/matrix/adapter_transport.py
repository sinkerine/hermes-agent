"""Matrix HTTP sessions and bounded, account-scoped event rate-limit retries."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from email.utils import parsedate_to_datetime

from gateway.platforms.base import SendResult, gateway_trust_env

logger = logging.getLogger(__name__)


def _positive_seconds(value, scale: float = 1.0) -> float:
    try:
        seconds = float(value) / scale
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


async def _retry_delay(response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "")
    header_delay = _positive_seconds(header)
    if header and not header_delay:
        try:
            header_delay = max(0.0, parsedate_to_datetime(header).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        body = await response.json(content_type=None)
    except ValueError:
        body = {}
    body_delay = _positive_seconds(body.get("retry_after_ms"), 1000) if isinstance(body, dict) else 0.0
    return max(header_delay, body_delay) or float(2 ** attempt)


def _rate_limit_error(delay: float):
    from mautrix.errors import MLimitExceeded

    error = MLimitExceeded(429, "Matrix rate limit exceeded: retry after cooldown")
    error.retry_after = delay
    return error


def send_failure(error: Exception) -> SendResult:
    """Keep a rate-limit delay when crossing the adapter's SendResult boundary."""
    return SendResult(success=False, error=str(error), retry_after=getattr(error, "retry_after", None))


class MatrixRateLimitMiddleware:
    """Retry rejected room writes using the same HTTP request/transaction ID.

    A session belongs to one bot account. Its cooldown delays concurrent room
    writes, while reads (especially sync and control messages) remain responsive.
    """

    MAX_RETRIES = 3
    MAX_WAIT_SECONDS = 30.0

    def __init__(self, *, clock=None, sleep=None):
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._retry_at = 0.0

    async def _wait(self, deadline: float) -> None:
        while (delay := self._retry_at - self._clock()) > 0:
            if self._retry_at > deadline:
                raise _rate_limit_error(delay)
            await self._sleep(delay)

    async def __call__(self, request, handler):
        # Only static room-event writes are replayable and share rc_message.
        path = request.url.path
        room_write = (request.method == "PUT" and "/rooms/" in path
                      and any(part in path for part in ("/send/", "/state/", "/redact/")))
        if not room_write:
            return await handler(request)
        deadline = self._clock() + self.MAX_WAIT_SECONDS
        for attempt in range(self.MAX_RETRIES + 1):
            await self._wait(deadline)
            response = await handler(request)
            if response.status != 429:
                return response
            try:
                delay = await _retry_delay(response, attempt)
            finally:
                response.release()
            self._retry_at = max(self._retry_at, self._clock() + delay)
            if attempt == self.MAX_RETRIES or self._retry_at > deadline:
                raise _rate_limit_error(delay)
            logger.warning("Matrix: HTTP 429 on room write; retry %d/%d after %.2fs",
                           attempt + 1, self.MAX_RETRIES, delay)


def create_matrix_session(proxy_url: str | None):
    """Apply proxies and rate-limit retries to mautrix's shared HTTP session."""
    import aiohttp

    kwargs = {"middlewares": (MatrixRateLimitMiddleware(),)}
    if not proxy_url:
        return aiohttp.ClientSession(trust_env=gateway_trust_env(), **kwargs)
    if proxy_url.split("://")[0].lower().startswith("socks"):
        try:
            from aiohttp_socks import ProxyConnector
            return aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy_url, rdns=True), **kwargs)
        except ImportError:
            logger.warning(
                "aiohttp_socks not installed — SOCKS proxy %s ignored. Run: pip install aiohttp-socks", proxy_url)
            return aiohttp.ClientSession(trust_env=gateway_trust_env(), **kwargs)
    return aiohttp.ClientSession(proxy=proxy_url, **kwargs)
