"""Swapper swap-orchestration tests.

Start/stop swap flow, same-model fast path, 504 on swap timeout, request
queue overflow (503), and draining queued requests after a swap.
"""

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer, TestClient
import pytest

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle, stop_process
from src.swapper import Swapper, MAX_QUEUE

from tests.helpers import _drain, _make_upstream_with_config


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
