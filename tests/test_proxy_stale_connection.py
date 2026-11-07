"""Retry once on a stale pooled upstream connection (plan P-20261107-01).

Expectations come from the plan's acceptance criteria:
- An upstream that drops a reused keep-alive connection before answering
  gets the request again on another connection; the client sees the
  upstream response, not 502, on both proxy paths.
- A failure after the response headers arrived is not retried.
- Two failures in a row still give 502 after exactly two attempts.
- A failed new connection (``ClientConnectorError``) and timeouts are not
  retried.

The upstream is a real local TCP server whose behavior is chosen per
request, so the stale connection is made on purpose instead of by timing:
it answers the first request on a connection with keep-alive, then drops
the connection on the second one (like vLLM closing an idle connection the
moment the switcher reuses it). Attempts are counted with aiohttp request
tracing on a real client session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.proxy import _is_stale_connection_error, proxy_request, stream_proxy

PATH = "/v1/chat/completions"
BIG_BODY = json.dumps({"model": "m", "pad": "x" * (1024 * 1024)}).encode()


class _Upstream:
    """Raw HTTP/1.1 upstream; ``behavior(conn_no, req_no)`` picks an action.

    Actions: ``ok`` (JSON 200, keep-alive), ``close`` (read the request,
    close without answering), ``reset`` (send RST as soon as the request
    headers arrive), ``partial`` (headers and part of the body, then
    close), ``hang`` (read the request, never answer).
    """

    def __init__(self, behavior):
        self.behavior = behavior
        self.connections = 0
        self.requests: list[tuple[int, int, int]] = []  # conn, req, body len
        self.answered = 0
        self._writers: list[asyncio.StreamWriter] = []
        self._stop = asyncio.Event()
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._serve, "127.0.0.1", 0,
        )
        self.port = self._server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def close(self) -> None:
        self._stop.set()
        assert self._server is not None
        self._server.close()
        for writer in self._writers:
            writer.close()
        await self._server.wait_closed()

    async def _serve(self, reader, writer) -> None:
        self.connections += 1
        conn_no = self.connections
        self._writers.append(writer)
        req_no = 0
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                req_no += 1
                action = self.behavior(conn_no, req_no)
                if action == "reset":
                    sock = writer.get_extra_info("socket")
                    sock.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack("ii", 1, 0),
                    )
                    writer.transport.abort()
                    self.requests.append((conn_no, req_no, -1))
                    return
                length = 0
                for line in head.split(b"\r\n"):
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip())
                body = await reader.readexactly(length) if length else b""
                self.requests.append((conn_no, req_no, len(body)))
                if action == "close":
                    writer.close()
                    return
                if action == "hang":
                    await self._stop.wait()
                    return
                if action == "partial":
                    writer.write(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/event-stream\r\n"
                        b"Content-Length: 100\r\n\r\n"
                        b"data: {}\n\n"
                    )
                    await writer.drain()
                    writer.close()
                    return
                assert action == "ok", action
                self.answered += 1
                payload = json.dumps(
                    {"answer": self.answered, "body_len": len(body)}
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Connection: keep-alive\r\n"
                    b"Content-Length: " + str(len(payload)).encode()
                    + b"\r\n\r\n" + payload
                )
                await writer.drain()
        finally:
            writer.close()


def _first_ok_then_drop(action):
    """Answer the first request on each connection; ``action`` the next."""
    return lambda conn_no, req_no: "ok" if req_no == 1 else action


@asynccontextmanager
async def _proxy(behavior, *, streaming, session_timeout=30, **proxy_kwargs):
    """Yield (proxy TestClient, upstream, attempt list) for one proxy path.

    The proxy uses one shared client session with aiohttp's default pool
    keep-alive (15 s), longer than the test's lifetime, so a connection
    is always reused when one is pooled.
    """
    upstream = _Upstream(behavior)
    await upstream.start()
    attempts: list[str] = []

    async def on_request_start(session, ctx, params):
        attempts.append(params.method)

    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(on_request_start)
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=session_timeout),
        trace_configs=[trace],
    )
    target = proxy_kwargs.pop("upstream_url", upstream.url)
    forward = stream_proxy if streaming else proxy_request

    async def handler(request):
        return await forward(
            request, target, await request.read(),
            client=session, **proxy_kwargs,
        )

    app = web.Application(client_max_size=10 * 1024**2)
    app.router.add_route("*", PATH, handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            yield client, upstream, attempts
    finally:
        await server.close()
        await session.close()
        await upstream.close()


def _retry_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == "src.proxy" and r.levelno == logging.WARNING
        and "retrying once" in r.getMessage()
    ]


# --- Stale pooled connection: retried, upstream response returned --------

@pytest.mark.parametrize("streaming", [False, True], ids=["plain", "stream"])
@pytest.mark.parametrize("drop", ["close", "reset"])
async def test_stale_pooled_connection_is_retried(streaming, drop, caplog):
    """The reused connection dies before an answer; the retry succeeds."""
    caplog.set_level(logging.WARNING, logger="src.proxy")
    async with _proxy(_first_ok_then_drop(drop), streaming=streaming) as (
        client, upstream, attempts,
    ):
        warm = await client.post(PATH, json={"model": "m"})
        assert warm.status == 200
        await warm.read()
        assert upstream.connections == 1

        resp = await client.post(
            PATH, data=BIG_BODY, headers={"Content-Type": "application/json"},
        )
        body = await resp.read()

    assert resp.status == 200, body
    assert json.loads(body) == {"answer": 2, "body_len": len(BIG_BODY)}
    # Warm-up, the attempt on the stale connection, and the retry.
    assert attempts == ["POST", "POST", "POST"]
    # The retry used a new connection: aiohttp dropped the dead one.
    assert upstream.connections == 2
    assert upstream.requests[-1] == (2, 1, len(BIG_BODY))
    [warning] = _retry_warnings(caplog)
    assert f"POST {upstream.url}{PATH}" in warning.getMessage()


# --- Two failures in a row: 502 after exactly two attempts ----------------

@pytest.mark.parametrize("streaming", [False, True], ids=["plain", "stream"])
async def test_second_failure_returns_502(streaming, caplog):
    """Both attempts are dropped; the existing 502 comes back."""
    caplog.set_level(logging.WARNING, logger="src.proxy")
    async with _proxy(lambda c, r: "close", streaming=streaming) as (
        client, upstream, attempts,
    ):
        resp = await client.post(PATH, data=BIG_BODY)
        payload = await resp.json()

    assert resp.status == 502
    assert payload["error"]["message"].startswith("Upstream error:")
    assert attempts == ["POST", "POST"]
    assert upstream.connections == 2
    assert len(_retry_warnings(caplog)) == 1


@pytest.mark.parametrize("streaming", [False, True], ids=["plain", "stream"])
@pytest.mark.parametrize(
    ("method", "switcher_retries"),
    [("GET", 0), ("PUT", 0), ("DELETE", 0), ("POST", 1), ("PATCH", 1)],
)
async def test_each_method_reaches_upstream_at_most_twice(
    streaming, method, switcher_retries, caplog,
):
    """Counted at the upstream: 2 sends for every method, never more.

    aiohttp retries idempotent methods itself, so the switcher retries
    only the others; the two retries do not stack.
    """
    caplog.set_level(logging.WARNING, logger="src.proxy")
    async with _proxy(lambda c, r: "close", streaming=streaming) as (
        client, upstream, attempts,
    ):
        resp = await client.request(method, PATH, data=b'{"model": "m"}')
        await resp.read()

    assert resp.status == 502
    assert len(upstream.requests) == 2
    assert upstream.connections == 2
    assert len(_retry_warnings(caplog)) == switcher_retries


# --- No retry after the response headers arrived --------------------------

async def test_plain_failure_after_headers_is_not_retried(caplog):
    """A body cut short after headers is a 502 with one attempt."""
    caplog.set_level(logging.WARNING, logger="src.proxy")
    async with _proxy(lambda c, r: "partial", streaming=False) as (
        client, upstream, attempts,
    ):
        resp = await client.post(PATH, json={"model": "m"})
        await resp.read()

    assert resp.status == 502
    assert attempts == ["POST"]
    assert upstream.connections == 1
    assert _retry_warnings(caplog) == []


async def test_stream_failure_after_headers_is_not_retried(caplog):
    """A stream cut short after headers ends in-band with one attempt."""
    caplog.set_level(logging.WARNING, logger="src.proxy")
    async with _proxy(lambda c, r: "partial", streaming=True) as (
        client, upstream, attempts,
    ):
        resp = await client.post(PATH, json={"model": "m", "stream": True})
        body = await resp.read()

    assert resp.status == 200
    assert body.startswith(b"data: {}\n\n")
    assert b"Upstream error" in body
    assert attempts == ["POST"]
    assert upstream.connections == 1
    assert _retry_warnings(caplog) == []


# --- Not stale-connection errors: no retry --------------------------------

def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("streaming", [False, True], ids=["plain", "stream"])
async def test_connect_failure_is_not_retried(streaming, caplog):
    """Nothing listens on the port: ClientConnectorError, one attempt."""
    caplog.set_level(logging.WARNING, logger="src.proxy")
    async with _proxy(
        lambda c, r: "ok", streaming=streaming,
        upstream_url=f"http://127.0.0.1:{_unused_port()}",
    ) as (client, upstream, attempts):
        resp = await client.post(PATH, json={"model": "m"})
        await resp.read()

    assert resp.status == 502
    assert attempts == ["POST"]
    assert _retry_warnings(caplog) == []


@pytest.mark.parametrize("streaming", [False, True], ids=["plain", "stream"])
async def test_timeout_before_headers_is_not_retried(streaming, caplog):
    """An upstream that never answers times out once; no retry."""
    caplog.set_level(logging.WARNING, logger="src.proxy")
    async with _proxy(
        lambda c, r: "hang", streaming=streaming,
        session_timeout=0.3, timeout=0.3,
    ) as (client, upstream, attempts):
        resp = await client.post(PATH, json={"model": "m"})
        await resp.read()

    assert resp.status == 502
    assert attempts == ["POST"]
    assert upstream.connections == 1
    assert _retry_warnings(caplog) == []


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (aiohttp.ServerDisconnectedError(), True),
        (aiohttp.ClientOSError(32, "Broken pipe"), True),
        (aiohttp.ClientOSError(104, "Connection reset by peer"), True),
        (BrokenPipeError(32, "Broken pipe"), True),
        (ConnectionResetError(104, "Connection reset by peer"), True),
        (aiohttp.ClientConnectionResetError("Cannot write"), True),
        (
            aiohttp.ClientConnectorError(
                aiohttp.client_reqrep.ConnectionKey(
                    "127.0.0.1", 1, False, False, None, None, None,
                ),
                ConnectionRefusedError(111, "refused"),
            ),
            False,
        ),
        (aiohttp.ServerTimeoutError("read timeout"), False),
        (aiohttp.ConnectionTimeoutError("connect timeout"), False),
        (asyncio.TimeoutError(), False),
        (aiohttp.ClientPayloadError("cut short"), False),
        (aiohttp.ClientResponseError(None, (), status=500), False),
    ],
)
def test_stale_connection_classification(exc, expected):
    """Only drops of an open connection count as stale."""
    assert _is_stale_connection_error(exc) is expected
