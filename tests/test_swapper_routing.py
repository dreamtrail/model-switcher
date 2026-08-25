"""Swapper routing and model-resolution tests.

/v1/models listing and alias, 400 when no model is specified, the unknown-model
error, and no-model requests proxied to the running model.
"""

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer, TestClient

from src.config import Config
from src.process_manager import ProcessHandle
from src.swapper import Swapper

from tests.helpers import _drain, _make_upstream_with_config


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
