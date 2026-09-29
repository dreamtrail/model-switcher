"""Client disconnect during streaming (plan P-20260929-06).

Expectations derived from the plan's acceptance criteria:
- AC1: client closes mid-stream -> no write after the failed write (no SSE
  error frame, no write_eof), no replacement 502, upstream connection
  closed, ``client_disconnect`` recorded (not ``stream_error``).
- AC3: pre-prepare failures still return 502 JSON with the "(pre-prepare)"
  label; that label never appears for post-prepare failures.
- AC4: no exception from the client-write path escapes ``stream_proxy``.

All disconnects are real: a local aiohttp client closes its TCP connection
to a local proxy server. ``StreamResponse.write``/``write_eof`` are only
wrapped by a recording spy that always delegates to the real method.

Servers under test run via ``web.AppRunner`` like ``src/cli.py`` (handler
cancellation off). ``aiohttp.test_utils.TestServer`` forces
``handler_cancellation=True``, which cancels the handler on disconnect and
never reaches the "write after close" path.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.proxy import stream_proxy
from src.stats import Outcome

from tests.helpers import _fake_handle
from tests.stats_helpers import _make_app, _stack

_WAIT_S = 2.0
_CHUNK1 = b'data: {"choices": [{"delta": {"content": "a"}}]}\n\n'
_CHUNK = b'data: {"choices": [{"delta": {"content": "b"}}]}\n\n'


@asynccontextmanager
async def _serve(app: web.Application):
    """Run ``app`` on 127.0.0.1 like src/cli.py; yield its base URL."""
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0][:2]
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


async def _wait_until(predicate, timeout: float = _WAIT_S) -> None:
    """Poll ``predicate`` every 10 ms; fail if it stays false."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class _UpstreamProbe:
    """Fake SSE upstream that writes until its connection is closed.

    ``state`` ends as "closed" only when a write raised ConnectionError
    (aiohttp's ClientConnectionResetError is one). A timeout ends as
    "timeout" — TimeoutError is an OSError subclass on 3.11+, so it must
    not be lumped in with the close signal.
    """

    def __init__(self, first: bytes, start: asyncio.Event | None = None):
        self.first = first
        self.start = start
        self.state = "pending"
        self.done = asyncio.Event()

    async def handler(self, request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        await resp.write(self.first)
        if self.start is not None:
            await self.start.wait()
        self.state = "running"
        try:
            async with asyncio.timeout(_WAIT_S):
                while True:
                    await resp.write(_CHUNK)
                    await asyncio.sleep(0.01)
        except ConnectionError:
            self.state = "closed"
        except TimeoutError:
            self.state = "timeout"
        finally:
            self.done.set()
        return resp

    async def assert_closed(self) -> None:
        # done is always set (close, timeout, or cancel); the extra second
        # only bounds the wait, it never turns a timeout into a pass.
        await asyncio.wait_for(self.done.wait(), _WAIT_S + 1)
        assert self.state == "closed", (
            f"upstream connection was not closed (state={self.state})"
        )


def _transport_gone(request: web.Request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


class _WriteSpy:
    """Record StreamResponse.write/write_eof calls; delegate to the real ones."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[tuple[int, str, BaseException | None]] = []
        real_write = web.StreamResponse.write
        real_write_eof = web.StreamResponse.write_eof
        spy = self

        async def write(resp, data):
            try:
                result = await real_write(resp, data)
            except BaseException as exc:
                spy.calls.append((id(resp), "write", exc))
                raise
            spy.calls.append((id(resp), "write", None))
            return result

        async def write_eof(resp, data=b""):
            try:
                result = await real_write_eof(resp, data)
            except BaseException as exc:
                spy.calls.append((id(resp), "write_eof", exc))
                raise
            spy.calls.append((id(resp), "write_eof", None))
            return result

        monkeypatch.setattr(web.StreamResponse, "write", write)
        monkeypatch.setattr(web.StreamResponse, "write_eof", write_eof)

    def for_response(self, resp, upto: int):
        return [
            (name, exc) for rid, name, exc in self.calls[:upto]
            if rid == id(resp)
        ]


class _ProxyHarness:
    """Proxy server calling stream_proxy directly and capturing its result."""

    def __init__(self, upstream_url: str, outcome: Outcome | None) -> None:
        self.upstream_url = upstream_url
        self.outcome = outcome
        self.request: web.Request | None = None
        self.result = None
        self.escaped: BaseException | None = None
        self.calls_at_return = 0
        self.done = asyncio.Event()
        self.spy: _WriteSpy | None = None

    async def handler(self, request):
        self.request = request
        body = await request.read()
        try:
            self.result = await stream_proxy(
                request, self.upstream_url, body,
                timeout=10, outcome=self.outcome,
            )
        except BaseException as exc:
            self.escaped = exc
            raise
        finally:
            if self.spy is not None:
                self.calls_at_return = len(self.spy.calls)
            self.done.set()
        return self.result


def _assert_prepared_stream(result) -> None:
    # web.Response (json_response) subclasses StreamResponse; require the
    # exact prepared stream, not a replacement response.
    assert type(result) is web.StreamResponse
    assert result.prepared
    assert result.status == 200


async def test_client_close_mid_stream_stops_cleanly(monkeypatch, caplog):
    """AC1/AC4: the failed write is the last one; upstream gets closed."""
    caplog.set_level(logging.DEBUG, logger="src.proxy")
    send_next = asyncio.Event()
    # Keeps sending until the proxy's side of the connection is gone.
    upstream = _UpstreamProbe(_CHUNK1, start=send_next)

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream.handler)
    outcome = Outcome()

    async with (
        _serve(upstream_app) as upstream_url,
        aiohttp.ClientSession() as session,
    ):
        harness = _ProxyHarness(upstream_url, outcome)
        proxy_app = web.Application()
        proxy_app.router.add_post("/v1/chat/completions", harness.handler)
        async with _serve(proxy_app) as proxy_url:
            harness.spy = _WriteSpy(monkeypatch)
            resp = await session.post(
                proxy_url + "/v1/chat/completions",
                json={"model": "m", "stream": True},
            )
            assert resp.status == 200
            first = await resp.content.readuntil(b"\n\n")
            assert first == _CHUNK1
            resp.close()  # drop the TCP connection mid-stream

            # Proxy notices the closed client transport before more data
            # arrives, so its next write hits "Cannot write to closing
            # transport".
            await _wait_until(lambda: _transport_gone(harness.request))
            send_next.set()

            await asyncio.wait_for(harness.done.wait(), _WAIT_S)
            await upstream.assert_closed()

    assert harness.escaped is None
    _assert_prepared_stream(harness.result)

    calls = harness.spy.for_response(harness.result, harness.calls_at_return)
    # One successful chunk, then exactly one failed write; nothing after.
    assert [name for name, _ in calls] == ["write", "write"]
    assert calls[0][1] is None
    assert isinstance(calls[1][1], OSError)

    assert outcome.client_disconnect is True
    assert outcome.stream_error is False
    assert outcome.bytes_out == len(_CHUNK1)

    messages = [r.getMessage() for r in caplog.records]
    assert (
        f"Client disconnected during stream ({len(_CHUNK1)} bytes sent)"
        in messages
    )
    assert not any("pre-prepare" in m for m in messages)
    assert not any(m.startswith("Stream proxy error") for m in messages)


async def test_failed_error_frame_write_is_suppressed(monkeypatch, caplog):
    """Upstream fails after the client left: the error frame write fails and
    is treated as a client disconnect; nothing escapes, no write_eof."""
    caplog.set_level(logging.DEBUG, logger="src.proxy")
    crash_now = asyncio.Event()

    async def upstream_crash(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        await resp.write(_CHUNK1)
        await crash_now.wait()
        # Crash: drop the connection mid-body (no final chunk), so the
        # proxy's upstream read fails with a payload error.
        request.transport.abort()
        return resp

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_crash)
    outcome = Outcome()

    async with (
        _serve(upstream_app) as upstream_url,
        aiohttp.ClientSession() as session,
    ):
        harness = _ProxyHarness(upstream_url, outcome)
        proxy_app = web.Application()
        proxy_app.router.add_post("/v1/chat/completions", harness.handler)
        async with _serve(proxy_app) as proxy_url:
            harness.spy = _WriteSpy(monkeypatch)
            resp = await session.post(
                proxy_url + "/v1/chat/completions",
                json={"model": "m", "stream": True},
            )
            assert resp.status == 200
            assert await resp.content.readuntil(b"\n\n") == _CHUNK1
            resp.close()

            await _wait_until(lambda: _transport_gone(harness.request))
            crash_now.set()
            await asyncio.wait_for(harness.done.wait(), _WAIT_S)

    assert harness.escaped is None
    _assert_prepared_stream(harness.result)

    calls = harness.spy.for_response(harness.result, harness.calls_at_return)
    # Chunk 1 ok, then the SSE error frame write fails; no write_eof.
    assert [name for name, _ in calls] == ["write", "write"]
    assert calls[0][1] is None
    assert isinstance(calls[1][1], OSError)

    assert outcome.stream_error is True
    assert outcome.client_disconnect is True

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Stream proxy error: ") for m in messages)
    assert any(
        m.startswith("Client disconnected during stream") for m in messages
    )
    assert not any("pre-prepare" in m for m in messages)


async def test_client_disconnect_recorded_in_stats(monkeypatch):
    """Swapper records the disconnect as its own error kind and closes the
    upstream connection while the shared client session stays open.

    Like src/cli.py, the swapper gets one shared ClientSession, so
    stream_proxy does not close a session of its own; leaving its upstream
    ``async with`` is the only thing that closes the upstream connection.
    """
    upstream = _UpstreamProbe(_CHUNK)

    async with (
        # Placeholder TestServer from _stack; the seeded handle is then
        # pointed at a production-like upstream (see module doc) so the
        # close shows up as a failed write, not a cancelled handler.
        _stack(monkeypatch, {"model-a": _make_app()}, seed="model-a") as stack,
        _serve(_make_app(upstream.handler)) as upstream_url,
    ):
        upstream_port = int(upstream_url.rsplit(":", 1)[1])
        stack.swapper.config.models["model-a"].port = upstream_port
        stack.swapper._current = _fake_handle("model-a", port=upstream_port)
        # Shared session as wired in src/cli.py; _stack builds the Swapper
        # without one. swapper.shutdown() (on _stack exit) closes it.
        assert stack.swapper._client is None
        stack.swapper._client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=stack.swapper.timeout)
        )
        # Production-like front server for the swapper (see module doc).
        proxy_app = web.Application()

        async def handler(request):
            return await stack.swapper.handle_request(request)

        proxy_app.router.add_route("*", "/{path:.*}", handler)
        async with (
            _serve(proxy_app) as proxy_url,
            aiohttp.ClientSession() as session,
        ):
            resp = await session.post(
                proxy_url + "/v1/chat/completions",
                json={"model": "model-a", "stream": True},
            )
            assert resp.status == 200
            assert await resp.content.readuntil(b"\n\n") == _CHUNK
            resp.close()

            await _wait_until(
                lambda: stack.swapper.stats.snapshot()["requests"]["total"]
                == 1
            )
            await upstream.assert_closed()
            # Only the request's connection closed, not the shared session.
            assert not stack.swapper._client.closed

            async with session.get(proxy_url + "/stats/json") as stats_resp:
                assert stats_resp.status == 200
                snap = await stats_resp.json()

    for req in (snap["requests"], snap["models"]["model-a"]["requests"]):
        assert req["errors"] == {"client_disconnect": 1}
        assert req["total"] == 1
        assert req["success"] == 0
        assert req["failure"] == 1


async def test_pre_prepare_failure_still_returns_502(caplog):
    """AC3: unreachable upstream -> 502 JSON with the pre-prepare label."""
    caplog.set_level(logging.DEBUG, logger="src.proxy")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]

    outcome = Outcome()

    async def proxy_handler(request):
        body = await request.read()
        return await stream_proxy(
            request, f"http://127.0.0.1:{dead_port}", body,
            timeout=1, outcome=outcome,
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", proxy_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "m", "stream": True},
            )
            assert resp.status == 502
            data = await resp.json()
            assert "error" in data
    finally:
        await server.close()

    assert outcome.client_disconnect is False
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith("Stream proxy error (pre-prepare): ") for m in messages
    )
    assert not any(
        m.startswith("Client disconnected during stream") for m in messages
    )
