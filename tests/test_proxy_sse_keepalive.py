"""SSE keepalive while a streaming upstream is quiet (plan P-20261028-01).

Expectations come from the plan's acceptance criteria:
- AC1: a quiet SSE upstream -> ``: keepalive\\n\\n`` at about the interval,
  real events unchanged and in order.
- AC2: never inside an event (partial line or partial multi-line event).
- AC3: ``sse_keepalive=0`` (and the default) -> byte-identical passthrough.
- AC4: stats (bytes, tokens, chunk counts) unchanged by keepalives.
- AC5: ``timeout`` read inactivity still ends a silent stream in-band.
- AC6: a failed keepalive write is a client disconnect.
- AC7: non-SSE streamed responses get no keepalive.
- AC8 (proxy side): Swapper passes its ``sse_keepalive`` to the stream.

All upstreams and clients are real local aiohttp servers and sessions.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle
from src.proxy import _UpstreamReader, _at_event_boundary, stream_proxy
from src.stats import Outcome
from src.swapper import Swapper

from tests.helpers import _drain

KEEPALIVE = b": keepalive\n\n"
INTERVAL = 0.1
QUIET = 0.35  # upstream pause; several intervals long
_WAIT_S = 3.0


def _url(server: TestServer) -> str:
    return f"http://{server.host}:{server.port}"


def _sse_upstream(steps, content_type="text/event-stream"):
    """Upstream handler: ``steps`` are bytes to write or float sleeps."""

    async def handler(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": content_type},
        )
        await resp.prepare(request)
        for step in steps:
            if isinstance(step, float):
                await asyncio.sleep(step)
            else:
                await resp.write(step)
        await resp.write_eof()
        return resp

    return handler


def _upstream_bytes(steps) -> bytes:
    return b"".join(s for s in steps if isinstance(s, bytes))


@asynccontextmanager
async def _proxy(upstream_handler, **stream_kwargs):
    """Yield a TestClient for a proxy that forwards to ``upstream_handler``."""
    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
    upstream = TestServer(upstream_app)
    await upstream.start_server()
    proxy_server = None
    try:
        async def proxy_handler(request):
            return await stream_proxy(
                request, _url(upstream), await request.read(),
                **stream_kwargs,
            )

        proxy_app = web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()
        async with TestClient(proxy_server) as client:
            yield client
    finally:
        if proxy_server is not None:
            await proxy_server.close()
        await upstream.close()


async def _timed_read(resp) -> list[tuple[float, bytes]]:
    """Read the whole body; return (arrival time, chunk) pairs."""
    clock = asyncio.get_running_loop().time
    arrivals = []
    async with asyncio.timeout(_WAIT_S):
        while True:
            chunk = await resp.content.readany()
            if not chunk:
                return arrivals
            arrivals.append((clock(), chunk))


async def _post_stream(client):
    return await client.post(
        "/v1/chat/completions", json={"model": "test", "stream": True},
    )


async def _run(steps, *, content_type="text/event-stream", **kwargs):
    async with _proxy(_sse_upstream(steps, content_type), **kwargs) as client:
        resp = await _post_stream(client)
        arrivals = await _timed_read(resp)
    return resp, arrivals


def _body(arrivals) -> bytes:
    return b"".join(chunk for _, chunk in arrivals)


# --- AC1: keepalives at about the interval, events unchanged ---------------

@pytest.mark.parametrize("sep", [b"\n\n", b"\r\n\r\n"])
@pytest.mark.parametrize(
    "content_type", ["text/event-stream", "text/event-stream; charset=utf-8"],
)
async def test_quiet_upstream_gets_keepalives_between_events(
    sep, content_type,
):
    """Quiet gaps before and between events are filled with keepalives."""
    first = b'data: {"chunk": 1}' + sep
    second = b'data: {"chunk": 2}' + sep
    steps = [QUIET, first, QUIET, second]

    resp, arrivals = await _run(
        steps, content_type=content_type, sse_keepalive=INTERVAL, timeout=10,
    )
    body = _body(arrivals)

    assert resp.status == 200
    # Events are forwarded unchanged and in order; only keepalives added.
    assert body.replace(KEEPALIVE, b"") == first + second
    before, rest = body.split(first, 1)
    between, after = rest.split(second, 1)
    assert after == b""
    for gap in (before, between):
        count = gap.count(KEEPALIVE)
        assert gap == KEEPALIVE * count
        assert 2 <= count <= 4, gap  # QUIET / INTERVAL ~= 3.5

    # Keepalives arrive about one interval apart (loose bounds for CI).
    ka_times = [t for t, chunk in arrivals if chunk == KEEPALIVE]
    gaps = [b - a for a, b in zip(ka_times, ka_times[1:])]
    assert gaps
    assert min(gaps) >= INTERVAL * 0.5


async def test_data_resets_the_keepalive_timer():
    """Events closer together than the interval get no keepalive."""
    events = [f'data: {{"n": {i}}}\n\n'.encode() for i in range(6)]
    steps = []
    for event in events:
        steps += [event, 0.04]

    _, arrivals = await _run(steps, sse_keepalive=0.15, timeout=10)

    assert _body(arrivals) == b"".join(events)


# --- AC2: never inside an event ---------------------------------------------

async def test_keepalive_waits_for_event_boundary():
    """A pause inside a line or inside a multi-line event gets no comment."""
    part_line_a = b'data: {"pa'
    part_line_b = b'rt": 1}\n'
    event_rest = b'data: {"part": 2}\n\n'
    done = b"data: [DONE]\n\n"
    steps = [part_line_a, QUIET, part_line_b, QUIET, event_rest, QUIET, done]

    _, arrivals = await _run(steps, sse_keepalive=INTERVAL, timeout=10)
    body = _body(arrivals)

    event = part_line_a + part_line_b + event_rest
    assert body.startswith(event)
    tail = body[len(event):]
    assert tail.endswith(done)
    # After the event ends, the quiet gap does get keepalives.
    gap = tail[: -len(done)]
    assert gap.count(KEEPALIVE) >= 2
    assert gap == KEEPALIVE * gap.count(KEEPALIVE)


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (b"", True),
        (b"}\n\n", True),
        (b"\r\n\r\n", True),
        (b"}\n", False),
        (b"}\r\n", False),
        (b'{"a', False),
    ],
)
def test_event_boundary_check(tail, expected):
    """Only nothing-yet or a trailing blank line counts as a boundary."""
    assert _at_event_boundary(tail) is expected


class _CountingContent:
    """Minimal readany() source that counts reads (unit-level helper)."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0
        self.data: asyncio.Future[bytes] | None = None

    async def readany(self) -> bytes:
        self.calls += 1
        self.data = asyncio.get_running_loop().create_future()
        try:
            return await self.data
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


