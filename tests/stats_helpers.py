from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

from src.config import Config, ModelConfig
from src.swapper import Swapper

from tests.helpers import _fake_handle


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------

def _make_app(chat_handler=None):
    """Upstream app with health + chat routes (chat handler replaceable)."""
    async def handle_health(request):
        return web.json_response({"status": "ok"})

    async def default_chat(request):
        body = await request.json()
        if body.get("stream"):
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(b'data: {"chunk": 1}\n\n')
            await resp.write_eof()
            return resp
        return web.json_response({"choices": [], "id": "test"})

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_post(
        "/v1/chat/completions", chat_handler or default_chat
    )
    return app


@asynccontextmanager
async def _stack(
    monkeypatch,
    upstream_apps: dict[str, web.Application] | None = None,
    *,
    seed: str | None = None,
    extra_models: tuple[str, ...] = (),
    timeout: float = 60.0,
    idle_timeout: float = 900.0,
    automatic_switch: bool = False,
):
    """Start upstream TestServers + a Swapper + a catch-all proxy server.

    Patches stop_process/start_process in src.swapper with fakes so no real
    subprocesses are spawned (established pattern from the idle-unload tests).
    Optionally pre-seeds an active, healthy handle for `seed` to bypass
    process startup (pattern from tests/test_swapper_swap_flow.py).

    Yields SimpleNamespace(swapper, servers, proxy).
    """
    import src.swapper as swapper_module

    async def fake_stop_process(handle):
        handle.process.returncode = 0

    async def fake_start_process(model_name, cmd, port):
        return _fake_handle(model_name, port=port)

    monkeypatch.setattr(swapper_module, "stop_process", fake_stop_process)
    monkeypatch.setattr(swapper_module, "start_process", fake_start_process)

    config = Config(models={})
    servers: dict[str, TestServer] = {}
    try:
        for name, app in (upstream_apps or {}).items():
            server = TestServer(app)
            await server.start_server()
            servers[name] = server
            config.models[name] = ModelConfig(
                name=name, cmd=f"echo {name} --port ${{PORT}}", port=server.port
            )
        for name in extra_models:
            config.models[name] = ModelConfig(
                name=name, cmd=f"echo {name} --port 19876", port=19876
            )

        swapper = Swapper(
            config, timeout=timeout, idle_timeout=idle_timeout,
            automatic_switch=automatic_switch,
        )

        if seed is not None:
            handle = _fake_handle(seed, port=servers[seed].port)
            swapper._current = handle
            swapper._current_model = seed
            swapper._healthy = True

        proxy_app = web.Application()

        async def handler(request):
            return await swapper.handle_request(request)

        proxy_app.router.add_route("*", "/{path:.*}", handler)
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()

        try:
            yield SimpleNamespace(
                swapper=swapper, servers=servers, proxy=proxy_server
            )
        finally:
            await swapper.shutdown()
            await proxy_server.close()
    finally:
        for server in servers.values():
            await server.close()


async def _wait_for_queue(swapper, timeout: float = 2.0) -> None:
    """Wait until a queued request future shows up during a fake swap."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not swapper._queue and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert swapper._queue, "request was not queued during swap"

