"""Tests for src/proxy.py — HTTP proxy handler.

Derived from plan acceptance criteria:
- Proxy: model extraction from JSON body, catch-all proxy, SSE streaming
- /v1/models returns static config list
- Header forwarding (all except hop-by-hop)
- Error response format (OpenAI-compatible JSON)
- Upstream crash returns 502

P-20260913-02 additions (plan §Testing strategy, file placement rule):
- stream_options injection guard matrix + end-to-end + Content-Length
  integrity (AC1)
- Buffered usage extraction into Outcome (AC2)
- SSE line parsing mechanics (AC3/AC4): first content event, usage chunk
  handling, split lines, final line without newline
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
    _process_sse_line,
    extract_model,
    handle_models_list,
    inject_stream_options,
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


# ---------------------------------------------------------------------------
# P-20260913-02: stream_options injection (AC1)
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal request stand-in — inject_stream_options only reads
    method and path."""

    def __init__(self, method="POST", path="/v1/chat/completions"):
        self.method = method
        self.path = path


class TestInjectStreamOptions:
    """Guard matrix for inject_stream_options (plan decision 2, AC1)."""

    def test_injects_when_all_guards_pass(self):
        data = {"model": "m", "stream": True}
        body = json.dumps(data).encode()

        out = inject_stream_options(_FakeRequest(), body, data)

        assert json.loads(out)["stream_options"] == {"include_usage": True}

    def test_injects_on_legacy_v1_completions_endpoint(self):
        # Plan decision 2: /v1/completions is intentionally included.
        data = {"model": "m", "prompt": "hi", "stream": True}
        body = json.dumps(data).encode()

        out = inject_stream_options(
            _FakeRequest(path="/v1/completions"), body, data
        )

        assert json.loads(out)["stream_options"] == {"include_usage": True}

    def test_injection_preserves_all_other_keys(self):
        data = {
            "model": "m",
            "stream": True,
            "temperature": 0.7,
            "messages": [{"role": "user", "content": "hi"}],
        }
        body = json.dumps(data).encode()

        out = json.loads(inject_stream_options(_FakeRequest(), body, data))

        assert out["model"] == "m"
        assert out["temperature"] == 0.7
        assert out["messages"] == data["messages"]

    @pytest.mark.parametrize(
        "client_value",
        [
            {"include_usage": False},
            {"include_usage": True},
            {"custom": [1, 2, 3]},
            "not-a-dict",
            None,
            0,
        ],
    )
    def test_client_stream_options_preserved_verbatim(self, client_value):
        # Any client-set stream_options value is forwarded byte-identical.
        data = {"model": "m", "stream": True, "stream_options": client_value}
        body = json.dumps(data).encode()

        assert inject_stream_options(_FakeRequest(), body, data) == body

    def test_non_stream_body_untouched(self):
        data = {"model": "m", "messages": []}
        body = json.dumps(data).encode()

        assert inject_stream_options(_FakeRequest(), body, data) == body

    def test_stream_false_body_untouched(self):
        data = {"model": "m", "stream": False}
        body = json.dumps(data).encode()

        assert inject_stream_options(_FakeRequest(), body, data) == body

    def test_non_json_body_untouched(self):
        body = b"not json at all"

        assert inject_stream_options(_FakeRequest(), body, None) == body

    def test_empty_or_missing_body_untouched(self):
        assert inject_stream_options(_FakeRequest(), b"", None) == b""
        assert inject_stream_options(_FakeRequest(), None, None) is None

    def test_non_endpoint_path_untouched(self):
        data = {"model": "m", "stream": True}
        body = json.dumps(data).encode()

        req = _FakeRequest(path="/v1/embeddings")

        assert inject_stream_options(req, body, data) == body

    def test_get_request_untouched(self):
        data = {"model": "m", "stream": True}
        body = json.dumps(data).encode()

        req = _FakeRequest(method="GET")

        assert inject_stream_options(req, body, data) == body

    def test_unserializable_data_returns_original_bytes(self):
        # Parsed dict containing a non-JSON-serializable value: the
        # re-serialization must fail and the ORIGINAL body be returned.
        data = {"model": "m", "stream": True, "bad": {1, 2}}
        body = json.dumps({"model": "m", "stream": True}).encode()

        assert inject_stream_options(_FakeRequest(), body, data) == body


