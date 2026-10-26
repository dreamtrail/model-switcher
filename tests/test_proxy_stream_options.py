"""Stream-options injection guards and end-to-end body integrity (AC1)."""

from __future__ import annotations

import json

import aiohttp
from aiohttp.test_utils import TestServer, TestClient
import pytest

from src.proxy import inject_stream_options, proxy_request, stream_proxy
from src.stats import Outcome


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
    integration tests in tests/test_stats_token_integration.py.
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
