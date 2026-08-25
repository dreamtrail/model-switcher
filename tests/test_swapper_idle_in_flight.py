"""Swapper idle-unload vs in-flight request tests.

Idle unload waiting for in-flight (including streaming) requests, losing to
recorded activity, and restarting the model after an idle unload.
"""

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer, TestClient

from src.config import Config
from src.swapper import Swapper

from tests.helpers import _fake_handle


class TestIdleUnloadInFlight:

    async def test_expired_idle_unload_loses_to_recorded_activity(
        self, simple_config: Config, monkeypatch
    ):
        """An expired idle task cannot unload after new activity is recorded."""
        import src.swapper as swapper_module

        idle_waiting_for_lock = asyncio.Event()
        allow_idle_to_check_state = asyncio.Event()
        proxy_started = asyncio.Event()
        release_proxy = asyncio.Event()

        class BlockingIdleLock:
            def __init__(self):
                self._lock = asyncio.Lock()

            async def __aenter__(self):
                if asyncio.current_task().get_name() == "idle-unload":
                    idle_waiting_for_lock.set()
                    await allow_idle_to_check_state.wait()
                await self._lock.acquire()
                return self

            async def __aexit__(self, exc_type, exc, tb):
                self._lock.release()

        stop_calls = []

        async def fake_stop_process(handle):
            stop_calls.append(handle)
            handle.process.returncode = 0

        async def fake_proxy_request(*args, **kwargs):
            assert swapper._active_requests == 1
            assert swapper.active_model == "model-a"
            proxy_started.set()
            await release_proxy.wait()
            return aiohttp.web.json_response({"ok": True})

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)
        monkeypatch.setattr(swapper_module, "proxy_request", fake_proxy_request)

        swapper = Swapper(simple_config, idle_timeout=0.001)
        swapper._lock = BlockingIdleLock()
        handle = _fake_handle("model-a", port=1234)
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True

        swapper._schedule_idle_timer()
        stale_idle_task = swapper._idle_task
        assert stale_idle_task is not None
        await asyncio.wait_for(idle_waiting_for_lock.wait(), timeout=1)

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                response_task = asyncio.create_task(client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a"},
                ))
                await asyncio.wait_for(proxy_started.wait(), timeout=1)

                allow_idle_to_check_state.set()
                await asyncio.wait_for(stale_idle_task, timeout=1)
                assert stop_calls == []
                assert swapper.active_model == "model-a"

                release_proxy.set()
                resp = await response_task
                assert resp.status == 200
                resp.release()
                assert stop_calls == []
                assert swapper.active_model == "model-a"
        finally:
            release_proxy.set()
            await swapper.shutdown()
            await server.close()

    async def test_idle_unload_waits_for_in_flight_request(
        self, simple_config: Config, monkeypatch
    ):
        """Idle timer starts after the proxied upstream request finishes."""
        import src.swapper as swapper_module

        request_started = asyncio.Event()
        release_response = asyncio.Event()
        stop_calls = []

        async def handle_chat(request):
            request_started.set()
            await release_response.wait()
            return aiohttp.web.json_response({"id": "model-a"})

        async def fake_stop_process(handle):
            stop_calls.append(handle)
            handle.process.returncode = 0

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", handle_chat)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()

        swapper = Swapper(simple_config, idle_timeout=0.03)
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
                response_task = asyncio.create_task(client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a"},
                ))
                await request_started.wait()
                await asyncio.sleep(0.06)
                assert stop_calls == []
                assert swapper.active_model == "model-a"

                release_response.set()
                resp = await response_task
                assert resp.status == 200
                resp.release()

                await asyncio.sleep(0.06)
                assert stop_calls == [handle]
                assert swapper.active_model is None
        finally:
            await swapper.shutdown()
            await proxy_server.close()
            await upstream_server.close()

    async def test_idle_unload_waits_for_in_flight_streaming_request(
        self, simple_config: Config, monkeypatch
    ):
        """Idle timeout does not stop a model while streaming is active."""
        import src.swapper as swapper_module

        stream_started = asyncio.Event()
        release_stream = asyncio.Event()
        stop_calls = []

        async def handle_chat(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            stream_started.set()
            await release_stream.wait()
            await resp.write(b'data: {"id": "model-a"}\n\n')
            await resp.write_eof()
            return resp

        async def fake_stop_process(handle):
            stop_calls.append(handle)
            handle.process.returncode = 0

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", handle_chat)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()

        swapper = Swapper(simple_config, idle_timeout=0.03)
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
                response_task = asyncio.create_task(client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                ))
                await stream_started.wait()
                await asyncio.sleep(0.06)
                assert stop_calls == []
                assert swapper.active_model == "model-a"

                release_stream.set()
                resp = await response_task
                assert resp.status == 200
                assert 'data: {"id": "model-a"}' in await resp.text()

                await asyncio.sleep(0.06)
                assert stop_calls == [handle]
                assert swapper.active_model is None
        finally:
            await swapper.shutdown()
            await proxy_server.close()
            await upstream_server.close()

    async def test_request_after_idle_unload_restarts_model(
        self, simple_config: Config, monkeypatch
    ):
        """A later model request starts a new model after idle unload."""
        import src.swapper as swapper_module

        old_handle = _fake_handle("model-a", port=1111)
        new_handle = _fake_handle("model-a", port=2222)
        start_calls = []

        async def fake_stop_process(handle):
            handle.process.returncode = 0

        async def fake_start_process(model_name, cmd, port):
            start_calls.append(model_name)
            return new_handle

        async def fake_wait_for_ready(*args, **kwargs):
            return None

        async def fake_proxy_request(*args, **kwargs):
            return aiohttp.web.json_response({"ok": True})

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)
        monkeypatch.setattr(swapper_module, "start_process", fake_start_process)
        monkeypatch.setattr(swapper_module, "wait_for_ready", fake_wait_for_ready)
        monkeypatch.setattr(swapper_module, "proxy_request", fake_proxy_request)

        swapper = Swapper(simple_config, idle_timeout=0.02)
        swapper._current = old_handle
        swapper._current_model = "model-a"
        swapper._healthy = True
        swapper._schedule_idle_timer()
        await asyncio.sleep(0.05)
        assert swapper.active_model is None

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                no_model_resp = await client.post(
                    "/v1/chat/completions",
                    json={"prompt": "no model after idle unload"},
                )
                assert no_model_resp.status == 400
                assert start_calls == []

                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a"},
                )
                assert resp.status == 200
                assert start_calls == ["model-a"]
                assert swapper._current is new_handle
                assert swapper.active_model == "model-a"
        finally:
            await swapper.shutdown()
            await server.close()
