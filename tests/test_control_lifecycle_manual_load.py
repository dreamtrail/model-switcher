from __future__ import annotations

import asyncio
import pytest

from aiohttp.test_utils import TestClient

from src.process_manager import ProcessStopError
from src.swapper import MAX_QUEUE, Swapper
from tests.control_helpers import HEADERS, handle, put_model
from tests.quick_switch_helpers import eventually, make_config, make_server


class TestManualLoad:
    async def test_cold_load_active_noop_and_switch_share_one_process_slot(
        self, monkeypatch
    ):
        import src.swapper as module

        starts, stops = [], []
        live = 0
        maximum_live = 0

        async def start(model, cmd, port):
            nonlocal live, maximum_live
            starts.append(model)
            live += 1
            maximum_live = max(maximum_live, live)
            return handle(model, len(starts))

        async def ready(*args, **kwargs):
            return None

        async def stop(stopped):
            nonlocal live
            stops.append(stopped.model_name)
            live -= 1
            stopped.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=60)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                cold = await put_model(client, "a")
                idle_task = swapper._idle_task
                idle_generation = swapper._idle_generation
                assert idle_task is not None

                same = await put_model(client, "a")
                assert same.status == 200
                assert (await same.json())["active_model"] == "a"
                # AC5: establish the no-op guarantee at the no-op boundary;
                # a subsequent switch must not hide a timer reset here.
                assert swapper._idle_task is idle_task
                assert swapper._idle_generation == idle_generation
                assert not idle_task.done()

                switched = await put_model(client, "b")

                assert cold.status == switched.status == 200
                assert (await cold.json())["active_model"] == "a"
                assert (await switched.json())["active_model"] == "b"
                assert swapper._idle_task is not idle_task  # switch schedules B
                assert swapper._idle_generation > idle_generation

            assert starts == ["a", "b"]
            assert stops == ["a"]
            assert maximum_live == 1
            stats = swapper.stats.snapshot()
            assert stats["models"]["a"]["starts"] == 1
            assert stats["models"]["a"]["stops"] == 1
            assert stats["models"]["b"]["starts"] == 1
            assert stats["requests"]["total"] == 0
        finally:
            await swapper.shutdown()
            await server.close()

    async def test_same_target_manual_calls_join_one_load(self, monkeypatch):
        import src.swapper as module

        readiness_entered = asyncio.Event()
        release_ready = asyncio.Event()
        starts = []

        async def start(model, cmd, port):
            starts.append(model)
            return handle(model)

        async def ready(*args, **kwargs):
            readiness_entered.set()
            await release_ready.wait()

        async def stop(stopped):
            stopped.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                owner = asyncio.create_task(put_model(client, "a"))
                await asyncio.wait_for(readiness_entered.wait(), 1)
                joiner = asyncio.create_task(put_model(client, "a"))
                await eventually(lambda: len(swapper._queue) == 1)
                assert swapper.control_state()["pending_model"] == "a"
                release_ready.set()
                responses = await asyncio.wait_for(asyncio.gather(owner, joiner), 1)
                assert [response.status for response in responses] == [200, 200]
            assert starts == ["a"]
            assert list(swapper._queue) == []
            assert swapper.stats.snapshot()["requests"]["total"] == 0
        finally:
            release_ready.set()
            await swapper.shutdown()
            await server.close()

    @pytest.mark.parametrize(
        ("quick_switch", "expected_first", "expected_second", "expected_starts"),
        [
            (False, 200, 409, ["a"]),
            (True, 409, 200, ["a", "b"]),
        ],
    )
    async def test_pending_different_manual_target_obeys_quick_switch_policy(
        self, monkeypatch, quick_switch, expected_first, expected_second, expected_starts
    ):
        import src.swapper as module

        a_ready = asyncio.Event()
        release_a = asyncio.Event()
        starts, stops = [], []

        async def start(model, cmd, port):
            starts.append(model)
            return handle(model, len(starts))

        async def ready(port, **kwargs):
            if port == 1001:
                a_ready.set()
                await release_a.wait()

        async def stop(stopped):
            stops.append(stopped.model_name)
            stopped.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), quick_switch=quick_switch, idle_timeout=0)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                first = asyncio.create_task(put_model(client, "a"))
                await asyncio.wait_for(a_ready.wait(), 1)
                second = asyncio.create_task(put_model(client, "b"))
                if quick_switch:
                    responses = await asyncio.wait_for(asyncio.gather(first, second), 1)
                else:
                    second_response = await asyncio.wait_for(second, 1)
                    assert (await second_response.json())["error"]["code"] == "switch_in_progress"
                    release_a.set()
                    responses = [await asyncio.wait_for(first, 1), second_response]

                assert [response.status for response in responses] == [
                    expected_first, expected_second
                ]
                if quick_switch:
                    assert (await responses[0].json())["error"]["code"] == "swap_superseded"
            assert starts == expected_starts
            assert stops == (["a"] if quick_switch else [])
            assert swapper.stats.snapshot()["requests"]["total"] == 0
        finally:
            release_a.set()
            await swapper.shutdown()
            await server.close()

    async def test_api_quick_switch_changes_affect_only_later_admissions(
        self, monkeypatch
    ):
        import src.swapper as module

        a_ready = asyncio.Event()
        b_ready = asyncio.Event()
        release_b = asyncio.Event()
        starts, stops = [], []

        async def start(model, cmd, port):
            starts.append(model)
            return handle(model, len(starts))

        async def ready(port, **kwargs):
            if port == 1001:
                a_ready.set()
                await asyncio.Event().wait()
            if port == 1002:
                b_ready.set()
                await release_b.wait()

        async def stop(stopped):
            stops.append(stopped.model_name)
            stopped.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), quick_switch=False, idle_timeout=0)
        server = await make_server(swapper)
        first = second = None
        try:
            async with TestClient(server) as client:
                first = asyncio.create_task(put_model(client, "a"))
                await asyncio.wait_for(a_ready.wait(), 1)
                a_generation = swapper._intent_generation
                assert swapper._desired_model == "a"

                enabled = await client.put(
                    "/api/v1/control/settings",
                    json={
                        "automatic_switch": True,
                        "quick_switch": True,
                        "idle_timeout_seconds": 0,
                    },
                    headers=HEADERS,
                )
                enabled_state = await enabled.json()
                assert enabled.status == 200
                assert enabled_state["pending_model"] == "a"
                assert enabled_state["settings"]["quick_switch"] is True
                assert swapper._intent_generation == a_generation
                assert swapper._desired_model == "a"
                assert not first.done()

                # This later admission observes quick-switch on and publishes B.
                second = asyncio.create_task(put_model(client, "b"))
                await asyncio.wait_for(b_ready.wait(), 1)
                b_generation = swapper._intent_generation
                assert b_generation == a_generation + 1
                assert swapper._desired_model == "b"

                disabled = await client.put(
                    "/api/v1/control/settings",
                    json={
                        "automatic_switch": True,
                        "quick_switch": False,
                        "idle_timeout_seconds": 0,
                    },
                    headers=HEADERS,
                )
                disabled_state = await disabled.json()
                assert disabled.status == 200
                assert disabled_state["pending_model"] == "b"
                assert disabled_state["settings"]["quick_switch"] is False
                assert swapper._intent_generation == b_generation
                assert swapper._desired_model == "b"
                assert not second.done()

                # This still-later admission observes quick-switch off and
                # cannot replace the already-published B generation.
                third = await put_model(client, "c")
                assert third.status == 409
                assert (await third.json())["error"]["code"] == "switch_in_progress"
                assert swapper._intent_generation == b_generation
                assert swapper._desired_model == "b"

                release_b.set()
                first_response, second_response = await asyncio.wait_for(
                    asyncio.gather(first, second), 1
                )
                assert first_response.status == 409
                assert (await first_response.json())["error"]["code"] == "swap_superseded"
                assert second_response.status == 200
                assert (await second_response.json())["active_model"] == "b"

            assert starts == ["a", "b"]
            assert stops == ["a"]
        finally:
            release_b.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )
            await swapper.shutdown()
            await server.close()

    @pytest.mark.parametrize(
        ("failure_phase", "status", "code"),
        [
            ("start", 500, "model_start_failed"),
            ("readiness", 504, "swap_timeout"),
        ],
    )
    async def test_manual_load_terminal_failures_map_without_request_metrics(
        self, monkeypatch, failure_phase, status, code
    ):
        import src.swapper as module

        stopped = []

        async def start(model, cmd, port):
            if failure_phase == "start":
                raise OSError("cannot start")
            return handle(model)

        async def ready(*args, **kwargs):
            raise TimeoutError("not ready")

        async def stop(tracked):
            stopped.append(tracked.model_name)
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                response = await put_model(client, "a")
                data = await response.json()
            assert response.status == status
            assert data["error"]["code"] == code
            assert stopped == ([] if failure_phase == "start" else ["a"])
            assert swapper.control_state()["lifecycle"] == "idle"
            assert swapper.stats.snapshot()["requests"]["total"] == 0
        finally:
            await swapper.shutdown()
            await server.close()

    async def test_manual_switch_cleanup_failure_retains_old_process_and_starts_no_replacement(
        self, monkeypatch
    ):
        import src.swapper as module

        starts = []
        old = handle("a")

        async def start(model, cmd, port):
            starts.append(model)
            return handle(model, 2)

        async def stop(tracked):
            assert tracked is old
            raise ProcessStopError("still running")

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        swapper._current, swapper._current_model, swapper._healthy = old, "a", True
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                response = await put_model(client, "b")
                data = await response.json()
            assert response.status == 503
            assert data["error"]["code"] == "process_cleanup_failed"
            assert starts == []
            assert swapper._current is old
            assert old.process.returncode is None
            assert swapper.control_state()["lifecycle"] == "cleanup_failed"
            assert swapper.stats.snapshot()["requests"]["total"] == 0
        finally:
            async def successful_stop(tracked):
                tracked.process.returncode = 0

            monkeypatch.setattr(module, "stop_process", successful_stop)
            await swapper.shutdown()
            await server.close()

    async def test_cancelled_manual_owner_cleans_spawned_process_without_orphan(
        self, monkeypatch
    ):
        import src.swapper as module

        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        spawned = handle("a")
        stops = []

        async def start(*args):
            spawn_entered.set()
            await release_spawn.wait()
            return spawned

        async def stop(tracked):
            stops.append(tracked)
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        task = asyncio.create_task(swapper.control_load_model("a"))
        await asyncio.wait_for(spawn_entered.wait(), 1)
        task.cancel()
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)

        assert stops == [spawned]
        assert swapper._current is None
        assert swapper._spawn_task is None
        assert swapper._cleanup_task is None
        assert swapper._swapping is False
        await swapper.shutdown()

    async def test_manual_waiters_are_bounded_and_queue_full_is_not_inference_metric(
        self, monkeypatch
    ):
        import src.swapper as module

        ready_entered = asyncio.Event()
        release_ready = asyncio.Event()

        async def start(model, cmd, port):
            return handle(model)

        async def ready(*args, **kwargs):
            ready_entered.set()
            await release_ready.wait()

        async def stop(tracked):
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        server = await make_server(swapper)
        tasks = []
        try:
            async with TestClient(server) as client:
                tasks.append(asyncio.create_task(put_model(client, "a")))
                await asyncio.wait_for(ready_entered.wait(), 1)
                tasks.extend(
                    asyncio.create_task(put_model(client, "a"))
                    for _ in range(MAX_QUEUE)
                )
                await eventually(lambda: len(swapper._queue) == MAX_QUEUE)
                overflow = await put_model(client, "a")
                assert overflow.status == 503
                overflow_data = await overflow.json()
                assert overflow_data["error"]["code"] is None
                assert len(swapper._queue) == MAX_QUEUE
                release_ready.set()
                completed = await asyncio.wait_for(asyncio.gather(*tasks), 1)
                assert all(response.status == 200 for response in completed)
            assert swapper.stats.snapshot()["requests"]["total"] == 0
        finally:
            release_ready.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await swapper.shutdown()
            await server.close()
