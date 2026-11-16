"""Request queue, admission, and model-acquisition ownership for Swapper."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from src.process_manager import ProcessStopError
from src.swapper_lifecycle import (
    _CleanupFailed,
    _LoadOutcome,
    _ModelStartFailed,
    _ShuttingDown,
    _SwapSuperseded,
)

MAX_QUEUE = 10  # max pending requests during swap


@dataclass(frozen=True)
class _Waiter:
    model_name: str
    generation: int


_OWNER = object()


class _SwapperAdmissionMixin:
    """Request admission and owner hand-off mixed into the public Swapper."""

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
        if automatic and self._automatic_switch_blocks(model_name):
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

    def _automatic_switch_blocks(self, model_name: str) -> bool:
        """True when automatic switching is off and the request would
        replace a running model or a load/switch of a different model.

        "Running" means ``active_model`` (a live, healthy process). With no
        load in progress and no active model (at start, after an idle or
        manual unload, a crash, or a failed load), the request may load.
        During a load only the model being loaded may be joined.
        """
        if self.automatic_switch:
            return False
        if self._swapping:
            return (
                self._desired_model is not None
                and model_name != self._desired_model
            )
        active = self.active_model
        return active is not None and model_name != active

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

    async def _drain_queue(self) -> None:
        """Resolve all queued futures (legacy private helper contract)."""
        while self._queue:
            future = self._queue.popleft()
            self._waiters.pop(future, None)
            if not future.done():
                future.set_result(None)

    async def _acquire_model(
        self,
        model_name: str,
        *,
        automatic: bool,
        on_admit: Callable[[], None] | None = None,
    ) -> _LoadOutcome:
        """Join or own the cancellation-safe model acquisition state machine.

        ``on_admit`` runs synchronously right after an admission that joins,
        owns, or already has the model, before any await, so it can publish
        state that later admissions must see.
        """
        generation: int
        while True:
            admission, value = self._admit_request(
                model_name, automatic=automatic
            )
            if on_admit is not None and (
                admission != "outcome" or value.kind == "success"
            ):
                on_admit()
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
