from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from src.process_manager import ProcessHandle
from src.swapper import Swapper
from tests.quick_switch_helpers import (
    FakeProcess,
    StubRequest,
    assert_http_error,
    eventually,
    make_config,
    make_server,
    post,
)


class TestLifecycleRaces:
    async def test_supersession_during_spawn_does_not_cancel_spawn(
        self, monkeypatch
    ):
        """A returned stale handle is tracked and stopped before B starts (AC7)."""
        import src.swapper as module

        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        starts, stops = [], []
        spawn_cancelled = False

        async def start(model, cmd, port):
            nonlocal spawn_cancelled
            starts.append(model)
            if model == "a":
                spawn_entered.set()
                try:
                    await release_spawn.wait()
                except asyncio.CancelledError:
                    spawn_cancelled = True
                    raise
            return ProcessHandle(model, port, FakeProcess(len(starts)))

        async def ready(*args, **kwargs):
            return None

        async def stop(handle):
            stops.append(handle.model_name)
            handle.process.returncode = 0

        async def proxy(*args, **kwargs):
            return web.json_response({"ok": True})

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        monkeypatch.setattr(module, "proxy_request", proxy)
        swapper = Swapper(make_config(), quick_switch=True)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                request_a = asyncio.create_task(post(client, "a"))
                await asyncio.wait_for(spawn_entered.wait(), 1)
                request_b = asyncio.create_task(post(client, "b"))
                await eventually(lambda: swapper._desired_model == "b")
                assert spawn_cancelled is False
                release_spawn.set()
                response_a, response_b = await asyncio.wait_for(
                    asyncio.gather(request_a, request_b), 1
                )
                await assert_http_error(response_a, 409, "swap_superseded")
                assert response_b.status == 200

            assert spawn_cancelled is False
            assert starts == ["a", "b"]
            assert stops == ["a"]
            assert swapper._spawn_task is None
        finally:
            release_spawn.set()
            await swapper.shutdown()
            await server.close()

    async def test_post_readiness_generation_race_never_commits_stale(
        self, monkeypatch
    ):
        """A supersession published as readiness completes prevents commit (AC7)."""
        import src.swapper as module

        handle = ProcessHandle("a", 1001, FakeProcess(1))
        stops = []
        superseding_admission = []

        async def start(*args):
            return handle

        async def ready(*args, **kwargs):
            asyncio.get_running_loop().call_soon(
                lambda: superseding_admission.append(
                    swapper._admit_request("b")
                )
            )

        async def stop(stopped_handle):
            stops.append(stopped_handle.model_name)
            stopped_handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), quick_switch=True)
        admission, generation = swapper._admit_request("a")
        assert admission == "owner"

        async with swapper._lock:
            with pytest.raises(module._SwapSuperseded):
                await swapper._ensure_model("a", generation)
            outcome = swapper._finish_owner(
                generation, module._LoadOutcome("swap_superseded")
            )

        assert outcome.kind == "swap_superseded"
        assert stops == ["a"]
        assert swapper._current is None
        assert swapper._healthy is False
        assert swapper.stats.snapshot()["swaps"]["count"] == 0
        assert superseding_admission[0][0] == "waiter"
        assert await superseding_admission[0][1] is module._OWNER
        swapper._swapping = False
        swapper._desired_model = None
        await swapper.shutdown()

    async def test_readiness_timeout_racing_owner_cancel_preserves_cancel(
        self, monkeypatch
    ):
        """A terminal timeout cannot replace owner cancellation or emit 504."""
        import src.swapper as module

        ready_entered = asyncio.Event()
        release_ready = asyncio.Event()
        handle = ProcessHandle("a", 1001, FakeProcess(41))
        stops = []
        owner_task = None

        async def start(*args):
            return handle

        async def ready(*args, **kwargs):
            ready_entered.set()
            await release_ready.wait()
            asyncio.get_running_loop().call_soon(owner_task.cancel)
            raise TimeoutError("ready timed out")

        async def stop(stopped_handle):
            stops.append(stopped_handle.model_name)
            stopped_handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config())
        owner_task = asyncio.create_task(
            swapper.handle_request(StubRequest("a"))
        )
        try:
            await asyncio.wait_for(ready_entered.wait(), 1)
            release_ready.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner_task, 1)

            assert stops == ["a"]
            assert swapper._current is None
            assert swapper._spawn_task is None
            assert swapper._readiness_task is None
            assert swapper._cleanup_task is None
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {}
            assert snapshot["models"]["a"]["starts"] == 1
            assert snapshot["models"]["a"]["stops"] == 1
            assert snapshot["swaps"]["count"] == 0
        finally:
            release_ready.set()
            if not owner_task.done():
                owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)
            await swapper.shutdown()

    async def test_cancellation_after_owner_award_reoffers_ownership(
        self, monkeypatch
    ):
        """Cancellation after an _OWNER award cannot wedge _swapping (AC14)."""
        import src.swapper as module

        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        starts = []

        async def start(model, cmd, port):
            starts.append(model)
            if len(starts) == 1:
                spawn_entered.set()
                await release_spawn.wait()
            return ProcessHandle(model, port, FakeProcess(50 + len(starts)))

        async def ready(*args, **kwargs):
            return None

        async def stop(handle):
            handle.process.returncode = 0

        async def proxy(*args, **kwargs):
            return web.json_response({"ok": True})

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        monkeypatch.setattr(module, "proxy_request", proxy)
        swapper = Swapper(make_config())
        owner = asyncio.create_task(
            swapper.handle_request(StubRequest("a"))
        )
        first_waiter = second_waiter = None
        try:
            await asyncio.wait_for(spawn_entered.wait(), 1)
            first_waiter = asyncio.create_task(
                swapper.handle_request(StubRequest("a"))
            )
            second_waiter = asyncio.create_task(
                swapper.handle_request(StubRequest("a"))
            )
            await eventually(lambda: len(swapper._queue) == 2)

            original_handoff = swapper._handoff_current_intent
            cancelled_award = False

            def handoff_and_cancel_first():
                nonlocal cancelled_award
                handed_off = original_handoff()
                if handed_off and not cancelled_award:
                    cancelled_award = True
                    first_waiter.cancel()
                return handed_off

            swapper._handoff_current_intent = handoff_and_cancel_first
            owner.cancel()
            release_spawn.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 1)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first_waiter, 1)
            response = await asyncio.wait_for(second_waiter, 1)
            assert response.status == 200
            assert cancelled_award is True
            assert starts == ["a", "a"]
            assert swapper._swapping is False
            assert swapper._desired_model is None
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            assert swapper._spawn_task is None
            assert swapper._cleanup_task is None
        finally:
            release_spawn.set()
            for task in (owner, first_waiter, second_waiter):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (owner, first_waiter, second_waiter)
                  if task is not None),
                return_exceptions=True,
            )
            await swapper.shutdown()
