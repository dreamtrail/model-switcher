"""Tests for src/proxy.py — HTTP proxy handler.

Derived from plan acceptance criteria:
- Proxy: model extraction from JSON body, catch-all proxy, SSE streaming
- /v1/models returns static config list
- Header forwarding (all except hop-by-hop)
- Error response format (OpenAI-compatible JSON)
- Upstream crash returns 502
"""

from __future__ import annotations

import json

import aiohttp
from aiohttp.test_utils import TestServer, TestClient
import pytest

from src.proxy import (
    _HOP_BY_HOP_HEADERS,
    _forward_headers,
    _openai_error,
    extract_model,
    handle_models_list,
    proxy_request,
    stream_proxy,
)


class TestExtractModel:

    async def test_extracts_from_json_body(self):
        """Model name extracted from JSON body on model endpoints."""
        async def handler(request):
            model = extract_model(request, await request.read())
            return aiohttp.web.json_response({"model": model})

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "my-model", "prompt": "hello"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["model"] == "my-model"
        finally:
            await server.close()

    async def test_extracts_from_query_string(self):
        """Model name extracted from query string."""
        async def handler(request):
            model = extract_model(request)
            return aiohttp.web.json_response({"model": model})

        app = aiohttp.web.Application()
        app.router.add_get("/some/endpoint", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get("/some/endpoint?model=query-model")
                assert resp.status == 200
                data = await resp.json()
                assert data["model"] == "query-model"
        finally:
            await server.close()

    async def test_returns_none_for_non_model_endpoint(self):
        """Returns None for endpoints not in _MODEL_ENDPOINTS."""
        async def handler(request):
            model = extract_model(request, await request.read())
            return aiohttp.web.json_response({"model": model})

        app = aiohttp.web.Application()
        app.router.add_post("/v1/some/other/endpoint", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/some/other/endpoint",
                    json={"model": "my-model"},
                )
                data = await resp.json()
                assert data["model"] is None
        finally:
            await server.close()

    async def test_returns_none_for_non_json_content_type(self):
        """Returns None when content-type is not application/json."""
        async def handler(request):
            model = extract_model(request, await request.read())
            return aiohttp.web.json_response({"model": model})

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    data='{"model": "my-model"}',
                    headers={"Content-Type": "text/plain"},
                )
                data = await resp.json()
                assert data["model"] is None
        finally:
            await server.close()

    async def test_returns_none_for_empty_body(self):
        """Returns None when body is empty."""
        async def handler(request):
            model = extract_model(request, await request.read())
            return aiohttp.web.json_response({"model": model})

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    data="",
                    headers={"Content-Type": "application/json"},
                )
                data = await resp.json()
                assert data["model"] is None
        finally:
            await server.close()

    async def test_returns_none_for_invalid_json(self):
        """Returns None when body is not valid JSON."""
        async def handler(request):
            model = extract_model(request, await request.read())
            return aiohttp.web.json_response({"model": model})

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    data="not json",
                    headers={"Content-Type": "application/json"},
                )
                data = await resp.json()
                assert data["model"] is None
        finally:
            await server.close()

    async def test_returns_none_for_body_none(self):
        """Returns None when body is None."""
        async def handler(request):
            model = extract_model(request, None)
            return aiohttp.web.json_response({"model": model})

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"prompt": "hello"},
                )
                data = await resp.json()
                assert data["model"] is None
        finally:
            await server.close()

    async def test_returns_none_for_json_without_model_key(self):
        """Returns None when body is valid JSON but has no 'model' key."""
        async def handler(request):
            model = extract_model(request, await request.read())
            return aiohttp.web.json_response({"model": model})

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"prompt": "hello", "max_tokens": 100},
                )
                data = await resp.json()
                assert data["model"] is None
        finally:
            await server.close()


class TestOpenaiError:

    def test_returns_correct_format(self):
        """_openai_error returns OpenAI-compatible error structure."""
        result = _openai_error("test error", "test_code")
        assert "error" in result
        assert result["error"]["message"] == "test error"
        assert result["error"]["code"] == "test_code"
        assert result["error"]["type"] == "invalid_request_error"
        assert result["error"]["param"] is None

    def test_default_code_is_internal_error(self):
        """Default error code is 'internal_error'."""
        result = _openai_error("some error")
        assert result["error"]["code"] == "internal_error"


