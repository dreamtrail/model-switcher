"""Process, readiness, idle, and shutdown lifecycle for Swapper."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass

from src.port_rewrite import cleanup_temp_dir, rewrite_command
from src.process_manager import ProcessHandle, ProcessStopError, is_running

logger = logging.getLogger("src.swapper")


@dataclass(frozen=True)
class _LoadOutcome:
    kind: str
    handle: ProcessHandle | None = None


class _SwapSuperseded(Exception):
    pass


class _ShuttingDown(Exception):
    pass


class _ModelStartFailed(Exception):
    pass


class _CleanupFailed(Exception):
    pass


class _SwapperLifecycleMixin:
    """Process lifecycle implementation mixed into the public Swapper."""

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
        self._cleanup_failed = False
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
            self._cleanup_failed = False

    async def _stop_tracked(
        self, handle: ProcessHandle, *, record_stop: bool
    ) -> None:
        """Shield one stop attempt and commit state only on confirmation."""
        async def stop_and_commit() -> None:
            try:
                await self._stop_process(handle)
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
                        self._cleanup_failed = True
                    raise cancel_exc
                raise
            except ProcessStopError:
                if self._current is handle:
                    self._healthy = False
                    self._cleanup_failed = True
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
            self._start_process(model_name, cmd, port),
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
            self._wait_for_ready(
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

    def _schedule_idle_timer(self) -> None:
        """Arm idle unload when enabled and no model requests are active."""
        if self.idle_timeout <= 0 or self._active_requests > 0:
            return
        if self._stop_only_operation is not None or self._swapping:
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
                if self._shutting_down or self._swapping:
                    return
                if self._stop_only_operation is not None:
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
                # A zero timeout cannot schedule this coroutine in normal
                # operation; retaining the legacy direct-helper behavior for
                # that impossible timer state keeps shutdown recovery usable.
                owns_stop_only = self.idle_timeout > 0
                if owns_stop_only:
                    self._stop_only_operation = "idle"
                try:
                    try:
                        await self._stop_tracked(handle, record_stop=True)
                    except ProcessStopError as exc:
                        logger.error("Idle unload failed: %s", exc)
                finally:
                    # Cleanup has already published confirmed success or the
                    # retained-handle failure marker before this is cleared.
                    if owns_stop_only:
                        self._stop_only_operation = None
        except asyncio.CancelledError:
            pass
        finally:
            self._idle_timer_expired = False
            if self._idle_task is current_task:
                self._idle_task = None

    def control_apply_settings(
        self,
        *,
        automatic_switch: bool,
        quick_switch: bool,
        idle_timeout_seconds: float,
    ) -> str:
        """Apply the complete runtime settings representation atomically."""
        if self._shutting_down:
            return "shutting_down"
        timeout_changed = self.idle_timeout != idle_timeout_seconds
        self.automatic_switch = automatic_switch
        self.quick_switch = quick_switch
        self.idle_timeout = idle_timeout_seconds
        if not timeout_changed:
            return "success"

        self._idle_generation += 1
        idle_cleanup_committed = self._stop_only_operation == "idle"
        self._cancel_idle_timer(force=not idle_cleanup_committed)
        if not idle_cleanup_committed:
            self._schedule_idle_timer()
        return "success"

    async def control_unload_model(self) -> str:
        """Stop the tracked model, or succeed unchanged when none exists."""
        if self._shutting_down:
            return "shutting_down"
        if self._stop_only_operation is not None or self._swapping:
            return "control_busy"

        # Publish stop-only ownership before the first await so later model
        # admissions cannot overtake this operation.
        self._stop_only_operation = "manual"
        self._idle_generation += 1
        self._cancel_idle_timer(force=True)
        try:
            async with self._lock:
                handle = self._current
                if handle is None:
                    return "success"
                try:
                    await self._stop_tracked(handle, record_stop=True)
                except ProcessStopError:
                    return "process_cleanup_failed"
                return "success"
        finally:
            # _stop_tracked commits success or marks retained cleanup failure
            # before control ownership is released.
            self._stop_only_operation = None

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
