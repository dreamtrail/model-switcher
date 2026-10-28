"""Real-process automatic_switch default (P-20261028-03).

automatic_switch starts off. A request may still load its model when no
model is running or loading; it gets 409 ``automatic_switch_disabled`` only
when a different model is running or loading. Each model is a small real
HTTP server started through the real config loader, port rewriting, process
start/stop, health checks, Swapper, proxy, and control API.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shlex
import signal
import sys
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import yaml

from src.config import load_config
from src.control import CONTROL_HEADER
from src.swapper import Swapper


HEADERS = {CONTROL_HEADER: "1"}

# Like tests/test_variants_e2e.py SERVER, plus ``--gate``: the server binds
# its port only after the gate file exists, so a load stays in progress
# until the test opens the gate.
SERVER = r'''
import argparse, json, os, signal, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--name", required=True)
parser.add_argument("--events", required=True)
parser.add_argument("--gate")
args = parser.parse_args()

def note(text):
    with open(args.events, "a") as out:
        out.write(text + "\n")

class Handler(BaseHTTPRequestHandler):
    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"status": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._send({"served_by": args.name})

    def log_message(self, *a):
        pass

def stop(*_):
    note("stop " + args.name)
    os._exit(0)

signal.signal(signal.SIGTERM, stop)
while args.gate and not os.path.exists(args.gate):
    time.sleep(0.02)
httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
note("launch " + args.name)
httpd.serve_forever()
'''


@asynccontextmanager
async def _stack(
    tmp_path: Path, monkeypatch, *, gate_a=False, gate_b=False, **kwargs
):
    """Swapper over real models ``a`` and ``b`` behind a TestServer."""
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    events = tmp_path / "events"
    gate = tmp_path / "gate-a"
    gate_b_file = tmp_path / "gate-b"
    server_py = tmp_path / "server.py"
    server_py.write_text(SERVER)

    def cmd(name: str, *extra: str) -> str:
        return shlex.join([
            sys.executable, str(server_py), "--port", "8000",
            "--name", name, "--events", str(events), *extra,
        ])

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "a": cmd("a", *(("--gate", str(gate)) if gate_a else ())),
        "b": cmd("b", *(("--gate", str(gate_b_file)) if gate_b else ())),
    }, sort_keys=False))
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("idle_timeout", 0)
    swapper = Swapper(load_config(config_path), **kwargs)

    app = web.Application()

    async def handler(request):
        return await swapper.handle_request(request)

    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:

            async def chat(model: str | None):
                body = {"messages": [{"role": "user", "content": "hi"}]}
                if model is not None:
                    body["model"] = model
                response = await client.post("/v1/chat/completions", json=body)
                return response.status, await response.json()

            def log() -> list[str]:
                return (
                    events.read_text().splitlines() if events.exists() else []
                )

            yield SimpleNamespace(
                swapper=swapper, client=client, chat=chat, log=log,
                gate=gate, gate_b=gate_b_file,
            )
    finally:
        await server.close()
        await swapper.shutdown()


def _code(body: dict) -> str | None:
    return body.get("error", {}).get("code")


async def _until(predicate, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_default_is_off_in_constructor_and_state(tmp_path, monkeypatch):
    async with _stack(tmp_path, monkeypatch) as stack:
        assert stack.swapper.automatic_switch is False
        response = await stack.client.get("/api/v1/control/state")
        state = await response.json()
        assert state["settings"]["automatic_switch"] is False
        assert state["lifecycle"] == "idle"


async def test_off_loads_from_empty_and_blocks_only_a_different_running_model(
    tmp_path, monkeypatch
):
    async with _stack(tmp_path, monkeypatch) as stack:
        # Nothing running: a model-less request is refused exactly as with
        # automatic switching on.
        status, body = await stack.chat(None)
        assert status == 400

        # Nothing running: the request loads its model.
        assert await stack.chat("a") == (200, {"served_by": "a"})

        # a running: b is refused; a and model-less requests reach a.
        status, body = await stack.chat("b")
        assert (status, _code(body)) == (409, "automatic_switch_disabled")
        assert await stack.chat("a") == (200, {"served_by": "a"})
        assert await stack.chat(None) == (200, {"served_by": "a"})
        assert stack.swapper.active_model == "a"

        # A manual switch is not limited by the setting.
        response = await stack.client.put(
            "/api/v1/control/model", json={"model": "b"}, headers=HEADERS
        )
        assert response.status == 200
        assert (await response.json())["active_model"] == "b"
        status, body = await stack.chat("a")
        assert (status, _code(body)) == (409, "automatic_switch_disabled")

        # After a manual unload nothing runs, so a request loads a.
        response = await stack.client.delete(
            "/api/v1/control/model", headers=HEADERS
        )
        assert response.status == 200
        assert (await response.json())["active_model"] is None
        assert await stack.chat("a") == (200, {"served_by": "a"})

        # After a crash nothing runs either, so a request loads b.
        process = stack.swapper._current.process
        os.kill(process.pid, signal.SIGKILL)
        await _until(lambda: process.returncode is not None)
        assert stack.swapper.active_model is None
        assert await stack.chat("b") == (200, {"served_by": "b"})

    assert stack.log() == [
        "launch a", "stop a", "launch b", "stop b", "launch a",
        "launch b", "stop b",
    ]


async def test_off_after_idle_unload_another_model_loads(tmp_path, monkeypatch):
    async with _stack(tmp_path, monkeypatch, idle_timeout=0.3) as stack:
        assert await stack.chat("a") == (200, {"served_by": "a"})
        await _until(lambda: stack.swapper._current is None)
        assert await stack.chat("b") == (200, {"served_by": "b"})
    assert stack.log() == ["launch a", "stop a", "launch b", "stop b"]


async def test_off_load_in_progress_joins_same_model_and_refuses_another(
    tmp_path, monkeypatch
):
    async with _stack(tmp_path, monkeypatch, gate_a=True) as stack:
        owner = asyncio.create_task(stack.chat("a"))
        await _until(
            lambda: stack.swapper.control_state()["pending_model"] == "a"
        )
        joiner = asyncio.create_task(stack.chat("a"))
        await _until(lambda: len(stack.swapper._queue) == 1)

        status, body = await stack.chat("b")
        assert (status, _code(body)) == (409, "automatic_switch_disabled")
        assert not owner.done() and not joiner.done()

        stack.gate.touch()
        assert await asyncio.wait_for(owner, 10) == (200, {"served_by": "a"})
        assert await asyncio.wait_for(joiner, 10) == (200, {"served_by": "a"})
        assert stack.swapper.active_model == "a"
    assert stack.log() == ["launch a", "stop a"]


async def test_off_manual_switch_in_progress_refuses_old_and_joins_new(
    tmp_path, monkeypatch
):
    async with _stack(tmp_path, monkeypatch, gate_b=True) as stack:
        assert await stack.chat("a") == (200, {"served_by": "a"})
        switch = asyncio.create_task(
            stack.client.put(
                "/api/v1/control/model", json={"model": "b"}, headers=HEADERS
            )
        )
        await _until(
            lambda: stack.swapper.control_state()["pending_model"] == "b"
        )

        # The manual switch to b is in progress: a is refused, b joins.
        status, body = await stack.chat("a")
        assert (status, _code(body)) == (409, "automatic_switch_disabled")
        joiner = asyncio.create_task(stack.chat("b"))
        await _until(lambda: len(stack.swapper._queue) == 1)
        assert not switch.done() and not joiner.done()

        stack.gate_b.touch()
        response = await asyncio.wait_for(switch, 10)
        assert response.status == 200
        assert (await response.json())["active_model"] == "b"
        assert await asyncio.wait_for(joiner, 10) == (200, {"served_by": "b"})
        assert stack.swapper.active_model == "b"
    assert stack.log() == ["launch a", "stop a", "launch b", "stop b"]


async def test_off_after_failed_load_another_model_loads(tmp_path, monkeypatch):
    # The gate never opens, so a's load times out and a is stopped.
    async with _stack(tmp_path, monkeypatch, gate_a=True, timeout=1) as stack:
        status, body = await stack.chat("a")
        assert (status, _code(body)) == (504, "swap_timeout")
        assert stack.swapper.control_state()["lifecycle"] == "idle"
        assert await stack.chat("b") == (200, {"served_by": "b"})
    assert stack.log() == ["stop a", "launch b", "stop b"]


async def test_on_request_switches_the_running_model(tmp_path, monkeypatch):
    async with _stack(tmp_path, monkeypatch) as stack:
        response = await stack.client.put(
            "/api/v1/control/settings",
            json={
                "automatic_switch": True,
                "quick_switch": False,
                "idle_timeout_seconds": 0,
            },
            headers=HEADERS,
        )
        assert response.status == 200
        assert await stack.chat("a") == (200, {"served_by": "a"})
        assert await stack.chat("b") == (200, {"served_by": "b"})
        assert await stack.chat(None) == (200, {"served_by": "b"})
    assert stack.log() == ["launch a", "stop a", "launch b", "stop b"]


async def test_constructor_flag_turns_it_on(tmp_path, monkeypatch):
    async with _stack(tmp_path, monkeypatch, automatic_switch=True) as stack:
        assert stack.swapper.control_state()["settings"]["automatic_switch"]
        assert await stack.chat("a") == (200, {"served_by": "a"})
        assert await stack.chat("b") == (200, {"served_by": "b"})
    assert stack.log() == ["launch a", "stop a", "launch b", "stop b"]
