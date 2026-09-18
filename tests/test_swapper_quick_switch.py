"""Contracts for pending-load policies and cancellation-safe lifecycle work.

Expected behavior is derived from plan P-20260918-02.  AC-to-test map:
- AC1: test_cli.py quick-switch default/help/wiring tests
- AC2/AC3: test_quick_off_same_target_joins_and_different_target_refused
- AC4/AC5: test_rapid_a_b_c_fans_out_only_after_cleanup
- AC6: test_cleanup_failure_fans_out_all_generations_and_allows_retry and
  test_process_manager.py stop-failure/process-group tests
- AC7: spawn/post-readiness race tests and the gated A/B/C fan-out test
- AC8: TestCompatibilityAndAdmission and test_same_generation_timeout_...
- AC9: test_request_for_prior_a_supersedes_mistaken_b
- AC10: readiness-owner cancellation, shutdown cleanup, and idle-cleanup
  shutdown precedence tests
- AC11: terminal outcome and shutdown-excluded stop statistics assertions here
  and in test_stats.py
- AC12: test_constructor_and_private_helpers_keep_default_contract plus the
  unchanged lifecycle, idle, proxy, and routing suites
- AC13: documentation audit (not an executable source contract)
- AC14: spawn/readiness cancellation, _OWNER award, and start-failure tests

Deterministic events expose lifecycle boundaries; no test uses wall-clock sleeps.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle, ProcessStopError
from src.swapper import MAX_QUEUE, Swapper


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None


class StubRequest:
    """Minimal request for pre-proxy/cancellation handler paths."""

    path = "/v1/chat/completions"
    method = "POST"
    query = {}
    content_type = "application/json"
    headers = {}

    def __init__(self, model: str):
        self._body = json.dumps({"model": model}).encode()

    async def read(self) -> bytes:
        return self._body


def make_config() -> Config:
    return Config(models={
        name: ModelConfig(name=name, cmd=f"serve-{name}", port=port)
        for name, port in (("a", 1001), ("b", 1002), ("c", 1003))
    })


async def make_server(swapper: Swapper) -> TestServer:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", swapper.handle_request)
    server = TestServer(app)
    await server.start_server()
    return server


async def post(client: TestClient, model: str):
    return await client.post(
        "/v1/chat/completions", json={"model": model}
    )


async def eventually(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def response_body(response: web.Response) -> dict:
    return json.loads(response.body)


async def assert_http_error(response, status: int, code: str) -> None:
    messages = {
        "switch_in_progress": "A different model is currently loading",
        "swap_superseded": "Model load was superseded by a newer request",
        "model_start_failed": "Model process could not be started",
        "process_cleanup_failed": "Previous model process could not be stopped",
        "shutting_down": "Model switcher is shutting down",
        "swap_timeout": "Model swap timed out",
    }
    assert response.status == status
    body = (
        await response.json()
        if hasattr(response, "json")
        else response_body(response)
    )
    assert body == {
        "error": {
            "message": messages[code],
            "type": "invalid_request_error",
            "param": None,
            "code": code,
        }
    }


async def install_success_lifecycle(monkeypatch, *, starts=None, stops=None):
    import src.swapper as module

    starts = [] if starts is None else starts
    stops = [] if stops is None else stops

    async def start(model, cmd, port):
        starts.append(model)
        return ProcessHandle(model, port, FakeProcess(len(starts)))

    async def ready(*args, **kwargs):
        return None

    async def stop(handle):
        stops.append(handle.model_name)
        handle.process.returncode = 0

    async def proxy(*args, **kwargs):
        handle = args[3] if len(args) > 3 else kwargs.get("handle")
        return web.json_response({"model": handle.model_name})

    monkeypatch.setattr(module, "start_process", start)
    monkeypatch.setattr(module, "wait_for_ready", ready)
    monkeypatch.setattr(module, "stop_process", stop)
    monkeypatch.setattr(module, "proxy_request", proxy)
    return starts, stops


class TestCompatibilityAndAdmission:
    async def test_constructor_and_private_helpers_keep_default_contract(
        self, monkeypatch
    ):
        """Default mode, direct _ensure_model, queue and drain remain usable (AC12)."""
        await install_success_lifecycle(monkeypatch)
        swapper = Swapper(make_config())
        assert swapper.quick_switch is False

        handle = await swapper._ensure_model("a")
        assert handle.model_name == "a"

        futures = [swapper._queue_request() for _ in range(MAX_QUEUE)]
        with pytest.raises(asyncio.QueueFull):
            swapper._queue_request()
        await swapper._drain_queue()
        assert all(future.done() and future.result() is None for future in futures)
        await swapper.shutdown()

    async def test_unknown_model_has_no_admission_side_effects(self):
        """Unknown targets are rejected before capacity or intent mutation (AC8)."""
        swapper = Swapper(make_config(), quick_switch=True)
        swapper._swapping = True
        swapper._intent_generation = 9
        swapper._desired_model = "a"
        future = swapper._queue_request()
        try:
            response = await swapper.handle_request(StubRequest("ghost"))
            assert response.status == 404
            assert response_body(response)["error"]["code"] == "internal_error"
            assert swapper._intent_generation == 9
            assert swapper._desired_model == "a"
            assert list(swapper._queue) == [future]
        finally:
            await swapper.shutdown()

    async def test_quick_mode_capacity_is_reserved_before_supersession(self):
        """A full queue cannot publish target/generation or cancel readiness (AC8)."""
        swapper = Swapper(make_config(), quick_switch=True)
        readiness = asyncio.create_task(asyncio.Event().wait())
        swapper._swapping = True
        swapper._intent_generation = 7
        swapper._desired_model = "a"
        swapper._readiness_task = readiness
        swapper._readiness_model = "a"
        for _ in range(MAX_QUEUE):
            admission, _ = swapper._admit_request("a")
            assert admission == "waiter"

        response = await swapper.handle_request(StubRequest("b"))
        assert response.status == 503
        assert response_body(response) == {
            "error": {
                "message": "Swap in progress, try again shortly",
                "type": "invalid_request_error",
                "param": None,
                "code": "internal_error",
            }
        }
        assert swapper._intent_generation == 7
        assert swapper._desired_model == "a"
        assert swapper._readiness_task is readiness
        assert not readiness.cancelled()

        readiness.cancel()
        with pytest.raises(asyncio.CancelledError):
            await readiness
        await swapper.shutdown()


class TestPendingLoadPolicies:
    async def test_quick_off_same_target_joins_and_different_target_refused(
        self, monkeypatch
    ):
        """Same A shares one load; B gets immediate non-mutating 409 (AC2/AC3)."""
        import src.swapper as module

        started = asyncio.Event()
        release_ready = asyncio.Event()
        starts = []

        async def start(model, cmd, port):
            starts.append(model)
            started.set()
            return ProcessHandle(model, port, FakeProcess(1))

        async def ready(*args, **kwargs):
            await release_ready.wait()

        async def stop(handle):
            handle.process.returncode = 0

        async def proxy(*args, **kwargs):
            return web.json_response({"ok": True})

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        monkeypatch.setattr(module, "proxy_request", proxy)
        swapper = Swapper(make_config())
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                owner = asyncio.create_task(post(client, "a"))
                await asyncio.wait_for(started.wait(), 1)
                joiner = asyncio.create_task(post(client, "a"))
                await eventually(lambda: len(swapper._queue) == 1)
                generation = swapper._intent_generation
                desired = swapper._desired_model
                readiness_task = swapper._readiness_task

                refused = await asyncio.wait_for(post(client, "b"), 1)
                await assert_http_error(refused, 409, "switch_in_progress")
                assert len(swapper._queue) == 1
                assert swapper._intent_generation == generation
                assert swapper._desired_model == desired == "a"
                assert swapper._readiness_task is readiness_task
                assert not readiness_task.cancelled()

                release_ready.set()
                responses = await asyncio.wait_for(
                    asyncio.gather(owner, joiner), 1
                )
                assert [response.status for response in responses] == [200, 200]
                assert starts == ["a"]

            stats = swapper.stats.snapshot()
            assert stats["requests"]["errors"] == {"409": 1}
            assert stats["models"]["b"]["requests"]["errors"] == {"409": 1}
        finally:
            release_ready.set()
            await swapper.shutdown()
            await server.close()

    async def test_quick_on_supersedes_a_and_same_target_b_joins(
        self, monkeypatch
    ):
        """B supersedes started A; both B requests share one replacement (AC4/AC5)."""
        import src.swapper as module

        a_waiting = asyncio.Event()
        b_waiting = asyncio.Event()
        release_b = asyncio.Event()
        starts, stops = [], []

        async def start(model, cmd, port):
            starts.append(model)
            return ProcessHandle(model, port, FakeProcess(len(starts)))

        async def ready(port, **kwargs):
            if port == 1001:
                a_waiting.set()
                await asyncio.Event().wait()
            b_waiting.set()
            await release_b.wait()

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
                await asyncio.wait_for(a_waiting.wait(), 1)
                request_b1 = asyncio.create_task(post(client, "b"))
                await asyncio.wait_for(b_waiting.wait(), 1)
                request_b2 = asyncio.create_task(post(client, "b"))
                await eventually(lambda: len(swapper._queue) == 1)
                release_b.set()

                response_a, response_b1, response_b2 = await asyncio.wait_for(
                    asyncio.gather(request_a, request_b1, request_b2), 1
                )
                await assert_http_error(response_a, 409, "swap_superseded")
                assert response_b1.status == response_b2.status == 200
                assert starts == ["a", "b"]
                assert stops == ["a"]
                assert swapper.active_model == "b"
        finally:
            release_b.set()
            await swapper.shutdown()
            await server.close()

    async def test_rapid_a_b_c_fans_out_only_after_cleanup(
        self, monkeypatch
    ):
        """Every stale same-generation request waits for confirmed cleanup.

        One A owner, two A joiners, two B-generation requests, and two
        C-generation requests prove stale fan-out, latest-wins handoff, exact
        accounting, and terminal queue cleanup (AC4/AC5/AC7/AC11).
        """
        import src.swapper as module

        a_waiting = asyncio.Event()
        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()
        starts, stops = [], []

        async def start(model, cmd, port):
            starts.append(model)
            return ProcessHandle(model, port, FakeProcess(len(starts)))

        async def ready(port, **kwargs):
            if port == 1001:
                a_waiting.set()
                await asyncio.Event().wait()

        async def stop(handle):
            stops.append(handle.model_name)
            if handle.model_name == "a":
                stop_entered.set()
                await release_stop.wait()
            handle.process.returncode = 0

        async def proxy(*args, **kwargs):
            return web.json_response({"model": "c"})

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)
        monkeypatch.setattr(module, "proxy_request", proxy)
        swapper = Swapper(make_config(), quick_switch=True)
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                a_requests = [asyncio.create_task(post(client, "a"))]
                await asyncio.wait_for(a_waiting.wait(), 1)
                a_requests.extend(
                    asyncio.create_task(post(client, "a")) for _ in range(2)
                )
                await eventually(lambda: len(swapper._queue) == 2)

                b_requests = [asyncio.create_task(post(client, "b"))]
                await asyncio.wait_for(stop_entered.wait(), 1)
                b_requests.append(asyncio.create_task(post(client, "b")))
                await eventually(lambda: swapper._desired_model == "b")

                c_requests = [
                    asyncio.create_task(post(client, "c")) for _ in range(2)
                ]
                await eventually(
                    lambda: swapper._desired_model == "c"
                    and len(swapper._queue) == 6
                )

                # No stale response is observable until A is confirmed gone.
                assert all(
                    not request.done() for request in a_requests + b_requests
                )
                assert starts == ["a"]
                release_stop.set()

                responses = await asyncio.wait_for(
                    asyncio.gather(*(a_requests + b_requests + c_requests)),
                    1,
                )
                for response in responses[:5]:
                    await assert_http_error(
                        response, 409, "swap_superseded"
                    )
                assert [response.status for response in responses[5:]] == [
                    200,
                    200,
                ]
                assert [await response.json() for response in responses[5:]] == [
                    {"model": "c"},
                    {"model": "c"},
                ]

            assert starts == ["a", "c"]
            assert stops == ["a"]
            assert list(swapper._queue) == []
            assert swapper._waiters == {}
            assert swapper._swapping is False
            snapshot = swapper.stats.snapshot()
            assert snapshot["requests"]["errors"] == {"409": 5}
            assert snapshot["models"]["a"]["requests"]["errors"] == {
                "409": 3
            }
            assert snapshot["models"]["b"]["requests"]["errors"] == {
                "409": 2
            }
        finally:
            release_stop.set()
            await swapper.shutdown()
            await server.close()

    async def test_request_for_prior_a_supersedes_mistaken_b(self, monkeypatch):
        """After healthy A was stopped, A can supersede pending B and reload (AC9)."""
        import src.swapper as module

        b_waiting = asyncio.Event()
        starts, stops = [], []
        started_handles, proxied_handles = [], []

        async def start(model, cmd, port):
            starts.append(model)
            handle = ProcessHandle(model, port, FakeProcess(10 + len(starts)))
            started_handles.append(handle)
            return handle

        async def ready(port, **kwargs):
            if port == 1002:
                b_waiting.set()
                await asyncio.Event().wait()

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

        async def capture_proxy(
            request, model_name, body, handle=None, outcome=None
        ):
            proxied_handles.append(handle)
            return web.json_response({"model": handle.model_name})

        swapper._proxy_for_model = capture_proxy
        old_a = ProcessHandle("a", 1001, FakeProcess(1))
        swapper._current = old_a
        swapper._current_model = "a"
        swapper._healthy = True
        server = await make_server(swapper)
        try:
            async with TestClient(server) as client:
                request_b = asyncio.create_task(post(client, "b"))
                await asyncio.wait_for(b_waiting.wait(), 1)
                request_a = asyncio.create_task(post(client, "a"))
                response_b, response_a = await asyncio.wait_for(
                    asyncio.gather(request_b, request_a), 1
                )
                await assert_http_error(response_b, 409, "swap_superseded")
                assert response_a.status == 200
                assert await response_a.json() == {"model": "a"}
                assert starts == ["b", "a"]
                assert stops[:2] == ["a", "b"]
                assert swapper.active_model == "a"
                assert proxied_handles == [started_handles[1]]
                assert proxied_handles[0] is not old_a
        finally:
            await swapper.shutdown()
            await server.close()


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
        swapper = Swapper(make_config(), quick_switch=True)
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
        swapper = Swapper(make_config(), quick_switch=True)
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
        swapper = Swapper(make_config(), quick_switch=True)
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
        swapper = Swapper(make_config(), quick_switch=True)
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
        swapper = Swapper(make_config(), quick_switch=True)
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
        swapper = Swapper(make_config(), idle_timeout=0, client=client)
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
                ProcessStopError, match="failed stop attempt 3"
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
        swapper = Swapper(make_config(), idle_timeout=0)
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
        swapper = Swapper(make_config(), quick_switch=True)
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

        with pytest.raises(ProcessStopError, match="still running"):
            await swapper.shutdown()

        assert client.closed is True
        assert swapper._client is None
        assert swapper._temp_dir is None
        assert swapper._current is handle
        assert swapper._healthy is False
        assert swapper._cleanup_task is None
        assert swapper.stats.snapshot()["models"] == {}
