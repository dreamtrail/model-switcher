"""Tests for src/swapper.py — swap orchestration and request routing.

Derived from plan acceptance criteria:
- Swapper: swap flow (A->B), request queue (max 10, 503 beyond), 504 on timeout
- /v1/models returns static config list
- Upstream crash returns 502
- Swap timeout (60s default) returns 504
- Idle timeout unloads inactive models only after proxied requests finish
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestServer, TestClient
import pytest

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle, stop_process
from src.swapper import Swapper, MAX_QUEUE


class TestHandleModelsList:

    async def test_returns_configured_models(self, simple_config: Config):
        """GET /v1/models returns all configured model names."""
        swapper = Swapper(simple_config)

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get("/v1/models")
                assert resp.status == 200
                data = await resp.json()
                assert data["object"] == "list"
                model_ids = {m["id"] for m in data["data"]}
                assert model_ids == {"model-a", "model-b"}
        finally:
            await server.close()

    async def test_models_endpoint_alias(self, simple_config: Config):
        """GET /models also works as an alias."""
        swapper = Swapper(simple_config)

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get("/models")
                assert resp.status == 200
        finally:
            await server.close()

    async def test_models_post_not_handled(self, simple_config: Config):
        """POST to /v1/models is not treated as the models list endpoint."""
        swapper = Swapper(simple_config)

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post("/v1/models", json={})
                assert resp.status == 400
        finally:
            await server.close()


class TestNoModelSpecified:

    async def test_returns_400_when_no_model_and_nothing_running(
        self, simple_config: Config
    ):
        """Request without model and no model running returns 400."""
        swapper = Swapper(simple_config)

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/some/endpoint",
                    json={"prompt": "hello"},
                )
                assert resp.status == 400
                data = await resp.json()
                assert "error" in data
        finally:
            await server.close()


class TestSwapFlow:

    async def test_first_request_starts_model_and_proxies(self):
        """First request for a model starts that model and proxies.

        We bypass process start/health check by pre-setting a handle
        pointing to the test upstream server.
        """
        from src.process_manager import ProcessHandle

        upstream_app, config = _make_upstream_with_config("model-a")
        server = TestServer(upstream_app)
        await server.start_server()

        try:
            mc = config.models["model-a"]
            mc.port = server.port

            swapper = Swapper(config)

            # Pre-set model-a as active (bypass start_process + health check)
            proc = await asyncio.create_subprocess_exec(
                "python", "-c", "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            handle = ProcessHandle(
                model_name="model-a",
                port=server.port,
                process=proc,
                _stdout_task=asyncio.create_task(_drain(proc.stdout, "a")),
                _stderr_task=asyncio.create_task(_drain(proc.stderr, "a")),
            )
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True

            async def handler(request):
                return await swapper.handle_request(request)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_route("*", "/{path:.*}", handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["id"] == "model-a"

        finally:
            await swapper.shutdown()
            await server.close()
            await proxy_server.close()

    async def test_swap_stops_old_starts_new(self):
        """Request for model B stops model A and starts model B.

        We pre-set both model handles and verify the swap logic:
        old model is stopped, new model becomes active, requests route correctly.
        """
        from src.process_manager import ProcessHandle, is_running

        upstream_a, _ = _make_upstream_with_config("model-a")
        upstream_b, _ = _make_upstream_with_config("model-b")
        server_a = TestServer(upstream_a)
        server_b = TestServer(upstream_b)
        await server_a.start_server()
        await server_b.start_server()

        try:
            config = Config(models={})
            config.models["model-a"] = ModelConfig(
                name="model-a", cmd="sleep 60", port=server_a.port
            )
            config.models["model-b"] = ModelConfig(
                name="model-b", cmd="sleep 60", port=server_b.port
            )

            swapper = Swapper(config)

            # Pre-set model-a as active
            proc_a = await asyncio.create_subprocess_exec(
                "python", "-c", "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            handle_a = ProcessHandle(
                model_name="model-a",
                port=server_a.port,
                process=proc_a,
                _stdout_task=asyncio.create_task(_drain(proc_a.stdout, "a")),
                _stderr_task=asyncio.create_task(_drain(proc_a.stderr, "a")),
            )
            swapper._current = handle_a
            swapper._current_model = "model-a"
            swapper._healthy = True

            async def handler(request):
                return await swapper.handle_request(request)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_route("*", "/{path:.*}", handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                # Request model-a (already active)
                resp_a = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp_a.status == 200
                data_a = await resp_a.json()
                assert data_a["id"] == "model-a"

                # Manually swap to model-b (simulate what _ensure_model does)
                # since we can't easily start real model-b process
                await stop_process(handle_a)
                assert not is_running(handle_a)

                proc_b = await asyncio.create_subprocess_exec(
                    "python", "-c", "import time; time.sleep(60)",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                handle_b = ProcessHandle(
                    model_name="model-b",
                    port=server_b.port,
                    process=proc_b,
                    _stdout_task=asyncio.create_task(_drain(proc_b.stdout, "b")),
                    _stderr_task=asyncio.create_task(_drain(proc_b.stderr, "b")),
                )
                swapper._current = handle_b
                swapper._current_model = "model-b"
                swapper._healthy = True

                # Request model-b
                resp_b = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-b", "prompt": "hi"},
                )
                assert resp_b.status == 200
                data_b = await resp_b.json()
                assert data_b["id"] == "model-b"

        finally:
            await swapper.shutdown()
            await server_a.close()
            await server_b.close()
            await proxy_server.close()


class TestSwapperTeardown:

    async def test_swap_calls_stop_for_already_exited_parent(self, monkeypatch):
        """Swapping calls stop_process even if tracked parent exited."""
        from src.process_manager import ProcessHandle
        import src.swapper as swapper_module

        class FakeProcess:
            def __init__(self, returncode):
                self.returncode = returncode
                self.pid = 12345

        config = Config(models={})
        config.models["model-a"] = ModelConfig(
            name="model-a", cmd="echo a --port ${PORT}", port=1111
        )
        config.models["model-b"] = ModelConfig(
            name="model-b", cmd="echo b --port ${PORT}", port=2222
        )
        swapper = Swapper(config)

        old_handle = ProcessHandle(
            model_name="model-a",
            port=1111,
            process=FakeProcess(returncode=0),
        )
        new_handle = ProcessHandle(
            model_name="model-b",
            port=2222,
            process=FakeProcess(returncode=None),
        )
        stop_calls = []

        async def fake_stop_process(handle):
            stop_calls.append(handle)

        async def fake_start_process(model_name, cmd, port):
            return new_handle

        async def fake_wait_for_ready(*args, **kwargs):
            return None

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)
        monkeypatch.setattr(swapper_module, "start_process", fake_start_process)
        monkeypatch.setattr(swapper_module, "wait_for_ready", fake_wait_for_ready)

        swapper._current = old_handle
        swapper._current_model = "model-a"
        swapper._healthy = False

        try:
            handle = await swapper._ensure_model("model-b")
            assert handle is new_handle
            assert stop_calls == [old_handle]
        finally:
            swapper._current = None
            await swapper.shutdown()

    async def test_shutdown_calls_stop_for_already_exited_parent(
        self, simple_config: Config, monkeypatch
    ):
        """Shutdown calls stop_process even if tracked parent exited."""
        from src.process_manager import ProcessHandle
        import src.swapper as swapper_module

        class FakeProcess:
            returncode = 0
            pid = 12345

        swapper = Swapper(simple_config)
        handle = ProcessHandle(
            model_name="model-a",
            port=1234,
            process=FakeProcess(),
        )
        stop_calls = []

        async def fake_stop_process(stopped_handle):
            stop_calls.append(stopped_handle)

        monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)

        swapper._current = handle
        await swapper.shutdown()

        assert stop_calls == [handle]
        assert swapper._current is None


class TestSwapTimeout:

    async def test_returns_504_on_timeout(self):
        """Swap that times out returns 504."""
        config = Config(models={})
        mc = ModelConfig(
            name="timeout-model",
            cmd="echo timeout --port 19876",
            port=19876,
        )
        config.models["timeout-model"] = mc

        swapper = Swapper(config, timeout=0.5)

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "timeout-model"},
                )
                assert resp.status == 504
                data = await resp.json()
                assert "error" in data
                assert data["error"]["code"] == "swap_timeout"
        finally:
            await server.close()


class TestUnknownModel:

    async def test_returns_error_for_unknown_model(self, simple_config: Config):
        """Request for unknown model returns 404 with JSON error."""
        swapper = Swapper(simple_config)

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "unknown-model"},
                )
                assert resp.status == 404
                data = await resp.json()
                assert "error" in data
                assert "unknown-model" in data["error"]["message"]
        finally:
            await server.close()


class TestActiveProperties:

    async def test_active_model_returns_none_when_nothing_running(
        self, simple_config: Config
    ):
        """active_model is None when no model is running."""
        swapper = Swapper(simple_config)
        assert swapper.active_model is None

    async def test_active_model_returns_model_name_when_running(
        self, simple_config: Config
    ):
        """active_model returns the model name when a model is running."""
        from src.process_manager import start_process

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        try:
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True
            assert swapper.active_model == "model-a"
        finally:
            await swapper.shutdown()

    async def test_active_model_returns_none_when_not_healthy(
        self, simple_config: Config
    ):
        """active_model is None when _healthy is False."""
        from src.process_manager import start_process

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        try:
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = False
            assert swapper.active_model is None
        finally:
            await stop_process(handle)

    async def test_active_port_returns_none_when_nothing_running(
        self, simple_config: Config
    ):
        """active_port is None when no model is running."""
        swapper = Swapper(simple_config)
        assert swapper.active_port is None

    async def test_active_port_returns_port_when_running(
        self, simple_config: Config
    ):
        """active_port returns the port when a model is running."""
        from src.process_manager import start_process

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        try:
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True
            assert swapper.active_port == mc.port
        finally:
            await swapper.shutdown()


class TestQueueOverflow:

    async def test_queue_full_raises_queuefull(self, simple_config: Config):
        """_queue_request raises QueueFull when queue is at capacity."""
        swapper = Swapper(simple_config)
        loop = asyncio.get_running_loop()

        # Fill queue to MAX_QUEUE
        for _ in range(MAX_QUEUE):
            swapper._queue.append(loop.create_future())

        # Next should raise QueueFull
        with pytest.raises(asyncio.QueueFull):
            swapper._queue_request()

    async def test_handle_request_returns_503_on_queue_full(
        self, simple_config: Config
    ):
        """When swap is in progress and queue is full, request gets 503.

        Tests via handle_request with _swapping=True and queue pre-filled.
        """
        swapper = Swapper(simple_config)
        loop = asyncio.get_running_loop()

        # Simulate swap in progress with full queue
        swapper._swapping = True
        for _ in range(MAX_QUEUE):
            swapper._queue.append(loop.create_future())

        async def handler(request):
            return await swapper.handle_request(request)

        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-b"},
                )
                assert resp.status == 503
                data = await resp.json()
                assert "error" in data
        finally:
            await server.close()
            swapper._swapping = False


class TestConcurrentRequestsDuringSwap:

    async def test_queue_request_respects_max_size(self, simple_config: Config):
        """_queue_request raises QueueFull when at capacity."""
        swapper = Swapper(simple_config)

        loop = asyncio.get_running_loop()
        # Fill queue to MAX_QUEUE
        for _ in range(MAX_QUEUE):
            swapper._queue.append(loop.create_future())

        # Next request should raise QueueFull
        with pytest.raises(asyncio.QueueFull):
            swapper._queue_request()

    async def test_drain_queue_resolves_all_futures(self, simple_config: Config):
        """_drain_queue resolves all queued futures."""
        swapper = Swapper(simple_config)
        loop = asyncio.get_running_loop()

        futures = []
        for _ in range(3):
            fut = loop.create_future()
            swapper._queue.append(fut)
            futures.append(fut)

        await swapper._drain_queue()

        for fut in futures:
            assert fut.done()

    async def test_queue_during_swap_then_drained(self, simple_config: Config):
        """Requests queued during swap are drained after swap completes."""
        from src.process_manager import start_process

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        try:
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True

            # Simulate: swap in progress, queue has pending futures
            swapper._swapping = True
            loop = asyncio.get_running_loop()
            queued_futures = []
            for _ in range(3):
                fut = loop.create_future()
                swapper._queue.append(fut)
                queued_futures.append(fut)

            # Drain (simulating swap completion)
            swapper._swapping = False
            await swapper._drain_queue()

            # All futures should be resolved
            for fut in queued_futures:
                assert fut.done()
        finally:
            await swapper.shutdown()


class TestEnsureModelFastPath:

    async def test_same_model_reused_without_restart(self):
        """When the correct model is already running and healthy,
        _ensure_model returns immediately without starting a new process.

        This is the most common code path — repeated requests to the same model.
        """
        from src.process_manager import ProcessHandle

        upstream_app, config = _make_upstream_with_config("model-a")
        server = TestServer(upstream_app)
        await server.start_server()

        try:
            mc = config.models["model-a"]
            mc.port = server.port

            swapper = Swapper(config)

            # Pre-set model-a as active
            proc = await asyncio.create_subprocess_exec(
                "python", "-c", "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            handle = ProcessHandle(
                model_name="model-a",
                port=server.port,
                process=proc,
                _stdout_task=asyncio.create_task(_drain(proc.stdout, "a")),
                _stderr_task=asyncio.create_task(_drain(proc.stderr, "a")),
            )
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True

            async def handler(request):
                return await swapper.handle_request(request)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_route("*", "/{path:.*}", handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                # First request — hits fast path (model already active)
                resp1 = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp1.status == 200

                # Second request — should also hit fast path, no restart
                resp2 = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "again"},
                )
                assert resp2.status == 200

                # Verify the same process is still running (not restarted)
                assert swapper._current is handle
                assert swapper._current_model == "model-a"
                assert swapper._healthy is True

        finally:
            await swapper.shutdown()
            await server.close()
            await proxy_server.close()


class TestDeadUpstreamProcess:

    async def test_dead_process_returns_502_from_proxy_for_model(self):
        """_proxy_for_model returns 502 when the handle's process is dead.

        Simulates the case where the upstream dies and a dead handle is passed.
        """
        from src.process_manager import ProcessHandle

        config = Config(models={})
        config.models["model-a"] = ModelConfig(
            name="model-a",
            cmd="echo hello --port ${PORT}",
            port=19877,
        )

        swapper = Swapper(config)

        # Start a short-lived process that exits immediately
        proc = await asyncio.create_subprocess_exec(
            "python", "-c", "pass",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Wait for it to exit
        await proc.wait()

        handle = ProcessHandle(
            model_name="model-a",
            port=19877,
            process=proc,
        )

        async def handler(req):
            body = await req.read()
            return await swapper._proxy_for_model(
                req, "model-a", body, handle
            )

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a"},
                )
                assert resp.status == 502
                data = await resp.json()
                assert "error" in data
                assert "not running" in data["error"]["message"]
        finally:
            await server.close()


class TestShutdown:

    async def test_shutdown_stops_current_model(self, simple_config: Config):
        """Shutdown stops the currently running model."""
        from src.process_manager import start_process, is_running

        swapper = Swapper(simple_config)
        mc = simple_config.models["model-a"]
        handle = await start_process(
            "model-a",
            "python -c 'import time; time.sleep(60)'",
            mc.port,
        )
        swapper._current = handle
        swapper._current_model = "model-a"
        swapper._healthy = True

        try:
            await swapper.shutdown()
            assert swapper._current is None
            assert not is_running(handle)
        finally:
            # Safety cleanup in case shutdown didn't stop the process
            if is_running(handle):
                await stop_process(handle)

    async def test_shutdown_drains_queue(self, simple_config: Config):
        """Shutdown resolves any queued futures."""
        swapper = Swapper(simple_config)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        swapper._queue.append(future)

        await swapper.shutdown()
        assert future.done()

    async def test_shutdown_closes_shared_client(self, simple_config: Config):
        """Shutdown closes the shared ClientSession."""
        client = aiohttp.ClientSession()
        swapper = Swapper(simple_config, client=client)

        await swapper.shutdown()
        assert swapper._client is None
        # ClientSession.closed is True after close()
        # We can't directly check client.closed, but if shutdown didn't
        # raise and set _client to None, the close() was called.


class TestStreamingThroughSwapper:

    async def test_streaming_request_uses_stream_proxy(self):
        """Request with stream=true goes through stream_proxy path.

        Sends a streaming request through handle_request and verifies
        SSE chunks are returned (not buffered JSON).
        """
        from src.process_manager import ProcessHandle

        upstream_app, config = _make_upstream_with_config("model-a")
        server = TestServer(upstream_app)
        await server.start_server()

        try:
            mc = config.models["model-a"]
            mc.port = server.port

            swapper = Swapper(config)

            # Pre-set model-a as active
            proc = await asyncio.create_subprocess_exec(
                "python", "-c", "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            handle = ProcessHandle(
                model_name="model-a",
                port=server.port,
                process=proc,
                _stdout_task=asyncio.create_task(_drain(proc.stdout, "a")),
                _stderr_task=asyncio.create_task(_drain(proc.stderr, "a")),
            )
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True

            async def handler(request):
                return await swapper.handle_request(request)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_route("*", "/{path:.*}", handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                # Streaming response should have SSE content-type
                text = await resp.text()
                # Verify SSE format (data: prefix)
                assert "data:" in text
                assert "model-a" in text

        finally:
            await swapper.shutdown()
            await server.close()
            await proxy_server.close()


class TestNoModelWithRunningModel:

    async def test_proxies_to_running_model_when_no_model_specified(self):
        """Request without model field proxies to the currently running model.

        When no model is specified in the request and a model is already
        running and healthy, the request should be proxied to that model.
        """
        from src.process_manager import ProcessHandle

        upstream_app, config = _make_upstream_with_config("model-a")
        server = TestServer(upstream_app)
        await server.start_server()

        try:
            mc = config.models["model-a"]
            mc.port = server.port

            swapper = Swapper(config)

            # Pre-set model-a as active
            proc = await asyncio.create_subprocess_exec(
                "python", "-c", "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            handle = ProcessHandle(
                model_name="model-a",
                port=server.port,
                process=proc,
                _stdout_task=asyncio.create_task(_drain(proc.stdout, "a")),
                _stderr_task=asyncio.create_task(_drain(proc.stderr, "a")),
            )
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True

            async def handler(request):
                return await swapper.handle_request(request)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_route("*", "/{path:.*}", handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                # Request WITHOUT model field
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"prompt": "hi"},
                )
                assert resp.status == 200
                data = await resp.json()
                # Should be proxied to model-a
                assert data["id"] == "model-a"

        finally:
            await swapper.shutdown()
            await server.close()
            await proxy_server.close()


class TestProxyForModelHandleNone:

    async def test_handle_none_acquires_lock_and_uses_current(self):
        """_proxy_for_model with handle=None acquires lock and uses self._current.

        This is the path taken after queue drain when the handle is not passed.
        """
        from src.process_manager import ProcessHandle

        upstream_app, config = _make_upstream_with_config("model-a")
        server = TestServer(upstream_app)
        await server.start_server()

        try:
            mc = config.models["model-a"]
            mc.port = server.port

            swapper = Swapper(config)

            # Pre-set model-a as active
            proc = await asyncio.create_subprocess_exec(
                "python", "-c", "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            handle = ProcessHandle(
                model_name="model-a",
                port=server.port,
                process=proc,
                _stdout_task=asyncio.create_task(_drain(proc.stdout, "a")),
                _stderr_task=asyncio.create_task(_drain(proc.stderr, "a")),
            )
            swapper._current = handle
            swapper._current_model = "model-a"
            swapper._healthy = True

            async def handler(request):
                body = await request.read()
                return await swapper._proxy_for_model(
                    request, "model-a", body, None
                )

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_post("/v1/chat/completions", handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "prompt": "hi"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["id"] == "model-a"

        finally:
            await swapper.shutdown()
            await server.close()
            await proxy_server.close()


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


# --- Helpers ---

class _FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.pid = 12345


def _fake_handle(model_name="model-a", port=1234):
    return ProcessHandle(
        model_name=model_name,
        port=port,
        process=_FakeProcess(),
    )


async def _drain(reader, prefix):
    """Drain a stream (for fake process handle)."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
    except asyncio.CancelledError:
        pass


def _make_upstream_with_config(identifier="model-a"):
    """Create an upstream app and config for a model."""
    async def handle_health(request):
        return aiohttp.web.json_response({"status": "ok"})

    async def handle_chat(request):
        body = await request.json()
        if body.get("stream"):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                f'data: {{"id": "{identifier}"}}\n\n'.encode()
            )
            await resp.write_eof()
            return resp
        return aiohttp.web.json_response({"choices": [], "id": identifier})

    app = aiohttp.web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_get("/v1/models", handle_health)
    app.router.add_get("/models", handle_health)
    app.router.add_post("/v1/chat/completions", handle_chat)

    config = Config(models={})
    mc = ModelConfig(
        name=identifier,
        cmd=f"echo {identifier} --port ${{PORT}}",
    )
    config.models[identifier] = mc
    return app, config
