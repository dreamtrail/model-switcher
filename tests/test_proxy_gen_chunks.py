"""Streamed output-chunk counting for the live generation estimate.

P-20260926-01 AC1: the streaming proxy reports one unit per choice whose
delta carries content, reasoning, or tool calls (or completions ``text``);
role-only, empty, usage-only, and non-OpenAI events report nothing, and
non-streaming requests never call the observer.
"""

from __future__ import annotations

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.proxy import _process_sse_line, proxy_request, stream_proxy
from src.stats import Outcome


def _recording_outcome() -> tuple[Outcome, list[int]]:
    calls: list[int] = []
    return Outcome(gen_chunk_observer=calls.append), calls


class TestProcessSseLineChunkCount:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (b'data: {"choices":[{"delta":{"content":"Hi"}}]}', [1]),
            (b'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}', [1]),
            (b'data: {"choices":[{"delta":{"reasoning":"th"}}]}', [1]),
            (
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"arguments":"{"}}]}}]}',
                [1],
            ),
            # /v1/completions streams carry text on the choice itself.
            (b'data: {"choices":[{"index":0,"text":"lo"}]}', [1]),
            # One unit per choice with output (n > 1).
            (
                b'data: {"choices":[{"index":0,"delta":{"content":"a"}},'
                b'{"index":1,"delta":{"content":"b"}},'
                b'{"index":2,"delta":{"content":""}}]}',
                [2],
            ),
            # Content plus reasoning in one delta is still one unit.
            (
                b'data: {"choices":[{"delta":{"content":"a",'
                b'"reasoning_content":"b"}}]}',
                [1],
            ),
        ],
    )
    def test_output_events_report_units(self, line, expected):
        outcome, calls = _recording_outcome()
        _process_sse_line(line, outcome)
        assert calls == expected

    @pytest.mark.parametrize(
        "line",
        [
            # NInfer-style role-only first delta.
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}',
            b'data: {"choices":[{"delta":{}}]}',
            b'data: {"choices":[{"delta":{"content":null}}]}',
            b'data: {"choices":[{"delta":{"tool_calls":[]}}]}',
            b'data: {"choices":[{"index":0,"text":""}]}',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            b'data: {"choices":[],"usage":{"prompt_tokens":1,'
            b'"completion_tokens":2}}',
            b'data: {"choices":["x", null]}',
            b'data: {"choices":{"delta":{"content":"x"}}}',
            # Named non-OpenAI events (Anthropic / Responses API).
            b"event: content_block_delta",
            b'data: {"type":"content_block_delta","delta":'
            b'{"type":"text_delta","text":"Hi"}}',
            b'data: {"type":"response.output_text.delta","delta":"Hi"}',
            b"data: [DONE]",
            b"data: not json",
            b": comment",
            b"",
        ],
    )
    def test_non_output_events_report_nothing(self, line):
        outcome, calls = _recording_outcome()
        _process_sse_line(line, outcome)
        assert calls == []

    def test_observer_failure_does_not_break_parsing(self):
        def boom(units):
            raise RuntimeError("observer failed")

        outcome = Outcome(gen_chunk_observer=boom)
        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"x"}}],'
            b'"usage":{"prompt_tokens":3,"completion_tokens":1}}',
            outcome,
        )
        # First-token timing and usage extraction still happen.
        assert outcome.first_token_at is not None
        assert outcome.completion_tokens == 1
        # P-20260926-04 AC7: the calibration count still advances.
        assert outcome.gen_chunks == 1

    def test_gen_chunks_totals_units_with_or_without_observer(self):
        # P-20260926-04: per-stream total used for tokens-per-chunk.
        lines = [
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            b'data: {"choices":[{"delta":{"content":"a"}}]}',
            b'data: {"choices":[{"index":0,"delta":{"content":"b"}},'
            b'{"index":1,"delta":{"content":"c"}}]}',
            b'data: {"choices":[],"usage":{"completion_tokens":4}}',
            b"data: [DONE]",
        ]
        for outcome in (Outcome(), _recording_outcome()[0]):
            for line in lines:
                _process_sse_line(line, outcome)
            assert outcome.gen_chunks == 3
            assert outcome.completion_tokens == 4

    def test_without_observer_parsing_is_unchanged(self):
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"x"}}]}', outcome,
        )
        assert outcome.first_token_at is not None


async def _through_proxy(handler, proxy_fn, outcome, payload):
    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    try:
        upstream_url = f"http://{upstream_server.host}:{upstream_server.port}"

        async def proxy_handler(request):
            body = await request.read()
            return await proxy_fn(
                request, upstream_url, body, timeout=10, outcome=outcome,
            )

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()
        try:
            async with TestClient(proxy_server) as client:
                resp = await client.post("/v1/chat/completions", json=payload)
                return resp.status, await resp.text()
        finally:
            await proxy_server.close()
    finally:
        await upstream_server.close()


class TestProxyPathsChunkCount:
    async def test_stream_proxy_reports_units_including_split_lines(self):
        async def sse_handler(request):
            resp = aiohttp.web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[{"delta":{"reasoning_content":"hm"}}]}\n\n'
            )
            # Output event split mid-line across chunks.
            await resp.write(b'data: {"choices":[{"del')
            await resp.write(b'ta":{"content":"Hi"}}]}\n\n')
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":4,'
                b'"completion_tokens":3}}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        outcome, calls = _recording_outcome()
        status, text = await _through_proxy(
            sse_handler, stream_proxy, outcome, {"model": "m", "stream": True},
        )

        assert status == 200
        assert '"content":"Hi"' in text
        assert calls == [1, 1]
        assert outcome.gen_chunks == 2
        assert outcome.completion_tokens == 3

    async def test_buffered_proxy_never_calls_observer(self):
        async def json_handler(request):
            return aiohttp.web.json_response({
                "choices": [{"message": {"content": "Hi"}, "text": "Hi"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            })

        outcome, calls = _recording_outcome()
        status, _ = await _through_proxy(
            json_handler, proxy_request, outcome, {"model": "m"},
        )

        assert status == 200
        assert calls == []
        assert outcome.gen_chunks == 0
        assert outcome.completion_tokens == 1