class TestInjectionEndToEnd:
    """AC1 end-to-end: the injected body reaches the upstream with a
    consistent Content-Length.

    The proxy handler mirrors the streaming branch of
    src/swapper.py::_proxy_for_model (parse once -> inject -> stream_proxy);
    the real swapper wiring is covered end-to-end by the streaming
    integration tests in tests/test_stats.py.
    """

    async def _proxy_handler(self, request, upstream_url, outcome):
        body = await request.read()
        data = None
        if body:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = None
        if data and data.get("stream"):
            body = inject_stream_options(request, body, data)
            return await stream_proxy(
                request, upstream_url, body, timeout=10, outcome=outcome
            )
        return await proxy_request(
            request, upstream_url, body, timeout=10, outcome=outcome
        )

    async def _post(self, payload: bytes, upstream_handler, content_type):
        """Run one POST through an injecting proxy; return (status, text,
        seen, outcome) where seen captures the upstream request."""
        seen = {}
        outcome = Outcome()

        async def capturing_handler(request):
            seen["body"] = await request.read()
            seen["content_length"] = request.headers.get("Content-Length")
            return await upstream_handler(request)

        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", capturing_handler)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()
        try:
            upstream_url = (
                f"http://{upstream_server.host}:{upstream_server.port}"
            )

            async def proxy_handler(request):
                return await self._proxy_handler(
                    request, upstream_url, outcome
                )

            proxy_app = aiohttp.web.Application()
            proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
            proxy_server = TestServer(proxy_app)
            await proxy_server.start_server()
            try:
                async with TestClient(proxy_server) as client:
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=payload,
                        headers={"Content-Type": content_type},
                    )
                    text = await resp.text()
                    return resp.status, text, seen, outcome
            finally:
                await proxy_server.close()
        finally:
            await upstream_server.close()

    async def test_injected_body_reaches_upstream_with_consistent_length(self):
        # AC1: eligible request gains stream_options; Content-Length of the
        # forwarded request matches the mutated body exactly.
        async def sse_upstream(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        payload = json.dumps({"model": "m", "stream": True}).encode()

        status, text, seen, _ = await self._post(
            payload, sse_upstream, "application/json"
        )

        assert status == 200
        received = json.loads(seen["body"])
        assert received["stream"] is True
        assert received["stream_options"] == {"include_usage": True}
        assert int(seen["content_length"]) == len(seen["body"])

    async def test_usage_chunk_flows_through_to_client(self):
        # The injected include_usage makes the final usage chunk part of the
        # client-visible stream (plan §Risks: accepted, spec'd behavior).
        async def sse_upstream(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":5,'
                b'"completion_tokens":2}}\n\n'
            )
            await resp.write_eof()
            return resp

        payload = json.dumps({"model": "m", "stream": True}).encode()

        _, text, _, _ = await self._post(
            payload, sse_upstream, "application/json"
        )

        assert '"usage"' in text

    async def test_client_stream_options_forwarded_verbatim_end_to_end(self):
        async def sse_upstream(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write_eof()
            return resp

        client_opts = {"include_usage": False, "custom": 7}
        payload = json.dumps(
            {"model": "m", "stream": True, "stream_options": client_opts}
        ).encode()

        _, _, seen, _ = await self._post(
            payload, sse_upstream, "application/json"
        )

        received = json.loads(seen["body"])
        assert received["stream_options"] == client_opts

    async def test_non_stream_body_forwarded_byte_identical(self):
        async def echo_upstream(request):
            return aiohttp.web.json_response({"ok": True})

        payload = json.dumps({"model": "m", "prompt": "hi"}).encode()

        status, _, seen, _ = await self._post(
            payload, echo_upstream, "application/json"
        )

        assert status == 200
        assert seen["body"] == payload  # byte-identical
        assert "stream_options" not in json.loads(seen["body"])
        assert int(seen["content_length"]) == len(payload)

    async def test_non_json_body_forwarded_byte_identical(self):
        async def echo_upstream(request):
            return aiohttp.web.Response(text="ok")

        payload = b"plain text body"

        status, _, seen, _ = await self._post(
            payload, echo_upstream, "text/plain"
        )

        assert status == 200
        assert seen["body"] == payload
        assert int(seen["content_length"]) == len(payload)


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


# ---------------------------------------------------------------------------
# P-20260913-02: SSE line parsing mechanics (AC3, AC4)
# ---------------------------------------------------------------------------


class TestProcessSseLine:
    """Deterministic unit coverage of the per-line SSE parser."""

    def test_first_content_event_sets_first_token_at(self):
        outcome = Outcome()

        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"Hi"}}]}', outcome
        )

        assert outcome.first_token_at is not None

    def test_first_token_set_only_once(self):
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"a"}}]}', outcome
        )
        first = outcome.first_token_at

        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"b"}}]}', outcome
        )

        assert outcome.first_token_at == first

    def test_usage_only_chunk_never_marks_first_token(self):
        # AC3: the choices:[] usage chunk is not a content event.
        outcome = Outcome()

        _process_sse_line(
            b'data: {"choices":[],"usage":{"prompt_tokens":3,'
            b'"completion_tokens":1}}',
            outcome,
        )

        assert outcome.first_token_at is None
        assert outcome.prompt_tokens == 3
        assert outcome.completion_tokens == 1

    def test_usage_chunk_after_content_does_not_move_first_token(self):
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"a"}}]}', outcome
        )
        first = outcome.first_token_at

        _process_sse_line(
            b'data: {"choices":[],"usage":{"prompt_tokens":3,'
            b'"completion_tokens":1}}',
            outcome,
        )

        assert outcome.first_token_at == first

    def test_later_usage_chunk_wins(self):
        # OpenAI semantics: the final usage chunk is authoritative.
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[],"usage":{"prompt_tokens":3,'
            b'"completion_tokens":1}}',
            outcome,
        )

        _process_sse_line(
            b'data: {"choices":[],"usage":{"prompt_tokens":10,'
            b'"completion_tokens":4,'
            b'"prompt_tokens_details":{"cached_tokens":3}}}',
            outcome,
        )

        assert outcome.prompt_tokens == 10
        assert outcome.completion_tokens == 4
        assert outcome.cached_tokens == 3

    def test_done_sentinel_comments_and_garbage_ignored(self):
        outcome = Outcome()

        _process_sse_line(b"data: [DONE]", outcome)
        _process_sse_line(b"data: not json", outcome)
        _process_sse_line(b": comment", outcome)
        _process_sse_line(b"event: ping", outcome)
        _process_sse_line(b"", outcome)

        assert outcome.first_token_at is None
        assert outcome.prompt_tokens is None

    def test_non_dict_event_ignored(self):
        outcome = Outcome()

        _process_sse_line(b"data: [1, 2]", outcome)
        _process_sse_line(b"data: null", outcome)

        assert outcome.first_token_at is None
        assert outcome.prompt_tokens is None

    def test_missing_space_after_prefix_tolerated(self):
        outcome = Outcome()

        _process_sse_line(b'data:{"choices":[{"delta":{}}]}', outcome)

        assert outcome.first_token_at is not None


