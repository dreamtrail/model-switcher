from __future__ import annotations

import asyncio
import pytest

from aiohttp import web

from src.swapper import MAX_QUEUE, Swapper
from tests.control_helpers import handle
from tests.quick_switch_helpers import StubRequest, make_config, response_body


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
