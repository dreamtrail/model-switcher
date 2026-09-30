"""Model load counting and timing (``loads`` JSON, P-20260930-03).

A load is one process start attempt in ``_ensure_model``: successful loads
are counted and timed (start -> healthy), the stop of a previous model is
timed apart, and started loads that never became healthy are counted by
kind under ``failed`` or ``abandoned``.  The ``swaps`` key is unchanged.
"""

from __future__ import annotations

import asyncio

from aiohttp import web
import pytest

from src.process_manager import ProcessHandle
from src.swapper import Swapper
from tests.quick_switch_helpers import (
    FakeProcess,
    StubRequest,
    eventually,
    install_success_lifecycle,
    make_config,
)


EMPTY_DURATION = {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}


def _loads(swapper: Swapper) -> dict:
    return swapper.stats.snapshot()["loads"]


def _model_loads(swapper: Swapper, name: str) -> dict:
    return swapper.stats.snapshot()["models"][name]["loads"]


async def _success(monkeypatch):
    """Shared success lifecycle fakes plus a proxy fake for direct calls."""
    import src.swapper as module

    starts, stops = await install_success_lifecycle(monkeypatch)

    async def proxy(*args, **kwargs):
        return web.json_response({"ok": True})

    monkeypatch.setattr(module, "proxy_request", proxy)
    return starts, stops


def _install(monkeypatch, *, start=None, ready=None, stop=None):
    """Patch process lifecycle fakes; defaults succeed immediately."""
    import src.swapper as module

    async def default_start(model, cmd, port):
        return ProcessHandle(model, port, FakeProcess(1))

    async def default_ready(*args, **kwargs):
        return None

    async def default_stop(handle):
        handle.process.returncode = 0

    async def proxy(*args, **kwargs):
        return web.json_response({"ok": True})

    monkeypatch.setattr(module, "start_process", start or default_start)
    monkeypatch.setattr(module, "wait_for_ready", ready or default_ready)
    monkeypatch.setattr(module, "stop_process", stop or default_stop)
    monkeypatch.setattr(module, "proxy_request", proxy)


