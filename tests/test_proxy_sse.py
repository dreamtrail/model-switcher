"""SSE forwarding, error handling, line parsing, and chunk buffering (AC3/AC4)."""

from __future__ import annotations

import aiohttp
from aiohttp.test_utils import TestServer, TestClient

from src.proxy import _process_sse_line, stream_proxy
from src.stats import Outcome


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

        # P-20260929-01: only generated output marks the first token.
        _process_sse_line(
            b'data:{"choices":[{"delta":{"content":"x"}}]}', outcome,
        )

        assert outcome.first_token_at is not None

    # P-20260927-01: prefill progress (return_progress) is not a first token.

    def test_prompt_progress_chat_chunk_does_not_mark_first_token(self):
        outcome = Outcome()
        observed = []
        outcome.first_token_observer = observed.append

        _process_sse_line(
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":null}],'
            b'"prompt_progress":{"total":10,"cache":0,"processed":5,'
            b'"time_ms":3}}',
            outcome,
        )

        assert outcome.first_token_at is None
        assert observed == []
        assert outcome.gen_chunks == 0

    def test_prompt_progress_completion_chunk_does_not_mark_first_token(self):
        outcome = Outcome()

        _process_sse_line(
            b'data: {"choices":[{"index":0,"text":"","finish_reason":null}],'
            b'"prompt_progress":{"total":10,"cache":0,"processed":10,'
            b'"time_ms":4}}',
            outcome,
        )

        assert outcome.first_token_at is None

    def test_content_after_prompt_progress_marks_first_token(self):
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[{"delta":{}}],"prompt_progress":{"total":1}}',
            outcome,
        )

        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"Hi"}}]}', outcome
        )

        assert outcome.first_token_at is not None
        assert outcome.gen_chunks == 1

    def test_prompt_progress_with_content_marks_first_token(self):
        outcome = Outcome()

        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"Hi"}}],'
            b'"prompt_progress":{"total":1}}',
            outcome,
        )

        assert outcome.first_token_at is not None
        assert outcome.gen_chunks == 1

    def test_prompt_progress_chunk_still_applies_usage(self):
        outcome = Outcome()

        _process_sse_line(
            b'data: {"choices":[{"delta":{}}],"prompt_progress":{"total":1},'
            b'"usage":{"prompt_tokens":7,"completion_tokens":0}}',
            outcome,
        )

        assert outcome.first_token_at is None
        assert outcome.prompt_tokens == 7
        assert outcome.completion_tokens == 0


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

    async def test_oversized_lineless_junk_discarded_usage_still_parsed(
        self, monkeypatch
    ):
        monkeypatch.setattr("src.proxy._SSE_LINE_BUFFER_CAP", 64)

        async def sse_handler(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            # Newline-free junk larger than the patched cap — not
            # line-shaped SSE, so the pending buffer is discarded.
            await resp.write(b"\x00" * 100)
            # Explicit newline closes the junk "line" whether or not the
            # transport coalesced it with the junk chunk; a valid usage
            # line after the discard still parses.
            await resp.write(
                b'\ndata: {"choices":[],"usage":{"prompt_tokens":3,'
                b'"completion_tokens":7}}\n\n'
            )
            await resp.write_eof()
            return resp

        outcome = await self._proxy_stream(sse_handler)

        assert outcome.prompt_tokens == 3
        assert outcome.completion_tokens == 7

    async def test_oversized_lineless_stream_completes_with_exact_bytes(
        self, monkeypatch
    ):
        monkeypatch.setattr("src.proxy._SSE_LINE_BUFFER_CAP", 64)
        sent = b"\x00" * 200

        async def binary_handler(request):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "application/octet-stream"},
            )
            await resp.prepare(request)
            await resp.write(sent)
            await resp.write_eof()
            return resp

        outcome = await self._proxy_stream(binary_handler)

        assert outcome.bytes_out == len(sent)
