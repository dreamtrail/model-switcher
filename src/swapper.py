"""Swap orchestration — stop/start models and queue requests."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

import aiohttp
from aiohttp import web

from src.config import Config, ModelConfig
from src.health import wait_for_ready
from src.process_manager import (
    ProcessHandle,
    is_running,
    start_process,
    stop_process,
)
from src.proxy import (
    _openai_error,
    extract_model,
    handle_models_list,
    proxy_request,
    stream_proxy,
)

logger = logging.getLogger(__name__)

MAX_QUEUE = 10  # max pending requests during swap


class Swapper:
    """Coordinates model swapping and request routing."""

    def __init__(
        self,
        config: Config,
        *,
        timeout: float = 60,
        client: aiohttp.ClientSession | None = None,
    ):
        self.config = config
        self.timeout = timeout
        self._client = client
        self._current: ProcessHandle | None = None
        self._current_model: str | None = None
        self._healthy = False
        self._lock = asyncio.Lock()
        self._queue: deque[asyncio.Future] = deque()
        self._swapping = False

    @property
    def active_model(self) -> str | None:
        """Name of the currently active model, or None."""
        if self._current and is_running(self._current) and self._healthy:
            return self._current_model
        return None

    @property
    def active_port(self) -> int | None:
        """Port of the currently active model, or None."""
        if self._current and is_running(self._current) and self._healthy:
            return self._current.port
        return None

    async def _ensure_model(self, model_name: str) -> ProcessHandle:
        """Ensure the named model is running and healthy.

        All state mutation and health checking happens inside the lock
        to prevent concurrent requests from seeing inconsistent state.
        """
        # Already running the right, healthy model
        if (
            self._current_model == model_name
            and self._current is not None
            and is_running(self._current)
            and self._healthy
        ):
            return self._current

        mc = self.config.models.get(model_name)
        if mc is None:
            raise ValueError(f"Unknown model: {model_name}")

        # Stop current model if different
        if self._current and is_running(self._current):
            logger.info(
                "Swapping from %s to %s", self._current_model, model_name
            )
            await stop_process(self._current)
            self._current = None
            self._current_model = None
            self._healthy = False

        # Start new model
        logger.info("Starting model %s", model_name)
        handle = await start_process(model_name, mc.cmd, mc.port)
        self._current = handle
        self._current_model = model_name
        self._healthy = False

        # Health check inside the lock — prevents concurrent requests
        # from seeing a started-but-not-ready process.
        try:
            await wait_for_ready(
                handle.port,
                timeout=self.timeout,
                client=self._client,
            )
            self._healthy = True
        except TimeoutError:
            logger.error(
                "Model %s on port %d did not become ready",
                model_name,
                handle.port,
            )
            self._current = None
            self._current_model = None
            self._healthy = False
            raise

        return handle

    def _queue_request(self) -> asyncio.Future:
        """Queue a request during swap. Returns a Future to await.

        Raises asyncio.QueueFull if queue is at capacity.
        """
        if len(self._queue) >= MAX_QUEUE:
            raise asyncio.QueueFull(
                f"Swap in progress and request queue is full ({MAX_QUEUE} pending)"
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._queue.append(future)
        return future

    async def _drain_queue(self) -> None:
        """Signal all queued futures so waiters can retry."""
        while self._queue:
            future = self._queue.popleft()
            if not future.done():
                future.set_result(None)

    async def handle_request(
        self, request: web.Request
    ) -> web.Response:
        """Main request handler — route to the correct upstream."""
        path = request.path

        # Static /v1/models endpoint
        if path in ("/v1/models", "/models") and request.method == "GET":
            model_names = list(self.config.models.keys())
            return await handle_models_list(request, model_names)

        # Read body once (needed for model extraction + upstream forwarding)
        body = await request.read()

        # Extract model from request
        model_name = extract_model(request, body)

        if model_name is None:
            if self._current and is_running(self._current) and self._healthy:
                port = self._current.port
                upstream = f"http://127.0.0.1:{port}"
                return await proxy_request(
                    request, upstream, body,
                    timeout=self.timeout, client=self._client,
                )
            return web.json_response(
                _openai_error(
                    "No model specified and no model is currently running"
                ),
                status=400,
            )

        # If a swap is in progress, queue this request
        if self._swapping:
            try:
                future = self._queue_request()
                await future  # wait until swap completes
            except asyncio.QueueFull:
                return web.json_response(
                    _openai_error("Swap in progress, try again shortly"),
                    status=503,
                )
            # After swap completes, ensure correct model for this request
            async with self._lock:
                handle = await self._ensure_model(model_name)
            return await self._proxy_for_model(
                request, model_name, body, handle
            )

        # Acquire lock and perform swap if needed
        async with self._lock:
            self._swapping = True
            try:
                handle = await self._ensure_model(model_name)
            except ValueError:
                self._swapping = False
                await self._drain_queue()
                return web.json_response(
                    _openai_error(f"Unknown model: {model_name}"),
                    status=404,
                )
            except TimeoutError:
                self._swapping = False
                await self._drain_queue()
                return web.json_response(
                    _openai_error("Model swap timed out", "swap_timeout"),
                    status=504,
                )
            finally:
                self._swapping = False
                await self._drain_queue()

        return await self._proxy_for_model(request, model_name, body, handle)

    async def _proxy_for_model(
        self,
        request: web.Request,
        model_name: str,
        body: bytes | None,
        handle: ProcessHandle | None = None,
    ) -> web.Response:
        """Proxy request to the upstream for the given model."""
        if handle is None:
            # Get current handle (after queue drain, model should be ready)
            async with self._lock:
                handle = self._current

        if handle is None or not is_running(handle):
            return web.json_response(
                _openai_error(
                    f"Model '{model_name}' process is not running"
                ),
                status=502,
            )

        port = handle.port
        upstream = f"http://127.0.0.1:{port}"

        # Determine if this is a streaming request (single parse)
        is_stream = False
        try:
            if body:
                data = json.loads(body)
                is_stream = data.get("stream", False)
        except (json.JSONDecodeError, TypeError):
            pass

        if is_stream:
            return await stream_proxy(
                request, upstream, body,
                timeout=self.timeout, client=self._client,
            )
        return await proxy_request(
            request, upstream, body,
            timeout=self.timeout, client=self._client,
        )

    async def shutdown(self) -> None:
        """Stop the currently running model and close shared client."""
        # Drain queued requests so waiters don't hang forever
        await self._drain_queue()
        if self._current and is_running(self._current):
            await stop_process(self._current)
        self._current = None
        self._current_model = None
        self._healthy = False

        if self._client:
            await self._client.close()
            self._client = None
