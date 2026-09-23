"""Deterministic lifecycle/control races for P-20260923-01."""

from __future__ import annotations

import asyncio
import builtins
import os
from pathlib import Path
import shutil

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from src.control import CONTROL_HEADER
from src.process_manager import ProcessHandle, ProcessStopError
from src.swapper import MAX_QUEUE, Swapper
from tests.quick_switch_helpers import (
    FakeProcess,
    StubRequest,
    eventually,
    install_success_lifecycle,
    make_config,
    make_server,
    response_body,
)


HEADERS = {CONTROL_HEADER: "1"}


async def put_model(client: TestClient, model: str):
    return await client.put(
        "/api/v1/control/model", json={"model": model}, headers=HEADERS
    )


async def delete_model(client: TestClient):
    return await client.delete("/api/v1/control/model", headers=HEADERS)


def handle(model: str, pid: int = 1) -> ProcessHandle:
    ports = {"a": 1001, "b": 1002, "c": 1003}
    return ProcessHandle(model, ports[model], FakeProcess(pid))


class TestLifecycleStatePriority:
    @pytest.mark.parametrize(
        ("case", "expected_lifecycle", "expected_active", "expected_pending"),
        [
            ("idle", "idle", None, None),
            ("ready-with-idle-timer", "ready", "a", None),
            ("cleanup-failed", "cleanup_failed", None, None),
            ("loading-over-ready-and-failure", "loading", None, "b"),
            ("unloading-over-loading", "unloading", None, None),
            ("shutdown-over-everything", "shutting_down", None, None),
        ],
    )
    async def test_state_uses_all_six_lifecycle_values_and_priority_order(
        self, case, expected_lifecycle, expected_active, expected_pending
    ):
        swapper = Swapper(make_config(), idle_timeout=60)
        sleeping_timer = None
        tracked = handle("a")
        if case != "idle":
            swapper._current = tracked
            swapper._current_model = "a"
        if case == "ready-with-idle-timer":
            swapper._healthy = True
            sleeping_timer = asyncio.create_task(asyncio.Event().wait())
            swapper._idle_task = sleeping_timer
        elif case == "cleanup-failed":
            swapper._cleanup_failed = True
        elif case == "loading-over-ready-and-failure":
            swapper._healthy = False
            swapper._cleanup_failed = True
            swapper._swapping = True
            swapper._desired_model = "b"
        elif case == "unloading-over-loading":
            swapper._cleanup_failed = True
            swapper._swapping = True
            swapper._desired_model = "b"
            swapper._stop_only_operation = "manual"
        elif case == "shutdown-over-everything":
            swapper._cleanup_failed = True
            swapper._swapping = True
            swapper._desired_model = "b"
            swapper._stop_only_operation = "manual"
            swapper._shutting_down = True

        state = swapper.control_state()
        assert state["lifecycle"] == expected_lifecycle
        assert state["active_model"] == expected_active
        assert state["pending_model"] == expected_pending
        assert (state["pending_model"] is not None) is (
            state["lifecycle"] == "loading"
        )

        if sleeping_timer is not None:
            sleeping_timer.cancel()
            await asyncio.gather(sleeping_timer, return_exceptions=True)
            swapper._idle_task = None
        # Avoid invoking lifecycle behavior after deliberately inconsistent
        # priority-table fixtures have served their purpose.
        swapper._current = None
        swapper._stop_only_operation = None
        swapper._swapping = False
        swapper._shutting_down = False
        await swapper.shutdown()

    async def test_actual_switch_cleanup_reports_loading_with_target(
        self, monkeypatch
    ):
        import src.swapper as module

        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()

        async def start(model, cmd, port):
            return handle(model, 2)

        async def ready(*args, **kwargs):
            return None

        async def stop(stopped):
            if stopped.model_name == "a":
                stop_entered.set()
                await release_stop.wait()
            stopped.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        old = handle("a")
        swapper._current, swapper._current_model, swapper._healthy = old, "a", True
        task = asyncio.create_task(swapper.control_load_model("b"))
        try:
            await asyncio.wait_for(stop_entered.wait(), 1)
            assert swapper.control_state() | {} == {
                "models": ["a", "b", "c"],
                "active_model": "a",
                "pending_model": "b",
                "lifecycle": "loading",
                "inflight_requests": 0,
                "settings": {
                    "automatic_switch": True,
                    "quick_switch": False,
                    "idle_timeout_seconds": 0.0,
                },
            }
            release_stop.set()
            assert await asyncio.wait_for(task, 1) == "success"
        finally:
            release_stop.set()
            await asyncio.gather(task, return_exceptions=True)
            await swapper.shutdown()


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


