"""Shared helpers for the split src/swapper.py test modules.

Moved verbatim from the former tests/test_swapper.py.
"""

import asyncio

import aiohttp

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle


class _FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.pid = 12345


def _fake_handle(model_name="model-a", port=1234):
    return ProcessHandle(
        model_name=model_name,
        port=port,
        process=_FakeProcess(),
    )


async def _drain(reader, prefix):
    """Drain a stream (for fake process handle)."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
    except asyncio.CancelledError:
        pass


def _make_upstream_with_config(identifier="model-a"):
    """Create an upstream app and config for a model."""
    async def handle_health(request):
        return aiohttp.web.json_response({"status": "ok"})

    async def handle_chat(request):
        body = await request.json()
        if body.get("stream"):
            resp = aiohttp.web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                f'data: {{"id": "{identifier}"}}\n\n'.encode()
            )
            await resp.write_eof()
            return resp
        return aiohttp.web.json_response({"choices": [], "id": identifier})

    app = aiohttp.web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_get("/v1/models", handle_health)
    app.router.add_get("/models", handle_health)
    app.router.add_post("/v1/chat/completions", handle_chat)

    config = Config(models={})
    mc = ModelConfig(
        name=identifier,
        cmd=f"echo {identifier} --port ${{PORT}}",
    )
    config.models[identifier] = mc
    return app, config