class TestSuccessfulLoads:
    async def test_cold_start_is_counted_and_timed(self, monkeypatch):
        await _success(monkeypatch)
        swapper = Swapper(make_config())
        try:
            response = await swapper.handle_request(StubRequest("a"))
            assert response.status == 200

            loads = _loads(swapper)
            assert loads["count"] == 1
            assert loads["avg_ms"] is not None and loads["avg_ms"] >= 0.0
            assert loads["swaps"] == 0
            assert loads["stop"] == EMPTY_DURATION
            assert loads["failed"] == {} and loads["abandoned"] == {}
            assert _model_loads(swapper, "a")["count"] == 1
            # swaps JSON unchanged: a cold start is not a swap.
            assert swapper.stats.snapshot()["swaps"] == EMPTY_DURATION
        finally:
            await swapper.shutdown()

    async def test_already_healthy_is_not_a_load(self, monkeypatch):
        starts, _ = await _success(monkeypatch)
        swapper = Swapper(make_config())
        try:
            await swapper.handle_request(StubRequest("a"))
            await swapper.handle_request(StubRequest("a"))
            # Direct no-op through _ensure_model's early return.
            async with swapper._lock:
                handle = await swapper._ensure_model("a")
            assert handle is swapper._current
            assert await swapper.control_load_model("a") == "success"

            assert starts == ["a"]
            assert _loads(swapper)["count"] == 1
            assert _model_loads(swapper, "a")["count"] == 1
        finally:
            await swapper.shutdown()

    async def test_load_after_idle_unload_is_counted(self, monkeypatch):
        starts, stops = await _success(monkeypatch)
        swapper = Swapper(make_config(), idle_timeout=0.01)
        try:
            await swapper.handle_request(StubRequest("a"))
            await eventually(
                lambda: swapper._current is None
                and swapper._stop_only_operation is None
            )
            assert stops == ["a"]
            response = await swapper.handle_request(StubRequest("a"))
            assert response.status == 200

            assert starts == ["a", "a"]
            loads = _loads(swapper)
            assert loads["count"] == 2
            # The idle unload is not part of any load: no stop time, no swap.
            assert loads["swaps"] == 0
            assert loads["stop"]["count"] == 0
            assert swapper.stats.snapshot()["swaps"]["count"] == 0
        finally:
            await swapper.shutdown()

    async def test_swap_times_stop_apart_and_splits_per_model(
        self, monkeypatch
    ):
        async def slow_stop(handle):
            await asyncio.sleep(0.2)
            handle.process.returncode = 0

        _install(monkeypatch, stop=slow_stop)
        swapper = Swapper(make_config())
        try:
            await swapper.handle_request(StubRequest("a"))
            await swapper.handle_request(StubRequest("b"))

            snap = swapper.stats.snapshot()
            loads = snap["loads"]
            assert loads["count"] == 2
            assert loads["swaps"] == 1
            assert loads["stop"]["count"] == 1
            assert loads["stop"]["avg_ms"] >= 190.0
            # Load time is start -> healthy only; the stop is not in it.
            assert loads["max_ms"] < 150.0
            # swaps JSON keeps its meaning: stop + start + health, once.
            assert snap["swaps"]["count"] == 1
            assert snap["swaps"]["avg_ms"] >= 190.0
            assert set(snap["swaps"]) == {"count", "avg_ms", "p95_ms", "max_ms"}

            a_loads = snap["models"]["a"]["loads"]
            b_loads = snap["models"]["b"]["loads"]
            assert a_loads["count"] == 1
            assert a_loads["swaps"] == 0
            assert a_loads["stop"] == EMPTY_DURATION
            assert b_loads["count"] == 1
            assert b_loads["swaps"] == 1
            assert b_loads["stop"]["count"] == 1
            assert b_loads["stop"]["avg_ms"] >= 190.0
        finally:
            await swapper.shutdown()

    async def test_control_panel_load_is_counted(self, monkeypatch):
        await _success(monkeypatch)
        swapper = Swapper(make_config())
        try:
            assert await swapper.control_load_model("a") == "success"
            assert await swapper.control_load_model("b") == "success"

            loads = _loads(swapper)
            assert loads["count"] == 2
            assert loads["swaps"] == 1
            assert loads["stop"]["count"] == 1
            assert _model_loads(swapper, "a")["count"] == 1
            assert _model_loads(swapper, "b")["count"] == 1
        finally:
            await swapper.shutdown()

    async def test_superseded_chain_counts_final_load_once(self, monkeypatch):
        readiness_entered = asyncio.Event()
        starts = []

        async def start(model, cmd, port):
            starts.append(model)
            return ProcessHandle(model, port, FakeProcess(len(starts)))

        async def ready(port, **kwargs):
            if port == 1001:  # model "a" never becomes ready on its own
                readiness_entered.set()
                await asyncio.Event().wait()

        _install(monkeypatch, start=start, ready=ready)
        swapper = Swapper(make_config(), quick_switch=True)
        requests = []
        try:
            requests.append(asyncio.create_task(
                swapper.handle_request(StubRequest("a"))
            ))
            await asyncio.wait_for(readiness_entered.wait(), 1)
            requests.append(asyncio.create_task(
                swapper.handle_request(StubRequest("b"))
            ))
            await eventually(lambda: swapper._desired_model == "b")
            requests.append(asyncio.create_task(
                swapper.handle_request(StubRequest("c"))
            ))
            responses = await asyncio.wait_for(asyncio.gather(*requests), 1)
            assert [r.status for r in responses] == [409, 409, 200]

            assert starts == ["a", "c"]
            loads = _loads(swapper)
            assert loads["count"] == 1
            assert loads["failed"] == {}
            assert loads["abandoned"] == {"swap_superseded": 1}
            snap = swapper.stats.snapshot()
            assert snap["models"]["a"]["loads"]["count"] == 0
            assert snap["models"]["a"]["loads"]["abandoned"] == {
                "swap_superseded": 1
            }
            assert snap["models"]["c"]["loads"]["count"] == 1
            assert snap["models"]["c"]["loads"]["abandoned"] == {}
            # Model "a" was cleaned up by its own abandoned load, so "c"
            # stopped nothing: no swap, same as the swaps key.
            assert loads["swaps"] == 0
            assert snap["swaps"]["count"] == 0

            # A later no-op for the loaded model adds nothing.
            await swapper.handle_request(StubRequest("c"))
            assert _loads(swapper)["count"] == 1
        finally:
            for task in requests:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            await swapper.shutdown()

    async def test_crash_recovery_counts_once_as_swap(self, monkeypatch):
        starts, stops = await _success(monkeypatch)
        swapper = Swapper(make_config())
        try:
            await swapper.handle_request(StubRequest("a"))
            swapper._current.process.returncode = -9  # process died
            response = await swapper.handle_request(StubRequest("a"))
            assert response.status == 200

            assert starts == ["a", "a"]
            assert stops == ["a"]  # the dead handle is stopped first
            snap = swapper.stats.snapshot()
            loads = snap["loads"]
            assert loads["count"] == 2  # cold start + one recovery load
            assert loads["swaps"] == 1
            assert loads["stop"]["count"] == 1
            # Same rule as the unchanged swaps key.
            assert snap["swaps"]["count"] == 1
            a_loads = snap["models"]["a"]["loads"]
            assert (a_loads["count"], a_loads["swaps"]) == (2, 1)
            assert a_loads["stop"]["count"] == 1
        finally:
            await swapper.shutdown()

    async def test_waiter_handoff_load_counts_once(self, monkeypatch):
        readiness_entered = asyncio.Event()
        starts = []

        async def start(model, cmd, port):
            starts.append(model)
            return ProcessHandle(model, port, FakeProcess(len(starts)))

        async def ready(*args, **kwargs):
            if len(starts) == 1:  # first attempt hangs until cancelled
                readiness_entered.set()
                await asyncio.Event().wait()

        _install(monkeypatch, start=start, ready=ready)
        swapper = Swapper(make_config())
        owner = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        waiter = None
        try:
            await asyncio.wait_for(readiness_entered.wait(), 1)
            waiter = asyncio.create_task(
                swapper.handle_request(StubRequest("a"))
            )
            await eventually(lambda: len(swapper._queue) == 1)
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 1)
            # The waiter is awarded ownership and runs its own load.
            response = await asyncio.wait_for(waiter, 1)
            assert response.status == 200

            assert starts == ["a", "a"]
            loads = _loads(swapper)
            assert loads["count"] == 1
            assert loads["abandoned"] == {"cancelled": 1}
            assert loads["failed"] == {}
            assert _model_loads(swapper, "a")["count"] == 1

            # Later requests for the healthy model add nothing.
            await swapper.handle_request(StubRequest("a"))
            assert _loads(swapper)["count"] == 1
        finally:
            for task in (owner, waiter):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (owner, waiter) if t is not None),
                return_exceptions=True,
            )
            await swapper.shutdown()


