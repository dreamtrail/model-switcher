"""HTTP proxy model extraction, headers, models, errors, and buffered usage."""

from __future__ import annotations

import asyncio
import http.server
import threading

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
from src.stats import Outcome


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


    async def test_drops_client_host_header(self):
        """The client's Host is not forwarded (P-20261005-01)."""
        async def handler(request):
            return aiohttp.web.json_response(_forward_headers(request))

        app = aiohttp.web.Application()
        app.router.add_get("/test", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.get(
                    "/test", headers={"Host": "dgx1:8080", "X-Keep": "1"},
                )
                data = await resp.json()
                assert {key.lower() for key in data} >= {"x-keep"}
                assert "host" not in {key.lower() for key in data}
        finally:
            await server.close()

    @pytest.mark.parametrize("stream", (False, True), ids=("json", "stream"))
    async def test_upstream_receives_its_own_address_as_host(self, stream):
        """A client Host such as dgx1:8080 reaches the backend as 127.0.0.1:<port>."""
        seen = []

        async def upstream_handler(request):
            seen.append(request.headers.getall("Host"))
            return aiohttp.web.json_response({"ok": True})

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
        upstream_server = TestServer(upstream_app, host="127.0.0.1")
        await upstream_server.start_server()
        upstream_port = upstream_server.port
        upstream_url = f"http://127.0.0.1:{upstream_port}"
        forward = stream_proxy if stream else proxy_request

        async def proxy_handler(request):
            body = await request.read()
            return await forward(request, upstream_url, body, timeout=10)

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()
        try:
            async with TestClient(proxy_server) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "test"},
                    headers={"Host": "dgx1:8080"},
                )
                assert resp.status == 200
                await resp.read()
        finally:
            await proxy_server.close()
            await upstream_server.close()

        assert seen == [[f"127.0.0.1:{upstream_port}"]]

    async def test_drops_expect_header(self):
        """Expect is not forwarded; other headers are (P-20261119-01)."""
        async def handler(request):
            return aiohttp.web.json_response(_forward_headers(request))

        app = aiohttp.web.Application()
        app.router.add_post("/test", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            async with TestClient(server) as client:
                resp = await client.post(
                    "/test",
                    data=b"{}",
                    headers={
                        "Expect": "100-continue",
                        "Authorization": "Bearer tok",
                        "X-Keep": "1",
                    },
                )
                data = await resp.json()
                assert "expect" not in {key.lower() for key in data}
                assert data["Authorization"] == "Bearer tok"
                assert data["X-Keep"] == "1"
        finally:
            await server.close()

    @pytest.mark.parametrize("stream", (False, True), ids=("json", "stream"))
    async def test_expect_100_continue_reaches_upstream_without_100(
        self, stream,
    ):
        """A client Expect: 100-continue does not hang on an upstream that
        never sends 100 Continue (stdlib HTTP/1.0 server, like Strata)."""
        seen_expect = []

        class Handler(http.server.BaseHTTPRequestHandler):
            # HTTP/1.0: BaseHTTPRequestHandler never sends 100 Continue.
            def do_POST(self):
                seen_expect.append(self.headers.get("Expect"))
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if stream:
                    payload = b"data: {}\n\ndata: [DONE]\n\n"
                    ctype = "text/event-stream"
                else:
                    payload = b'{"ok": true}'
                    ctype = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        upstream_url = f"http://127.0.0.1:{upstream.server_port}"
        forward = stream_proxy if stream else proxy_request

        async def proxy_handler(request):
            body = await request.read()
            return await forward(request, upstream_url, body, timeout=10)

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()
        try:
            async with TestClient(proxy_server) as client:
                async def send():
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={"model": "test", "stream": stream},
                        headers={"Expect": "100-continue"},
                    )
                    return resp.status, await resp.read()

                status, body = await asyncio.wait_for(send(), timeout=3)
        finally:
            await proxy_server.close()
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

        assert status == 200
        if stream:
            assert b"data: [DONE]" in body
        else:
            assert body == b'{"ok": true}'
        assert seen_expect == [None]


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


# ---------------------------------------------------------------------------
# P-20260913-02: buffered usage extraction (AC2)
# ---------------------------------------------------------------------------


class TestBufferedUsageExtraction:
    """proxy_request populates Outcome token fields from a JSON usage body."""

    async def _post_buffered(self, upstream_handler):
        outcome = Outcome()

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()
        try:
            upstream_url = (
                f"http://{upstream_server.host}:{upstream_server.port}"
            )

            async def proxy_handler(request):
                body = await request.read()
                return await proxy_request(
                    request, upstream_url, body, timeout=10, outcome=outcome
                )

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()
            try:
                async with TestClient(proxy_server) as client:
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={"model": "m", "prompt": "hi"},
                    )
                    return resp.status, outcome
            finally:
                await proxy_server.close()
        finally:
            await upstream_server.close()

    async def test_full_usage_populates_outcome_tokens(self):
        async def upstream(request):
            return aiohttp.web.json_response(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "prompt_tokens_details": {"cached_tokens": 2},
                    },
                }
            )

        status, outcome = await self._post_buffered(upstream)

        assert status == 200
        assert outcome.prompt_tokens == 7
        assert outcome.completion_tokens == 3
        assert outcome.cached_tokens == 2

    async def test_usage_without_details_leaves_cached_none(self):
        async def upstream(request):
            return aiohttp.web.json_response(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                }
            )

        _, outcome = await self._post_buffered(upstream)

        assert outcome.prompt_tokens == 7
        assert outcome.completion_tokens == 3
        assert outcome.cached_tokens is None

    async def test_missing_usage_leaves_tokens_none(self):
        async def upstream(request):
            return aiohttp.web.json_response({"choices": [], "id": "x"})

        _, outcome = await self._post_buffered(upstream)

        assert outcome.prompt_tokens is None
        assert outcome.completion_tokens is None
        assert outcome.cached_tokens is None

    async def test_unparseable_body_leaves_tokens_none(self):
        async def upstream(request):
            return aiohttp.web.Response(text="<html>oops</html>")

        _, outcome = await self._post_buffered(upstream)

        assert outcome.prompt_tokens is None
        assert outcome.completion_tokens is None

    async def test_usage_extracted_even_on_error_status(self):
        # Plan decision 6: no status gate — a body carrying usage counts.
        async def upstream(request):
            return aiohttp.web.json_response(
                {
                    "error": {"message": "partial"},
                    "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                },
                status=500,
            )

        status, outcome = await self._post_buffered(upstream)

        assert status == 500
        assert outcome.prompt_tokens == 4
        assert outcome.completion_tokens == 1

    async def test_non_dict_usage_ignored(self):
        async def upstream(request):
            return aiohttp.web.json_response(
                {"choices": [], "usage": "12 tokens"}
            )

        _, outcome = await self._post_buffered(upstream)

        assert outcome.prompt_tokens is None
        assert outcome.completion_tokens is None

    async def test_non_int_token_values_ignored(self):
        async def upstream(request):
            return aiohttp.web.json_response(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": "12",
                        "completion_tokens": True,
                        "prompt_tokens_details": {"cached_tokens": "3"},
                    },
                }
            )

        _, outcome = await self._post_buffered(upstream)

        assert outcome.prompt_tokens is None
        assert outcome.completion_tokens is None
        assert outcome.cached_tokens is None