async def test_reader_keeps_one_pending_read_across_ticks():
    """Wait limits never cancel or restart the upstream read."""
    content = _CountingContent()
    reader = _UpstreamReader(content)

    assert await reader.read(0.02) is None
    assert await reader.read(0.02) is None
    assert await reader.read(0) is None
    content.data.set_result(b"data: x\n\n")
    assert await reader.read(0.5) == b"data: x\n\n"
    assert (content.calls, content.cancelled) == (1, 0)

    assert await reader.read(0.02) is None
    reader.close()
    await asyncio.sleep(0)
    assert (content.calls, content.cancelled) == (2, 1)


# --- AC3: off means unchanged ------------------------------------------------

@pytest.mark.parametrize("kwargs", [{"sse_keepalive": 0}, {}])
async def test_keepalive_off_is_plain_passthrough(kwargs):
    """With 0 (or the stream_proxy default) the body is byte-identical."""
    steps = [b'data: {"chunk": 1}\n\n', QUIET, b'data: {"chunk": 2}\n\n']

    _, arrivals = await _run(steps, timeout=10, **kwargs)

    assert _body(arrivals) == _upstream_bytes(steps)


# --- AC4: stats unchanged ----------------------------------------------------

def _stats_steps():
    def ev(obj):
        return f"data: {json.dumps(obj)}\n\n".encode()

    return [
        QUIET,
        ev({"choices": [{"delta": {"role": "assistant"}}]}),
        QUIET,
        ev({"choices": [{"delta": {"content": "Hel"}}]}),
        QUIET,
        ev({"choices": [{"delta": {"content": "lo"}}]}),
        ev({
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }),
        b"data: [DONE]\n\n",
    ]


