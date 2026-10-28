from __future__ import annotations

import asyncio

from aiohttp import web
import pytest

from src.process_manager import ProcessHandle, ProcessStopError
from src.swapper import Swapper
from tests.quick_switch_helpers import (
    FakeProcess,
    StubRequest,
    assert_http_error,
    eventually,
    make_config,
    response_body,
)


class TestCancellationAndShutdown:
    async def test_readiness_owner_cancellation_waits_for_cleanup_then_handoffs(
        self, monkeypatch
    ):
        """The actual readiness owner stays cancelled; latest C wins (AC10/AC14)."""
        import src.swapper as module

        readiness_entered = asyncio.Event()
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()
        a_handle = ProcessHandle("a", 1001, FakeProcess(81))
        starts, stops = [], []

        async def start(model, cmd, port):
            starts.append(model)
            if model == "a":
                return a_handle
            return ProcessHandle(model, port, FakeProcess(82))

        async def ready(port, **kwargs):
            if port == 1001:
                readiness_entered.set()
                await asyncio.Event().wait()

        async def stop(handle):
            stops.append(handle.model_name)
            if handle is a_handle:
                cleanup_entered.set()
                await release_cleanup.wait()
            handle.process.returncode = 0

        async def proxy(*args, **kwargs):
            return web.json_response({"ok": True})

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        monkeypatch.setattr(module, "proxy_request", proxy)
        swapper = Swapper(make_config(), quick_switch=True, automatic_switch=True)
        owner = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        requests = []
        try:
            await asyncio.wait_for(readiness_entered.wait(), 1)
            a_joiners = [
                asyncio.create_task(swapper.handle_request(StubRequest("a")))
                for _ in range(2)
            ]
            requests.extend(a_joiners)
            await eventually(lambda: len(swapper._queue) == 2)

            owner.cancel()
            await asyncio.wait_for(cleanup_entered.wait(), 1)
            assert not owner.done()
            assert swapper._current is a_handle
            assert swapper._cleanup_task is not None

            b_requests = [
                asyncio.create_task(swapper.handle_request(StubRequest("b")))
                for _ in range(2)
            ]
            c_requests = [
                asyncio.create_task(swapper.handle_request(StubRequest("c")))
                for _ in range(2)
            ]
            requests.extend(b_requests + c_requests)
            await eventually(
                lambda: swapper._desired_model == "c"
                and len(swapper._queue) == 6
            )
            release_cleanup.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 1)
            responses = await asyncio.wait_for(
                asyncio.gather(*requests), 1
            )
            for response in responses[:4]:
                await assert_http_error(response, 409, "swap_superseded")
            assert [response.status for response in responses[4:]] == [200, 200]

            assert starts == ["a", "c"]
            assert stops == ["a"]
            assert a_handle.process.returncode == 0
            assert swapper._current is not a_handle
            assert swapper._current_model == "c"
            assert swapper._healthy is True
            assert swapper._spawn_task is None
            assert swapper._readiness_task is None
            assert swapper._cleanup_task is None
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            assert swapper._swapping is False
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"409": 4}
            assert snapshot["requests"]["total"] == 6
            assert snapshot["models"]["a"]["requests"]["errors"] == {
                "409": 2
            }
            assert snapshot["models"]["b"]["requests"]["errors"] == {
                "409": 2
            }
            assert snapshot["models"]["a"]["starts"] == 1
            assert snapshot["models"]["a"]["stops"] == 1
        finally:
            release_cleanup.set()
            for task in [owner, *requests]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(owner, *requests, return_exceptions=True)
            await swapper.shutdown()

    async def test_readiness_owner_cancellation_cleanup_failure_fans_out_and_retries(
        self, monkeypatch
    ):
        """Cancelled owner is preserved while all live waiters get 503 (AC10/AC14)."""
        import src.swapper as module

        readiness_entered = asyncio.Event()
        first_cleanup_entered = asyncio.Event()
        release_first_cleanup = asyncio.Event()
        request_retry_entered = asyncio.Event()
        release_request_retry = asyncio.Event()
        shutdown_retry_entered = asyncio.Event()
        release_shutdown_retry = asyncio.Event()
        handle = ProcessHandle("a", 1001, FakeProcess(91))
        starts = []
        stop_attempts = 0

        async def start(model, cmd, port):
            starts.append(model)
            return handle

        async def ready(*args, **kwargs):
            readiness_entered.set()
            await asyncio.Event().wait()

        async def stop(stopped_handle):
            nonlocal stop_attempts
            stop_attempts += 1
            if stop_attempts == 1:
                first_cleanup_entered.set()
                await release_first_cleanup.wait()
                raise ProcessStopError("owner cleanup failed")
            if stop_attempts == 2:
                request_retry_entered.set()
                await release_request_retry.wait()
                raise ProcessStopError("request retry failed")
            shutdown_retry_entered.set()
            await release_shutdown_retry.wait()
            stopped_handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config(), quick_switch=True, automatic_switch=True)
        owner = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        requests = []
        shutdown_task = None
        try:
            await asyncio.wait_for(readiness_entered.wait(), 1)
            requests.extend(
                asyncio.create_task(swapper.handle_request(StubRequest("a")))
                for _ in range(2)
            )
            await eventually(lambda: len(swapper._queue) == 2)
            owner.cancel()
            await asyncio.wait_for(first_cleanup_entered.wait(), 1)

            requests.extend(
                asyncio.create_task(swapper.handle_request(StubRequest(model)))
                for model in ("b", "b", "c", "c")
            )
            await eventually(
                lambda: swapper._desired_model == "c"
                and len(swapper._queue) == 6
            )
            assert not owner.done()
            release_first_cleanup.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 1)
            responses = await asyncio.wait_for(
                asyncio.gather(*requests), 1
            )
            for response in responses:
                assert response_body(response) == {
                    "error": {
                        "message": "Previous model process could not be stopped",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "process_cleanup_failed",
                    }
                }
                assert response.status == 503

            assert starts == ["a"]
            assert swapper._current is handle
            assert swapper._current_model == "a"
            assert swapper._healthy is False
            assert swapper._spawn_task is None
            assert swapper._readiness_task is None
            assert swapper._cleanup_task is None
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            assert swapper._swapping is False
            assert swapper._desired_model is None
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["total"] == 6
            assert snapshot["requests"]["errors"] == {"503": 6}
            assert snapshot["models"]["a"]["requests"]["errors"] == {
                "503": 2
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

            # An independent request retries cleanup and cannot start B while
            # that retry remains unconfirmed.
            retry = asyncio.create_task(
                swapper.handle_request(StubRequest("b"))
            )
            await asyncio.wait_for(request_retry_entered.wait(), 1)
            assert starts == ["a"]
            release_request_retry.set()
            retry_response = await asyncio.wait_for(retry, 1)
            await assert_http_error(
                retry_response, 503, "process_cleanup_failed"
            )
            assert starts == ["a"]

            # Shutdown remains eligible to retry the retained handle too.
            shutdown_task = asyncio.create_task(swapper.shutdown())
            await asyncio.wait_for(shutdown_retry_entered.wait(), 1)
            assert not shutdown_task.done()
            assert starts == ["a"]
            release_shutdown_retry.set()
            await asyncio.wait_for(shutdown_task, 1)
            assert swapper._current is None
            assert swapper._cleanup_task is None
            final = swapper.stats.snapshot()
            assert final["requests"]["errors"] == {"503": 7}
            # Shutdown confirmation never adds a model stop.
            assert final["models"]["a"]["stops"] == 0
        finally:
            release_first_cleanup.set()
            release_request_retry.set()
            release_shutdown_retry.set()
            for task in [owner, *requests]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(owner, *requests, return_exceptions=True)
            if shutdown_task is None:
                await swapper.shutdown()
            elif not shutdown_task.done():
                await asyncio.gather(shutdown_task, return_exceptions=True)

    @pytest.mark.parametrize("cleanup_fails", [False, True])
    async def test_external_cancellation_during_spawn_is_terminal_and_shared(
        self, monkeypatch, cleanup_fails
    ):
        """Owner cancellation preserves cancellation after shielded cleanup (AC10/AC14)."""
        import src.swapper as module

        entered = asyncio.Event()
        release = asyncio.Event()
        retry_entered = asyncio.Event()
        release_retry = asyncio.Event()
        handle = ProcessHandle("a", 1001, FakeProcess(1))
        starts, stops = [], []
        spawn_cancelled = False
        spawn_invocations = 0

        async def start(model, cmd, port):
            nonlocal spawn_cancelled, spawn_invocations
            spawn_invocations += 1
            gate = release
            if spawn_invocations == 1:
                entered.set()
            else:
                retry_entered.set()
                gate = release_retry
            try:
                await gate.wait()
            except asyncio.CancelledError:
                spawn_cancelled = True
                raise
            starts.append(model)
            if spawn_invocations == 1:
                return handle
            return ProcessHandle("a", 1001, FakeProcess(2))

        async def stop(stopped_handle):
            stops.append(stopped_handle.model_name)
            if cleanup_fails:
                raise ProcessStopError("still running")
            stopped_handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config())
        owner = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        await asyncio.wait_for(entered.wait(), 1)
        joiner = asyncio.create_task(swapper.handle_request(StubRequest("a")))
        await eventually(lambda: len(swapper._queue) == 1)

        owner.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 1)

        assert spawn_cancelled is False
        assert starts == ["a"]
        assert stops == ["a"]
        assert swapper._cleanup_task is None
        snapshot = swapper.stats.snapshot()
        assert snapshot["models"]["a"]["starts"] == 1

        if cleanup_fails:
            assert swapper._spawn_task is None
            joined = await asyncio.wait_for(joiner, 1)
            assert joined.status == 503
            assert response_body(joined)["error"]["code"] == \
                "process_cleanup_failed"
            assert swapper._current is handle
            assert swapper._healthy is False
            assert swapper._swapping is False
            assert swapper._desired_model is None
            assert snapshot["models"]["a"]["stops"] == 0
            final = swapper.stats.snapshot()
            # The cancelled owner is not counted; only its live waiter is.
            assert final["requests"]["total"] == 1
            assert final["requests"]["errors"] == {"503": 1}

            # A later independent request may retry cleanup before it starts B.
            async def successful_stop(stopped_handle):
                stops.append(stopped_handle.model_name)
                stopped_handle.process.returncode = 0

            async def successful_start(model, cmd, port):
                starts.append(model)
                return ProcessHandle(model, port, FakeProcess(2))

            async def ready(*args, **kwargs):
                return None

            async def proxy(*args, **kwargs):
                return web.json_response({"ok": True})

            monkeypatch.setattr(module, "stop_process", successful_stop)
            monkeypatch.setattr(module, "start_process", successful_start)
            monkeypatch.setattr(module, "wait_for_ready", ready)
            monkeypatch.setattr(module, "proxy_request", proxy)
            retry = await swapper.handle_request(StubRequest("b"))
            assert retry.status == 200
            assert stops == ["a", "a"]
            assert starts == ["a", "b"]
        else:
            # The owner has no HTTP rejection; its joiner owns the retry.
            assert snapshot["requests"]["total"] == 0
            await asyncio.wait_for(retry_entered.wait(), 1)
            joiner.cancel()
            release_retry.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(joiner, 1)
            assert swapper._current is None
            assert snapshot["models"]["a"]["stops"] == 1
            assert spawn_cancelled is False

        release_retry.set()
        await swapper.shutdown()

    async def test_external_cancellation_immediately_after_spawn_return(
        self, monkeypatch
    ):
        """Cancellation scheduled with handle return cannot orphan it (AC14)."""
        import src.swapper as module

        handle = ProcessHandle("a", 1001, FakeProcess(1))
        stopped = asyncio.Event()
        owner_task = None

        async def start(*args):
            asyncio.get_running_loop().call_soon(owner_task.cancel)
            return handle

        async def stop(stopped_handle):
            stopped_handle.process.returncode = 0
            stopped.set()

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "stop_process", stop)
        swapper = Swapper(make_config())
        owner_task = asyncio.create_task(
            swapper.handle_request(StubRequest("a"))
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner_task, 1)

        assert stopped.is_set()
        assert swapper._current is None
        assert swapper._spawn_task is None
        assert swapper._cleanup_task is None
        snapshot = swapper.stats.snapshot()
        assert snapshot["models"]["a"]["starts"] == 1
        assert snapshot["models"]["a"]["stops"] == 1
        assert snapshot["requests"]["total"] == 0
        await swapper.shutdown()