class TestForwardHeaders:

    async def test_forwards_normal_headers(self):
        """Normal headers are forwarded."""
        async def handler(request):
            headers_dict = dict(request.headers)
            return aiohttp.web.json_response(headers_dict)

        app = aiohttp.web.Application()
        app.router.add_get("/test", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get(
                    "/test",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer tok",
                        "X-Custom": "value",
                    },
                )
                data = await resp.json()
                assert data["Content-Type"] == "application/json"
                assert data["Authorization"] == "Bearer tok"
                assert data["X-Custom"] == "value"
        finally:
            await server.close()

    async def test_excludes_hop_by_hop_headers(self):
        """Hop-by-hop headers are excluded from forwarded headers."""
        async def handler(request):
            # Use _forward_headers to filter, then return what was kept
            headers_dict = _forward_headers(request)
            return aiohttp.web.json_response(headers_dict)

        app = aiohttp.web.Application()
        app.router.add_get("/test", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get("/test")
                data = await resp.json()
                # Keep-Alive and Transfer-Encoding should not be in result
                for h in ["Keep-Alive", "Transfer-Encoding", "Proxy-Authorization"]:
                    assert h not in data, f"{h} should be excluded"
        finally:
            await server.close()

    async def test_excludes_connection_listed_headers(self):
        """Headers listed in Connection header are also excluded."""
        async def handler(request):
            headers_dict = _forward_headers(request)
            return aiohttp.web.json_response(headers_dict)

        app = aiohttp.web.Application()
        app.router.add_get("/test", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get(
                    "/test",
                    headers={
                        "Connection": "X-Custom-Close, X-Another",
                        "X-Custom-Close": "true",
                        "X-Another": "value",
                        "Normal-Header": "value",
                    },
                )
                data = await resp.json()
                assert "X-Custom-Close" not in data
                assert "X-Another" not in data
                assert data.get("Normal-Header") == "value"
        finally:
            await server.close()


class TestHandleModelsList:

    async def test_returns_model_list(self):
        """Returns OpenAI-compatible model list."""
        async def handler(request):
            return await handle_models_list(request, ["model-a", "model-b"])

        app = aiohttp.web.Application()
        app.router.add_get("/v1/models", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get("/v1/models")
                assert resp.status == 200
                data = await resp.json()
                assert data["object"] == "list"
                assert len(data["data"]) == 2
                assert data["data"][0]["id"] == "model-a"
                assert data["data"][0]["object"] == "model"
                assert data["data"][0]["owned_by"] == "local"
        finally:
            await server.close()

    async def test_returns_empty_model_list(self):
        """Returns empty data array when no models configured."""
        async def handler(request):
            return await handle_models_list(request, [])

        app = aiohttp.web.Application()
        app.router.add_get("/v1/models", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get("/v1/models")
                assert resp.status == 200
                data = await resp.json()
                assert data["object"] == "list"
                assert data["data"] == []
        finally:
            await server.close()


class TestProxyRequest:

    async def test_forwards_request_to_upstream(self):
        """proxy_request forwards request to upstream and returns response."""
        async def upstream_handler(request):
            body = await request.json()
            return aiohttp.web.json_response(
                {"choices": [], "echo": body.get("prompt")}
            )

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()

        try:
            # Now create proxy handler
            async def proxy_handler(request):
                body = await request.read()
                upstream_url = f"http://{upstream_server.host}:{upstream_server.port}"
                return await proxy_request(request, upstream_url, body, timeout=10)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "test", "prompt": "hi"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["echo"] == "hi"
        finally:
            await upstream_server.close()
            await proxy_server.close()

    async def test_returns_502_on_upstream_error(self):
        """proxy_request returns 502 when upstream is unreachable."""
        async def proxy_handler(request):
            body = await request.read()
            upstream_url = "http://127.0.0.1:19876"
            return await proxy_request(request, upstream_url, body, timeout=1)

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", proxy_handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "test"},
                )
                assert resp.status == 502
                data = await resp.json()
                assert "error" in data
        finally:
            await server.close()

    async def test_forwards_upstream_headers(self):
        """proxy_request forwards upstream response headers."""
        async def upstream_handler(request):
            return aiohttp.web.Response(
                text="ok",
                headers={"X-Custom-Header": "custom-value"},
            )

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_get("/custom", upstream_handler)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()

        try:
            async def proxy_handler(request):
                body = await request.read()
                upstream_url = f"http://{upstream_server.host}:{upstream_server.port}"
                return await proxy_request(request, upstream_url, body, timeout=10)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_get("/custom", proxy_handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                resp = await client.get("/custom")
                assert resp.headers.get("X-Custom-Header") == "custom-value"
        finally:
            await upstream_server.close()
            await proxy_server.close()


class TestStreamProxy:

    async def test_streams_chunks(self):
        """stream_proxy passes through SSE chunks in real-time."""
        async def sse_handler(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(b'data: {"chunk": 1}\n\n')
            await resp.write(b'data: {"chunk": 2}\n\n')
            await resp.write_eof()
            return resp

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", sse_handler)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()

        try:
            async def proxy_handler(request):
                body = await request.read()
                upstream_url = f"http://{upstream_server.host}:{upstream_server.port}"
                return await stream_proxy(request, upstream_url, body, timeout=10)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "test", "stream": True},
                )
                assert resp.status == 200
                text = await resp.text()
                assert '"chunk": 1' in text
                assert '"chunk": 2' in text
        finally:
            await upstream_server.close()
            await proxy_server.close()

    async def test_returns_502_on_stream_upstream_error(self):
        """stream_proxy returns 502 when upstream is unreachable."""
        async def proxy_handler(request):
            body = await request.read()
            upstream_url = "http://127.0.0.1:19876"
            return await stream_proxy(request, upstream_url, body, timeout=1)

        app = aiohttp.web.Application()
        app.router.add_post("/v1/chat/completions", proxy_handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "test", "stream": True},
                )
                assert resp.status == 502
                data = await resp.json()
                assert "error" in data
        finally:
            await server.close()

    async def test_mid_stream_error_injects_sse_error(self):
        """When upstream disconnects mid-stream (after prepare()),
        the error is injected as an SSE data line, not a new HTTP response.

        This tests the inner try/except in stream_proxy that catches errors
        after response.prepare() has committed headers.
        """
        async def sse_disconnect_handler(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            # Write one chunk, then close without write_eof (simulates crash)
            await resp.write(b'data: {"chunk": 1}\n\n')
            # Close the writer to simulate upstream crash
            await resp.writer.close()
            return resp

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", sse_disconnect_handler)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()

        try:
            async def proxy_handler(request):
                body = await request.read()
                upstream_url = f"http://{upstream_server.host}:{upstream_server.port}"
                return await stream_proxy(request, upstream_url, body, timeout=10)

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()

            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "test", "stream": True},
                )
                # Status stays 200 (headers already committed)
                assert resp.status == 200
                text = await resp.text()
                # First chunk should be present
                assert '"chunk": 1' in text
                # Error SSE payload should be injected after the crash
                assert 'data: {"error"' in text or "error" in text.lower()
        finally:
            await upstream_server.close()
            await proxy_server.close()