async def _stats_run(keepalive):
    observed = []
    outcome = Outcome(gen_chunk_observer=observed.append)
    steps = _stats_steps()
    _, arrivals = await _run(
        steps, sse_keepalive=keepalive, timeout=10, outcome=outcome,
    )
    return outcome, observed, _body(arrivals), _upstream_bytes(steps)


async def test_stats_ignore_keepalives():
    """Bytes, tokens, and chunk counts match a run without keepalives."""
    on, on_observed, on_body, upstream = await _stats_run(INTERVAL)
    off, off_observed, off_body, _ = await _stats_run(0)

    assert on_body.count(KEEPALIVE) >= 3
    assert off_body == upstream
    assert on_body.replace(KEEPALIVE, b"") == upstream
    assert on.bytes_out == off.bytes_out == len(upstream)
    assert on.gen_chunks == off.gen_chunks == 2
    assert on_observed == off_observed == [1, 1]
    assert (on.prompt_tokens, on.completion_tokens) == (7, 2)
    assert (off.prompt_tokens, off.completion_tokens) == (7, 2)
    assert on.first_token_at is not None
    assert not on.stream_error and not on.client_disconnect


# --- AC5: --timeout still ends a silent stream -------------------------------

async def test_read_inactivity_timeout_still_applies():
    """Keepalives do not reset the upstream read-inactivity limit."""
    configured_timeout = 0.4
    first = b'data: {"chunk": 1}\n\n'
    release = asyncio.Event()

    async def stalled(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        await resp.write(first)
        await release.wait()
        return resp

    outcome = Outcome()
    try:
        async with _proxy(
            stalled, sse_keepalive=INTERVAL, timeout=configured_timeout,
            outcome=outcome,
        ) as client:
            resp = await _post_stream(client)
            arrivals = await _timed_read(resp)
    finally:
        release.set()
    body = _body(arrivals)

    assert body.startswith(first)
    rest = body[len(first):]
    assert rest.count(KEEPALIVE) >= 2
    error_frame = rest.replace(KEEPALIVE, b"")
    assert error_frame.startswith(b"data: ") and error_frame.endswith(b"\n\n")
    error = json.loads(error_frame[len(b"data: "):])
    assert error["error"]["message"].startswith("Upstream error:")
    assert error["error"]["code"] == "internal_error"
    assert outcome.stream_error is True
    # Ended by the read limit, measured from the last upstream data.
    first_at = arrivals[0][0]
    assert arrivals[-1][0] - first_at >= configured_timeout * 0.8


# --- AC6: failed keepalive write = client disconnect -------------------------

@asynccontextmanager
async def _serve(app: web.Application):
    """Run ``app`` like src/cli.py (no handler cancellation); yield URL."""
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0][:2]
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


