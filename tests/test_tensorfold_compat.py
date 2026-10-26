"""TensorFold compatibility contracts for plan P-20261005-04.

The deterministic fixtures follow ashhart/TensorFold at commit 609ca419
(release 0.6.5, 2026-10-03), ``src/tensorfold``:
- launch ``tensorfold serve MODEL --host H --port N --name ID
  --no-update-check``: ``cli_args.py``
- the HTTP socket opens only after the model has loaded (and downloaded on
  first use); ``/health`` then answers 200 ``{"status": "ok", ...}``:
  ``cli.py`` (``Server(...)`` after load), ``server/http.py`` ``do_GET``
- the ``model`` field is not enforced: ``server/http.py`` ``reply_model``
- stdlib ``ThreadingHTTPServer`` speaking HTTP/1.1; streams send
  ``Connection: close`` and end by closing: ``server/http.py``
- no llama.cpp ``timings``; a top-level ``tensorfold`` object (MLX
  ``prefill_seconds``, CUDA ``prefill_s``, both seconds) on the non-stream
  body and on the stream's finish chunk; with
  ``stream_options.include_usage`` the usage follows in its own
  ``choices: []`` chunk, then ``data: [DONE]``: ``server/http.py``
  ``response_extras``/``finish_stream``, ``server/app.py``,
  ``cuda/http.py``
- plain ``print`` log lines prefixed ``[tensorfold] ``; ``warning:``,
  ``request error:`` plus a traceback on stderr; request access lines are
  printed without flush: ``server/http.py`` ``log_message``,
  ``cuda/http.py`` ``_log_error``, ``server/authentication.py``
- SIGTERM is turned into ``KeyboardInterrupt`` so cleanup runs: ``cli.py``

Tests use a loopback fake launched through the ``config.example.yaml``
recipe as a real subprocess (a fake ``tensorfold`` on ``PATH``) and never
install TensorFold, download models, or need a GPU.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import shlex
import sys
import time

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import yaml

from src.config import load_config
from src.swapper import Swapper


TENSORFOLD_COMMIT = "609ca419"
RECIPE_KEY = "qwen3.8-27b-tensorfold"

USAGE = {
    "prompt_tokens": 300,
    "completion_tokens": 3,
    "total_tokens": 303,
    "prompt_tokens_details": {"cached_tokens": 256},
    "completion_tokens_details": {"reasoning_tokens": 0},
}
# MLX ``runtime`` block (server/app.py); prefill_seconds is in seconds.
RUNTIME = {
    "enable_thinking": False,
    "reasoning_effort": "none",
    "engine": "mlx",
    "tokens_per_second": 50.0,
    "seconds": 0.2,
    "prefill_seconds": 0.088,
    "time_to_first_token": 0.09,
    "sampling": "greedy",
    "drafts": True,
    "token_sha": "0123abcd",
    "min_rows": 2,
}
SPECULATIVE = {
    "rounds": 2, "drafted": 2, "accepted": 1,
    "acceptance_rate": 0.5, "tokens_per_round": 1.5,
}
EXTRAS = {
    "exact_mode": "target-verified",
    "tensorfold": RUNTIME,
    "speculative": SPECULATIVE,
}


def _sse(data: dict | str) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data)
    return f"data: {text}\n\n".encode()


def _chunk(delta: dict, finish_reason=None, *, model: str) -> dict:
    return {
        "id": "chatcmpl-tf2",
        "object": "chat.completion.chunk",
        "created": 1790900000,
        "model": model,
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish_reason}],
    }


def _expected_sse(model: str) -> bytes:
    finish = {**_chunk({}, "stop", model=model), **EXTRAS}
    usage = {**_chunk({}, model=model), "choices": [], "usage": USAGE}
    return b"".join((
        _sse(_chunk({"reasoning_content": "Think"}, model=model)),
        _sse(_chunk({"content": "Hel"}, model=model)),
        _sse(_chunk({"content": "lo!"}, model=model)),
        # The tensorfold object rides on the finish chunk ...
        _sse(finish),
        # ... and usage on its own chunk (include_usage), then [DONE].
        _sse(usage),
        _sse("[DONE]"),
    ))


def _expected_json(model: str) -> dict:
    return {
        "id": "chatcmpl-tf1",
        "object": "chat.completion",
        "created": 1790900000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!",
                            "reasoning_content": "Think"},
                "finish_reason": "stop",
            }
        ],
        "usage": USAGE,
        **EXTRAS,
    }


# A TensorFold-shaped stdlib server installed as a fake ``tensorfold`` on
# PATH. STATE_DIR is substituted when the script is written.
FAKE_TENSORFOLD = r'''
import argparse, json, os, signal, sys, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = __STATE_DIR__

parser = argparse.ArgumentParser(prog="tensorfold")
commands = parser.add_subparsers(dest="command", required=True)
serve = commands.add_parser("serve")
serve.add_argument("model")
serve.add_argument("--host", default="127.0.0.1")
serve.add_argument("--port", type=int, default=8080)
serve.add_argument("--name")
serve.add_argument("--no-update-check", action="store_true")
args = parser.parse_args()
served = args.name or args.model.rstrip("/").split("/")[-1]

with open(os.path.join(STATE_DIR, "launch.json"), "w") as out:
    json.dump({"argv": sys.argv[1:], "pid": os.getpid(),
               "unbuffered": os.environ.get("PYTHONUNBUFFERED")}, out)
frames = json.load(open(os.path.join(STATE_DIR, "frames.json")))

print("[tensorfold] loading " + args.model, flush=True)
print("[tensorfold] warning: a reply reached max_tokens while thinking",
      flush=True)
time.sleep(float(frames["load_delay"]))  # port opens only after load


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # As TensorFold: printed without flush (block-buffered on a pipe
        # unless PYTHONUNBUFFERED is set).
        print("[tensorfold] " + self.address_string() + " " + format % args)

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        route = self.path.split("?", 1)[0].rstrip("/")
        if route in ("", "/health"):
            self._json(200, {"status": "ok", "model": served,
                             "model_ids": [served], "max_batch_size": 8,
                             "warming": False, "memory": {}})
            return
        if route.endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": served, "object": "model", "created": 0,
                 "owned_by": "tensorfold"}]})
            return
        self._json(404, {"error": {"message": "unknown path " + self.path}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with open(os.path.join(STATE_DIR, "requests.jsonl"), "a") as out:
            out.write(json.dumps({"path": self.path, "body": body}) + "\n")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "unknown path " + self.path}})
            return
        messages = body.get("messages") or [{}]
        if messages[-1].get("content") == "boom":
            try:
                raise RuntimeError("boom")
            except RuntimeError as exc:
                print("[tensorfold] request error: RuntimeError: " + str(exc),
                      flush=True)
                traceback.print_exception(exc)
                self._json(500, {"error": {"message": str(exc)}})
            return
        # The model field is not enforced: echo the served name.
        if not body.get("stream"):
            self._json(200, frames["json"])
            return
        # The switcher always sets stream_options.include_usage, so usage
        # comes in its own chunk after the finish chunk.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in frames["sse"]:
            self.wfile.write(frame.encode("latin-1"))
            self.wfile.flush()
            time.sleep(0.01)
        self.close_connection = True
        print("[tensorfold] done req-0123456789ab prompt=300 cached=256 "
              "tokens=3 finish=stop tok/s=50.0 ttft=0.09s prefill=0.09s "
              "rounds=2 accepted=1/2", flush=True)


def _terminate(signum, frame):
    raise KeyboardInterrupt


httpd = ThreadingHTTPServer((args.host, args.port), Handler)
print("[tensorfold] serving " + served + " at http://" + args.host + ":"
      + str(args.port) + "/v1 (loaded in 0.5s)", flush=True)
signal.signal(signal.SIGTERM, _terminate)
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    pass
httpd.server_close()
with open(os.path.join(STATE_DIR, "stopped"), "w") as out:
    out.write("clean")
'''


def _example_recipe() -> tuple[str, str]:
    raw_config = yaml.safe_load(Path("config.example.yaml").read_text())
    recipes = {
        model_id: command
        for model_id, command in raw_config.items()
        if isinstance(command, str) and "tensorfold serve" in command
    }
    assert len(recipes) == 1
    return next(iter(recipes.items()))


def _install_fake(tmp_path: Path, monkeypatch, *, load_delay: float,
                  model: str) -> Path:
    """Put a fake ``tensorfold`` first on PATH; return its state folder."""
    state = tmp_path / "state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "tensorfold"
    fake.write_text(
        f"#!{sys.executable}\n"
        + FAKE_TENSORFOLD.replace("__STATE_DIR__", repr(str(state)))
    )
    fake.chmod(0o755)
    sse_frames = _expected_sse(model).split(b"\n\n")[:-1]
    (state / "frames.json").write_text(json.dumps({
        "load_delay": load_delay,
        "json": _expected_json(model),
        "sse": [frame.decode("latin-1") + "\n\n" for frame in sse_frames],
    }))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    return state


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


async def _model_requests(swapper: Swapper, model: str, total: int) -> dict:
    """Per-model request stats once ``total`` requests have been recorded."""
    deadline = time.monotonic() + 5
    while True:
        models = swapper.stats.snapshot()["models"]
        requests = models.get(model, {}).get("requests", {})
        if requests.get("total", 0) >= total or time.monotonic() > deadline:
            return requests
        await asyncio.sleep(0.01)


async def _wait_for_log(caplog, text: str) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if any(text in record.getMessage() for record in caplog.records):
            return True
        await asyncio.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Recipe
# ---------------------------------------------------------------------------


def test_example_recipe_runs_tensorfold_serve(tmp_path):
    model_id, command = _example_recipe()
    outer = shlex.split(command)
    # bash -c only to set PYTHONUNBUFFERED; exec keeps one process.
    assert outer[:2] == ["bash", "-c"] and len(outer) == 3
    tokens = shlex.split(outer[2])
    assert tokens[:4] == ["PYTHONUNBUFFERED=1", "exec", "tensorfold", "serve"]
    assert tokens[4] and not tokens[4].startswith("-")
    assert tokens[tokens.index("--host") + 1] == "127.0.0.1"
    assert tokens[tokens.index("--port") + 1] == "${PORT}"
    assert tokens[tokens.index("--name") + 1] == model_id
    assert "--no-update-check" in tokens
    assert "--tp" not in tokens

    isolated_path = tmp_path / "tensorfold.yaml"
    isolated_path.write_text(yaml.safe_dump({model_id: command}))
    loaded = load_config(isolated_path).models[model_id]
    assert "${PORT}" not in loaded.cmd
    assert f"--port {loaded.port} " in loaded.cmd


# ---------------------------------------------------------------------------
# End to end through the real config, process, health, Swapper, and proxy
# ---------------------------------------------------------------------------


async def test_recipe_launched_fake_serves_json_sse_stats_logs_and_stops(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="src.process_manager")
    model_id, command = _example_recipe()
    state = _install_fake(tmp_path, monkeypatch, load_delay=0.5,
                          model=model_id)
    config_path = tmp_path / "tensorfold.yaml"
    config_path.write_text(yaml.safe_dump({model_id: command}))
    swapper = Swapper(load_config(config_path), timeout=30, idle_timeout=0)
    messages = [{"role": "user", "content": "Hello"}]

    async with _switcher_client(swapper) as client:
        started = time.monotonic()
        chat_json = await client.post(
            "/v1/chat/completions",
            json={"model": model_id, "messages": messages},
        )
        json_payload = await chat_json.json()
        # The first request waited for the port to open after "load".
        assert time.monotonic() - started >= 0.5
        assert swapper.active_model == model_id
        process = swapper._current.process

        chat_sse = await client.post(
            "/v1/chat/completions",
            json={"model": model_id, "messages": messages, "stream": True},
        )
        sse_raw = await chat_sse.read()

        failed = await client.post(
            "/v1/chat/completions",
            json={"model": model_id,
                  "messages": [{"role": "user", "content": "boom"}]},
        )
        failed_payload = await failed.json()

        requests = await _model_requests(swapper, model_id, 3)
        # The last access line (the 500, printed without flush and with no
        # flushed print after it) arrives while the server still runs only
        # with PYTHONUNBUFFERED=1 from the recipe.
        access_seen = await _wait_for_log(
            caplog, '"POST /v1/chat/completions HTTP/1.1" 500'
        )
        traceback_seen = await _wait_for_log(
            caplog, "Traceback (most recent call last):"
        )

    # Stopped by SIGTERM, cleanly within the grace period.
    assert process.returncode is not None
    assert (state / "stopped").read_text() == "clean"
    assert access_seen
    assert traceback_seen

    launch = json.loads((state / "launch.json").read_text())
    assert launch["unbuffered"] == "1"
    assert launch["argv"][:2] == ["serve", "TensorFold/Qwen3.8-27B-MLX-4bit"]
    assert launch["argv"][launch["argv"].index("--name") + 1] == model_id
    # exec: the switcher's child is TensorFold itself, not a bash wrapper.
    assert launch["pid"] == process.pid

    # JSON and SSE pass through unchanged, tensorfold object included.
    assert chat_json.status == 200
    assert json_payload == _expected_json(model_id)
    assert chat_sse.status == 200
    assert chat_sse.headers["Content-Type"].startswith("text/event-stream")
    assert sse_raw == _expected_sse(model_id)
    assert sse_raw.endswith(b"data: [DONE]\n\n")
    assert failed.status == 500
    assert failed_payload == {"error": {"message": "boom"}}

    received = [
        json.loads(line)
        for line in (state / "requests.jsonl").read_text().splitlines()
    ]
    assert [item["path"] for item in received] == [
        "/v1/chat/completions"
    ] * 3
    for item in received:
        assert item["body"]["model"] == model_id
    assert "stream_options" not in received[0]["body"]
    assert received[1]["body"]["stream_options"] == {"include_usage": True}

    # Stats: usage and cached tokens; server-timed prompt speed from
    # tensorfold.prefill_seconds; generation speed from proxy timing.
    assert requests["total"] == 3
    tokens = requests["tokens"]
    assert tokens["prompt"] == 2 * USAGE["prompt_tokens"]
    assert tokens["completion"] == 2 * USAGE["completion_tokens"]
    assert tokens["cached"] == 2 * USAGE["prompt_tokens_details"]["cached_tokens"]
    assert tokens["prefill"] == 2 * (300 - 256)
    prompt = requests["rates"]["prompt_tps"]
    assert abs(prompt["avg"] - 44 / 0.088) < 1e-6
    assert prompt["server_timed_share"] == 1.0
    gen = requests["rates"]["generation_tps"]
    # Only the stream is timed (by the proxy); the server's
    # tokens_per_second (50) is not used.
    assert gen["server_timed_share"] == 0.0
    assert gen["avg"] is not None and gen["avg"] != 50.0

    # TensorFold's own lines reached the log at the right level.
    records = {
        record.getMessage(): record
        for record in caplog.records
        if record.name == "src.process_manager"
    }
    expected = {
        "[tensorfold] loading TensorFold/Qwen3.8-27B-MLX-4bit": logging.INFO,
        "[tensorfold] warning: a reply reached max_tokens while thinking":
            logging.WARNING,
        "[tensorfold] request error: RuntimeError: boom": logging.ERROR,
        "[tensorfold] done req-0123456789ab prompt=300 cached=256 tokens=3 "
        "finish=stop tok/s=50.0 ttft=0.09s prefill=0.09s rounds=2 "
        "accepted=1/2": logging.INFO,
    }
    for line, level in expected.items():
        assert records[line].levelno == level, line
        assert getattr(records[line], "plain_upstream", False) is (
            level == logging.INFO
        )
    serving = next(m for m in records if m.startswith("[tensorfold] serving "))
    assert records[serving].levelno == logging.INFO


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def _document_section(path: str, heading: str) -> str:
    document = Path(path).read_text()
    return document.split(heading, 1)[1].split("\n## ", 1)[0]


def test_readme_documents_tensorfold_launch_and_limits():
    section = _document_section("README.md", "## TensorFold")

    for token in (
        "https://github.com/ashhart/TensorFold",
        TENSORFOLD_COMMIT,
        "tensorfold serve",
        "--host 127.0.0.1",
        "--port ${PORT}",
        "--name",
        "--no-update-check",
        "PYTHONUNBUFFERED=1",
        "exec",
        "not enforced",
        "/health",
        "--timeout",
        "tensorfold pull",
        "prefill_seconds",
        "prefill_s",
        "include_usage",
        "data: [DONE]",
        "[tensorfold] ",
        "warning:",
        "request error:",
        "SIGTERM",
        "SIGKILL",
        "3 s",
        "--tp 2",
        "real hardware",
    ):
        assert token in section, token


def test_readme_speed_tables_list_tensorfold():
    section = _document_section("README.md", "## Statistics")
    assert "| TensorFold | none |" in section
    assert "`tensorfold.prefill_seconds`" in section


def test_decision_records_tensorfold_choices():
    section = _document_section(
        "docs/decisions/runtime.md", "## P-20261005-04 — TensorFold Compatibility"
    )

    for token in (
        TENSORFOLD_COMMIT,
        "prefill_seconds",
        "prefill_s",
        "usage.prompt_time",
        "tokens_per_second",
        "[tensorfold] ",
        "PYTHONUNBUFFERED",
        "3 s",
        "deterministic loopback fake",
    ):
        assert token in section, token