class TestRuntimePolicyAndRaces:
    @pytest.mark.parametrize(
        ("scenario", "expected_status", "expected_code"),
        [
            ("unknown-over-shutdown", 404, "internal_error"),
            ("shutdown-over-busy-policy", 503, "shutting_down"),
            ("busy-over-policy", 409, "control_busy"),
            ("policy-over-existing-admission", 409, "automatic_switch_disabled"),
        ],
    )
    async def test_inference_rejection_precedence_and_exactly_once_metrics(
        self, scenario, expected_status, expected_code
    ):
        swapper = Swapper(make_config(), quick_switch=False, idle_timeout=0)
        swapper.automatic_switch = False
        model = "ghost" if scenario == "unknown-over-shutdown" else "b"
        if scenario in ("unknown-over-shutdown", "shutdown-over-busy-policy"):
            swapper._shutting_down = True
            swapper._stop_only_operation = "manual"
        elif scenario == "busy-over-policy":
            swapper._stop_only_operation = "manual"
        else:
            swapper._swapping = True
            swapper._desired_model = "a"
            for _ in range(MAX_QUEUE):
                swapper._queue_request()

        response = await swapper.handle_request(StubRequest(model))
        assert response.status == expected_status
        assert response_body(response)["error"]["code"] == expected_code
        snapshot = swapper.stats.snapshot()
        assert snapshot["requests"]["total"] == 1
        assert snapshot["requests"]["errors"] == {str(expected_status): 1}
        assert snapshot["models"][model]["requests"]["errors"] == {
            str(expected_status): 1
        }

        swapper._shutting_down = False
        swapper._stop_only_operation = None
        swapper._swapping = False
        swapper._desired_model = None
        await swapper._drain_queue()
        await swapper.shutdown()

    async def test_admitted_inference_finishes_after_automatic_switch_is_disabled(
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

        async def proxy(*args, **kwargs):
            return web.json_response({"ok": True})

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        monkeypatch.setattr(module, "proxy_request", proxy)
        swapper = Swapper(make_config(), idle_timeout=0)
        admitted = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        await asyncio.wait_for(ready_entered.wait(), 1)
        assert swapper.control_apply_settings(
            automatic_switch=False, quick_switch=False, idle_timeout_seconds=0
        ) == "success"
        pending_new = await swapper.handle_request(StubRequest("a"))
        assert pending_new.status == 409
        assert response_body(pending_new)["error"]["code"] == "automatic_switch_disabled"
        release_ready.set()
        completed = await asyncio.wait_for(admitted, 1)
        assert completed.status == 200
        await swapper.shutdown()

    async def test_manual_and_inference_share_quick_latest_wins_ordering(
        self, monkeypatch
    ):
        import src.swapper as module

        a_ready = asyncio.Event()
        starts, stops = [], []
        live = 0
        maximum_live = 0

        async def start(model, cmd, port):
            nonlocal live, maximum_live
            starts.append(model)
            live += 1
            maximum_live = max(maximum_live, live)
            return handle(model, len(starts))

        async def ready(port, **kwargs):
            if port == 1001:
                a_ready.set()
                await asyncio.Event().wait()

        async def stop(tracked):
            nonlocal live
            stops.append(tracked.model_name)
            live -= 1
            tracked.process.returncode = 0

        async def proxy(*args, **kwargs):
            return web.json_response({"model": "b"})

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        monkeypatch.setattr(module, "proxy_request", proxy)
        swapper = Swapper(make_config(), quick_switch=True, idle_timeout=0)
        manual = asyncio.create_task(swapper.control_load_model("a"))
        await asyncio.wait_for(a_ready.wait(), 1)
        inference = asyncio.create_task(swapper.handle_request(StubRequest("b")))
        manual_outcome, inference_response = await asyncio.wait_for(
            asyncio.gather(manual, inference), 1
        )

        assert manual_outcome == "swap_superseded"
        assert inference_response.status == 200
        assert starts == ["a", "b"]
        assert stops == ["a"]
        assert maximum_live == 1
        assert swapper.active_model == "b"
        snapshot = swapper.stats.snapshot()
        assert snapshot["requests"]["total"] == 1
        assert snapshot["requests"]["errors"] == {}
        await swapper.shutdown()

    async def test_manual_unload_can_interrupt_old_inflight_proxy(self, monkeypatch):
        import src.swapper as module

        proxy_entered = asyncio.Event()
        release_proxy = asyncio.Event()
        stopped_while_inflight = []

        async def proxy(*args, **kwargs):
            proxy_entered.set()
            await release_proxy.wait()
            return web.json_response({"ok": True})

        async def stop(tracked):
            stopped_while_inflight.append(swapper._active_requests)
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "proxy_request", proxy)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        request = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        await asyncio.wait_for(proxy_entered.wait(), 1)
        assert swapper.control_state()["inflight_requests"] == 1
        assert await swapper.control_unload_model() == "success"
        assert stopped_while_inflight == [1]
        assert swapper.active_model is None
        release_proxy.set()
        assert (await asyncio.wait_for(request, 1)).status == 200
        assert swapper.control_state()["inflight_requests"] == 0
        await swapper.shutdown()


