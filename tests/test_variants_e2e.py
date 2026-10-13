"""Real-process run of model variants (P-20261013-01).

Two variants of one public name share one startup script. Each variant runs
a small real HTTP server and has its own shutdown hook. The run goes through
the real config loader, port rewriting, process start/stop, health checks,
Swapper, proxy, and control API; nothing is faked.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import yaml

from src.config import load_config
from src.control import CONTROL_HEADER
from src.swapper import Swapper


SERVER = r'''
import argparse, json, os, signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--name", required=True)
parser.add_argument("--events", required=True)
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
        data = json.loads(self.rfile.read(length) or b"{}")
        self._send({"served_by": args.name, "model": data.get("model"),
                    "port": args.port})

    def log_message(self, *a):
        pass

def stop(*_):
    note("stop " + args.name)
    os._exit(0)

signal.signal(signal.SIGTERM, stop)
httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
note("launch " + args.name)
httpd.serve_forever()
'''


def _hook(events: Path, name: str) -> str:
    code = (
        f"open({str(events)!r}, 'a').write('hook {name}\\n')"
    )
    return shlex.join([sys.executable, "-c", code])


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
    events = tmp_path / "events"
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    # One shared script; the switcher copies it per variant and rewrites
    # the port flag inside each copy.
    script = tmp_path / "run-model.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(server))} "
        f"--port 8000 --events {shlex.quote(str(events))} \"$@\"\n"
    )
    script.chmod(0o755)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "qwen": {
            "default": "text",
            "variants": {
                "text": {
                    "cmd": f"{script} --name text",
                    "shutdown_cmd": _hook(events, "text"),
                },
                "vision": {
                    "cmd": f"{script} --name vision",
                    "shutdown_cmd": _hook(events, "vision"),
                    "shutdown_timeout": 10,
                },
            },
        },
    }, sort_keys=False))
    return config, events


async def test_variants_switch_with_real_processes(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    config_path, events = _write_config(tmp_path)
    swapper = Swapper(load_config(config_path), timeout=30, idle_timeout=0)
    ports = {
        model_id: mc.port for model_id, mc in swapper.config.models.items()
    }
    copies = {
        model_id: mc.cmd.split()[0]
        for model_id, mc in swapper.config.models.items()
    }

    app = web.Application()

    async def handler(request):
        return await swapper.handle_request(request)

    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    chat = {"model": "qwen", "messages": [{"role": "user", "content": "hi"}]}
    try:
        async with TestClient(server) as client:
            # Separate temp copies of the shared script, one per variant.
            assert set(copies) == {"qwen@text", "qwen@vision"}
            assert copies["qwen@text"] != copies["qwen@vision"]
            assert all(os.path.isfile(path) for path in copies.values())

            listed = await (await client.get("/v1/models")).json()
            assert [item["id"] for item in listed["data"]] == ["qwen"]

            first = await (await client.post(
                "/v1/chat/completions", json=chat
            )).json()
            assert first == {
                "served_by": "text", "model": "qwen",
                "port": ports["qwen@text"],
            }

            response = await client.put(
                "/api/v1/control/model",
                json={"model": "qwen", "variant": "vision"},
                headers={CONTROL_HEADER: "1"},
            )
            state = await response.json()
            assert response.status == 200, state
            assert state["active_model"] == "qwen@vision"
            assert state["variants"]["qwen"]["active"] == "vision"

            # Same client request, now served by the other variant.
            second = await (await client.post(
                "/v1/chat/completions", json=chat
            )).json()
            assert second == {
                "served_by": "vision", "model": "qwen",
                "port": ports["qwen@vision"],
            }

            internal = await client.post(
                "/v1/chat/completions", json={**chat, "model": "qwen@text"}
            )
            assert internal.status == 404

            models = swapper.stats.snapshot()["models"]
            assert models["qwen@text"]["requests"]["total"] == 1
            assert models["qwen@vision"]["requests"]["total"] == 1
            assert models["qwen@text"]["loads"]["count"] == 1
            assert models["qwen@vision"]["loads"]["count"] == 1
    finally:
        await server.close()
        await swapper.shutdown()

    # Each variant ran its own hook before its local stop.
    assert events.read_text().splitlines() == [
        "launch text",
        "hook text",
        "stop text",
        "launch vision",
        "hook vision",
        "stop vision",
    ]
