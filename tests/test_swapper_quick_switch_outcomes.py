from __future__ import annotations

import asyncio

from aiohttp import web
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


class TestTerminalOutcomes:
    @pytest.mark.parametrize(
        "failure",
        [ValueError("bad command"), FileNotFoundError("missing"), OSError("fork")],
        ids=["tokenization", "missing-binary", "subprocess-oserror"],
    )
    async def test_spawn_failure_is_shared_without_retry_or_lifecycle_counts(
        self, monkeypatch, failure
    ):
        """Ordinary spawn exceptions map to shared exact 500 (AC11/AC14)."""
        import src.swapper as module

        entered = asyncio.Event()
        release = asyncio.Event()
        starts = 0

        async def start(*args):
            nonlocal starts
            starts += 1
            entered.set()
            await release.wait()
            raise failure

        monkeypatch.setattr(module, "start_process", start)
        swapper = Swapper(make_config())
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                owner = asyncio.create_task(post(client, "a"))
                await asyncio.wait_for(entered.wait(), 1)
                joiner = asyncio.create_task(post(client, "a"))
                await eventually(lambda: len(swapper._queue) == 1)
                release.set()
                responses = await asyncio.wait_for(
                    asyncio.gather(owner, joiner), 1
                )
                for response in responses:
                    await assert_http_error(response, 500, "model_start_failed")

            assert starts == 1
            assert swapper._swapping is False
            assert swapper._current is None
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"500": 2}
            row = snapshot["models"]["a"]
            assert row["starts"] == row["stops"] == 0
            assert snapshot["swaps"]["count"] == 0
        finally:
            release.set()
            await swapper.shutdown()
            await server.close()

    async def test_supersession_precedes_gated_spawn_exception(
        self, monkeypatch
    ):
        """Published supersession outranks old spawn failure; latest B runs (AC14)."""
        import src.swapper as module

        entered = asyncio.Event()
        release_failure = asyncio.Event()
        calls = []

        async def start(model, cmd, port):
            calls.append(model)
            if model == "a":
                entered.set()
                await release_failure.wait()
                raise OSError("late spawn failure")
            return ProcessHandle(model, port, FakeProcess(2))

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
        swapper = Swapper(make_config(), quick_switch=True, automatic_switch=True)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                request_a = asyncio.create_task(post(client, "a"))
                await asyncio.wait_for(entered.wait(), 1)
                request_b = asyncio.create_task(post(client, "b"))
                await eventually(lambda: swapper._desired_model == "b")
                release_failure.set()
                response_a, response_b = await asyncio.wait_for(
                    asyncio.gather(request_a, request_b), 1
                )
                await assert_http_error(response_a, 409, "swap_superseded")
                assert response_b.status == 200

            assert calls == ["a", "b"]
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"409": 1}
            assert snapshot["models"]["a"]["starts"] == 0
            assert snapshot["models"]["a"]["stops"] == 0
            assert snapshot["models"]["b"]["starts"] == 1
        finally:
            release_failure.set()
            await swapper.shutdown()
            await server.close()

    async def test_shutdown_precedes_gated_spawn_exception(
        self, monkeypatch
    ):
        """Published shutdown outranks a late spawn failure with no events (AC14)."""
        import src.swapper as module

        entered = asyncio.Event()
        release_failure = asyncio.Event()

        async def start(*args):
            entered.set()
            await release_failure.wait()
            raise FileNotFoundError("late failure")

        monkeypatch.setattr(module, "start_process", start)
        swapper = Swapper(make_config())
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                owner = asyncio.create_task(post(client, "a"))
                await asyncio.wait_for(entered.wait(), 1)
                shutdown = asyncio.create_task(swapper.shutdown())
                await eventually(lambda: swapper._shutting_down)
                release_failure.set()
                response = await asyncio.wait_for(owner, 1)
                await assert_http_error(response, 503, "shutting_down")
                await asyncio.wait_for(shutdown, 1)

            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"503": 1}
            assert snapshot["models"]["a"]["starts"] == 0
            assert snapshot["models"]["a"]["stops"] == 0
            assert swapper._spawn_task is None
            assert swapper._swapping is False
        finally:
            release_failure.set()
            await server.close()

    async def test_same_generation_timeout_is_shared_once(self, monkeypatch):
        """Joiners share one timed-out load and exact 504 outcome (AC2/AC8)."""
        import src.swapper as module

        ready_entered = asyncio.Event()
        release_timeout = asyncio.Event()
        starts, stops = [], []

        async def start(model, cmd, port):
            starts.append(model)
            return ProcessHandle(model, port, FakeProcess(1))

        async def ready(*args, **kwargs):
            ready_entered.set()
            await release_timeout.wait()
            raise TimeoutError("not ready")

        async def stop(handle):
            stops.append(handle.model_name)
            handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config())
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                owner = asyncio.create_task(post(client, "a"))
                await asyncio.wait_for(ready_entered.wait(), 1)
                joiner = asyncio.create_task(post(client, "a"))
                await eventually(lambda: len(swapper._queue) == 1)
                release_timeout.set()
                responses = await asyncio.wait_for(
                    asyncio.gather(owner, joiner), 1
                )
                for response in responses:
                    await assert_http_error(response, 504, "swap_timeout")

            assert starts == ["a"]
            assert stops == ["a"]
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"504": 2}
            assert snapshot["models"]["a"]["starts"] == 1
            assert snapshot["models"]["a"]["stops"] == 1
            assert snapshot["swaps"]["count"] == 0
        finally:
            release_timeout.set()
            await swapper.shutdown()
            await server.close()

    async def test_cleanup_failure_fans_out_all_generations_and_allows_retry(
        self, monkeypatch
    ):
        """Failed A cleanup gives every A/B/C request one exact 503 (AC6/AC10)."""
        import src.swapper as module

        waiting = asyncio.Event()
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()
        retry_entered = asyncio.Event()
        release_retry = asyncio.Event()
        starts = []
        stop_attempts = 0

        async def start(model, cmd, port):
            starts.append(model)
            return ProcessHandle(model, port, FakeProcess(len(starts)))

        async def ready(port, **kwargs):
            if port == 1001:
                waiting.set()
                await asyncio.Event().wait()

        async def stop(handle):
            nonlocal stop_attempts
            stop_attempts += 1
            if stop_attempts == 1:
                cleanup_entered.set()
                await release_cleanup.wait()
                raise ProcessStopError("still running")
            retry_entered.set()
            await release_retry.wait()
            handle.process.returncode = 0

        async def proxy(*args, **kwargs):
            return web.json_response({"ok": True})

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        monkeypatch.setattr(module, "proxy_request", proxy)
        swapper = Swapper(make_config(), quick_switch=True, automatic_switch=True)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                requests = [asyncio.create_task(post(client, "a"))]
                await asyncio.wait_for(waiting.wait(), 1)
                requests.extend(
                    asyncio.create_task(post(client, "a")) for _ in range(2)
                )
                await eventually(lambda: len(swapper._queue) == 2)
                requests.append(asyncio.create_task(post(client, "b")))
                await asyncio.wait_for(cleanup_entered.wait(), 1)
                requests.append(asyncio.create_task(post(client, "b")))
                requests.extend(
                    asyncio.create_task(post(client, "c")) for _ in range(2)
                )
                await eventually(
                    lambda: swapper._desired_model == "c"
                    and len(swapper._queue) == 6
                )
                assert all(not request.done() for request in requests)
                assert starts == ["a"]
                release_cleanup.set()

                responses = await asyncio.wait_for(
                    asyncio.gather(*requests), 1
                )
                for response in responses:
                    await assert_http_error(
                        response, 503, "process_cleanup_failed"
                    )

                assert starts == ["a"]
                assert swapper._current_model == "a"
                assert swapper._current is not None
                assert swapper._current.stopped is False
                assert swapper._healthy is False
                assert swapper._swapping is False
                assert list(swapper._queue) == []
                assert swapper._waiters == {}
                assert swapper._readiness_task is None
                assert swapper._cleanup_task is None
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

                # A later independent request may retry retained cleanup, but
                # cannot start B before absence is confirmed.
                retry = asyncio.create_task(post(client, "b"))
                await asyncio.wait_for(retry_entered.wait(), 1)
                assert starts == ["a"]
                release_retry.set()
                assert (await asyncio.wait_for(retry, 1)).status == 200
                assert starts == ["a", "b"]
        finally:
            release_cleanup.set()
            release_retry.set()
            await swapper.shutdown()
            await server.close()

    async def test_raw_stop_exception_is_normalized_and_fanned_out(
        self, monkeypatch
    ):
        """A raw signaling error cannot strand the lifecycle owner or waiters."""
        import src.swapper as module

        readiness_entered = asyncio.Event()
        handle = ProcessHandle("a", 1001, FakeProcess(71))

        async def start(*args):
            return handle

        async def ready(*args, **kwargs):
            readiness_entered.set()
            await asyncio.Event().wait()

        async def raw_stop_failure(*args):
            raise PermissionError("signal denied")

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", raw_stop_failure)
        swapper = Swapper(make_config(), quick_switch=True, automatic_switch=True)
        owner = asyncio.create_task(
            swapper.handle_request(StubRequest("a"))
        )
        joiner = replacement = None
        try:
            await asyncio.wait_for(readiness_entered.wait(), 1)
            joiner = asyncio.create_task(
                swapper.handle_request(StubRequest("a"))
            )
            replacement = asyncio.create_task(
                swapper.handle_request(StubRequest("b"))
            )
            responses = await asyncio.wait_for(
                asyncio.gather(owner, joiner, replacement), 1
            )
            for response in responses:
                await assert_http_error(
                    response, 503, "process_cleanup_failed"
                )

            assert swapper._current is handle
            assert swapper._healthy is False
            assert swapper._spawn_task is None
            assert swapper._readiness_task is None
            assert swapper._cleanup_task is None
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            assert swapper._swapping is False
            assert swapper.stats.snapshot()["requests"]["errors"] == {
                "503": 3
            }
        finally:
            async def successful_stop(stopped_handle):
                stopped_handle.process.returncode = 0

            monkeypatch.setattr(module, "stop_process", successful_stop)
            for task in (owner, joiner, replacement):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (owner, joiner, replacement)
                  if task is not None),
                return_exceptions=True,
            )
            await swapper.shutdown()
