"""Health check polling for upstream servers."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 0.5   # seconds between polls
DEFAULT_TIMEOUT = 120         # total timeout in seconds


async def wait_for_ready(
    port: int,
    *,
    interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    client: aiohttp.ClientSession | None = None,
) -> None:
    """Poll an upstream server until it reports healthy.

    Tries /health first, falls back to /v1/models.

    Args:
        port: Upstream port to poll.
        interval: Seconds between polls.
        timeout: Total timeout in seconds.
        client: Optional shared client session. If None, creates a temporary one.

    Raises:
        TimeoutError: if the server does not become ready within timeout.
    """
    base = f"http://127.0.0.1:{port}"
    endpoints = ["/health", "/v1/models"]

    own_client = client is None
    if client is None:
        timeout_cfg = aiohttp.ClientTimeout(total=5)
        client = aiohttp.ClientSession(timeout=timeout_cfg)

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for ep in endpoints:
                url = base + ep
                try:
                    async with client.get(url, ssl=False) as resp:
                        if resp.status == 200:
                            logger.info(
                                "Upstream on port %d is ready (%s)", port, ep
                            )
                            return
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    ConnectionError,
                    OSError,
                ):
                    pass  # not ready yet or endpoint not supported

            if loop.time() > deadline:
                raise TimeoutError(
                    f"Upstream on port {port} did not become ready "
                    f"within {timeout}s"
                )

            await asyncio.sleep(interval)
    finally:
        if own_client:
            await client.close()
