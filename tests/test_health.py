"""Tests for src/health.py — health check polling.

Derived from plan acceptance criteria:
- Health checker: /health polling, /v1/models fallback, timeout
"""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer, TestClient
import pytest

from src.health import wait_for_ready


class TestWaitForReady:

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

    async def test_falls_back_to_v1_models(self):
        """wait_for_ready falls back to /v1/models when /health 404s."""
        async def handle_models(request):
            return aiohttp.web.json_response({"object": "list", "data": []})

        app = aiohttp.web.Application()
        # Only /v1/models, no /health
        app.router.add_get("/v1/models", handle_models)
        server = TestServer(app)
        await server.start_server()
        try:
            await wait_for_ready(server.port, timeout=5)
        finally:
            await server.close()

    async def test_raises_timeout_when_never_ready(self):
        """wait_for_ready raises TimeoutError when server never starts."""
        with pytest.raises(TimeoutError, match="did not become ready"):
            await wait_for_ready(19876, timeout=0.5, interval=0.1)

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
        """wait_for_ready can use a shared ClientSession."""
        async def handle_health(request):
            return aiohttp.web.json_response({"status": "ok"})

        app = aiohttp.web.Application()
        app.router.add_get("/health", handle_health)
        server = TestServer(app)
        await server.start_server()
        client = aiohttp.ClientSession()
        try:
            await wait_for_ready(server.port, timeout=5, client=client)
        finally:
            await client.close()
            await server.close()

    async def test_creates_own_client_when_none(self):
        """wait_for_ready creates and closes its own client when none provided."""
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