class TestUnfinishedLoads:
    async def test_start_failure_is_failed_and_not_timed(self, monkeypatch):
        async def start(model, cmd, port):
            raise OSError("fork")

        _install(monkeypatch, start=start)
        swapper = Swapper(make_config())
        try:
            response = await swapper.handle_request(StubRequest("a"))
            assert response.status == 500

            loads = _loads(swapper)
            assert loads["count"] == 0
            assert loads["avg_ms"] is None
            assert loads["failed"] == {"model_start_failed": 1}
            assert loads["abandoned"] == {}
            assert _model_loads(swapper, "a")["failed"] == {
                "model_start_failed": 1
            }
        finally:
            await swapper.shutdown()

    async def test_health_timeout_is_failed_and_not_timed(self, monkeypatch):
        calls = 0

        async def ready(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError

        _install(monkeypatch, ready=ready)
        swapper = Swapper(make_config())
        try:
            first = await swapper.handle_request(StubRequest("a"))
            assert first.status == 504
            second = await swapper.handle_request(StubRequest("a"))
            assert second.status == 200

            loads = _loads(swapper)
            assert loads["count"] == 1  # only the successful retry
            assert loads["failed"] == {"swap_timeout": 1}
            assert _model_loads(swapper, "a")["count"] == 1
            assert _model_loads(swapper, "a")["failed"] == {"swap_timeout": 1}
        finally:
            await swapper.shutdown()

    async def test_cleanup_failure_after_start_is_failed(self, monkeypatch):
        stop_calls = 0

        async def ready(*args, **kwargs):
            raise TimeoutError

        async def stop(handle):
            nonlocal stop_calls
            stop_calls += 1
            if stop_calls == 1:
                raise OSError("kill failed")
            handle.process.returncode = 0

        _install(monkeypatch, ready=ready, stop=stop)
        swapper = Swapper(make_config())
        try:
            response = await swapper.handle_request(StubRequest("a"))
            assert response.status == 503

            loads = _loads(swapper)
            assert loads["count"] == 0
            assert loads["failed"] == {"process_cleanup_failed": 1}
            assert loads["abandoned"] == {}
        finally:
            await swapper.shutdown()

    async def test_shutdown_during_load_is_abandoned(self, monkeypatch):
        readiness_entered = asyncio.Event()

        async def ready(*args, **kwargs):
            readiness_entered.set()
            await asyncio.Event().wait()

        _install(monkeypatch, ready=ready)
        swapper = Swapper(make_config(), quick_switch=True)
        owner = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        try:
            await asyncio.wait_for(readiness_entered.wait(), 1)
            await asyncio.wait_for(swapper.shutdown(), 1)
            response = await asyncio.wait_for(owner, 1)
            assert response.status == 503

            loads = _loads(swapper)
            assert loads["count"] == 0
            assert loads["failed"] == {}
            assert loads["abandoned"] == {"shutting_down": 1}
        finally:
            if not owner.done():
                owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    async def test_owner_cancel_during_load_is_abandoned(self, monkeypatch):
        readiness_entered = asyncio.Event()

        async def ready(*args, **kwargs):
            readiness_entered.set()
            await asyncio.Event().wait()

        _install(monkeypatch, ready=ready)
        swapper = Swapper(make_config())
        owner = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        try:
            await asyncio.wait_for(readiness_entered.wait(), 1)
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 1)

            loads = _loads(swapper)
            assert loads["count"] == 0
            assert loads["failed"] == {}
            assert loads["abandoned"] == {"cancelled": 1}
            assert swapper._current is None
        finally:
            await swapper.shutdown()