async def test_client_gone_during_keepalive_is_client_disconnect(caplog):
    """A keepalive write to a closed client ends the stream as a disconnect."""
    first = b'data: {"chunk": 1}\n\n'
    upstream_closed = asyncio.Event()
    proxy_done = asyncio.Event()
    outcome = Outcome()

    async def silent_upstream(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        await resp.write(first)
        # Stay silent until the proxy closes the upstream connection.
        async with asyncio.timeout(_WAIT_S):
            while request.transport is not None and not (
                request.transport.is_closing()
            ):
                await asyncio.sleep(0.01)
        upstream_closed.set()
        return resp

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", silent_upstream)

    async with _serve(upstream_app) as upstream_url:
        async def proxy_handler(request):
            try:
                return await stream_proxy(
                    request, upstream_url, await request.read(),
                    timeout=10, outcome=outcome, sse_keepalive=INTERVAL,
                )
            finally:
                proxy_done.set()

        proxy_app = web.Application()
        proxy_app.router.add_post("/v1/chat/completions", proxy_handler)
        async with _serve(proxy_app) as proxy_url:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    proxy_url + "/v1/chat/completions",
                    json={"model": "test", "stream": True},
                )
                seen = b""
                async with asyncio.timeout(_WAIT_S):
                    while KEEPALIVE not in seen:
                        seen += await resp.content.readany()
                resp.close()  # drop the TCP connection mid-stream

            async with asyncio.timeout(_WAIT_S):
                await proxy_done.wait()
                await upstream_closed.wait()

    assert seen.startswith(first + KEEPALIVE)
    assert outcome.client_disconnect is True
    assert outcome.stream_error is False
    assert "Client disconnected during stream" in caplog.text


# --- AC7: non-SSE streams get no keepalive -----------------------------------

@pytest.mark.parametrize(
    "content_type", ["application/x-ndjson", "application/json"],
)
async def test_non_sse_stream_gets_no_keepalive(content_type):
    """Only text/event-stream responses get keepalive comments."""
    # Quiet gaps at points that would be SSE boundaries (start, after a
    # blank line), so only the content-type check can hold keepalives.
    steps = [QUIET, b'{"chunk": 1}\n\n', QUIET, b'{"chunk": 2}\n']

    _, arrivals = await _run(
        steps, content_type=content_type, sse_keepalive=INTERVAL, timeout=10,
    )

    assert _body(arrivals) == _upstream_bytes(steps)


# --- AC8 (proxy side): Swapper passes its setting through --------------------

def test_swapper_default_keepalive_is_15_seconds():
    """Swapper matches the CLI default."""
    assert Swapper(Config(models={})).sse_keepalive == 15


@pytest.mark.parametrize("keepalive", [INTERVAL, 0])
async def test_swapper_stream_uses_its_keepalive(keepalive):
    """A stream through Swapper.handle_request honors ``sse_keepalive``."""
    steps = [b'data: {"chunk": 1}\n\n', QUIET, b'data: {"chunk": 2}\n\n']
    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", _sse_upstream(steps))
    upstream = TestServer(upstream_app)
    await upstream.start_server()

    config = Config(models={})
    config.models["model-a"] = ModelConfig(
        name="model-a", cmd="echo --port ${PORT}", port=upstream.port,
    )
    swapper = Swapper(config, sse_keepalive=keepalive)
    proxy_server = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "python", "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        swapper._current = ProcessHandle(
            model_name="model-a",
            port=upstream.port,
            process=proc,
            _stdout_task=asyncio.create_task(_drain(proc.stdout, "a")),
            _stderr_task=asyncio.create_task(_drain(proc.stderr, "a")),
        )
        swapper._current_model = "model-a"
        swapper._healthy = True

        proxy_app = web.Application()
        proxy_app.router.add_route("*", "/{path:.*}", swapper.handle_request)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        async with TestClient(proxy_server) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "model-a", "stream": True},
            )
            body = _body(await _timed_read(resp))
    finally:
        await swapper.shutdown()
        if proxy_server is not None:
            await proxy_server.close()
        await upstream.close()

    assert body.replace(KEEPALIVE, b"") == _upstream_bytes(steps)
    if keepalive:
        assert body.count(KEEPALIVE) >= 2
    else:
        assert KEEPALIVE not in body
