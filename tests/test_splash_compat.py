"""Splash compatibility contracts for plan P-20261027-01.

The deterministic fixtures follow incoai/splash at commit 39a9738
(39a97385f8560cf8a93972cec9b0a963c9195250, release 1.4.1):
- launch ``splash serve --model OWNER/REPO [--model-dir PATH]
  [--served-model-name NAME] --port N``; ``--host`` defaults to
  127.0.0.1: ``install/launcher.py``, ``server/serve_options.py``
- the socket is bound early but listens only after the engine is ready,
  then ``Ready · ...`` is printed; ``/health`` answers 200
  ``{"status": "ok"}`` and ``/ready`` 200 or 503: ``server/server.py``
- a request ``model`` must be the ``--model`` name or a
  ``--served-model-name`` alias (else 404 ``model_not_found``); responses
  report the ``--model`` name: ``server/frontend.py`` ``accepts_model``
- ``Host`` must be the bind address, localhost, 127.0.0.1 or ::1; a
  browser ``Origin`` not allowed gets 403 and a ``Refused · Origin`` line:
  ``server/server.py``, ``server/http_security.py``
- streams: ``: splash-keepalive`` comments before the start, a role chunk,
  ``prompt_progress`` chunks, empty-delta keepalive chunks, a finish chunk
  with llama.cpp-style ``timings``, then (``include_usage``) a
  ``choices: []`` chunk with ``usage`` and ``metrics``, then ``[DONE]``;
  ``Connection: close``: ``server/server.py`` ``_openai_stream``,
  ``server/api_shapes.py``
- ``timings.prompt_n`` counts cached prompt tokens too and
  ``timings.predicted_n`` counts the first emission that ``predicted_ms``
  leaves out; ``metrics.prefill.tokens`` and ``metrics.decode.tokens`` are
  the matching counts: ``server/metrics.py``
- log lines ``HH:MM:SS <event> · <details>`` (stderr for errors):
  ``server/diagnostics.py`` ``print_status``
- SIGTERM becomes ``KeyboardInterrupt``; the engine then closes, which can
  take up to ~15 s: ``server/server.py``, ``server/runtime.py``

Tests use a loopback fake launched through the ``config.example.yaml``
recipe as a real subprocess (a fake ``splash`` on ``PATH``) and never
install Splash, download models, or need a Mac.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sys
import time

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import yaml

from src.config import load_config
import src.process_manager as pm
from src.swapper import Swapper


SPLASH_COMMIT = "39a9738"
SERVED = "local/qwen3.8-27b"  # the recipe's --model name

USAGE = {
    "prompt_tokens": 300,
    "completion_tokens": 10,
    "total_tokens": 310,
    "prompt_tokens_details": {"cached_tokens": 256},
    "completion_tokens_details": {"reasoning_tokens": 0},
}
# timings_dict: prompt_n includes the 256 cached tokens; predicted_n
# includes the first emission (2 tokens) that predicted_ms leaves out.
TIMINGS = {
    "prompt_n": 300,
    "prompt_ms": 88.0,
    "prompt_per_second": 500.0,
    "predicted_n": 10,
    "predicted_ms": 40.0,
    "predicted_per_second": 200.0,
    "cache_n": 256,
}
METRICS = {
    "prefill": {"tokens": 44},
    "decode": {"tokens": 8},
    "request_latency": {
        "start_to_first_token_ms": 88.0,
        "first_token_to_done_ms": 40.0,
        "wall_ms": 130.0,
        "ttft_ms": 90.0,
        "queue_to_start_ms": 2.0,
        "stream_tokens_per_second": 200.0,
    },
    "cache": {"status": "hit", "matched_tokens": 256,
              "deduplicated_tokens": 0, "lane": 0},
}
PROGRESS = {"total": 300, "cache": 256, "processed": 44, "time_ms": 80.0}


def _sse(data: dict | str) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data)
    return f"data: {text}\n\n".encode()


def _chunk(delta: dict, finish_reason=None) -> dict:
    return {
        "id": "chatcmpl-sp2",
        "object": "chat.completion.chunk",
        "created": 1790900000,
        "model": SERVED,
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish_reason}],
    }


def _expected_sse_parts() -> tuple[list[bytes], bytes]:
    """Frames up to the finish chunk, and the include_usage chunk."""
    head = [
        b": splash-keepalive\n\n",
        _sse(_chunk({"role": "assistant", "content": ""})),
        _sse({**_chunk({}), "prompt_progress": PROGRESS}),
        _sse(_chunk({"reasoning_content": "Think"})),
        _sse(_chunk({"content": "Hel"})),
        # An empty-delta keepalive chunk after the start.
        _sse(_chunk({})),
        _sse(_chunk({"content": "lo!"})),
        _sse({**_chunk({}, "stop"), "timings": TIMINGS}),
    ]
    usage = _sse({**_chunk({}), "choices": [], "usage": USAGE,
                  "metrics": METRICS})
    return head, usage


def _expected_sse() -> bytes:
    head, usage = _expected_sse_parts()
    return b"".join(head) + usage + _sse("[DONE]")


def _expected_json() -> dict:
    return {
        "id": "chatcmpl-sp1",
        "object": "chat.completion",
        "created": 1790900000,
        "model": SERVED,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!",
                            "reasoning_content": "Think"},
                "finish_reason": "stop",
            }
        ],
        "usage": USAGE,
        "metrics": METRICS,
        "timings": TIMINGS,
    }


# A Splash-shaped stdlib server installed as a fake ``splash`` on PATH.
# STATE_DIR is substituted when the script is written.
FAKE_SPLASH = r'''
import argparse, json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = __STATE_DIR__


def print_status(message, error=False):
    # server/diagnostics.py: one write per line, flushed.
    stream = sys.stderr if error else sys.stdout
    stream.write(time.strftime("%H:%M:%S") + " " + message + "\n")
    stream.flush()


def _interrupt(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _interrupt)

parser = argparse.ArgumentParser(prog="splash")
commands = parser.add_subparsers(dest="command", required=True)
serve = commands.add_parser("serve")
serve.add_argument("--model", required=True)
serve.add_argument("--model-dir")
serve.add_argument("--served-model-name", action="append", default=[])
serve.add_argument("--host", default="127.0.0.1")
serve.add_argument("--port", type=int)
serve.add_argument("--idle-release")
args = parser.parse_args()
port = args.port if args.port is not None else int(
    os.environ.get("SPLASH_PORT", "8000"))
names = tuple(dict.fromkeys((args.model, *args.served_model_name)))
allowed_hosts = {args.host, "localhost", "127.0.0.1", "::1"}

with open(os.path.join(STATE_DIR, "launch.json"), "w") as out:
    json.dump({"argv": sys.argv[1:], "pid": os.getpid()}, out)
frames = json.load(open(os.path.join(STATE_DIR, "frames.json")))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass  # Splash prints its own request lines.

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message, code):
        print_status("Error · %d · %s %s" % (status, self.command, self.path),
                     error=True)
        self._json(status, {"error": {"message": message,
                                      "type": "invalid_request_error",
                                      "code": code}})

    def _headers_ok(self):
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
        if host not in allowed_hosts:
            self._error(403, "Host %s is not allowed" % host, "forbidden")
            return False
        origin = self.headers.get("Origin")
        if origin:
            print_status("Refused · Origin %s · restart with --allowed-origin "
                         "%s to accept it" % (origin, origin), error=True)
            self._json(403, {"error": {"message": "origin not allowed",
                                       "type": "invalid_request_error",
                                       "code": "forbidden"}})
            return False
        return True

    def do_GET(self):
        if not self._headers_ok():
            return
        route = self.path.split("?", 1)[0]
        if route == "/health":
            self._json(200, {"status": "ok"})
        elif route == "/ready":
            self._json(200, {"status": "ready"})
        elif route == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": name, "object": "model", "owned_by": "splash"}
                for name in names]})
        else:
            self._error(404, "not found", "not_found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with open(os.path.join(STATE_DIR, "requests.jsonl"), "a") as out:
            out.write(json.dumps({"path": self.path, "body": body,
                                  "host": self.headers.get("Host")}) + "\n")
        if not self._headers_ok():
            return
        if self.path != "/v1/chat/completions":
            self._error(404, "not found", "not_found")
            return
        model = body.get("model")
        if model not in (None, "") and model not in names:
            self._error(404, "model %s not found" % model, "model_not_found")
            return
        print_status("Started · input 300 · cached 256")
        messages = body.get("messages") or [{}]
        if messages[-1].get("content") == "boom":
            print_status("Error · internal_server_error · RuntimeError · "
                         "server/server.py:1234", error=True)
            self._json(500, {"error": {"message": "internal server error",
                                       "type": "server_error",
                                       "code": "internal_server_error"}})
            return
        if not body.get("stream"):
            self._json(200, frames["json"])
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            include_usage = (body.get("stream_options") or {}).get(
                "include_usage")
            sse = frames["sse_head"] + (
                [frames["sse_usage"]] if include_usage else []
            ) + ["data: [DONE]\n\n"]
            for frame in sse:
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.01)
            self.close_connection = True
        print_status("Done · input 300 · cached 256 · TTFT 0.1s · "
                     "200.0 tok/s")


httpd = None
try:
    print_status("Loading · " + args.model)
    print_status("Warning · --allowed-origin '*' without --api-key lets "
                 "every web page use this server", error=True)
    httpd = ThreadingHTTPServer((args.host, port), Handler,
                                bind_and_activate=False)
    httpd.server_bind()
    time.sleep(float(frames["load_delay"]))  # engine "loads"
    httpd.server_activate()  # listen only now
    print_status("Ready · %s · context 32,768 · http://%s:%d"
                 % (args.model, args.host, port))
    httpd.serve_forever()
except KeyboardInterrupt:
    pass
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print_status("Stopping · releasing engine resources")
time.sleep(float(frames["close_delay"]))  # native engine close and flush
if httpd is not None:
    httpd.server_close()
with open(os.path.join(STATE_DIR, "stopped"), "w") as out:
    out.write("clean")
'''


def _example_recipe() -> tuple[str, dict]:
    raw_config = yaml.safe_load(Path("config.example.yaml").read_text())
    recipes = {
        model_id: entry
        for model_id, entry in raw_config.items()
        if isinstance(entry, dict)
        and "splash serve" in str(entry.get("cmd", ""))
    }
    assert len(recipes) == 1
    return next(iter(recipes.items()))


def _install_fake(tmp_path: Path, monkeypatch, *, load_delay: float,
                  close_delay: float) -> Path:
    """Put a fake ``splash`` first on PATH; return its state folder."""
    state = tmp_path / "state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "splash"
    fake.write_text(
        f"#!{sys.executable}\n"
        + FAKE_SPLASH.replace("__STATE_DIR__", repr(str(state)))
    )
    fake.chmod(0o755)
    head, usage = _expected_sse_parts()
    (state / "frames.json").write_text(json.dumps({
        "load_delay": load_delay,
        "close_delay": close_delay,
        "json": _expected_json(),
        "sse_head": [frame.decode("utf-8") for frame in head],
        "sse_usage": usage.decode("utf-8"),
    }))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
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


_TIME = re.compile(r"^\d{2}:\d{2}:\d{2} ")


def _splash_records(caplog) -> dict[str, logging.LogRecord]:
    """Splash status lines by their text after the ``HH:MM:SS `` time."""
    return {
        record.getMessage()[9:]: record
        for record in caplog.records
        if record.name == "src.process_manager"
        and _TIME.match(record.getMessage())
    }


# ---------------------------------------------------------------------------
# Recipe
# ---------------------------------------------------------------------------


def test_example_recipe_runs_splash_serve_with_alias_and_stop_grace(
    tmp_path,
):
    model_id, entry = _example_recipe()
    assert set(entry) == {"cmd", "stop_grace"}
    assert entry["stop_grace"] == 20
    tokens = shlex.split(entry["cmd"])
    assert tokens[:2] == ["splash", "serve"]
    assert tokens[tokens.index("--model") + 1] == SERVED
    assert tokens[tokens.index("--model-dir") + 1].startswith("/")
    # Splash refuses a request model it does not know: alias the key.
    assert tokens[tokens.index("--served-model-name") + 1] == model_id
    assert tokens[tokens.index("--port") + 1] == "${PORT}"
    # Splash binds 127.0.0.1 by default.
    assert "--host" not in tokens

    isolated_path = tmp_path / "splash.yaml"
    isolated_path.write_text(yaml.safe_dump({model_id: entry}))
    loaded = load_config(isolated_path).models[model_id]
    assert "${PORT}" not in loaded.cmd
    assert loaded.cmd.endswith(f"--port {loaded.port}")
    assert loaded.stop_grace == 20


# ---------------------------------------------------------------------------
# End to end through the real config, process, health, Swapper, and proxy
# ---------------------------------------------------------------------------


async def test_recipe_launched_fake_serves_json_sse_stats_logs_and_stops(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="src.process_manager")
    # A global wait far below Splash's close time: only the recipe's
    # stop_grace lets the fake finish its "engine close".
    monkeypatch.setattr(pm, "GRACE_PERIOD", 0.2)
    model_id, entry = _example_recipe()
    state = _install_fake(tmp_path, monkeypatch, load_delay=0.5,
                          close_delay=1.0)
    config_path = tmp_path / "splash.yaml"
    config_path.write_text(yaml.safe_dump({model_id: entry}))
    swapper = Swapper(load_config(config_path), timeout=30, idle_timeout=0)
    messages = [{"role": "user", "content": "Hello"}]

    async with _switcher_client(swapper) as client:
        started = time.monotonic()
        chat_json = await client.post(
            "/v1/chat/completions",
            json={"model": model_id, "messages": messages},
        )
        json_payload = await chat_json.json()
        # The first request waited for the socket to listen after "load".
        assert time.monotonic() - started >= 0.5
        assert swapper.active_model == model_id
        process = swapper._current.process
        assert swapper._current.stop_grace == 20

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

        refused = await client.post(
            "/v1/chat/completions",
            json={"model": model_id, "messages": messages},
            headers={"Origin": "https://page.example"},
        )
        refused_payload = await refused.json()

        requests = await _model_requests(swapper, model_id, 4)
        stop_started = time.monotonic()

    stop_seconds = time.monotonic() - stop_started
    # Stopped by SIGTERM and allowed to finish its slow close.
    assert process.returncode == 0
    assert (state / "stopped").read_text() == "clean"
    assert stop_seconds >= 1.0

    launch = json.loads((state / "launch.json").read_text())
    assert launch["argv"][:3] == ["serve", "--model", SERVED]
    assert launch["pid"] == process.pid

    # JSON and SSE pass through unchanged (comment, progress, keepalive,
    # timings, usage + metrics, [DONE]).
    assert chat_json.status == 200
    assert json_payload == _expected_json()
    assert chat_sse.status == 200
    assert chat_sse.headers["Content-Type"].startswith("text/event-stream")
    assert sse_raw == _expected_sse()
    assert failed.status == 500
    assert failed_payload["error"]["code"] == "internal_server_error"
    # Origin is forwarded unchanged, so Splash's own guard answers.
    assert refused.status == 403
    assert refused_payload["error"]["code"] == "forbidden"

    received = [
        json.loads(line)
        for line in (state / "requests.jsonl").read_text().splitlines()
    ]
    assert [item["path"] for item in received] == [
        "/v1/chat/completions"
    ] * 4
    for item in received:
        # The config key reaches Splash and is accepted as an alias.
        assert item["body"]["model"] == model_id
        # The switcher sends its own upstream address as Host.
        assert item["host"].startswith("127.0.0.1:")
    assert "stream_options" not in received[0]["body"]
    assert received[1]["body"]["stream_options"] == {"include_usage": True}

    # Stats: a cache hit does not inflate prompt speed, and the first
    # emission does not inflate generation speed.
    assert requests["total"] == 4
    tokens = requests["tokens"]
    assert tokens["prompt"] == 2 * USAGE["prompt_tokens"]
    assert tokens["completion"] == 2 * USAGE["completion_tokens"]
    assert tokens["cached"] == 2 * 256
    assert tokens["prefill"] == 2 * 44
    prompt = requests["rates"]["prompt_tps"]
    assert abs(prompt["avg"] - 44 / 0.088) < 1e-6
    assert prompt["server_timed_share"] == 1.0
    gen = requests["rates"]["generation_tps"]
    assert abs(gen["avg"] - 8 / 0.040) < 1e-6
    assert gen["server_timed_share"] == 1.0

    # Splash's own lines reached the log at the right level.
    records = _splash_records(caplog)
    expected = {
        f"Loading · {SERVED}": logging.INFO,
        "Warning · --allowed-origin '*' without --api-key lets every web "
        "page use this server": logging.WARNING,
        "Started · input 300 · cached 256": logging.INFO,
        "Done · input 300 · cached 256 · TTFT 0.1s · 200.0 tok/s":
            logging.INFO,
        "Error · internal_server_error · RuntimeError · "
        "server/server.py:1234": logging.ERROR,
        "Refused · Origin https://page.example · restart with "
        "--allowed-origin https://page.example to accept it":
            logging.WARNING,
        "Stopping · releasing engine resources": logging.INFO,
    }
    for line, level in expected.items():
        assert records[line].levelno == level, line
        assert getattr(records[line], "plain_upstream", False) is (
            level == logging.INFO
        )
    ready = next(m for m in records if m.startswith(f"Ready · {SERVED} · "))
    assert records[ready].levelno == logging.INFO


async def test_without_stop_grace_a_slow_close_is_killed(
    tmp_path, monkeypatch
):
    """Control case: the same fake with the default wait gets SIGKILL."""
    monkeypatch.setattr(pm, "GRACE_PERIOD", 0.2)
    model_id, entry = _example_recipe()
    state = _install_fake(tmp_path, monkeypatch, load_delay=0.0,
                          close_delay=1.0)
    config_path = tmp_path / "splash.yaml"
    config_path.write_text(yaml.safe_dump({model_id: entry["cmd"]}))
    swapper = Swapper(load_config(config_path), timeout=30, idle_timeout=0)

    async with _switcher_client(swapper) as client:
        response = await client.get("/v1/models")
        assert response.status == 200
        assert await swapper.control_load_model(model_id) == "success"
        process = swapper._current.process
        assert swapper._current.stop_grace is None

    assert process.returncode == -9
    assert not (state / "stopped").exists()


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def _document_section(path: str, heading: str) -> str:
    document = Path(path).read_text()
    return document.split(heading, 1)[1].split("\n## ", 1)[0]


def test_readme_documents_splash_launch_and_limits():
    section = _document_section("docs/CONFIGURATION.md", "## Splash")

    for token in (
        "https://github.com/incoai/splash",
        SPLASH_COMMIT,
        "M3",
        "macOS 26.4",
        "splash serve",
        "--model",
        "--model-dir",
        "--served-model-name",
        "--port ${PORT}",
        "model_not_found",
        "stop_grace: 20",
        "--idle-release",
        "10m",
        "--idle-timeout",
        "/health",
        "--timeout",
        "metrics.prefill.tokens",
        "metrics.decode.tokens",
        "include_usage",
        "data: [DONE]",
        "Error ·",
        "Warning ·",
        "Refused ·",
        "error:",
        "SIGTERM",
        "SIGKILL",
        "Origin",
        "real hardware",
    ):
        assert token in section, token


def test_readme_configuration_documents_stop_grace():
    section = _document_section("docs/CONFIGURATION.md", "## Configuration")
    for token in ("stop_grace", "SIGTERM", "SIGKILL", "3"):
        assert token in section, token


def test_readme_speed_tables_list_splash():
    section = _document_section("docs/STATISTICS.md", "## Statistics")
    assert "| Splash | none |" in section
    assert "`metrics.prefill.tokens`" in section
    assert "`metrics.decode.tokens`" in section


def test_decision_records_splash_choices():
    section = _document_section(
        "docs/decisions/runtime.md", "## P-20261027-01 — Splash Compatibility"
    )

    for token in (
        SPLASH_COMMIT,
        "metrics.prefill.tokens",
        "metrics.decode.tokens",
        "prompt_n",
        "predicted_n",
        "stop_grace",
        "Per-model setting",
        "--served-model-name",
        "deterministic loopback fake",
    ):
        assert token in section, token
    index = Path("docs/DECISIONS.md").read_text()
    assert (
        "[P-20261027-01 — Splash Compatibility]"
        "(decisions/runtime.md#p-20261027-01--splash-compatibility)"
    ) in index