class TestStreamProxySseParsing:
    """Line-buffer handling of chunks that split SSE lines (AC3)."""

    async def _proxy_stream(self, sse_handler):
        outcome = Outcome()
        upstream_app = aiohttp.web.Application()
        upstream_app.router.add_post("/v1/chat/completions", sse_handler)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()
        try:
            upstream_url = (
                f"http://{upstream_server.host}:{upstream_server.port}"
            )

            async def proxy_handler(request):
                body = await request.read()
                return await stream_proxy(
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
                        json={"model": "m", "stream": True},
                    )
                    assert resp.status == 200
                    await resp.text()
                    return outcome
            finally:
                await proxy_server.close()
        finally:
            await upstream_server.close()

    async def test_split_lines_across_chunks_still_parsed(self):
        async def sse_handler(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            # Content event split mid-line across two chunks...
            await resp.write(b'data: {"choices":[{"del')
            await resp.write(b'ta":{"content":"Hi"}}]}\n\n')
            # ...and the usage event split right before the newline.
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":5,'
                b'"completion_tokens":2}}'
            )
            await resp.write(b"\n\n")
            await resp.write_eof()
            return resp

        outcome = await self._proxy_stream(sse_handler)

        assert outcome.first_token_at is not None
        assert outcome.prompt_tokens == 5
        assert outcome.completion_tokens == 2

    async def test_final_line_without_trailing_newline_still_parsed(self):
        async def sse_handler(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            )
            # Final usage event without trailing newline (defensive path).
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":1}}'
            )
            await resp.write_eof()
            return resp

        outcome = await self._proxy_stream(sse_handler)

        assert outcome.prompt_tokens == 1
        assert outcome.completion_tokens == 1
