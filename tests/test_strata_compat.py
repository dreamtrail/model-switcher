"""Strata compatibility contracts for plan P-20261005-01.

The deterministic fixtures follow Niko1221/Strata at commit 6f32ec0
(2026-10-04), ``serve/server.py``:
- launch ``python serve/server.py --engine strata --config <json> --port N``
  from the repo root (default engine is ``mock``): ``main()``
- the HTTP socket opens only after the engine is ready; ``/health`` then
  answers 200 with ``"loaded": true`` (``false`` in lazy mode or after its own
  idle unload): ``StrataEngine.__init__``, ``do_GET``
- stdlib ``ThreadingHTTPServer`` speaking HTTP/1.0; SSE ends by closing the
  connection: ``Handler``
- SSE ``data:`` frames, ``: keep-alive`` comments, ``reasoning_content``
  deltas, usage plus llama.cpp-style ``timings`` on the final chunk that also
  carries ``finish_reason``, then ``data: [DONE]``; ``stream_options`` is
  ignored: ``openai_chunks``, ``request_timings``
- no ``/v1/completions`` (404 ``{"error":{"message":"not found"}}``)
- without an API key a Host header that is a DNS name other than localhost is
  refused with 403 (DNS-rebinding guard): ``host_allowed``
- plain ``print`` log lines prefixed ``[strata] ``

Tests use loopback fakes (one launched through the documented ``env -C``
recipe as a real subprocess) and never install Strata, download models, or
need a GPU. The fake's 403 body is an assumption; only the status is pinned.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import shlex
import socket
import sys
import time

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import yaml

from src.config import load_config
from src.health import wait_for_ready
from src.swapper import Swapper


STRATA_COMMIT = "6f32ec0"
STRATA_MODEL_ID = "qwen3.8-flash-next"
CLIENT_HOST = "dgx1:8080"

USAGE = {
    "prompt_tokens": 300,
    "completion_tokens": 3,
    "total_tokens": 303,
    "prompt_tokens_details": {"cached_tokens": 256},
}
TIMINGS = {
    "cache_n": 256,
    "prompt_n": 44,
    "prompt_ms": 88.0,
    "prompt_per_token_ms": 2.0,
    "prompt_per_second": 500.0,
    "predicted_n": 3,
    "predicted_ms": 30.0,
    "predicted_per_token_ms": 10.0,
    "predicted_per_second": 100.0,
}

JSON_BODY = {
    "id": "chatcmpl-strata1",
    "object": "chat.completion",
    "created": 1790900000,
    "model": STRATA_MODEL_ID,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello!",
                "reasoning_content": "Think",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": USAGE,
    "timings": TIMINGS,
}


def _sse(data: dict | str) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data)
    return f"data: {text}\n\n".encode()


def _chunk(delta: dict, finish_reason=None, **extra) -> dict:
    chunk = {
        "id": "chatcmpl-strata2",
        "object": "chat.completion.chunk",
        "created": 1790900000,
        "model": STRATA_MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    chunk.update(extra)
    return chunk


KEEP_ALIVE = b": keep-alive\n\n"
SSE_FRAMES = (
    _sse(_chunk({"role": "assistant", "content": ""})),
    KEEP_ALIVE,
    _sse(_chunk({"reasoning_content": "Think"})),
    _sse(_chunk({"content": "Hel"})),
    KEEP_ALIVE,
    _sse(_chunk({"content": "lo!"})),
    # Usage and timings ride on the finish chunk; choices is not empty.
    _sse(_chunk({}, "stop", usage=USAGE, timings=TIMINGS)),
    _sse("[DONE]"),
)
CHAT_SSE = b"".join(SSE_FRAMES)


# A Strata-shaped stdlib server launched as a real subprocess via the recipe.
FAKE_SERVER = r'''
import argparse, ipaddress, json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--engine", choices=["mock", "strata"], default="mock")
parser.add_argument("--config")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8095)
args = parser.parse_args()

# Relative paths: the recipe must start this from the Strata repo root.
config = json.load(open(args.config))
frames = json.load(open("frames.json"))
model = config["model_name"]

print("loading the model " + model + " (engine " + args.engine
      + ") - this can take a minute or two", flush=True)
print("[strata] still starting (0 s) - please wait ...", flush=True)
print("[strata] WARNING: RAM is tight - 3.0 GiB free", flush=True)
time.sleep(float(frames["load_delay"]))  # port opens only after READY


def host_allowed(host):
    if os.environ.get("STRATA_ALLOWED_HOSTS") == "*" or not host:
        return True
    if host.startswith("["):
        return True  # bracketed IPv6 literal
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    name = name.lower()
    return name == "localhost" or name.endswith(".localhost")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self):
        host = self.headers.get("Host", "")
        if host_allowed(host):
            return True
        print("[strata] refused a request for Host " + repr(host), flush=True)
        self._json(403, {"error": {"type": "forbidden",
                                   "message": "host not allowed"}})
        return False

    def do_GET(self):
        if not self._guard():
            return
        if self.path == "/health":
            self._json(200, {"status": "ok", "max_context": 32768,
                             "model": model, "images": False,
                             "api_key": False, "loaded": True,
                             "service": "strata"})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self._guard():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with open("requests.jsonl", "a") as out:
            out.write(json.dumps({"path": self.path,
                                  "host": self.headers.get("Host"),
                                  "engine": args.engine,
                                  "body": body}) + "\n")
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        if not body.get("stream"):
            self._json(200, frames["json"])
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        for frame in frames["sse"]:
            self.wfile.write(frame.encode("latin-1"))
            self.wfile.flush()
            time.sleep(0.01)
        print("[strata] done: 3 tokens in 0.0 s (100.0 tok/s)", flush=True)


def stop(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stop)
httpd = ThreadingHTTPServer((args.host, args.port), Handler)
print("ready: http://127.0.0.1:" + str(args.port)
      + "/v1  (OpenAI: /v1/chat/completions)", flush=True)
print("       WARNING: no API key - anyone on this machine can use it",
      flush=True)
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    print("[strata] stopping ...", flush=True)
httpd.server_close()
'''


def _fake_strata_dir(tmp_path: Path, *, load_delay: float) -> Path:
    strata_dir = tmp_path / "Strata repo"
    (strata_dir / "serve").mkdir(parents=True)
    (strata_dir / "serve" / "server.py").write_text(FAKE_SERVER)
    (strata_dir / "strata-test.json").write_text(
        json.dumps({"model_name": STRATA_MODEL_ID})
    )
    (strata_dir / "frames.json").write_text(
        json.dumps(
            {
                "load_delay": load_delay,
                "json": JSON_BODY,
                "sse": [frame.decode("latin-1") for frame in SSE_FRAMES],
            }
        )
    )
    return strata_dir


def _recipe_config(tmp_path: Path, strata_dir: Path) -> Path:
    """Write a switcher config using the documented Strata recipe shape."""
    command = (
        f"env -C {shlex.quote(str(strata_dir))} "
        f"{shlex.quote(sys.executable)} serve/server.py --engine strata "
        f"--config strata-test.json --port ${{PORT}}"
    )
    config_path = tmp_path / "strata.yaml"
    config_path.write_text(yaml.safe_dump({STRATA_MODEL_ID: command}))
    return config_path


@asynccontextmanager
async def _switcher_client(swapper: Swapper):
    async def switcher_handler(request):
        return await swapper.handle_request(request)

    app = aiohttp.web.Application()
    app.router.add_route("*", "/{path:.*}", switcher_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            yield client
    finally:
        await server.close()
        await swapper.shutdown()


async def _model_requests(swapper: Swapper, total: int) -> dict:
    """Per-model request stats once ``total`` requests have been recorded."""
    deadline = time.monotonic() + 5
    while True:
        models = swapper.stats.snapshot()["models"]
        requests = models.get(STRATA_MODEL_ID, {}).get("requests", {})
        if requests.get("total", 0) >= total or time.monotonic() > deadline:
            return requests
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Recipe and readiness
# ---------------------------------------------------------------------------


def test_example_recipe_runs_strata_server_from_repo_root(tmp_path):
    raw_config = yaml.safe_load(Path("config.example.yaml").read_text())
    recipes = {
        model_id: command
        for model_id, command in raw_config.items()
        if isinstance(command, str) and "serve/server.py" in command
    }

    assert len(recipes) == 1
    model_id, command = next(iter(recipes.items()))
    tokens = shlex.split(command)
    # No shell: env -C sets the working directory (the Strata repo root).
    assert tokens[:2] == ["env", "-C"] and tokens[2]
    assert tokens[4] == "serve/server.py"
    assert tokens[tokens.index("--engine") + 1] == "strata"
    assert tokens[tokens.index("--config") + 1].endswith(".json")
    assert tokens[tokens.index("--port") + 1] == "${PORT}"
    assert "--lazy" not in tokens and "--idle-unload" not in tokens

    isolated_path = tmp_path / "strata.yaml"
    isolated_path.write_text(yaml.safe_dump({model_id: command}))
    loaded = load_config(isolated_path).models[model_id]
    assert "${PORT}" not in loaded.cmd
    assert loaded.cmd.endswith(f"--port {loaded.port}")


async def test_readiness_waits_for_port_then_loaded_true():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    health_calls = 0
    loaded = False

    async def handle_health(request):
        nonlocal health_calls
        health_calls += 1
        return aiohttp.web.json_response(
            {"status": "ok", "max_context": 32768, "model": STRATA_MODEL_ID,
             "images": False, "api_key": False, "loaded": loaded,
             "service": "strata"}
        )

    app = aiohttp.web.Application()
    app.router.add_get("/health", handle_health)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    task = asyncio.create_task(wait_for_ready(port, timeout=5, interval=0.05))
    try:
        # Connection refused while the engine loads.
        await asyncio.sleep(0.3)
        assert not task.done()
        assert health_calls == 0

        # Port open but "loaded": false (lazy / idle-unloaded): keep polling.
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        await asyncio.sleep(0.3)
        assert not task.done()
        assert health_calls >= 2

        loaded = True
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()


# ---------------------------------------------------------------------------
# End to end through the real config, process, health, Swapper, and proxy
# ---------------------------------------------------------------------------


async def test_recipe_launched_fake_serves_json_sse_stats_host_and_logs(
    tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="src.process_manager")
    strata_dir = _fake_strata_dir(tmp_path, load_delay=0.5)
    swapper = Swapper(
        load_config(_recipe_config(tmp_path, strata_dir)), timeout=30,
        idle_timeout=0,
    )
    client_headers = {"Host": CLIENT_HOST}
    messages = [{"role": "user", "content": "Hello"}]

    async with _switcher_client(swapper) as client:
        started = time.monotonic()
        chat_json = await client.post(
            "/v1/chat/completions",
            json={"model": STRATA_MODEL_ID, "messages": messages},
            headers=client_headers,
        )
        json_payload = await chat_json.json()
        assert time.monotonic() - started >= 0.5
        assert swapper.active_model == STRATA_MODEL_ID
        port = swapper._current.port

        chat_sse = await client.post(
            "/v1/chat/completions",
            json={"model": STRATA_MODEL_ID, "messages": messages,
                  "stream": True},
            headers=client_headers,
        )
        sse_raw = await chat_sse.read()

        completion = await client.post(
            "/v1/completions",
            json={"model": STRATA_MODEL_ID, "prompt": "Hello"},
            headers=client_headers,
        )
        completion_payload = await completion.json()

        # Contrast: the fake mirrors Strata's guard for a forwarded Host.
        async with aiohttp.ClientSession() as direct:
            async with direct.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Host": CLIENT_HOST},
            ) as refused:
                refused_status = refused.status
            async with direct.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Host": f"localhost:{port}"},
            ) as allowed:
                allowed_status = allowed.status

        requests = await _model_requests(swapper, 3)

    # JSON and SSE pass through unchanged.
    assert chat_json.status == 200
    assert json_payload == JSON_BODY
    assert chat_sse.status == 200
    assert chat_sse.headers["Content-Type"].startswith("text/event-stream")
    assert sse_raw == CHAT_SSE
    assert sse_raw.count(KEEP_ALIVE) == 2
    assert sse_raw.endswith(b"data: [DONE]\n\n")
    # Strata has no /v1/completions; its 404 is passed through.
    assert completion.status == 404
    assert completion_payload == {"error": {"message": "not found"}}

    # Host rewrite: Strata saw its own address, never the client's Host.
    assert (refused_status, allowed_status) == (403, 200)
    received = [
        json.loads(line)
        for line in (strata_dir / "requests.jsonl").read_text().splitlines()
    ]
    assert [item["path"] for item in received] == [
        "/v1/chat/completions",
        "/v1/chat/completions",
        "/v1/completions",
    ]
    for item in received:
        assert item["host"] == f"127.0.0.1:{port}"
        assert item["engine"] == "strata"
        assert item["body"]["model"] == STRATA_MODEL_ID
    # The switcher's include_usage injection reaches Strata (which ignores it).
    assert "stream_options" not in received[0]["body"]
    assert received[1]["body"]["stream_options"] == {"include_usage": True}

    # Stats: usage, cached tokens, and server prefill/decode timings.
    assert requests["total"] == 3
    tokens = requests["tokens"]
    assert tokens["prompt"] == 2 * USAGE["prompt_tokens"]
    assert tokens["completion"] == 2 * USAGE["completion_tokens"]
    assert tokens["cached"] == 2 * USAGE["prompt_tokens_details"]["cached_tokens"]
    assert tokens["prefill"] == 2 * TIMINGS["prompt_n"]
    prompt = requests["rates"]["prompt_tps"]
    assert abs(prompt["avg"] - 500.0) < 1e-6
    assert prompt["server_timed_share"] == 1.0
    gen = requests["rates"]["generation_tps"]
    assert abs(gen["avg"] - 100.0) < 1e-6
    assert gen["server_timed_share"] == 1.0

    # Strata's own lines reached the log at the right level.
    records = {
        record.getMessage(): record
        for record in caplog.records
        if record.name == "src.process_manager"
    }
    expected = {
        "[strata] still starting (0 s) - please wait ...": logging.INFO,
        "[strata] WARNING: RAM is tight - 3.0 GiB free": logging.WARNING,
        "       WARNING: no API key - anyone on this machine can use it":
            logging.WARNING,
        "[strata] done: 3 tokens in 0.0 s (100.0 tok/s)": logging.INFO,
        f"[strata] refused a request for Host '{CLIENT_HOST}'": logging.INFO,
    }
    for line, level in expected.items():
        assert records[line].levelno == level, line
        assert getattr(records[line], "plain_upstream", False) is (
            level == logging.INFO
        )
    loading = next(m for m in records if m.startswith("loading the model "))
    assert "(engine strata)" in loading


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def _document_section(path: str, heading: str) -> str:
    document = Path(path).read_text()
    return document.split(heading, 1)[1].split("\n## ", 1)[0]


def test_readme_documents_strata_launch_and_limits():
    section = _document_section("README.md", "## Strata")

    for token in (
        "https://github.com/Niko1221/Strata",
        STRATA_COMMIT,
        "--engine strata",
        "mock",
        "env -C",
        "serve/server.py",
        "--port ${PORT}",
        "executes commands directly",
        "any `model` name",
        "/health",
        '"loaded": true',
        "--timeout",
        "/v1/completions",
        "one request at a time",
        "--idle-unload",
        "--lazy",
        "Host",
        "127.0.0.1:<port>",
        ": keep-alive",
        "data: [DONE]",
        "timings",
        "cached_tokens",
        "SIGTERM",
        "SIGKILL",
        "shutdown_cmd",
        "smoke check",
    ):
        assert token in section, token


def test_readme_mentions_host_rewrite_for_all_backends():
    section = _document_section("README.md", "## How It Works")
    assert "Host" in section and "127.0.0.1:<port>" in section


def test_decision_records_host_rewrite_and_readiness():
    section = _document_section(
        "docs/DECISIONS.md", "## P-20261005-01 — Strata Compatibility"
    )

    for token in (
        STRATA_COMMIT,
        "### Backend Integration",
        "### Host Rewrite",
        "### Readiness and Output",
        "### Verification Trade-off",
        "--engine strata",
        "env -C",
        "403",
        "user decision 2026-10-05",
        "STRATA_ALLOWED_HOSTS",
        '"loaded": false',
        "[strata] ",
        "timings",
        "deterministic loopback fakes",
    ):
        assert token in section, token
