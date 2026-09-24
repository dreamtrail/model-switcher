from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient

from src.process_manager import ProcessStopError
from src.swapper import Swapper
from tests.control_helpers import delete_model, handle, put_model
from tests.quick_switch_helpers import (
    StubRequest,
    eventually,
    make_config,
    make_server,
    response_body,
)


class TestManualUnload:
    async def test_unload_is_idempotent_waits_for_cleanup_and_records_real_stop(
        self, monkeypatch
    ):
        import src.swapper as module

        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()
        stops = []

        async def stop(tracked):
            stops.append(tracked.model_name)
            stop_entered.set()
            await release_stop.wait()
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                first = asyncio.create_task(delete_model(client))
                await asyncio.wait_for(stop_entered.wait(), 1)
                assert not first.done()
                state = swapper.control_state()
                assert state["lifecycle"] == "unloading"
                assert state["active_model"] == "a"
                release_stop.set()
                first_response = await asyncio.wait_for(first, 1)
                second_response = await delete_model(client)
                assert first_response.status == second_response.status == 200
                assert (await first_response.json())["lifecycle"] == "idle"
                assert (await second_response.json())["lifecycle"] == "idle"
            assert stops == ["a"]
            assert swapper.stats.snapshot()["models"]["a"]["stops"] == 1
        finally:
            release_stop.set()
            await swapper.shutdown()
            await server.close()

    async def test_pending_load_and_concurrent_stop_only_actions_return_busy_without_mutation(
        self, monkeypatch
    ):
        import src.swapper as module

        ready_entered = asyncio.Event()
        release_ready = asyncio.Event()
        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()

        async def start(model, cmd, port):
            return handle(model)

        async def ready(*args, **kwargs):
            ready_entered.set()
            await release_ready.wait()

        async def stop(tracked):
            stop_entered.set()
            await release_stop.wait()
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                loading = asyncio.create_task(put_model(client, "a"))
                await asyncio.wait_for(ready_entered.wait(), 1)
                busy_unload = await delete_model(client)
                assert busy_unload.status == 409
                assert (await busy_unload.json())["error"]["code"] == "control_busy"
                assert swapper._desired_model == "a"
                release_ready.set()
                assert (await asyncio.wait_for(loading, 1)).status == 200

                unloading = asyncio.create_task(delete_model(client))
                await asyncio.wait_for(stop_entered.wait(), 1)
                second_unload = await delete_model(client)
                competing_load = await put_model(client, "b")
                inference = await swapper.handle_request(StubRequest("a"))
                assert second_unload.status == competing_load.status == 409
                assert (await second_unload.json())["error"]["code"] == "control_busy"
                assert (await competing_load.json())["error"]["code"] == "control_busy"
                assert inference.status == 409
                assert response_body(inference)["error"]["code"] == "control_busy"
                assert swapper._current_model == "a"
                release_stop.set()
                assert (await asyncio.wait_for(unloading, 1)).status == 200

            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["total"] == 1
            assert snapshot["requests"]["errors"] == {"409": 1}
        finally:
            release_ready.set()
            release_stop.set()
            await swapper.shutdown()
            await server.close()

    async def test_cleanup_failure_retains_handle_then_retry_transitions_through_unloading(
        self, monkeypatch
    ):
        import src.swapper as module

        attempts = 0
        retry_entered = asyncio.Event()
        release_retry = asyncio.Event()
        tracked = handle("a")

        async def stop(stopped):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ProcessStopError("still running")
            retry_entered.set()
            await release_retry.wait()
            stopped.process.returncode = 0

        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        swapper._current, swapper._current_model, swapper._healthy = tracked, "a", True
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                failed = await delete_model(client)
                assert failed.status == 503
                assert (await failed.json())["error"]["code"] == "process_cleanup_failed"
                assert swapper._current is tracked
                assert swapper.control_state()["lifecycle"] == "cleanup_failed"
                assert swapper.stats.snapshot()["models"].get("a", {}).get("stops", 0) == 0

                retry = asyncio.create_task(delete_model(client))
                await asyncio.wait_for(retry_entered.wait(), 1)
                assert swapper.control_state()["lifecycle"] == "unloading"
                release_retry.set()
                response = await asyncio.wait_for(retry, 1)
                assert response.status == 200
                assert (await response.json())["lifecycle"] == "idle"
            assert attempts == 2
            assert swapper.stats.snapshot()["models"]["a"]["stops"] == 1
            assert swapper.stats.snapshot()["requests"]["total"] == 0
        finally:
            release_retry.set()
            if swapper._current is not None:
                swapper._current.process.returncode = 0
            await swapper.shutdown()
            await server.close()

    async def test_control_state_cannot_clear_dead_retained_cleanup_failure(
        self, monkeypatch
    ):
        import src.swapper as module

        stop_attempts = []
        tracked = handle("a")
        tracked.process.returncode = -9  # Parent is dead; handle remains retained.

        async def stop(stopped):
            stop_attempts.append(stopped)

        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        swapper._current = tracked
        swapper._current_model = "a"
        swapper._healthy = False
        swapper._cleanup_failed = True
        try:
            first = swapper.control_state()
            second = swapper.control_state()

            assert first["lifecycle"] == second["lifecycle"] == "cleanup_failed"
            assert first["active_model"] is second["active_model"] is None
            assert swapper._current is tracked
            assert swapper._current_model == "a"
            assert swapper._cleanup_failed is True
            assert stop_attempts == []

            # Only the owned lifecycle retry may confirm cleanup and clear it.
            assert await swapper.control_unload_model() == "success"
            assert stop_attempts == [tracked]
            assert swapper._current is None
            assert swapper._cleanup_failed is False
            assert swapper.control_state()["lifecycle"] == "idle"
        finally:
            await swapper.shutdown()

    async def test_shutdown_wins_snapshot_and_new_mutations_during_unload_overlap(
        self, monkeypatch
    ):
        import src.swapper as module

        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()

        async def stop(tracked):
            stop_entered.set()
            await release_stop.wait()
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        unload = asyncio.create_task(swapper.control_unload_model())
        await asyncio.wait_for(stop_entered.wait(), 1)
        shutdown = asyncio.create_task(swapper.shutdown())
        await eventually(lambda: swapper._shutting_down)

        assert swapper.control_state()["lifecycle"] == "shutting_down"
        assert swapper.control_apply_settings(
            automatic_switch=True, quick_switch=True, idle_timeout_seconds=0
        ) == "shutting_down"
        assert await swapper.control_load_model("b") == "shutting_down"
        assert await swapper.control_unload_model() == "shutting_down"

        release_stop.set()
        assert await asyncio.wait_for(unload, 1) == "success"
        await asyncio.wait_for(shutdown, 1)
