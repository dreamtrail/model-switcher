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

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from src.process_manager import ProcessHandle
from src.swapper import MAX_QUEUE, Swapper
from tests.quick_switch_helpers import (
    FakeProcess,
    StubRequest,
    assert_http_error,
    eventually,
    install_success_lifecycle,
    make_config,
    make_server,
    post,
    response_body,
)


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
