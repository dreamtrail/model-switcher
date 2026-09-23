from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.config import Config, ModelConfig
from src.process_manager import ProcessHandle
from src.swapper import Swapper


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None


class StubRequest:
    """Minimal request for pre-proxy/cancellation handler paths."""

    path = "/v1/chat/completions"
    method = "POST"
    query = {}
    content_type = "application/json"
    headers = {}

    def __init__(self, model: str):
        self._body = json.dumps({"model": model}).encode()

    async def read(self) -> bytes:
        return self._body


def make_config() -> Config:
    return Config(models={
        name: ModelConfig(name=name, cmd=f"serve-{name}", port=port)
        for name, port in (("a", 1001), ("b", 1002), ("c", 1003))
    })


async def make_server(swapper: Swapper) -> TestServer:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", swapper.handle_request)
    server = TestServer(app)
    await server.start_server()
    return server


async def post(client: TestClient, model: str):
    return await client.post(
        "/v1/chat/completions", json={"model": model}
    )


async def eventually(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def response_body(response: web.Response) -> dict:
    return json.loads(response.body)


async def assert_http_error(response, status: int, code: str) -> None:
    messages = {
        "switch_in_progress": "A different model is currently loading",
        "swap_superseded": "Model load was superseded by a newer request",
        "model_start_failed": "Model process could not be started",
        "process_cleanup_failed": "Previous model process could not be stopped",
        "shutting_down": "Model switcher is shutting down",
        "swap_timeout": "Model swap timed out",
    }
    assert response.status == status
    body = (
        await response.json()
        if hasattr(response, "json")
        else response_body(response)
    )
    assert body == {
        "error": {
            "message": messages[code],
            "type": "invalid_request_error",
            "param": None,
            "code": code,
        }
    }


async def install_success_lifecycle(monkeypatch, *, starts=None, stops=None):
    import src.swapper as module

    starts = [] if starts is None else starts
    stops = [] if stops is None else stops

    async def start(model, cmd, port):
        starts.append(model)
        return ProcessHandle(model, port, FakeProcess(len(starts)))

    async def ready(*args, **kwargs):
        return None

    async def stop(handle):
        stops.append(handle.model_name)
        handle.process.returncode = 0

    async def proxy(*args, **kwargs):
        handle = args[3] if len(args) > 3 else kwargs.get("handle")
        return web.json_response({"model": handle.model_name})

    monkeypatch.setattr(module, "start_process", start)
    monkeypatch.setattr(module, "wait_for_ready", ready)
    monkeypatch.setattr(module, "stop_process", stop)
    monkeypatch.setattr(module, "proxy_request", proxy)
    return starts, stops