class TestControlPersistenceBoundary:
    async def test_settings_load_and_unload_use_no_write_capable_filesystem_entrypoint(
        self, monkeypatch
    ):
        import src.swapper as module

        swapper = Swapper(make_config(), idle_timeout=0)
        config_snapshot = {
            name: (model.cmd, model.port)
            for name, model in swapper.config.models.items()
        }
        writes = []
        recording = True

        async def start(model, cmd, port):
            return handle(model)

        async def ready(*args, **kwargs):
            return None

        async def stop(tracked):
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)

        def note(name, *details):
            if recording:
                writes.append((name, details))

        original_open = builtins.open

        def audited_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                note("builtins.open", file, mode)
            return original_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", audited_open)

        original_path_open = Path.open

        def audited_path_open(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                note("Path.open", path, mode)
            return original_path_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", audited_path_open)

        original_os_open = os.open
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

        def audited_os_open(path, flags, *args, **kwargs):
            if flags & write_flags:
                note("os.open", path, flags)
            return original_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", audited_os_open)

        def audit_all_calls(owner, names):
            for name in names:
                original = getattr(owner, name)

                def audited(*args, _name=name, _original=original, **kwargs):
                    note(f"{owner.__name__}.{_name}", *args[:2])
                    return _original(*args, **kwargs)

                monkeypatch.setattr(owner, name, audited)

        audit_all_calls(
            Path,
            (
                "write_text",
                "write_bytes",
                "touch",
                "mkdir",
                "unlink",
                "rename",
                "replace",
            ),
        )
        audit_all_calls(
            os,
            ("mkdir", "makedirs", "remove", "unlink", "rename", "replace"),
        )
        audit_all_calls(shutil, ("copyfile", "copy", "copy2", "move", "rmtree"))

        try:
            assert swapper.control_apply_settings(
                automatic_switch=False,
                quick_switch=True,
                idle_timeout_seconds=0,
            ) == "success"
            assert await swapper.control_load_model("a") == "success"
            assert await swapper.control_unload_model() == "success"

            assert writes == []
            assert {
                name: (model.cmd, model.port)
                for name, model in swapper.config.models.items()
            } == config_snapshot
            assert swapper.control_state()["lifecycle"] == "idle"
        finally:
            # Swapper shutdown legitimately removes its construction-time
            # temporary directory; it is not a control-triggered write.
            recording = False
            await swapper.shutdown()


class TestIdleSettingTransitions:
    async def test_positive_update_restarts_sleeping_timer_and_zero_cancels_it(
        self, monkeypatch
    ):
        swapper = Swapper(make_config(), idle_timeout=10)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        started = asyncio.Queue()

        async def fake_idle(generation):
            await started.put((generation, swapper.idle_timeout, asyncio.current_task()))
            await asyncio.Event().wait()

        monkeypatch.setattr(swapper, "_idle_unload_after_timeout", fake_idle)
        swapper._schedule_idle_timer()
        first_generation, first_timeout, first_task = await asyncio.wait_for(started.get(), 1)
        assert first_timeout == 10

        assert swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=25
        ) == "success"
        second_generation, second_timeout, second_task = await asyncio.wait_for(started.get(), 1)
        assert second_timeout == 25
        assert second_generation > first_generation
        assert second_task is swapper._idle_task
        await asyncio.sleep(0)
        assert first_task.cancelled()

        # Repeating the representation is idempotent and keeps the countdown.
        swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=25
        )
        assert swapper._idle_task is second_task
        swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=0
        )
        await asyncio.sleep(0)
        assert second_task.cancelled()
        assert swapper._idle_task is None
        swapper._current = None
        await swapper.shutdown()

    async def test_active_request_defers_new_timeout_until_final_activity_ends(
        self, monkeypatch
    ):
        swapper = Swapper(make_config(), idle_timeout=10)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        started = asyncio.Event()
        observed = []

        async def fake_idle(generation):
            observed.append((generation, swapper.idle_timeout))
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(swapper, "_idle_unload_after_timeout", fake_idle)
        swapper._begin_activity()
        swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=30
        )
        assert swapper._idle_task is None
        swapper._end_activity()
        await asyncio.wait_for(started.wait(), 1)
        assert observed == [(swapper._idle_generation, 30)]
        swapper._cancel_idle_timer(force=True)
        swapper._current = None
        await swapper.shutdown()

    async def test_setting_change_does_not_cancel_idle_cleanup_after_commit(
        self, monkeypatch
    ):
        import src.swapper as swapper_module
        import src.swapper_lifecycle as lifecycle_module

        sleep_entered = asyncio.Event()
        release_sleep = asyncio.Event()
        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()
        real_sleep = asyncio.sleep

        async def controlled_sleep(delay):
            assert delay == 10
            sleep_entered.set()
            await release_sleep.wait()

        async def stop(tracked):
            stop_entered.set()
            await release_stop.wait()
            tracked.process.returncode = 0

        monkeypatch.setattr(lifecycle_module.asyncio, "sleep", controlled_sleep)
        monkeypatch.setattr(swapper_module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=10)
        swapper._current, swapper._current_model, swapper._healthy = handle("a"), "a", True
        swapper._schedule_idle_timer()
        task = swapper._idle_task
        assert task is not None
        await asyncio.wait_for(sleep_entered.wait(), 1)
        release_sleep.set()
        await asyncio.wait_for(stop_entered.wait(), 1)
        assert swapper._stop_only_operation == "idle"

        swapper.control_apply_settings(
            automatic_switch=True, quick_switch=False, idle_timeout_seconds=99
        )
        assert swapper._idle_task is task
        assert not task.cancelled()
        release_stop.set()
        await asyncio.wait_for(task, 1)
        assert swapper.control_state()["lifecycle"] == "idle"
        assert swapper.idle_timeout == 99
        # Restore before shutdown because the monkeypatch altered the shared
        # asyncio module object rather than a private facade.
        monkeypatch.setattr(lifecycle_module.asyncio, "sleep", real_sleep)
        await swapper.shutdown()
