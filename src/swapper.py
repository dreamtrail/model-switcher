"""Swap orchestration — stop/start models and queue requests."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web

from src.config import Config
from src.control import dispatch_control
from src.health import wait_for_ready
from src.port_rewrite import create_temp_dir
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
from src.swapper_lifecycle import (
    _CleanupFailed,
    _LoadOutcome,
    _ModelStartFailed,
    _ShuttingDown,
    _SwapperLifecycleMixin,
    _SwapSuperseded,
)

MAX_QUEUE = 10  # max pending requests during swap


@dataclass(frozen=True)
class _Waiter:
    model_name: str
    generation: int


_OWNER = object()


class Swapper(_SwapperLifecycleMixin):
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
        self.automatic_switch = True
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
        self._stop_only_operation: str | None = None
        self._cleanup_failed = False
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

    async def _start_process(
        self, model_name: str, cmd: str, port: int
    ) -> ProcessHandle:
        return await start_process(model_name, cmd, port)

    async def _stop_process(self, handle: ProcessHandle) -> None:
        await stop_process(handle)

    async def _wait_for_ready(self, port: int, **kwargs) -> None:
        await wait_for_ready(port, **kwargs)

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

    def _admit_request(
        self, model_name: str, *, automatic: bool = True
    ) -> tuple[str, object]:
        """Admit one model request without awaiting or taking the lock."""
        if self._shutting_down:
            return "outcome", _LoadOutcome("shutting_down")
        if self._stop_only_operation is not None:
            return "outcome", _LoadOutcome("control_busy")
        if (
            automatic
            and not self.automatic_switch
            and model_name != self.active_model
        ):
            return "outcome", _LoadOutcome("automatic_switch_disabled")
        if not self._swapping and model_name == self.active_model:
            return "outcome", _LoadOutcome("success", self._current)

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
            "control_busy": (
                409,
                "A model lifecycle operation is already in progress",
                "control_busy",
            ),
            "automatic_switch_disabled": (
                409,
                "Automatic model switching is disabled",
                "automatic_switch_disabled",
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

        control_response = await dispatch_control(request, self)
        if control_response is not None:
            return control_response

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

        outcome = await self._acquire_model(model_name, automatic=True)
        if outcome.kind != "success" or outcome.handle is None:
            if outcome.kind == "success":
                outcome = _LoadOutcome("model_start_failed")
            return self._response_for_outcome(outcome, model_name)
        self._begin_activity()
        return await self._proxy_with_recorded_activity(
            request, model_name, body, outcome.handle
        )

    async def _acquire_model(
        self, model_name: str, *, automatic: bool
    ) -> _LoadOutcome:
        """Join or own the cancellation-safe model acquisition state machine."""
        generation: int
        while True:
            admission, value = self._admit_request(
                model_name, automatic=automatic
            )
            if admission == "outcome":
                return value
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
            return joined

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
                    outcome = _LoadOutcome("success", handle)
                outcome = self._finish_owner(generation, outcome)
        except asyncio.CancelledError:
            self._finish_cancelled_owner(generation)
            raise
        return outcome

    def control_state(self) -> dict:
        """Return a synchronous control API snapshot."""
        active_model = self.active_model
        if self._shutting_down:
            lifecycle = "shutting_down"
        elif self._stop_only_operation is not None:
            lifecycle = "unloading"
        elif self._swapping:
            lifecycle = "loading"
        elif active_model is not None:
            lifecycle = "ready"
        elif self._cleanup_failed:
            lifecycle = "cleanup_failed"
        else:
            lifecycle = "idle"
        return {
            "models": list(self.config.models.keys()),
            "active_model": active_model,
            "pending_model": (
                self._desired_model if lifecycle == "loading" else None
            ),
            "lifecycle": lifecycle,
            "inflight_requests": self._active_requests,
            "settings": {
                "automatic_switch": self.automatic_switch,
                "quick_switch": self.quick_switch,
                "idle_timeout_seconds": float(self.idle_timeout),
            },
        }

    async def control_load_model(self, model_name: str) -> str:
        """Synchronously load one model without inference accounting."""
        if model_name not in self.config.models:
            return "unknown_model"
        # Preserve precedence while making an already-active selection a true
        # no-op that does not rearm its idle countdown.
        if self._shutting_down:
            return "shutting_down"
        if self._stop_only_operation is not None:
            return "control_busy"
        if not self._swapping and model_name == self.active_model:
            return "success"

        outcome = await self._acquire_model(model_name, automatic=False)
        if outcome.kind == "success":
            self._schedule_idle_timer()
        return outcome.kind

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
        decode_identity = object()
        outcome = Outcome(
            first_token_observer=lambda first_token_at: (
                self.stats.record_decode_start(
                    model_name, decode_identity, first_token_at,
                )
            ),
            # Only the streaming proxy reports chunks (live rate estimate).
            gen_chunk_observer=lambda units: (
                self.stats.record_gen_chunks(model_name, units)
            ),
        )
        resp: web.Response | None = None
        # Paired with record_prefill_end in the finally block below.
        self.stats.record_request_start(model_name, decode_identity, started)
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
            self.stats.record_decode_end(
                model_name,
                decode_identity,
                ended_at=ended,
                ok=ok,
                completion_tokens=(
                    outcome.completion_tokens if ok else None
                ),
            )
            self.stats.record_prefill_end(
                model_name,
                decode_identity,
                first_token_at=outcome.first_token_at,
                ok=ok,
                prompt_tokens=outcome.prompt_tokens,
                cached_tokens=outcome.cached_tokens,
                server_prefill=outcome.server_prefill,
                ended_at=ended,
            )
            if decode_s is not None:
                # Successful stream: calibrate live-rate tokens per chunk.
                self.stats.record_gen_calibration(
                    model_name, outcome.completion_tokens, outcome.gen_chunks,
                )
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
                server_prefill=outcome.server_prefill,
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
