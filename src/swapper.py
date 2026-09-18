"""Swap orchestration — stop/start models and queue requests."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web

from src.config import Config
from src.health import wait_for_ready
from src.port_rewrite import create_temp_dir, cleanup_temp_dir, rewrite_command
from src.process_manager import (
    ProcessHandle,
    ProcessStopError,
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


@dataclass(frozen=True)
class _LoadOutcome:
    kind: str
    handle: ProcessHandle | None = None


@dataclass(frozen=True)
class _Waiter:
    model_name: str
    generation: int


class _SwapSuperseded(Exception):
    pass


class _ShuttingDown(Exception):
    pass


class _ModelStartFailed(Exception):
    pass


class _CleanupFailed(Exception):
    pass


_OWNER = object()


class Swapper:
    """Coordinates model swapping and request routing."""

    def __init__(
        self,
        config: Config,
        *,
        timeout: float = 60,
        idle_timeout: float = 900,
        quick_switch: bool = False,
        client: aiohttp.ClientSession | None = None,
        stats: StatsCollector | None = None,
    ):
        self.config = config
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.quick_switch = quick_switch
        self._client = client
        self.stats = stats or StatsCollector()
        self._current: ProcessHandle | None = None
        self._current_model: str | None = None
        self._healthy = False
        self._lock = asyncio.Lock()
        self._queue: deque[asyncio.Future] = deque()
        self._waiters: dict[asyncio.Future, _Waiter] = {}
        self._swapping = False
        self._intent_generation = 0
        self._desired_model: str | None = None
        self._spawn_task: asyncio.Task | None = None
        self._readiness_task: asyncio.Task | None = None
        self._readiness_model: str | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._cancelled_owner_outcome: _LoadOutcome | None = None
        self._shutting_down = False
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

    def _intent_outcome(self, generation: int) -> _LoadOutcome | None:
        """Return the terminal intent outcome, if this generation is stale."""
        if self._shutting_down:
            return _LoadOutcome("shutting_down")
        if generation != self._intent_generation:
            return _LoadOutcome("swap_superseded")
        return None

    def _raise_for_intent(self, generation: int, *, managed: bool) -> None:
        if not managed:
            return
        outcome = self._intent_outcome(generation)
        if outcome is None:
            return
        if outcome.kind == "shutting_down":
            raise _ShuttingDown
        raise _SwapSuperseded

    def _commit_started_handle(self, handle: ProcessHandle) -> None:
        self._current = handle
        self._current_model = handle.model_name
        self._healthy = False
        self.stats.record_model_start(handle.model_name)

    def _commit_stopped_handle(
        self, handle: ProcessHandle, *, record_stop: bool
    ) -> None:
        if record_stop:
            self.stats.record_model_stop(handle.model_name)
        if self._current is handle:
            self._current = None
            self._current_model = None
            self._healthy = False

    async def _stop_tracked(
        self, handle: ProcessHandle, *, record_stop: bool
    ) -> None:
        """Shield one stop attempt and commit state only on confirmation."""
        async def stop_and_commit() -> None:
            try:
                await stop_process(handle)
            except ProcessStopError:
                raise
            except Exception as exc:
                raise ProcessStopError(
                    f"Could not stop {handle.model_name} lifecycle"
                ) from exc
            # Commit in the owned cleanup task before it wakes other tasks.
            self._commit_stopped_handle(handle, record_stop=record_stop)

        task = asyncio.create_task(
            stop_and_commit(), name=f"stop-{handle.model_name}"
        )
        self._cleanup_task = task
        try:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as cancel_exc:
                # Preserve outer cancellation, but let teardown reach a
                # terminal success or ProcessStopError first.
                try:
                    await task
                except ProcessStopError:
                    if self._current is handle:
                        self._healthy = False
                    raise cancel_exc
                raise
            except ProcessStopError:
                if self._current is handle:
                    self._healthy = False
                raise
        finally:
            if self._cleanup_task is task and task.done():
                self._cleanup_task = None

    async def _start_tracked(
        self,
        model_name: str,
        cmd: str,
        port: int,
        generation: int,
        *,
        managed: bool,
    ) -> ProcessHandle:
        """Start a process without allowing owner cancellation to orphan it."""
        task = asyncio.create_task(
            start_process(model_name, cmd, port),
            name=f"start-{model_name}",
        )
        self._spawn_task = task
        try:
            try:
                handle = await asyncio.shield(task)
            except asyncio.CancelledError as cancel_exc:
                try:
                    handle = await task
                except asyncio.CancelledError:
                    self._cancelled_owner_outcome = (
                        self._intent_outcome(generation)
                        if managed
                        else _LoadOutcome("model_start_failed")
                    )
                    if self._cancelled_owner_outcome is None:
                        self._cancelled_owner_outcome = _LoadOutcome(
                            "model_start_failed"
                        )
                    raise cancel_exc
                except Exception:
                    self._cancelled_owner_outcome = (
                        self._intent_outcome(generation)
                        if managed
                        else _LoadOutcome("model_start_failed")
                    )
                    if self._cancelled_owner_outcome is None:
                        self._cancelled_owner_outcome = _LoadOutcome(
                            "model_start_failed"
                        )
                    raise cancel_exc

                self._commit_started_handle(handle)
                try:
                    await self._stop_tracked(
                        handle, record_stop=not self._shutting_down
                    )
                except ProcessStopError:
                    self._cancelled_owner_outcome = _LoadOutcome(
                        "process_cleanup_failed"
                    )
                else:
                    self._cancelled_owner_outcome = (
                        self._intent_outcome(generation)
                        if managed
                        else None
                    )
                raise cancel_exc
            except Exception as exc:
                outcome = self._intent_outcome(generation) if managed else None
                if outcome is not None:
                    if outcome.kind == "shutting_down":
                        raise _ShuttingDown from exc
                    raise _SwapSuperseded from exc
                logger.error("Could not start model %s: %s", model_name, exc)
                raise _ModelStartFailed from exc
        finally:
            if self._spawn_task is task and task.done():
                self._spawn_task = None

        self._commit_started_handle(handle)
        return handle

    async def _cleanup_for_terminal_outcome(
        self, handle: ProcessHandle, *, record_stop: bool
    ) -> None:
        try:
            await self._stop_tracked(handle, record_stop=record_stop)
        except ProcessStopError as exc:
            raise _CleanupFailed from exc

    async def _ensure_model(
        self, model_name: str, intent_generation: int | None = None
    ) -> ProcessHandle:
        """Ensure the named model is running and healthy under the lock."""
        managed = intent_generation is not None
        generation = (
            self._intent_generation
            if intent_generation is None
            else intent_generation
        )
        self._raise_for_intent(generation, managed=managed)

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

        swap_started: float | None = None
        if self._current:
            self._idle_generation += 1
            self._cancel_idle_timer()
            logger.info(
                "Swapping from %s to %s", self._current_model, model_name
            )
            swap_started = time.monotonic()
            try:
                await self._stop_tracked(
                    self._current, record_stop=not self._shutting_down
                )
            except ProcessStopError as exc:
                raise _CleanupFailed from exc
            self._raise_for_intent(generation, managed=managed)

        logger.info("Starting model %s", model_name)
        handle = await self._start_tracked(
            model_name,
            mc.cmd,
            mc.port,
            generation,
            managed=managed,
        )

        try:
            self._raise_for_intent(generation, managed=managed)
        except (_SwapSuperseded, _ShuttingDown):
            await self._cleanup_for_terminal_outcome(
                handle, record_stop=not self._shutting_down
            )
            raise

        readiness = asyncio.create_task(
            wait_for_ready(
                handle.port,
                timeout=self.timeout,
                client=self._client,
            ),
            name=f"ready-{model_name}",
        )
        self._readiness_task = readiness
        self._readiness_model = model_name
        try:
            try:
                await asyncio.shield(readiness)
            except TimeoutError:
                logger.error(
                    "Model %s on port %d did not become ready",
                    model_name,
                    handle.port,
                )
                await self._cleanup_for_terminal_outcome(
                    handle, record_stop=not self._shutting_down
                )
                self._raise_for_intent(generation, managed=managed)
                raise
            except asyncio.CancelledError as cancel_exc:
                externally_cancelled = (
                    asyncio.current_task() is not None
                    and asyncio.current_task().cancelling() > 0
                )
                if externally_cancelled and not readiness.done():
                    readiness.cancel()
                # Consume any terminal child result without allowing a
                # concurrent timeout/error to replace owner cancellation.
                try:
                    await readiness
                except (asyncio.CancelledError, Exception):
                    pass
                try:
                    await self._cleanup_for_terminal_outcome(
                        handle, record_stop=not self._shutting_down
                    )
                except _CleanupFailed:
                    if externally_cancelled:
                        self._cancelled_owner_outcome = _LoadOutcome(
                            "process_cleanup_failed"
                        )
                        raise cancel_exc
                    raise
                if externally_cancelled:
                    self._cancelled_owner_outcome = (
                        self._intent_outcome(generation)
                        if managed
                        else None
                    )
                    raise cancel_exc
                self._raise_for_intent(generation, managed=managed)
                raise
        finally:
            if self._readiness_task is readiness and readiness.done():
                self._readiness_task = None
                self._readiness_model = None

        try:
            self._raise_for_intent(generation, managed=managed)
        except (_SwapSuperseded, _ShuttingDown):
            await self._cleanup_for_terminal_outcome(
                handle, record_stop=not self._shutting_down
            )
            raise

        self._healthy = True
        if swap_started is not None:
            self.stats.record_swap(time.monotonic() - swap_started)
        return handle

    def _prune_queue(self) -> None:
        retained: deque[asyncio.Future] = deque()
        while self._queue:
            future = self._queue.popleft()
            if future.done():
                self._waiters.pop(future, None)
            else:
                retained.append(future)
        self._queue = retained

    def _queue_request(self) -> asyncio.Future:
        """Queue a request during swap. Returns a Future to await."""
        self._prune_queue()
        if len(self._queue) >= MAX_QUEUE:
            raise asyncio.QueueFull(
                f"Swap in progress and request queue is full ({MAX_QUEUE} pending)"
            )
        future = asyncio.get_running_loop().create_future()
        self._queue.append(future)
        return future

    def _reserve_waiter(
        self, model_name: str, generation: int
    ) -> asyncio.Future:
        future = self._queue_request()
        self._waiters[future] = _Waiter(model_name, generation)
        return future

    def _admit_request(self, model_name: str) -> tuple[str, object]:
        """Admit one model request without awaiting or taking the lock."""
        if self._shutting_down:
            return "outcome", _LoadOutcome("shutting_down")

        if not self._swapping:
            self._swapping = True
            self._intent_generation += 1
            self._desired_model = model_name
            return "owner", self._intent_generation

        # Preserve the private legacy queue state used by baseline tests.
        if self._desired_model is None:
            try:
                future = self._reserve_waiter(
                    model_name, self._intent_generation
                )
            except asyncio.QueueFull:
                return "outcome", _LoadOutcome("queue_full")
            return "waiter", future

        if model_name == self._desired_model:
            try:
                future = self._reserve_waiter(
                    model_name, self._intent_generation
                )
            except asyncio.QueueFull:
                return "outcome", _LoadOutcome("queue_full")
            return "waiter", future

        if not self.quick_switch:
            return "outcome", _LoadOutcome("switch_in_progress")

        # Capacity is reserved before publishing a superseding intent.
        try:
            future = self._queue_request()
        except asyncio.QueueFull:
            return "outcome", _LoadOutcome("queue_full")
        self._intent_generation += 1
        self._desired_model = model_name
        self._waiters[future] = _Waiter(
            model_name, self._intent_generation
        )
        readiness = self._readiness_task
        if (
            readiness is not None
            and self._readiness_model != model_name
            and not readiness.done()
        ):
            readiness.cancel()
        return "waiter", future

    def _remove_waiter(self, future: asyncio.Future) -> None:
        self._waiters.pop(future, None)
        try:
            self._queue.remove(future)
        except ValueError:
            pass

    def _resolve_waiters(
        self,
        predicate,
        outcome: _LoadOutcome,
        *,
        resolve_legacy: bool = False,
    ) -> None:
        retained: deque[asyncio.Future] = deque()
        while self._queue:
            future = self._queue.popleft()
            waiter = self._waiters.get(future)
            if future.done():
                self._waiters.pop(future, None)
                continue
            if waiter is not None and predicate(waiter):
                self._waiters.pop(future, None)
                future.set_result(outcome)
            elif waiter is None and resolve_legacy:
                future.set_result(None)
            else:
                retained.append(future)
        self._queue = retained

    def _handoff_current_intent(self) -> bool:
        """Resolve stale waiters and offer ownership to the latest intent."""
        current_generation = self._intent_generation
        self._resolve_waiters(
            lambda waiter: waiter.generation != current_generation,
            _LoadOutcome("swap_superseded"),
            resolve_legacy=True,
        )
        for future in list(self._queue):
            waiter = self._waiters.get(future)
            if (
                waiter is not None
                and waiter.generation == current_generation
                and waiter.model_name == self._desired_model
                and not future.done()
            ):
                self._queue.remove(future)
                # Keep metadata until the waiter acknowledges the award. If
                # cancellation wins that race, it can re-offer ownership.
                future.set_result(_OWNER)
                return True
        return False

    def _finish_owner(
        self, generation: int, outcome: _LoadOutcome
    ) -> _LoadOutcome:
        """Publish an owner's terminal result and select any successor."""
        if outcome.kind == "process_cleanup_failed":
            self._resolve_waiters(
                lambda waiter: True,
                outcome,
                resolve_legacy=True,
            )
            self._desired_model = None
            self._swapping = False
            return outcome

        if self._shutting_down or outcome.kind == "shutting_down":
            shutdown_outcome = _LoadOutcome("shutting_down")
            self._resolve_waiters(
                lambda waiter: True,
                shutdown_outcome,
                resolve_legacy=True,
            )
            self._desired_model = None
            self._swapping = False
            return shutdown_outcome

        is_current = (
            generation == self._intent_generation
            and self._desired_model is not None
        )
        if is_current and outcome.kind != "cancelled":
            self._resolve_waiters(
                lambda waiter: waiter.generation == generation,
                outcome,
                resolve_legacy=True,
            )
            self._desired_model = None
            self._swapping = False
            return outcome

        if self._handoff_current_intent():
            return (
                _LoadOutcome("swap_superseded")
                if generation != self._intent_generation
                else outcome
            )

        self._desired_model = None
        self._swapping = False
        if generation != self._intent_generation:
            return _LoadOutcome("swap_superseded")
        return outcome

    def _finish_cancelled_owner(self, generation: int) -> None:
        outcome = self._cancelled_owner_outcome
        self._cancelled_owner_outcome = None
        if outcome is None:
            if self._current is not None and not self._healthy:
                outcome = _LoadOutcome("process_cleanup_failed")
            else:
                outcome = _LoadOutcome("cancelled")
        self._finish_owner(generation, outcome)

    def _response_for_outcome(
        self, outcome: _LoadOutcome, model_name: str
    ) -> web.Response:
        responses = {
            "switch_in_progress": (
                409,
                "A different model is currently loading",
                "switch_in_progress",
            ),
            "swap_superseded": (
                409,
                "Model load was superseded by a newer request",
                "swap_superseded",
            ),
            "model_start_failed": (
                500,
                "Model process could not be started",
                "model_start_failed",
            ),
            "process_cleanup_failed": (
                503,
                "Previous model process could not be stopped",
                "process_cleanup_failed",
            ),
            "shutting_down": (
                503,
                "Model switcher is shutting down",
                "shutting_down",
            ),
            "queue_full": (
                503,
                "Swap in progress, try again shortly",
                None,
            ),
            "swap_timeout": (
                504,
                "Model swap timed out",
                "swap_timeout",
            ),
        }
        status, message, code = responses[outcome.kind]
        self.stats.record_rejection(str(status), model_name)
        return web.json_response(
            _openai_error(message, code) if code else _openai_error(message),
            status=status,
        )

    async def _drain_queue(self) -> None:
        """Resolve all queued futures (legacy private helper contract)."""
        while self._queue:
            future = self._queue.popleft()
            self._waiters.pop(future, None)
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

        # Unknown targets cannot consume capacity or mutate switch intent.
        if model_name not in self.config.models:
            self.stats.record_rejection("404", model_name)
            return web.json_response(
                _openai_error(f"Unknown model: {model_name}"),
                status=404,
            )

        generation: int
        while True:
            admission, value = self._admit_request(model_name)
            if admission == "outcome":
                return self._response_for_outcome(value, model_name)
            if admission == "owner":
                generation = value
                break

            future = value
            waiter = self._waiters.get(future)
            generation = (
                waiter.generation
                if waiter is not None
                else self._intent_generation
            )
            try:
                joined = await future
            except asyncio.CancelledError:
                waiter = self._waiters.get(future)
                was_awarded = False
                if future.done() and not future.cancelled():
                    try:
                        was_awarded = future.result() is _OWNER
                    except Exception:
                        pass
                self._remove_waiter(future)
                if was_awarded:
                    awarded_generation = (
                        waiter.generation if waiter is not None else generation
                    )
                    self._finish_owner(
                        awarded_generation, _LoadOutcome("cancelled")
                    )
                raise
            if joined is _OWNER:
                # Acknowledge ownership before the next cancellation point.
                self._waiters.pop(future, None)
                break
            if joined is None:
                # Compatibility for legacy callers of _drain_queue().
                continue
            if joined.kind != "success":
                return self._response_for_outcome(joined, model_name)
            handle = joined.handle
            if handle is None:
                return self._response_for_outcome(
                    _LoadOutcome("model_start_failed"), model_name
                )
            self._begin_activity()
            return await self._proxy_with_recorded_activity(
                request, model_name, body, handle
            )

        try:
            async with self._lock:
                try:
                    handle = await self._ensure_model(model_name, generation)
                except _SwapSuperseded:
                    outcome = _LoadOutcome("swap_superseded")
                except _ShuttingDown:
                    # Shutdown can race lifecycle work outside the request
                    # owner (notably idle unload).  If that work retained a
                    # handle after failed cleanup, establish cleanup outcome
                    # before publishing an irreversible waiter response.
                    if self._current is not None:
                        try:
                            await self._stop_tracked(
                                self._current, record_stop=False
                            )
                        except ProcessStopError:
                            outcome = _LoadOutcome(
                                "process_cleanup_failed"
                            )
                        else:
                            outcome = _LoadOutcome("shutting_down")
                    else:
                        outcome = _LoadOutcome("shutting_down")
                except _ModelStartFailed:
                    outcome = _LoadOutcome("model_start_failed")
                except _CleanupFailed:
                    outcome = _LoadOutcome("process_cleanup_failed")
                except TimeoutError:
                    outcome = _LoadOutcome("swap_timeout")
                else:
                    self._begin_activity()
                    outcome = _LoadOutcome("success", handle)
                outcome = self._finish_owner(generation, outcome)
        except asyncio.CancelledError:
            self._finish_cancelled_owner(generation)
            raise

        if outcome.kind != "success":
            return self._response_for_outcome(outcome, model_name)
        return await self._proxy_with_recorded_activity(
            request, model_name, body, outcome.handle
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
                try:
                    await self._stop_tracked(handle, record_stop=True)
                except ProcessStopError as exc:
                    logger.error("Idle unload failed: %s", exc)
        except asyncio.CancelledError:
            pass
        finally:
            self._idle_timer_expired = False
            if self._idle_task is current_task:
                self._idle_task = None

    async def shutdown(self) -> None:
        """Stop lifecycle work, close the client, and clean temp files."""
        if not self._shutting_down:
            self._shutting_down = True
            self._intent_generation += 1
            self._desired_model = None
            readiness = self._readiness_task
            if readiness is not None and not readiness.done():
                readiness.cancel()
            # Publish shutdown synchronously, but keep live waiters pending
            # until the active owner's cleanup result establishes whether
            # shutting_down or process_cleanup_failed takes precedence.
            if not self._swapping:
                self._resolve_waiters(
                    lambda waiter: True,
                    _LoadOutcome("shutting_down"),
                    resolve_legacy=True,
                )

        idle_task = self._cancel_idle_timer(force=True)
        if idle_task:
            with suppress(asyncio.CancelledError):
                await idle_task

        stop_error: ProcessStopError | None = None
        try:
            async with self._lock:
                if self._current:
                    try:
                        await self._stop_tracked(
                            self._current, record_stop=False
                        )
                    except ProcessStopError as exc:
                        stop_error = exc
                waiter_outcome = _LoadOutcome(
                    "process_cleanup_failed"
                    if stop_error is not None
                    else "shutting_down"
                )
                self._resolve_waiters(
                    lambda waiter: True,
                    waiter_outcome,
                    resolve_legacy=True,
                )
                self._desired_model = None
                self._swapping = False
        finally:
            if self._client:
                await self._client.close()
                self._client = None
            if self._temp_dir:
                cleanup_temp_dir(self._temp_dir)
                self._temp_dir = None

        if stop_error is not None:
            raise stop_error

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
