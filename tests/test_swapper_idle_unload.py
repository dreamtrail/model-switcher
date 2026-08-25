"""Swapper idle-unload scheduling tests.

Idle unload of the active model, timeout=0 disabling, idle-timer resets on
proxied requests (and non-reset on /models), and timer cancellation at
shutdown.
"""

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer, TestClient
import pytest

from src.config import Config
from src.swapper import Swapper

from tests.helpers import _fake_handle, _make_upstream_with_config


class TestIdleUnload:

    async def test_idle_unload_stops_active_model_and_clears_state(
        self, simple_config: Config, monkeypatch
    ):
        """Idle timeout unloads the active model via stop_process."""
        import src.swapper as swapper_module

        stop_calls = []
        stopped = asyncio.Event()

        async def fake_stop_process(handle):
            stop_calls.append(handle)
            handle.process.returncode = 0
            stopped.set()

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        swapper = Swapper(simple_config, idle_timeout=0.02)
        handle = _fake_handle("model-a", port=1234)
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True

        swapper._schedule_idle_timer()
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert stop_calls == [handle]
        assert swapper._current is None
        assert swapper._current_model is None
        assert swapper._healthy is False
        assert swapper.active_model is None

    async def test_idle_timeout_zero_disables_unload(
        self, simple_config: Config, monkeypatch
    ):
        """idle_timeout=0 never schedules idle unload."""
        import src.swapper as swapper_module

        stop_calls = []

        async def fake_stop_process(handle):
            stop_calls.append(handle)
            handle.process.returncode = 0

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        swapper = Swapper(simple_config, idle_timeout=0)
        handle = _fake_handle("model-a", port=1234)
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True

        swapper._schedule_idle_timer()
        await asyncio.sleep(0.03)

        assert swapper._idle_task is None
        assert stop_calls == []
        assert swapper.active_model == "model-a"
        swapper._current = None

    async def test_same_model_request_resets_existing_idle_timer(
        self, simple_config: Config, monkeypatch
    ):
        """A second explicit same-model request restarts the idle countdown."""
        import src.swapper as swapper_module

        stop_calls = []
        stopped = asyncio.Event()

        async def fake_stop_process(handle):
            stop_calls.append(handle)
            handle.process.returncode = 0
            stopped.set()

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        upstream_app, _ = _make_upstream_with_config("model-a")
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()

        swapper = Swapper(simple_config, idle_timeout=0.12)
        handle = _fake_handle("model-a", port=upstream_server.port)
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True

        async def handler(request):
            return await swapper.handle_request(request)

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_route("*", "/{path:.*}", handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        try:
            async with TestClient(proxy_server) as client:
                resp1 = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a"},
                )
                assert resp1.status == 200

                await asyncio.sleep(0.07)

                resp2 = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a"},
                )
                assert resp2.status == 200

                await asyncio.sleep(0.07)
                assert stop_calls == []
                assert swapper.active_model == "model-a"

                await asyncio.wait_for(stopped.wait(), timeout=1)
                assert stop_calls == [handle]
                assert swapper.active_model is None
        finally:
            await swapper.shutdown()
            await proxy_server.close()
            await upstream_server.close()

    async def test_proxied_request_without_model_resets_idle_timer(
        self, simple_config: Config, monkeypatch
    ):
        """A request proxied to the active model without a model resets idle."""
        import src.swapper as swapper_module

        stop_calls = []

        async def fake_stop_process(handle):
            stop_calls.append(handle)
            handle.process.returncode = 0

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        upstream_app, _ = _make_upstream_with_config("model-a")
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()

        swapper = Swapper(simple_config, idle_timeout=0.12)
        handle = _fake_handle("model-a", port=upstream_server.port)
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True

        async def handler(request):
            return await swapper.handle_request(request)

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_route("*", "/{path:.*}", handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        try:
            async with TestClient(proxy_server) as client:
                resp1 = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a"},
                )
                assert resp1.status == 200

                await asyncio.sleep(0.07)

                resp2 = await client.post(
                    "/v1/chat/completions",
                    json={"prompt": "no explicit model"},
                )
                assert resp2.status == 200

                await asyncio.sleep(0.07)
                assert stop_calls == []
                assert swapper.active_model == "model-a"

                await asyncio.sleep(0.08)
                assert stop_calls == [handle]
                assert swapper.active_model is None
        finally:
            await swapper.shutdown()
            await proxy_server.close()
            await upstream_server.close()

    @pytest.mark.parametrize("path", ["/v1/models", "/models"])
    async def test_models_endpoints_do_not_reset_idle_timer(
        self, simple_config: Config, monkeypatch, path: str
    ):
        """Switcher-local model listings do not count as model activity."""
        import src.swapper as swapper_module

        async def fake_stop_process(handle):
            handle.process.returncode = 0

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        swapper = Swapper(simple_config, idle_timeout=60)
        handle = _fake_handle("model-a", port=1234)
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True
        swapper._schedule_idle_timer()
        idle_task = swapper._idle_task

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get(path)
                assert resp.status == 200
                assert swapper._idle_task is idle_task
        finally:
            await swapper.shutdown()
            await server.close()

    async def test_shutdown_cancels_idle_timer(
        self, simple_config: Config, monkeypatch
    ):
        """Shutdown cancels pending idle unload cleanly."""
        import src.swapper as swapper_module

        async def fake_stop_process(handle):
            handle.process.returncode = 0

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        swapper = Swapper(simple_config, idle_timeout=60)
        handle = _fake_handle("model-a", port=1234)
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True
        swapper._schedule_idle_timer()
        idle_task = swapper._idle_task
        assert idle_task is not None

        await swapper.shutdown()

        assert idle_task.done()
        assert swapper._idle_task is None
        assert swapper._current is None
