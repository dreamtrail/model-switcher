"""Swap orchestration — stop/start models and queue requests."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import suppress
from pathlib import Path

import aiohttp
from aiohttp import web

from src.config import Config
from src.health import wait_for_ready
from src.port_rewrite import create_temp_dir, cleanup_temp_dir, rewrite_command
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
    inject_stream_options,
    proxy_request,
    stream_proxy,
)
from src.stats import STATS_HTML, Outcome, StatsCollector

logger = logging.getLogger(__name__)

MAX_QUEUE = 10  # max pending requests during swap


class Swapper:
    """Coordinates model swapping and request routing."""

    def __init__(
        self,
        config: Config,
        *,
        timeout: float = 60,
        idle_timeout: float = 900,
        client: aiohttp.ClientSession | None = None,
        stats: StatsCollector | None = None,
    ):
        self.config = config
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self._client = client
        self.stats = stats or StatsCollector()
        self._current: ProcessHandle | None = None
        self._current_model: str | None = None
        self._healthy = False
        self._lock = asyncio.Lock()
        self._queue: deque[asyncio.Future] = deque()
        self._swapping = False
        self._idle_task: asyncio.Task | None = None
        self._idle_timer_expired = False
        self._idle_generation = 0
        self._active_requests = 0
        self._temp_dir: Path | None = None

        # Create temp dir for rewritten script copies
        self._temp_dir = create_temp_dir()
        # Rewrite all model commands to use auto-assigned ports
        self._rewrite_commands()

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

        # Stop current model if different. Call stop_process even if the
        # tracked parent has exited; wrapper children may still be alive.
        swap_started: float | None = None
        if self._current:
            self._idle_generation += 1
            self._cancel_idle_timer()
            logger.info(
                "Swapping from %s to %s", self._current_model, model_name
            )
            swap_started = time.monotonic()
            self.stats.record_model_stop(self._current_model)
            await stop_process(self._current)
            self._current = None
            self._current_model = None
            self._healthy = False

        # Start new model
        logger.info("Starting model %s", model_name)
        handle = await start_process(model_name, mc.cmd, mc.port)
        self.stats.record_model_start(model_name)
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
            if swap_started is not None:
                self.stats.record_swap(time.monotonic() - swap_started)
        except TimeoutError:
            logger.error(
                "Model %s on port %d did not become ready",
                model_name,
                handle.port,
            )
            self.stats.record_model_stop(model_name)
            await stop_process(handle)
            self._current = None
            self._current_model = None
            self._healthy = False
            raise
        except asyncio.CancelledError:
            await stop_process(handle)
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

        # Statistics endpoints (switcher-local, never proxied upstream)
        if request.method == "GET":
            if path == "/stats/json":
                return web.json_response(
                    self.stats.snapshot(
                        current_model=self._current_model,
                        inflight=self._active_requests,
                    )
                )
            if path == "/stats":
                return web.Response(
                    text=STATS_HTML, content_type="text/html"
                )

        # Read body once (needed for model extraction + upstream forwarding)
        body = await request.read()

        # Extract model from request
        model_name = extract_model(request, body)

        if model_name is None:
            async with self._lock:
                if self._current and is_running(self._current) and self._healthy:
                    handle = self._current
                    active_model = self._current_model or "active"
                    self._begin_activity()
                else:
                    handle = None

            if handle is not None:
                return await self._proxy_with_recorded_activity(
                    request,
                    active_model,
                    body,
                    handle,
                )
            self.stats.record_rejection("400")
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
                self.stats.record_rejection("503", model_name)
                return web.json_response(
                    _openai_error("Swap in progress, try again shortly"),
                    status=503,
                )
            # After swap completes, ensure correct model for this request.
            # Failure here must not escape as a bare 500 — mirror the
            # primary path's error responses.
            async with self._lock:
                try:
                    handle = await self._ensure_model(model_name)
                except ValueError:
                    self.stats.record_rejection("404", model_name)
                    return web.json_response(
                        _openai_error(f"Unknown model: {model_name}"),
                        status=404,
                    )
                except TimeoutError:
                    self.stats.record_rejection("504", model_name)
                    return web.json_response(
                        _openai_error("Model swap timed out", "swap_timeout"),
                        status=504,
                    )
                self._begin_activity()
            return await self._proxy_with_recorded_activity(
                request, model_name, body, handle
            )

        # Acquire lock and perform swap if needed
        async with self._lock:
            self._swapping = True
            try:
                handle = await self._ensure_model(model_name)
                self._begin_activity()
            except ValueError:
                self._swapping = False
                await self._drain_queue()
                self.stats.record_rejection("404", model_name)
                return web.json_response(
                    _openai_error(f"Unknown model: {model_name}"),
                    status=404,
                )
            except TimeoutError:
                self._swapping = False
                await self._drain_queue()
                self.stats.record_rejection("504", model_name)
                return web.json_response(
                    _openai_error("Model swap timed out", "swap_timeout"),
                    status=504,
                )
            finally:
                self._swapping = False
                await self._drain_queue()

        return await self._proxy_with_recorded_activity(
            request, model_name, body, handle
        )

    def _begin_activity(self) -> None:
        """Mark a model request active and cancel pending idle unload."""
        self._active_requests += 1
        self._idle_generation += 1
        self._cancel_idle_timer()
        self.stats.observe_inflight(self._active_requests)

    def _end_activity(self) -> None:
        """Mark a model request complete and arm idle unload if needed."""
        self._active_requests -= 1
        self._schedule_idle_timer()

    async def _proxy_with_recorded_activity(
        self,
        request: web.Request,
        model_name: str,
        body: bytes | None,
        handle: ProcessHandle,
    ) -> web.Response:
        """Proxy a request already recorded as active model usage."""
        started = time.monotonic()
        outcome = Outcome()
        resp: web.Response | None = None
        try:
            resp = await self._proxy_for_model(
                request, model_name, body, handle, outcome=outcome,
            )
            return resp
        finally:
            self._end_activity()
            ended = time.monotonic()
            latency_s = ended - started
            if resp is None:
                # Exception escaped the proxy call — no response produced.
                ok, kind = False, "error"
            elif outcome.stream_error:
                ok, kind = False, "stream_error"
            elif resp.status >= 400:
                ok, kind = False, str(resp.status)
            else:
                ok, kind = True, None
            # Streaming-only timing: from chokepoint start to the first
            # content event (TTFT) and from there to stream end (decode).
            ttft_s = decode_s = None
            if ok and outcome.first_token_at is not None:
                ttft_s = outcome.first_token_at - started
                decode_s = ended - outcome.first_token_at
            self.stats.record_request(
                model_name,
                ok=ok,
                kind=kind,
                latency_s=latency_s,
                bytes_in=len(body) if body else 0,
                bytes_out=outcome.bytes_out,
                prompt_tokens=outcome.prompt_tokens,
                completion_tokens=outcome.completion_tokens,
                cached_tokens=outcome.cached_tokens,
                ttft_s=ttft_s,
                decode_s=decode_s,
            )

    async def _proxy_for_model(
        self,
        request: web.Request,
        model_name: str,
        body: bytes | None,
        handle: ProcessHandle | None = None,
        outcome: Outcome | None = None,
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
        data = None
        try:
            if body:
                data = json.loads(body)
                is_stream = data.get("stream", False)
        except (json.JSONDecodeError, TypeError):
            pass

        if is_stream:
            body = inject_stream_options(request, body, data)
            return await stream_proxy(
                request, upstream, body,
                timeout=self.timeout, client=self._client,
                outcome=outcome,
            )
        return await proxy_request(
            request, upstream, body,
            timeout=self.timeout, client=self._client,
            outcome=outcome,
        )

    def _schedule_idle_timer(self) -> None:
        """Arm idle unload when enabled and no model requests are active."""
        if self.idle_timeout <= 0 or self._active_requests > 0:
            return
        if not (self._current and self._healthy and is_running(self._current)):
            return

        self._idle_generation += 1
        generation = self._idle_generation
        self._cancel_idle_timer()
        self._idle_timer_expired = False
        self._idle_task = asyncio.create_task(
            self._idle_unload_after_timeout(generation),
            name="idle-unload",
        )

    def _cancel_idle_timer(self, *, force: bool = False) -> asyncio.Task | None:
        """Cancel the pending idle timer if it has not started unloading."""
        task = self._idle_task
        if task is None:
            return None
        if task.done():
            self._idle_task = None
            return None
        if force or not self._idle_timer_expired:
            self._idle_task = None
            task.cancel()
            return task
        return None

    async def _idle_unload_after_timeout(self, generation: int) -> None:
        """Stop the active model when it has been idle long enough."""
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self.idle_timeout)
            self._idle_timer_expired = True
            async with self._lock:
                if generation != self._idle_generation:
                    return
                if self._active_requests > 0:
                    return
                if not (
                    self._current and self._healthy and is_running(self._current)
                ):
                    return

                handle = self._current
                model_name = self._current_model
                logger.info(
                    "Idle timeout reached for %s; unloading model",
                    model_name,
                )
                self.stats.record_model_stop(model_name)
                await stop_process(handle)
                self._current = None
                self._current_model = None
                self._healthy = False
        except asyncio.CancelledError:
            pass
        finally:
            self._idle_timer_expired = False
            if self._idle_task is current_task:
                self._idle_task = None

    async def shutdown(self) -> None:
        """Stop the currently running model, close client, clean temp dir."""
        idle_task = self._cancel_idle_timer(force=True)
        if idle_task:
            with suppress(asyncio.CancelledError):
                await idle_task

        # Drain queued requests so waiters don't hang forever
        await self._drain_queue()
        if self._current:
            await stop_process(self._current)
        self._current = None
        self._current_model = None
        self._healthy = False

        if self._client:
            await self._client.close()
            self._client = None

        # Clean up temp scripts
        if self._temp_dir:
            cleanup_temp_dir(self._temp_dir)
            self._temp_dir = None

    def _rewrite_commands(self) -> None:
        """Rewrite all model commands to use auto-assigned ports.

        For script files, copies to temp dir and rewrites port flags.
        For direct commands, replaces port flags in the command string.
        """
        if self._temp_dir is None:
            return
        for mc in self.config.models.values():
            rewritten_cmd, _temp_path = rewrite_command(
                mc.cmd, mc.port, temp_dir=self._temp_dir
            )
            mc.cmd = rewritten_cmd
