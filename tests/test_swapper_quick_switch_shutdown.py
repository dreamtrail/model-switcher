from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient
import pytest

from src.process_manager import ProcessHandle, ProcessStopError
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


class TestCancellationAndShutdown:
    @pytest.mark.parametrize("phase", ["spawn", "readiness"])
    async def test_shutdown_during_lifecycle_returns_503_and_no_shutdown_stop(
        self, monkeypatch, phase
    ):
        """Shutdown publishes first, waits for spawn, and never counts stop (AC10/AC11)."""
        import src.swapper as module

        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        readiness_entered = asyncio.Event()
        spawn_cancelled = False
        stops = []

        async def start(model, cmd, port):
            nonlocal spawn_cancelled
            spawn_entered.set()
            if phase == "spawn":
                try:
                    await release_spawn.wait()
                except asyncio.CancelledError:
                    spawn_cancelled = True
                    raise
            return ProcessHandle(model, port, FakeProcess(1))

        async def ready(*args, **kwargs):
            readiness_entered.set()
            await asyncio.Event().wait()

        async def stop(handle):
            stops.append(handle.model_name)
            handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), quick_switch=True)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                owner = asyncio.create_task(post(client, "a"))
                event = spawn_entered if phase == "spawn" else readiness_entered
                await asyncio.wait_for(event.wait(), 1)
                shutdown = asyncio.create_task(swapper.shutdown())
                await eventually(lambda: swapper._shutting_down)
                release_spawn.set()
                response = await asyncio.wait_for(owner, 1)
                await assert_http_error(response, 503, "shutting_down")
                await asyncio.wait_for(shutdown, 1)

            assert spawn_cancelled is False
            assert stops == ["a"]
            assert swapper._current is None
            assert swapper._spawn_task is None
            assert swapper._readiness_task is None
            snapshot = swapper.stats.snapshot()
            assert snapshot["models"]["a"]["starts"] == 1
            assert snapshot["models"]["a"]["stops"] == 0
            assert snapshot["requests"]["errors"] == {"503": 1}
        finally:
            release_spawn.set()
            await server.close()

    async def test_shutdown_waits_for_failed_idle_cleanup_before_503_fanout(
        self, monkeypatch
    ):
        """Idle/shutdown retries establish cleanup failure before responses.

        The idle unload owns the lifecycle lock while its first stop attempt is
        unresolved.  Shutdown intent must not make the owner or either joiner
        irreversibly observe ``shutting_down`` before the retained handle's
        cleanup result is known (AC6/AC10/AC11/AC14).
        """
        import src.swapper as module

        class FakeClient:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        first_stop_entered = asyncio.Event()
        release_first_stop = asyncio.Event()
        second_stop_entered = asyncio.Event()
        release_second_stop = asyncio.Event()
        final_stop_entered = asyncio.Event()
        release_final_stop = asyncio.Event()
        stop_attempts = 0
        starts = []
        handle = ProcessHandle("a", 1001, FakeProcess(111))
        client = FakeClient()

        async def start(model, cmd, port):
            starts.append(model)
            return ProcessHandle(model, port, FakeProcess(112))

        async def stop(stopped_handle):
            nonlocal stop_attempts
            assert stopped_handle is handle
            stop_attempts += 1
            if stop_attempts == 1:
                first_stop_entered.set()
                await release_first_stop.wait()
            elif stop_attempts == 2:
                second_stop_entered.set()
                await release_second_stop.wait()
            else:
                final_stop_entered.set()
                await release_final_stop.wait()
            raise ProcessStopError(f"failed stop attempt {stop_attempts}")

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(
            make_config(), idle_timeout=0, client=client,
            automatic_switch=True,
        )
        swapper._current = handle
        swapper._current_model = "a"
        swapper._healthy = True
        swapper._idle_generation = 1
        temp_dir = swapper._temp_dir
        assert temp_dir is not None and temp_dir.exists()

        idle = asyncio.create_task(
            swapper._idle_unload_after_timeout(1), name="test-idle-unload"
        )
        swapper._idle_task = idle
        owner = joiner_one = joiner_two = shutdown = None
        try:
            await asyncio.wait_for(first_stop_entered.wait(), 1)
            owner = asyncio.create_task(
                swapper.handle_request(StubRequest("b"))
            )
            await eventually(lambda: swapper._swapping)
            joiner_one = asyncio.create_task(
                swapper.handle_request(StubRequest("b"))
            )
            joiner_two = asyncio.create_task(
                swapper.handle_request(StubRequest("b"))
            )
            await eventually(lambda: len(swapper._queue) == 2)

            shutdown = asyncio.create_task(swapper.shutdown())
            await eventually(lambda: swapper._shutting_down)
            requests = [owner, joiner_one, joiner_two]
            assert all(not request.done() for request in requests)
            assert not shutdown.done()

            release_first_stop.set()
            await asyncio.wait_for(second_stop_entered.wait(), 1)
            # Shutdown intent is published, but the owner and joiners remain
            # blocked until the retry determines whether cleanup succeeded.
            assert all(not request.done() for request in requests)
            assert starts == []
            assert swapper._current is handle

            release_second_stop.set()
            await asyncio.wait_for(final_stop_entered.wait(), 1)
            responses = await asyncio.wait_for(
                asyncio.gather(*requests), 1
            )
            for response in responses:
                await assert_http_error(
                    response, 503, "process_cleanup_failed"
                )

            assert not shutdown.done()
            assert client.closed is False
            assert temp_dir.exists()
            assert starts == []
            assert swapper._current is handle
            assert swapper._healthy is False

            release_final_stop.set()
            with pytest.raises(
                ProcessStopError, match="Model lifecycle cleanup failed"
            ):
                await asyncio.wait_for(shutdown, 1)
            await asyncio.wait_for(idle, 1)

            assert client.closed is True
            assert swapper._client is None
            assert swapper._temp_dir is None
            assert not temp_dir.exists()
            assert stop_attempts == 3
            assert handle.stopped is False
            assert swapper._spawn_task is None
            assert swapper._readiness_task is None
            assert swapper._cleanup_task is None
            assert swapper._idle_task is None
            assert swapper._swapping is False
            assert swapper._desired_model is None
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"503": 3}
            assert snapshot["models"]["b"]["requests"]["errors"] == {
                "503": 3
            }
            assert snapshot["models"].get("a", {}).get("stops", 0) == 0
            assert snapshot["swaps"]["count"] == 0
        finally:
            release_first_stop.set()
            release_second_stop.set()
            release_final_stop.set()
            tasks = [
                task
                for task in (owner, joiner_one, joiner_two, shutdown, idle)
                if task is not None
            ]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_shutdown_after_idle_failure_confirms_cleanup_before_response(
        self, monkeypatch
    ):
        """A confirmed shutdown retry yields shutting_down and no stop count."""
        import src.swapper as module

        first_stop_entered = asyncio.Event()
        release_first_stop = asyncio.Event()
        retry_entered = asyncio.Event()
        release_retry = asyncio.Event()
        stop_attempts = 0
        starts = []
        handle = ProcessHandle("a", 1001, FakeProcess(113))

        async def start(model, cmd, port):
            starts.append(model)
            return ProcessHandle(model, port, FakeProcess(114))

        async def stop(stopped_handle):
            nonlocal stop_attempts
            assert stopped_handle is handle
            stop_attempts += 1
            if stop_attempts == 1:
                first_stop_entered.set()
                await release_first_stop.wait()
                raise ProcessStopError("idle cleanup failed")
            retry_entered.set()
            await release_retry.wait()
            stopped_handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), idle_timeout=0, automatic_switch=True)
        swapper._current = handle
        swapper._current_model = "a"
        swapper._healthy = True
        swapper._idle_generation = 1

        idle = asyncio.create_task(
            swapper._idle_unload_after_timeout(1), name="test-idle-unload"
        )
        swapper._idle_task = idle
        owner = joiner = shutdown = None
        try:
            await asyncio.wait_for(first_stop_entered.wait(), 1)
            owner = asyncio.create_task(
                swapper.handle_request(StubRequest("b"))
            )
            await eventually(lambda: swapper._swapping)
            joiner = asyncio.create_task(
                swapper.handle_request(StubRequest("b"))
            )
            await eventually(lambda: len(swapper._queue) == 1)
            shutdown = asyncio.create_task(swapper.shutdown())
            await eventually(lambda: swapper._shutting_down)

            release_first_stop.set()
            await asyncio.wait_for(retry_entered.wait(), 1)
            assert not owner.done()
            assert not joiner.done()
            assert starts == []
            release_retry.set()

            responses = await asyncio.wait_for(
                asyncio.gather(owner, joiner), 1
            )
            for response in responses:
                await assert_http_error(response, 503, "shutting_down")
            await asyncio.wait_for(shutdown, 1)
            await asyncio.wait_for(idle, 1)

            assert stop_attempts == 2
            assert starts == []
            assert swapper._current is None
            assert swapper._cleanup_task is None
            assert swapper._idle_task is None
            assert swapper._swapping is False
            assert swapper._desired_model is None
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"503": 2}
            assert snapshot["models"]["b"]["requests"]["errors"] == {
                "503": 2
            }
            # The successful retry is shutdown cleanup, so it is excluded.
            assert snapshot["models"].get("a", {}).get("stops", 0) == 0
        finally:
            release_first_stop.set()
            release_retry.set()
            tasks = [
                task
                for task in (owner, joiner, shutdown, idle)
                if task is not None
            ]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_shutdown_cleanup_failure_precedes_shutdown_for_all_waiters(
        self, monkeypatch
    ):
        """Every blocked generation sees cleanup failure before shutdown retry."""
        import src.swapper as module

        readiness_entered = asyncio.Event()
        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()
        stop_attempts = 0
        handle = ProcessHandle("a", 1001, FakeProcess(101))

        async def start(*args):
            return handle

        async def ready(*args, **kwargs):
            readiness_entered.set()
            await asyncio.Event().wait()

        async def stop(stopped_handle):
            nonlocal stop_attempts
            stop_attempts += 1
            if stop_attempts == 1:
                stop_entered.set()
                await release_stop.wait()
                raise ProcessStopError("first attempt failed")
            stopped_handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), quick_switch=True, automatic_switch=True)
        requests = [
            asyncio.create_task(swapper.handle_request(StubRequest("a")))
        ]
        shutdown = None
        try:
            await asyncio.wait_for(readiness_entered.wait(), 1)
            requests.extend(
                asyncio.create_task(swapper.handle_request(StubRequest(model)))
                for model in ("a", "a", "b")
            )
            await asyncio.wait_for(stop_entered.wait(), 1)
            requests.extend(
                asyncio.create_task(swapper.handle_request(StubRequest(model)))
                for model in ("b", "c", "c")
            )
            await eventually(
                lambda: swapper._desired_model == "c"
                and len(swapper._queue) == 6
            )
            shutdown = asyncio.create_task(swapper.shutdown())
            await eventually(lambda: swapper._shutting_down)
            assert all(not request.done() for request in requests)
            release_stop.set()

            responses = await asyncio.wait_for(
                asyncio.gather(*requests), 1
            )
            for response in responses:
                await assert_http_error(
                    response, 503, "process_cleanup_failed"
                )
            await asyncio.wait_for(shutdown, 1)

            assert stop_attempts == 2
            assert swapper._current is None
            assert swapper._spawn_task is None
            assert swapper._readiness_task is None
            assert swapper._cleanup_task is None
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            assert swapper._swapping is False
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"503": 7}
            assert snapshot["models"]["a"]["requests"]["errors"] == {
                "503": 3
            }
            assert snapshot["models"]["b"]["requests"]["errors"] == {
                "503": 2
            }
            assert snapshot["models"]["c"]["requests"]["errors"] == {
                "503": 2
            }
            assert snapshot["models"]["a"]["starts"] == 1
            assert snapshot["models"]["a"]["stops"] == 0
            assert snapshot["swaps"]["count"] == 0
        finally:
            release_stop.set()
            for request in requests:
                if not request.done():
                    request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            if shutdown is None:
                await swapper.shutdown()
            elif not shutdown.done():
                await asyncio.gather(shutdown, return_exceptions=True)

    async def test_shutdown_final_cleanup_failure_raises_after_resources_close(
        self, monkeypatch
    ):
        """Final unconfirmed shutdown cleanup raises and leaves state tracked (AC10)."""
        import src.swapper as module

        class FakeClient:
            closed = False

            async def close(self):
                self.closed = True

        client = FakeClient()
        handle = ProcessHandle("a", 1001, FakeProcess(1))

        async def stop(*args):
            raise ProcessStopError("still running")

        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), client=client)
        swapper._current = handle
        swapper._current_model = "a"
        swapper._healthy = False

        with pytest.raises(ProcessStopError, match="Model lifecycle cleanup failed"):
            await swapper.shutdown()

        assert client.closed is True
        assert swapper._client is None
        assert swapper._temp_dir is None
        assert swapper._current is handle
        assert swapper._healthy is False
        assert swapper._cleanup_task is None
        assert swapper.stats.snapshot()["models"] == {}
