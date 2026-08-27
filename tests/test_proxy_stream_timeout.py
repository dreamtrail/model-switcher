"""Regression tests for positive streaming timeout policy.

Expectations are derived from plan P-20260827-01: active streams have no total
wall-clock deadline, while connection/read inactivity and non-stream requests
remain bounded by a positive configured timeout.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.cli import parse_args
from src.proxy import proxy_request, stream_proxy


class _CapturingClient:
    """Delegate requests to a real session while recording request options."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.request_kwargs: list[dict] = []

    @property
    def timeout(self) -> aiohttp.ClientTimeout:
        return self._session.timeout

    def request(self, *args, **kwargs):
        self.request_kwargs.append(kwargs)
        return self._session.request(*args, **kwargs)


def _server_url(server: TestServer) -> str:
    return f"http://{server.host}:{server.port}"


async def test_active_stream_outlives_positive_timeout_without_error():
    """Frequent chunks keep a stream alive beyond its total timeout value."""
    configured_timeout = 0.4
    inter_chunk_delay = 0.08
    chunks = tuple(
        f'data: {{"chunk": {index}}}\n\n'.encode()
        for index in range(9)
    )
    write_times: list[float] = []

    async def active_sse(request):
        response = aiohttp.web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        for index, chunk in enumerate(chunks):
            await response.write(chunk)
            write_times.append(asyncio.get_running_loop().time())
            if index < len(chunks) - 1:
                await asyncio.sleep(inter_chunk_delay)
        await response.write_eof()
        return response

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", active_sse)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    shared_client = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=configured_timeout)
    )
    proxy_server = None
    try:
        async def proxy_handler(request):
            body = await request.read()
            return await stream_proxy(
                request,
                _server_url(upstream_server),
                body,
                timeout=configured_timeout,
                client=shared_client,
            )

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        async with TestClient(proxy_server) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "test", "stream": True},
            )
            body = await response.read()

        assert response.status == 200
        assert body == b"".join(chunks)
        assert b"Upstream error" not in body
        assert write_times[-1] - write_times[0] > configured_timeout
        assert max(
            later - earlier
            for earlier, later in zip(write_times, write_times[1:])
        ) < configured_timeout
    finally:
        if proxy_server is not None:
            await proxy_server.close()
        await shared_client.close()
        await upstream_server.close()


async def test_positive_stream_request_uses_exact_timeout_policy():
    """Positive streams disable total timeout but retain connection/read bounds."""
    configured_timeout = 1.25

    async def one_chunk_sse(request):
        return aiohttp.web.Response(
            body=b'data: {"done": true}\n\n',
            headers={"Content-Type": "text/event-stream"},
        )

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", one_chunk_sse)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=configured_timeout)
    )
    capturing_client = _CapturingClient(session)
    proxy_server = None
    try:
        async def proxy_handler(request):
            body = await request.read()
            return await stream_proxy(
                request,
                _server_url(upstream_server),
                body,
                timeout=configured_timeout,
                client=capturing_client,
            )

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        async with TestClient(proxy_server) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "test", "stream": True},
            )
            await response.read()

        request_timeout = capturing_client.request_kwargs[0]["timeout"]
        assert isinstance(request_timeout, aiohttp.ClientTimeout)
        assert request_timeout.total is None
        assert request_timeout.connect == configured_timeout
        assert request_timeout.sock_read == configured_timeout
        assert request_timeout.sock_connect is None
    finally:
        if proxy_server is not None:
            await proxy_server.close()
        await session.close()
        await upstream_server.close()


@pytest.mark.parametrize("configured_timeout", [0, -1])
async def test_non_positive_stream_timeout_keeps_shared_session_policy(
    configured_timeout,
):
    """The positive-only override does not redefine non-positive semantics."""
    session_timeout = aiohttp.ClientTimeout(total=configured_timeout)

    async def one_chunk_sse(request):
        return aiohttp.web.Response(
            body=b"data: done\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", one_chunk_sse)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    session = aiohttp.ClientSession(timeout=session_timeout)
    capturing_client = _CapturingClient(session)
    proxy_server = None
    try:
        async def proxy_handler(request):
            return await stream_proxy(
                request,
                _server_url(upstream_server),
                await request.read(),
                timeout=configured_timeout,
                client=capturing_client,
            )

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        async with TestClient(proxy_server) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "test", "stream": True},
            )
            await response.read()

        assert capturing_client.request_kwargs[0]["timeout"] is session_timeout
    finally:
        if proxy_server is not None:
            await proxy_server.close()
        await session.close()
        await upstream_server.close()


async def test_stalled_stream_uses_post_prepare_sse_error_path():
    """Read inactivity ends an already-prepared response with an SSE error."""
    configured_timeout = 0.25
    stalled = asyncio.Event()
    release_upstream = asyncio.Event()
    first_chunk = b'data: {"chunk": 1}\n\n'

    async def stalled_sse(request):
        response = aiohttp.web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(first_chunk)
        stalled.set()
        await release_upstream.wait()
        try:
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", stalled_sse)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    proxy_server = None
    try:
        async def proxy_handler(request):
            return await stream_proxy(
                request,
                _server_url(upstream_server),
                await request.read(),
                timeout=configured_timeout,
            )

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        async with TestClient(proxy_server) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "test", "stream": True},
            )
            async with asyncio.timeout(2):
                body = await response.read()

        assert stalled.is_set()
        assert response.status == 200
        assert body.startswith(first_chunk)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in body.decode().splitlines()
            if line.startswith("data: ")
        ]
        assert events[0] == {"chunk": 1}
        assert events[1]["error"]["message"].startswith("Upstream error:")
        assert events[1]["error"]["code"] == "internal_error"
        assert len(events) == 2
    finally:
        release_upstream.set()
        if proxy_server is not None:
            await proxy_server.close()
        await upstream_server.close()


async def test_shared_session_non_stream_request_retains_total_timeout():
    """A production-style shared session still bounds non-stream requests."""
    configured_timeout = 0.2
    request_started = asyncio.Event()
    release_upstream = asyncio.Event()

    async def delayed_response(request):
        request_started.set()
        await release_upstream.wait()
        return aiohttp.web.json_response({"late": True})

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", delayed_response)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    shared_client = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=configured_timeout)
    )
    proxy_server = None
    try:
        async def proxy_handler(request):
            return await proxy_request(
                request,
                _server_url(upstream_server),
                await request.read(),
                timeout=configured_timeout,
                client=shared_client,
            )

        proxy_app = aiohttp.web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        async with TestClient(proxy_server) as client:
            async with asyncio.timeout(2):
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "test", "stream": False},
                )
                payload = await response.json()

        assert request_started.is_set()
        assert response.status == 502
        assert payload["error"]["message"].startswith("Upstream error:")
        assert payload["error"]["code"] == "internal_error"
    finally:
        release_upstream.set()
        if proxy_server is not None:
            await proxy_server.close()
        await shared_client.close()
        await upstream_server.close()


def test_cli_help_explains_positive_stream_and_non_stream_timeout_policy(capsys):
    """Help qualifies the split timeout policy as applying to positive values."""
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert exc_info.value.code == 0
    assert "For positive values:" in help_text
    assert "total swap/non-stream timeout" in help_text
    assert "stream connection/read-inactivity timeout" in help_text
    assert "streams have no total timeout" in help_text
