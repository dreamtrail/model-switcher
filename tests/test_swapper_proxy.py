"""Swapper proxy-path tests.

SSE streaming passthrough, 502 from a dead upstream process, and the
model-handle lock path when the proxy is called with handle=None.
"""

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer, TestClient

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle
from src.swapper import Swapper

from tests.helpers import _drain, _make_upstream_with_config


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
