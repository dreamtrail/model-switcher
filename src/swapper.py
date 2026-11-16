"""Swap orchestration — stop/start models and queue requests."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

import aiohttp
from aiohttp import web

from src.config import Config, variant_id
from src.control import dispatch_control
from src.engine_metrics import EngineCounterPoller
from src.health import wait_for_ready
from src.port_rewrite import create_temp_dir
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
from src.swapper_admission import (
    MAX_QUEUE,
    _OWNER,
    _SwapperAdmissionMixin,
    _Waiter,
)
# Lifecycle names stay importable from src.swapper (tests read them here).
from src.swapper_lifecycle import (
    _CleanupFailed,
    _LoadOutcome,
    _ModelStartFailed,
    _ShuttingDown,
    _SwapperLifecycleMixin,
    _SwapSuperseded,
)
from src.variant_state import VariantStateStore

logger = logging.getLogger("src.swapper")


class Swapper(_SwapperAdmissionMixin, _SwapperLifecycleMixin):
    """Coordinates model swapping and request routing."""

    def __init__(
        self,
        config: Config,
        *,
        timeout: float = 60,
        idle_timeout: float = 900,
        quick_switch: bool = False,
        automatic_switch: bool = False,
        sse_keepalive: float = 15,
        client: aiohttp.ClientSession | None = None,
        stats: StatsCollector | None = None,
    ):
        self.config = config
        self.timeout = timeout
        # Seconds of client-side quiet before an SSE keepalive comment on
        # a streaming response; 0 disables.
        self.sse_keepalive = sse_keepalive
        self.idle_timeout = idle_timeout
        self.quick_switch = quick_switch
        # Off: a request may load its model only while nothing is running
        # or loading (see _automatic_switch_blocks).
        self.automatic_switch = automatic_switch
        self._client = client
        self.stats = stats or StatsCollector()
        # Reads the backend's generated-token counter during decodes for
        # the live generation rate (src/engine_metrics.py).
        self._engine_poller = EngineCounterPoller(self.stats)
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
        # Requests in flight from validation to finish, including queue and
        # model-load wait (utilized time); "Active now" stays separate.
        self._busy_requests = 0
        self._temp_dir: Path | None = None
        # Config file to re-read on control reload; the CLI sets it after
        # construction. None means reload is unavailable.
        self.config_path: Path | None = None
        # Reload count; names temp script copies made by each reload.
        self._config_generation = 0
        # Saves the active variants for the next start; the CLI sets it.
        # None means nothing is saved.
        self.variant_state: VariantStateStore | None = None

        # Create temp dir for rewritten script copies
        self._temp_dir = create_temp_dir()
        # Rewrite all model commands to use auto-assigned ports
        self._rewrite_commands(self.config)

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
            model_names = self.config.public_names()
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
                self._begin_busy()
                try:
                    return await self._proxy_with_recorded_activity(
                        request,
                        active_model,
                        body,
                        handle,
                    )
                finally:
                    self._end_busy()
            self.stats.record_rejection("400")
            return web.json_response(
                _openai_error(
                    "No model specified and no model is currently running"
                ),
                status=400,
            )

        # A public name maps to the model id to run (the active variant
        # for variant entries). Internal ids such as "name@variant" are
        # not public, so clients sending them get 404 like unknown names.
        target = self.config.resolve(model_name)

        # Unknown targets cannot consume capacity or mutate switch intent.
        if target is None:
            # A client-sent internal id must not add a 404 to that
            # variant's stats row; count it globally only.
            self.stats.record_rejection(
                "404",
                None if model_name in self.config.models else model_name,
            )
            return web.json_response(
                _openai_error(f"Unknown model: {model_name}"),
                status=404,
            )

        # Busy from here: queue and model-load wait count as utilized time.
        self._begin_busy()
        try:
            outcome = await self._acquire_model(target, automatic=True)
            if outcome.kind != "success" or outcome.handle is None:
                if outcome.kind == "success":
                    outcome = _LoadOutcome("model_start_failed")
                return self._response_for_outcome(outcome, target)
            self._begin_activity()
            return await self._proxy_with_recorded_activity(
                request, target, body, outcome.handle
            )
        finally:
            self._end_busy()

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
            "models": self.config.public_names(),
            "variants": {
                name: {
                    "variants": list(group.variants),
                    "default": group.default,
                    "active": group.active,
                }
                for name, group in self.config.variants.items()
            },
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

    async def control_load_model(
        self, model_name: str, variant: str | None = None
    ) -> str:
        """Synchronously load one model without inference accounting.

        ``model_name`` is a public name. For a variant entry, ``variant``
        picks the variant to make active and load; without it the current
        active variant loads. The choice is published in the same
        synchronous step as admission, so requests arriving afterwards
        resolve to the new variant; it stays if the load later fails.
        """
        group = self.config.variants.get(model_name)
        target = self.config.resolve(model_name)
        if target is None:
            return "unknown_model"
        on_admit = None
        if variant is not None:
            if group is None:
                return "model_has_no_variants"
            if variant not in group.variants:
                return "unknown_variant"
            target = variant_id(model_name, variant)

            def on_admit() -> None:
                group.active = variant
                self._save_variant_state(picked=model_name)
        # Preserve precedence while making an already-active selection a true
        # no-op that does not rearm its idle countdown.
        if self._shutting_down:
            return "shutting_down"
        if self._stop_only_operation is not None:
            return "control_busy"
        if not self._swapping and target == self.active_model:
            if on_admit is not None:
                on_admit()
            return "success"

        outcome = await self._acquire_model(
            target, automatic=False, on_admit=on_admit
        )
        if outcome.kind == "success":
            self._schedule_idle_timer()
        return outcome.kind

    def _save_variant_state(self, picked: str | None = None) -> None:
        """Save the active variants; a failure never stops a switch."""
        if self.variant_state is None:
            return
        try:
            self.variant_state.save(self.config, picked=picked)
        except Exception:
            logger.exception("Could not save variant state")

    def _begin_activity(self) -> None:
        """Mark a model request active and cancel pending idle unload."""
        self._active_requests += 1
        self._idle_generation += 1
        self._cancel_idle_timer()
        self.stats.observe_inflight(self._active_requests)

    def _begin_busy(self) -> None:
        """Count a validated request as in flight for utilized time."""
        self._busy_requests += 1
        self.stats.observe_busy(self._busy_requests)

    def _end_busy(self) -> None:
        """Release a request counted by _begin_busy (exactly once)."""
        self._busy_requests -= 1
        self.stats.observe_busy(self._busy_requests)

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

        def on_first_token(first_token_at: float) -> None:
            self.stats.record_decode_start(
                model_name, decode_identity, first_token_at,
            )
            if handle is None:
                return
            try:
                self._engine_poller.notify_decode(
                    model_name, handle, f"http://127.0.0.1:{handle.port}",
                )
            except Exception:
                # The live rate must never break the proxied stream.
                logger.debug("Engine counter poll not started", exc_info=True)

        outcome = Outcome(
            first_token_observer=on_first_token,
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
            elif outcome.client_disconnect:
                ok, kind = False, "client_disconnect"
            elif outcome.stream_error:
                ok, kind = False, "stream_error"
            elif resp.status >= 400:
                ok, kind = False, str(resp.status)
            else:
                ok, kind = True, None
            # Streaming-only proxy timing: from chokepoint start to the first
            # content event (TTFT) and from there to stream end (decode).
            # Server decode timing, when reported, takes precedence for
            # generation speed (src/decode.py), streaming or not.
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
                server_decode=outcome.server_decode,
                gen_chunks=outcome.gen_chunks,
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
            if decode_s is not None and outcome.gen_chunks >= 2:
                # Successful stream with real chunking (not one buffered
                # burst): calibrate live-rate tokens per chunk.
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
                server_decode=outcome.server_decode,
                gen_chunks=outcome.gen_chunks,
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
                outcome=outcome, sse_keepalive=self.sse_keepalive,
            )
        return await proxy_request(
            request, upstream, body,
            timeout=self.timeout, client=self._client,
            outcome=outcome,
        )