class TestCollectorLoads:
    def test_aggregates_global_and_per_model(self):
        from src.stats import StatsCollector

        collector = StatsCollector()
        collector.record_load("a", 1.0)
        collector.record_load("b", 2.0, stop_s=0.5, swap=True)
        collector.record_load("a", 4.0, stop_s=0.25, swap=True)
        collector.record_load_outcome("b", "failed", "swap_timeout")
        collector.record_load_outcome("b", "failed", "swap_timeout")
        collector.record_load_outcome("a", "abandoned", "cancelled")

        snap = collector.snapshot()
        loads = snap["loads"]
        assert loads["count"] == 3
        assert loads["avg_ms"] == pytest.approx(7000.0 / 3)
        assert loads["p95_ms"] == pytest.approx(4000.0)  # nearest rank
        assert loads["max_ms"] == pytest.approx(4000.0)
        assert loads["swaps"] == 2
        assert loads["stop"] == {
            "count": 2,
            "avg_ms": pytest.approx(375.0),
            "p95_ms": pytest.approx(500.0),
            "max_ms": pytest.approx(500.0),
        }
        assert loads["failed"] == {"swap_timeout": 2}
        assert loads["abandoned"] == {"cancelled": 1}

        a = snap["models"]["a"]["loads"]
        assert (a["count"], a["swaps"], a["stop"]["count"]) == (2, 1, 1)
        assert a["avg_ms"] == pytest.approx(2500.0)
        assert a["abandoned"] == {"cancelled": 1} and a["failed"] == {}
        b = snap["models"]["b"]["loads"]
        assert (b["count"], b["swaps"]) == (1, 1)
        assert b["failed"] == {"swap_timeout": 2}
        # record_swap is separate and untouched by loads.
        assert snap["swaps"] == EMPTY_DURATION
