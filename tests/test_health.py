"""Tests for src/health.py — health check polling.

Derived from plan acceptance criteria:
- Health checker: /health polling, /v1/models fallback, timeout
"""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer
import pytest

from src.health import wait_for_ready


class TestWaitForReady:

    @pytest.mark.parametrize(
        ("body", "content_type"),
        (
            pytest.param(b"healthy", "text/plain", id="non-json-text"),
            pytest.param(b'{"status":', "application/json", id="malformed-json"),
            pytest.param(b'["loading"]', "application/json", id="json-list"),
            pytest.param(b'"loading"', "application/json", id="json-scalar"),
            pytest.param(b'{"phase":"loading"}', "application/json", id="missing-status"),
            pytest.param(b'{"status":"warming"}', "application/json", id="unknown-status"),
            pytest.param(b'{"status":"loading"}', "application/json", id="loading-status"),
            pytest.param(b'{"status":"error"}', "application/json", id="error-status"),
        ),
    )
    async def test_legacy_200_bodies_remain_ready_without_models_fallback(
        self, body, content_type
    ):
        """HTTP 200 bodies without loaded false preserve generic readiness."""
        models_count = 0

        async def handle_health(request):
            return aiohttp.web.Response(body=body, content_type=content_type)

        async def handle_models(request):
            nonlocal models_count
            models_count += 1
            return aiohttp.web.json_response({"object": "list", "data": []})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", handle_models)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=1, interval=0.01)
            assert models_count == 0
        finally:
            await server.close()

    # --- "loaded" flag (Strata, P-20261005-01) ---

    async def test_loaded_false_waits_until_loaded_true(self):
        """A 200 body with ``"loaded": false`` stays pending, even with ok."""
        allow_loaded = asyncio.Event()
        unloaded_seen = asyncio.Event()
        health_count = 0
        models_count = 0

        async def handle_health(request):
            nonlocal health_count
            health_count += 1
            if not allow_loaded.is_set():
                unloaded_seen.set()
                return aiohttp.web.json_response(
                    {"status": "ok", "loaded": False, "service": "strata"}
                )
            return aiohttp.web.json_response(
                {"status": "ok", "loaded": True, "service": "strata"}
            )

        async def handle_models(request):
            nonlocal models_count
            models_count += 1
            return aiohttp.web.json_response({"object": "list", "data": []})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", handle_models)
        server = TestServer(app)
        await server.start_server()
        task = asyncio.create_task(
            wait_for_ready(server.port, timeout=2, interval=0.01)
        )
        try:
            await asyncio.wait_for(unloaded_seen.wait(), timeout=1)
            await asyncio.sleep(0.05)
            assert not task.done()
            assert health_count >= 2

            allow_loaded.set()
            await asyncio.wait_for(task, timeout=1)
            assert models_count == 0
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await server.close()

    @pytest.mark.parametrize("status", ("ok", "loading", "error", "warming"))
    async def test_persistent_loaded_false_times_out_without_fallback(self, status):
        models_count = 0

        async def handle_health(request):
            return aiohttp.web.json_response({"status": status, "loaded": False})

        async def handle_models(request):
            nonlocal models_count
            models_count += 1
            return aiohttp.web.json_response({"object": "list", "data": []})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", handle_models)
        server = TestServer(app)
        await server.start_server()
        try:
            with pytest.raises(TimeoutError, match="did not become ready"):
                await wait_for_ready(server.port, timeout=0.05, interval=0.01)
            assert models_count == 0
        finally:
            await server.close()

    @pytest.mark.parametrize(
        "body",
        (
            pytest.param({"status": "ok", "loaded": True}, id="ok-loaded"),
            pytest.param({"loaded": True}, id="loaded-only"),
            pytest.param({"status": "ok"}, id="loaded-absent"),
            pytest.param({"loaded": "false"}, id="loaded-string"),
            pytest.param({"loaded": 0}, id="loaded-zero"),
            pytest.param({"loaded": None}, id="loaded-null"),
        ),
    )
    async def test_loaded_true_absent_or_non_bool_is_ready(self, body):
        """Only a JSON ``false`` blocks readiness; other values keep old rules."""
        async def handle_health(request):
            return aiohttp.web.json_response(body)

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=1, interval=0.01)
        finally:
            await server.close()

    @pytest.mark.parametrize("status", ("loading", "error"))
    async def test_loaded_true_is_ready_regardless_of_status(self, status):
        async def handle_health(request):
            return aiohttp.web.json_response(
                {"status": status, "loaded": True}
            )

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=1, interval=0.01)
        finally:
            await server.close()

    async def test_returns_on_health_200(self):
        """wait_for_ready returns when /health returns 200."""
        async def handle_health(request):
            return aiohttp.web.json_response({"status": "ok"})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=5)
        finally:
            await server.close()

    @pytest.mark.parametrize("health_status", (404, 405))
    async def test_falls_back_to_v1_models_when_health_is_unsupported(
        self, health_status
    ):
        """Original /health 404 and 405 permit the model-list fallback."""
        async def handle_health(request):
            return aiohttp.web.Response(status=health_status)

        async def handle_models(request):
            return aiohttp.web.json_response({"object": "list", "data": []})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", handle_models)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=5)
        finally:
            await server.close()

    @pytest.mark.parametrize(
        ("health_status", "models_status"),
        (
            pytest.param(404, 503, id="health-404-models-loading"),
            pytest.param(405, 401, id="health-405-models-authentication"),
        ),
    )
    async def test_models_fallback_non_200_waits_for_primary_health_200(
        self, health_status, models_status
    ):
        """A failed permitted fallback stays pending until primary health is 200."""
        health_ready = False
        fallback_retried = asyncio.Event()
        models_count = 0

        async def handle_health(request):
            return aiohttp.web.Response(status=200 if health_ready else health_status)

        async def handle_models(request):
            nonlocal models_count
            models_count += 1
            if models_count >= 2:
                fallback_retried.set()
            return aiohttp.web.Response(status=models_status)

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", handle_models)
        server = TestServer(app)
        await server.start_server()
        task = asyncio.create_task(
            wait_for_ready(server.port, timeout=2, interval=0.01)
        )
        try:
            await asyncio.wait_for(fallback_retried.wait(), timeout=1)
            assert not task.done()
            assert models_count >= 2

            health_ready = True
            await asyncio.wait_for(task, timeout=1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await server.close()

    @pytest.mark.parametrize("health_status", (404, 405))
    async def test_models_fallback_request_failure_waits_for_primary_health_200(
        self, health_status
    ):
        """A fallback transport failure stays pending until primary health is 200."""
        health_ready = False
        fallback_retried = asyncio.Event()
        models_count = 0

        async def handle_health(request):
            return aiohttp.web.Response(status=200 if health_ready else health_status)

        async def disconnect_models(request):
            nonlocal models_count
            models_count += 1
            if models_count >= 2:
                fallback_retried.set()
            request.transport.close()
            return aiohttp.web.Response()

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", disconnect_models)
        server = TestServer(app)
        await server.start_server()
        task = asyncio.create_task(
            wait_for_ready(server.port, timeout=2, interval=0.01)
        )
        try:
            await asyncio.wait_for(fallback_retried.wait(), timeout=1)
            assert not task.done()
            assert models_count >= 2

            health_ready = True
            await asyncio.wait_for(task, timeout=1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await server.close()

    async def test_models_fallback_preserves_redirect_success(self):
        """The permitted /v1/models fallback keeps following redirects."""
        redirect_count = 0
        ready_count = 0

        async def handle_health(request):
            return aiohttp.web.Response(status=404)

        async def handle_models(request):
            nonlocal redirect_count
            redirect_count += 1
            raise aiohttp.web.HTTPFound("/advertised-models")

        async def handle_advertised_models(request):
            nonlocal ready_count
            ready_count += 1
            return aiohttp.web.json_response({"object": "list", "data": []})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", handle_models)
        app.router.add_get("/advertised-models", handle_advertised_models)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=5)
            assert redirect_count == 1
            assert ready_count == 1
        finally:
            await server.close()

    @pytest.mark.parametrize(
        "health_status",
        (
            pytest.param(503, id="loading"),
            pytest.param(500, id="server-error"),
            pytest.param(401, id="authentication"),
        ),
    )
    async def test_explicit_health_failure_cannot_be_overridden_by_models_200(
        self, health_status
    ):
        """Implemented health failures remain pending despite model discovery."""
        health_count = 0
        models_count = 0

        async def handle_health(request):
            nonlocal health_count
            health_count += 1
            if health_count >= 3:
                return aiohttp.web.Response(status=200)
            return aiohttp.web.Response(status=health_status)

        async def handle_models(request):
            nonlocal models_count
            models_count += 1
            return aiohttp.web.json_response({"object": "list", "data": []})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", handle_models)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=2, interval=0.01)
            assert health_count == 3
            assert models_count == 0
        finally:
            await server.close()

    async def test_health_redirect_to_models_200_remains_pending(self):
        """The primary probe observes its original redirect response."""
        health_count = 0
        models_count = 0

        async def handle_health(request):
            nonlocal health_count
            health_count += 1
            if health_count >= 3:
                return aiohttp.web.Response(status=200)
            raise aiohttp.web.HTTPFound("/v1/models")

        async def handle_models(request):
            nonlocal models_count
            models_count += 1
            return aiohttp.web.json_response({"object": "list", "data": []})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/v1/models", handle_models)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=2, interval=0.01)
            assert health_count == 3
            assert models_count == 0
        finally:
            await server.close()

    async def test_raises_timeout_when_never_ready(self, unused_tcp_port):
        """wait_for_ready raises TimeoutError when server never starts."""
        with pytest.raises(TimeoutError, match="did not become ready"):
            await wait_for_ready(unused_tcp_port, timeout=0.5, interval=0.1)

    async def test_respects_custom_interval(self):
        """wait_for_ready uses the provided interval between polls."""
        poll_count = 0

        async def handle_health(request):
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 3:
                return aiohttp.web.json_response({"status": "ok"})
            return aiohttp.web.json_response({"status": "loading"}, status=503)

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=5, interval=0.05)
            assert poll_count >= 3
        finally:
            await server.close()

    async def test_uses_shared_client(self):
        """A supplied ClientSession remains open and reusable after success."""
        health_count = 0

        async def handle_health(request):
            nonlocal health_count
            health_count += 1
            return aiohttp.web.json_response({"status": "ok"})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        server = TestServer(app)
        await server.start_server()
        client = aiohttp.ClientSession()
        try:
            await wait_for_ready(server.port, timeout=5, client=client)

            assert not client.closed
            async with client.get(
                f"http://127.0.0.1:{server.port}/health"
            ) as response:
                assert response.status == 200
                assert await response.json() == {"status": "ok"}
            assert health_count == 2
        finally:
            await client.close()
            await server.close()

    async def test_creates_own_client_when_none(self, monkeypatch):
        """An internally created ClientSession is closed after success."""
        async def handle_health(request):
            return aiohttp.web.json_response({"status": "ok"})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        server = TestServer(app)
        await server.start_server()
        real_client_session = aiohttp.ClientSession
        created_sessions = []

        def tracking_client_session(*args, **kwargs):
            session = real_client_session(*args, **kwargs)
            created_sessions.append(session)
            return session

        monkeypatch.setattr(aiohttp, "ClientSession", tracking_client_session)
        try:
            await wait_for_ready(server.port, timeout=5)

            assert len(created_sessions) == 1
            assert created_sessions[0].closed
        finally:
            for session in created_sessions:
                if not session.closed:
                    await session.close()
            await server.close()

    async def test_closes_own_client_after_timeout(self, monkeypatch):
        """An internally created ClientSession is also closed on timeout."""
        async def handle_health(request):
            return aiohttp.web.Response(status=503)

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        server = TestServer(app)
        await server.start_server()
        real_client_session = aiohttp.ClientSession
        created_sessions = []

        def tracking_client_session(*args, **kwargs):
            session = real_client_session(*args, **kwargs)
            created_sessions.append(session)
            return session

        monkeypatch.setattr(aiohttp, "ClientSession", tracking_client_session)
        try:
            with pytest.raises(TimeoutError, match="did not become ready"):
                await wait_for_ready(server.port, timeout=0, interval=0)

            assert len(created_sessions) == 1
            assert created_sessions[0].closed
        finally:
            for session in created_sessions:
                if not session.closed:
                    await session.close()
            await server.close()
