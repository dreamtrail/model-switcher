"""Non-UTF-8 upstream bodies are forwarded, not crashed on (P-20260929-02).

``json.loads(bytes)`` raises ``UnicodeDecodeError`` (not ``JSONDecodeError``)
for bytes that are not valid UTF-8. The usage parser must treat that like
any other unparseable body: forward it unchanged and leave token fields
None. The end-to-end tests send real requests through a Swapper (stats on)
to a local aiohttp upstream.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from src.proxy import _extract_usage, _process_sse_line
from src.stats import Outcome

from tests.stats_helpers import _make_app, _stack

# PNG-like bytes: no NUL in the first two, so json decodes as UTF-8 and fails.
_BINARY_BODY = b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe binary \x80\x81"
# A Latin-1 encoded event ("caf\xe9" is not valid UTF-8).
_LATIN1_LINE = b'data: {"choices":[{"delta":{"content":"caf\xe9"}}]}'
_USAGE_LINE = (
    b'data: {"choices":[],"usage":{"prompt_tokens":5,'
    b'"completion_tokens":2}}'
)


class TestUnitParsers:

    @pytest.mark.parametrize("body", [
        _BINARY_BODY,
        b'{"usage":{"prompt_tokens":3,"note":"caf\xe9"}}',
    ])
    def test_extract_usage_ignores_non_utf8_body(self, body):
        outcome = Outcome()
        _extract_usage(body, outcome)
        assert outcome.prompt_tokens is None
        assert outcome.completion_tokens is None
        assert outcome.cached_tokens is None

    def test_sse_line_ignores_non_utf8_payload(self):
        outcome = Outcome()
        _process_sse_line(_LATIN1_LINE, outcome)
        assert outcome.first_token_at is None
        assert outcome.gen_chunks == 0
        assert outcome.prompt_tokens is None

    def test_later_valid_line_still_parsed(self):
        outcome = Outcome()
        _process_sse_line(_LATIN1_LINE, outcome)
        _process_sse_line(_USAGE_LINE, outcome)
        assert outcome.prompt_tokens == 5
        assert outcome.completion_tokens == 2


class TestEndToEnd:

    async def test_binary_buffered_body_is_forwarded(self, monkeypatch):
        async def chat(request):
            return web.Response(
                body=_BINARY_BODY, content_type="application/octet-stream",
            )

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions", json={"model": "model-a"},
                )
                assert resp.status == 200
                assert await resp.read() == _BINARY_BODY
            stats = stack.swapper.stats._models["model-a"]
            assert stats.success == 1
            assert stats.prompt_tokens == 0

    async def test_non_utf8_sse_line_is_forwarded(self, monkeypatch):
        sent = _LATIN1_LINE + b"\n\n" + _USAGE_LINE + b"\n\n"

        async def chat(request):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(_LATIN1_LINE + b"\n\n")
            await resp.write(_USAGE_LINE + b"\n\n")
            await resp.write_eof()
            return resp

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                assert await resp.read() == sent
            stats = stack.swapper.stats._models["model-a"]
            assert stats.success == 1
            assert stats.prompt_tokens == 5
            assert stats.completion_tokens == 2
