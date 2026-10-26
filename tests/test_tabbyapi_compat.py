"""TabbyAPI compatibility contracts for plan P-20260927-01.

The deterministic fixtures follow theroyallab/tabbyAPI master at commit
816c321 (2026-09-26):
- launch flags built from the config schema, CLI over ``config.yml``, and the
  ``--config`` early return: ``common/args.py``, ``common/config_models.py``,
  ``common/tabby_config.py``
- port opens after the startup model loads, port + 1 fallback with a warning:
  ``main.py``
- public ``GET /health`` -> ``{"status": "healthy", "issues": []}``:
  ``endpoints/core/router.py``, ``endpoints/core/types/health.py``
- chat stream chunks, prefill ``prompt_progress`` chunks, the non-empty-choices
  usage chunk with ``timings``, and ``[DONE]``:
  ``endpoints/OAI/utils/chat_completion.py``
- the same for text completions: ``endpoints/OAI/utils/completion.py``
- ``UsageStats`` / ``Timings`` field order: ``endpoints/OAI/types/common.py``
- ``\\r\\n`` SSE separators and ``: ping - <utc time>`` keep-alive comments:
  ``sse-starlette`` ``ServerSentEvent`` / ``EventSourceResponse`` defaults
- ``HH:MM:SS.mmm LEVEL:`` console lines and ``TABBY_LOG_CONSOLE_WIDTH``:
  ``common/logger.py``
- ``x-api-key`` / ``Authorization: Bearer`` checks: ``common/auth.py``

Tests use loopback fakes (one launched through the documented ``bash -c``
recipe as a real subprocess) and never install TabbyAPI, download models, or
need a GPU.
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
import pytest
import yaml

from src.config import load_config
from src.health import wait_for_ready
from src.process_manager import ProcessHandle, _log_upstream_line, _upstream_log_level
from src.swapper import Swapper


TABBY_COMMIT = "816c321"
TABBY_MODEL_ID = "qwen3-8b-exl3"

HEALTH_BODY = {"status": "healthy", "issues": []}
PING = b": ping - 2026-09-27 12:00:00.000000+00:00\r\n\r\n"

USAGE = {
    "prompt_tokens": 12,
    "prompt_tokens_details": {"cached_tokens": 4},
    "prompt_time": 0.02,
    "prompt_tokens_per_sec": 400.0,
    "completion_tokens": 3,
    "completion_tokens_details": {
        "accepted_prediction_tokens": 0,
        "rejected_prediction_tokens": 0,
    },
    "completion_time": 0.03,
    "completion_tokens_per_sec": 100.0,
    "total_tokens": 15,
    "total_time": 0.05,
}
TIMINGS = {
    "cache_n": 4,
    "prompt_n": 8,
    "prompt_ms": 20.0,
    "prompt_per_token_ms": 2.5,
    "prompt_per_second": 400.0,
    "predicted_n": 3,
    "predicted_ms": 30.0,
    "predicted_per_token_ms": 10.0,
    "predicted_per_second": 100.0,
}
PROGRESS = {"total": 12, "cache": 4, "processed": 8, "time_ms": 5}


def _sse(data: dict | str) -> bytes:
    """Encode one event the way sse-starlette does (``\\r\\n`` separators)."""
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"data: {text}\r\n\r\n".encode()


def _chat_chunk(delta: dict, finish_reason=None, **extra) -> dict:
    """Chat chunk in TabbyAPI key order (id, object, created, choices, model)."""
    chunk = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion.chunk",
        "created": 1790500000,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        "model": TABBY_MODEL_ID,
    }
    chunk.update(extra)
    return chunk


def _completion_chunk(text: str, finish_reason=None, **extra) -> dict:
    """Completion chunk in TabbyAPI key order (id, object, choices, created)."""
    chunk = {
        "id": "chatcmpl-abc123",
        "object": "text_completion",
        "choices": [{"index": 0, "text": text, "finish_reason": finish_reason}],
        "created": 1790500000,
        "model_name": TABBY_MODEL_ID,
    }
    chunk.update(extra)
    return chunk


# With include_usage the finish chunk is suppressed when it has no content, and
# the usage chunk carries a non-empty choices list with an empty delta.
CHAT_PREFILL = (
    _sse(_chat_chunk({}, prompt_progress={**PROGRESS, "processed": 6})),
    _sse(_chat_chunk({}, prompt_progress=PROGRESS)),
)
CHAT_BODY = (
    _sse(_chat_chunk({"content": "Hel"})),
    PING,
    _sse(_chat_chunk({"content": "lo!"})),
    _sse(
        {
            "id": "chatcmpl-abc123",
            "object": "chat.completion.chunk",
            "created": 1790500000,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": USAGE,
            "model": TABBY_MODEL_ID,
            "timings": TIMINGS,
        }
    ),
    _sse("[DONE]"),
)
CHAT_SSE = PING + b"".join(CHAT_BODY)

COMPLETION_PREFILL = (_sse(_completion_chunk("", prompt_progress=PROGRESS)),)
COMPLETION_BODY = (
    _sse(_completion_chunk("Hello")),
    PING,
    _sse(_completion_chunk(" there")),
    _sse(
        {
            "id": "chatcmpl-abc123",
            "object": "text_completion",
            "choices": [{"index": 0, "text": "", "finish_reason": "stop"}],
            "created": 1790500000,
            "usage": USAGE,
            "model_name": TABBY_MODEL_ID,
            "timings": TIMINGS,
        }
    ),
    _sse("[DONE]"),
)
COMPLETION_SSE = PING + b"".join(COMPLETION_BODY)


# A TabbyAPI-shaped server launched as a real subprocess through the recipe.
FAKE_MAIN = r'''
import argparse, asyncio, json, os, sys, time
from aiohttp import web

parser = argparse.ArgumentParser()
parser.add_argument("--host")
parser.add_argument("--port")
parser.add_argument("--model-dir")
parser.add_argument("--model-name")
parser.add_argument("--disable-auth")
parser.add_argument("--log-timestamps")
args = parser.parse_args()

# config.yml and api_tokens.yml are read relative to the working directory.
if not os.path.exists("config.yml"):
    print("ERROR:    config.yml not found in " + os.getcwd(), flush=True)
    sys.exit(1)
frames = json.load(open("frames.json"))
stamp = args.log_timestamps != "false"

def log(level, message):
    prefix = "12:00:00.000 " if stamp else ""
    pad = " " * (9 - len(level))
    print(f"{prefix}{level}:{pad}{message}", flush=True)

log("INFO", "Loading model " + args.model_name + " width="
    + os.environ.get("TABBY_LOG_CONSOLE_WIDTH", "unset")
    + " auth=" + str(args.disable_auth))
if args.disable_auth == "true":
    log("WARNING", "Disabling authentication makes your instance vulnerable.")
time.sleep(float(frames["load_delay"]))  # port opens only after loading
log("SUCCESS", "Model successfully loaded.")

async def health(request):
    return web.json_response({"status": "healthy", "issues": []})

def handler(kind):
    async def handle(request):
        body = await request.json()
        with open("requests.jsonl", "a") as out:
            out.write(json.dumps({"path": request.path, "body": body}) + "\n")
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream; charset=utf-8"}
        )
        await response.prepare(request)
        include_usage = (body.get("stream_options") or {}).get("include_usage")
        for frame in frames[kind]:
            if '"usage"' in frame and not include_usage:
                continue
            await response.write(frame.encode("latin-1"))
            await asyncio.sleep(0.01)
        await response.write_eof()
        return response
    return handle

app = web.Application()
app.router.add_get("/health", health)
app.router.add_post("/v1/chat/completions", handler("chat"))
app.router.add_post("/v1/completions", handler("completions"))
web.run_app(app, host=args.host, port=int(args.port), print=None,
             handle_signals=True)
'''


def _recipe_config(tmp_path: Path, tabby_dir: Path) -> Path:
    """Write a switcher config using the documented TabbyAPI recipe shape."""
    command = (
        f'bash -c "cd {shlex.quote(str(tabby_dir))} && '
        f"TABBY_LOG_CONSOLE_WIDTH=1000 exec {shlex.quote(sys.executable)} "
        f"main.py --host 127.0.0.1 --port ${{PORT}} --model-dir models "
        f'--model-name {TABBY_MODEL_ID} --disable-auth true"'
    )
    config_path = tmp_path / "tabby.yaml"
    config_path.write_text(yaml.safe_dump({TABBY_MODEL_ID: command}))
    return config_path


def _fake_tabby_dir(tmp_path: Path, *, load_delay: float) -> Path:
    tabby_dir = tmp_path / "tabbyAPI dir"
    tabby_dir.mkdir()
    (tabby_dir / "main.py").write_text(FAKE_MAIN)
    (tabby_dir / "config.yml").write_text("network:\n  port: 5000\n")
    (tabby_dir / "frames.json").write_text(
        json.dumps(
            {
                "load_delay": load_delay,
                "chat": [f.decode("latin-1") for f in (PING, *CHAT_BODY)],
                "completions": [
                    f.decode("latin-1") for f in (PING, *COMPLETION_BODY)
                ],
            }
        )
    )
    return tabby_dir


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


@asynccontextmanager
async def _active_switcher(tmp_path, upstream_app):
    """Expose a real Swapper proxying to an in-process loopback upstream."""
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    config_path = tmp_path / "tabby-active.yaml"
    config_path.write_text(f'{TABBY_MODEL_ID}: "echo --port ${{PORT}}"\n')
    swapper = Swapper(load_config(config_path), idle_timeout=0)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    swapper._current = ProcessHandle(TABBY_MODEL_ID, upstream_server.port, process)
    swapper._current_model = TABBY_MODEL_ID
    swapper._healthy = True
    try:
        async with _switcher_client(swapper) as client:
            yield client, swapper
    finally:
        await upstream_server.close()


async def _model_requests(swapper: Swapper, total: int) -> dict:
    """Per-model request stats once ``total`` requests have been recorded.

    The swapper records a streamed request after the proxied response ends,
    which can be just after the client has read the last byte.
    """
    deadline = time.monotonic() + 5
    while True:
        models = swapper.stats.snapshot()["models"]
        requests = models.get(TABBY_MODEL_ID, {}).get("requests", {})
        if requests.get("total", 0) >= total or time.monotonic() > deadline:
            return requests
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# AC1: recipe, readiness, and streamed transport
# ---------------------------------------------------------------------------


def test_example_recipe_runs_main_py_in_tabby_folder_with_explicit_flags(tmp_path):
    raw_config = yaml.safe_load(Path("config.example.yaml").read_text())
    recipes = {
        model_id: command
        for model_id, command in raw_config.items()
        if isinstance(command, str) and "main.py" in command
    }

    assert len(recipes) == 1
    model_id, command = next(iter(recipes.items()))
    outer = shlex.split(command)
    assert outer[:2] == ["bash", "-c"] and len(outer) == 3
    cd_part, run_part = outer[2].split(" && ")
    assert cd_part.startswith("cd ")
    tokens = shlex.split(run_part)
    assert tokens[0].startswith("TABBY_LOG_CONSOLE_WIDTH=")
    assert tokens[1:4] == ["exec", "venv/bin/python", "main.py"]
    assert tokens[tokens.index("--host") + 1] == "127.0.0.1"
    assert tokens[tokens.index("--port") + 1] == "${PORT}"
    assert tokens[tokens.index("--model-dir") + 1]
    assert tokens[tokens.index("--model-name") + 1] == model_id
    # Bool flags are schema-built argparse options and need a value.
    assert tokens[tokens.index("--disable-auth") + 1] == "true"
    assert "--config" not in tokens
    assert "start.sh" not in command and "start.py" not in command

    isolated_path = tmp_path / "tabby.yaml"
    isolated_path.write_text(yaml.safe_dump({model_id: command}))
    loaded = load_config(isolated_path).models[model_id]
    assert "${PORT}" not in loaded.cmd
    assert f"--port {loaded.port} " in loaded.cmd


async def test_readiness_waits_for_port_opened_after_model_load():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    health_calls = 0

    async def handle_health(request):
        nonlocal health_calls
        health_calls += 1
        return aiohttp.web.json_response(HEALTH_BODY)

    app = aiohttp.web.Application()
    app.router.add_get("/health", handle_health)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    task = asyncio.create_task(wait_for_ready(port, timeout=5, interval=0.05))
    try:
        # Connection refused while the model "loads".
        await asyncio.sleep(0.3)
        assert not task.done()
        assert health_calls == 0

        site = aiohttp.web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        await asyncio.wait_for(task, timeout=2)
        assert health_calls >= 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()


async def test_recipe_launched_fake_serves_streams_logs_and_token_stats(
    tmp_path, caplog
):
    """AC1/AC2/AC4 end to end through the real config, process, and proxy."""
    caplog.set_level(logging.INFO, logger="src.process_manager")
    tabby_dir = _fake_tabby_dir(tmp_path, load_delay=0.5)
    swapper = Swapper(
        load_config(_recipe_config(tmp_path, tabby_dir)), timeout=30,
        idle_timeout=0,
    )

    async with _switcher_client(swapper) as client:
        started = time.monotonic()
        chat = await client.post(
            "/v1/chat/completions",
            json={
                "model": TABBY_MODEL_ID,
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )
        chat_raw = await chat.read()
        assert time.monotonic() - started >= 0.5
        assert swapper.active_model == TABBY_MODEL_ID

        completion = await client.post(
            "/v1/completions",
            json={"model": TABBY_MODEL_ID, "prompt": "Hello", "stream": True},
        )
        completion_raw = await completion.read()
        tokens = (await _model_requests(swapper, 2))["tokens"]

    assert chat.status == 200
    assert chat_raw == CHAT_SSE
    assert b"\r\n\r\n" in chat_raw and b": ping - " in chat_raw
    assert chat_raw.endswith(b"data: [DONE]\r\n\r\n")
    assert completion.status == 200
    assert completion_raw == COMPLETION_SSE
    assert completion_raw.endswith(b"data: [DONE]\r\n\r\n")

    # The switcher's include_usage injection reached TabbyAPI unchanged.
    received = [
        json.loads(line)
        for line in (tabby_dir / "requests.jsonl").read_text().splitlines()
    ]
    assert [item["path"] for item in received] == [
        "/v1/chat/completions",
        "/v1/completions",
    ]
    for item in received:
        assert item["body"]["model"] == TABBY_MODEL_ID
        assert item["body"]["stream_options"] == {"include_usage": True}

    # AC2: usage from the non-empty-choices usage chunk, both requests.
    assert tokens["prompt"] == 2 * USAGE["prompt_tokens"]
    assert tokens["completion"] == 2 * USAGE["completion_tokens"]
    assert tokens["cached"] == 2 * USAGE["prompt_tokens_details"]["cached_tokens"]

    # AC4: TabbyAPI's own lines reached the log at the right level.
    records = {
        record.getMessage(): record
        for record in caplog.records
        if record.name == "src.process_manager"
    }
    loading = next(m for m in records if "INFO:     Loading model" in m)
    assert "width=1000" in loading and "auth=true" in loading
    assert records[loading].levelno == logging.INFO
    warning = (
        "12:00:00.000 WARNING:  Disabling authentication makes your instance "
        "vulnerable."
    )
    assert records[warning].levelno == logging.WARNING
    assert not getattr(records[warning], "plain_upstream", False)
    success = "12:00:00.000 SUCCESS:  Model successfully loaded."
    assert records[success].levelno == logging.INFO
    assert records[success].plain_upstream is True


# ---------------------------------------------------------------------------
# AC2 / AC3: token stats and prefill progress vs first token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "request_body", "prefill", "body", "expected"),
    (
        pytest.param(
            "/v1/chat/completions",
            {
                "model": TABBY_MODEL_ID,
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
                "return_progress": True,
            },
            CHAT_PREFILL,
            CHAT_BODY,
            PING + b"".join(CHAT_PREFILL) + b"".join(CHAT_BODY),
            id="chat",
        ),
        pytest.param(
            "/v1/completions",
            {
                "model": TABBY_MODEL_ID,
                "prompt": "Hello",
                "stream": True,
                "return_progress": True,
            },
            COMPLETION_PREFILL,
            COMPLETION_BODY,
            PING + b"".join(COMPLETION_PREFILL) + b"".join(COMPLETION_BODY),
            id="completions",
        ),
    ),
)
async def test_prefill_progress_chunks_do_not_start_first_token(
    tmp_path, path, request_body, prefill, body, expected
):
    received = []
    content_at: list[float] = []

    async def upstream_handler(request):
        received.append(await request.json())
        response = aiohttp.web.StreamResponse(
            headers={"Content-Type": "text/event-stream; charset=utf-8"}
        )
        await response.prepare(request)
        await response.write(PING)
        for frame in prefill:
            await response.write(frame)
            await asyncio.sleep(0.1)
        content_at.append(time.monotonic())
        for frame in body:
            await response.write(frame)
        await response.write_eof()
        return response

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post(path, upstream_handler)

    async with _active_switcher(tmp_path, upstream_app) as (client, swapper):
        decode_starts = []
        original = swapper.stats.record_decode_start

        def capture(model, request_id, started_at):
            decode_starts.append(started_at)
            original(model, request_id, started_at)

        swapper.stats.record_decode_start = capture
        response = await client.post(path, json=request_body)
        raw = await response.read()
        requests = await _model_requests(swapper, 1)
        tokens = requests["tokens"]
        ttft = requests["ttft_ms"]

    assert response.status == 200
    assert raw == expected
    assert received[0]["return_progress"] is True
    assert received[0]["stream_options"] == {"include_usage": True}
    # The first token is the first content chunk, not a progress chunk.
    assert len(decode_starts) == 1
    assert decode_starts[0] >= content_at[0]
    assert ttft["count"] == 1
    assert ttft["max"] >= 100 * len(prefill) * 0.9
    assert tokens["prompt"] == USAGE["prompt_tokens"]
    assert tokens["completion"] == USAGE["completion_tokens"]
    assert tokens["cached"] == USAGE["prompt_tokens_details"]["cached_tokens"]


# ---------------------------------------------------------------------------
# AC4: log level mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ("12:34:56.789 ", ""), ids=("timestamp", "plain"))
@pytest.mark.parametrize(
    ("level", "expected"),
    (
        ("TRACE", logging.INFO),
        ("DEBUG", logging.INFO),
        ("INFO", logging.INFO),
        ("SUCCESS", logging.INFO),
        ("WARNING", logging.WARNING),
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.ERROR),
    ),
)
def test_tabby_log_lines_map_to_switcher_levels(caplog, prefix, level, expected):
    line = f"{prefix}{level}:{' ' * (9 - len(level))}message text"
    caplog.set_level(logging.DEBUG, logger="src.process_manager")

    _log_upstream_line(line)

    assert _upstream_log_level(line) == expected
    record = next(record for record in caplog.records if record.message == line)
    assert record.levelno == expected
    assert getattr(record, "plain_upstream", False) is (expected == logging.INFO)


@pytest.mark.parametrize(
    "line",
    (
        "12:34:56.789 INFO:     request mentions ERROR: in its body",
        "12:34:56.789 SUCCESS:  no WARNING: here",
        "12:34:56.789 warning:  lowercase is not a TabbyAPI marker",
        "12:34:56.789 WARNINGS: not a level",
    ),
)
def test_tabby_message_words_do_not_promote_level(line):
    assert _upstream_log_level(line) == logging.INFO


# ---------------------------------------------------------------------------
# AC5: auth headers pass through
# ---------------------------------------------------------------------------


API_KEY = "tabby-api-key-for-tests"


@pytest.mark.parametrize(
    ("headers", "status"),
    (
        pytest.param({"x-api-key": API_KEY}, 200, id="x-api-key"),
        pytest.param({"Authorization": f"Bearer {API_KEY}"}, 200, id="bearer"),
        pytest.param({"x-api-key": "wrong"}, 401, id="wrong-key"),
    ),
)
async def test_auth_headers_reach_tabby_unchanged(tmp_path, headers, status):
    seen = []

    async def upstream_handler(request):
        # Mirrors common/auth.py check_api_key: x-api-key first, then Bearer.
        seen.append(
            {
                "x-api-key": request.headers.get("x-api-key"),
                "authorization": request.headers.get("authorization"),
            }
        )
        x_api_key = request.headers.get("x-api-key")
        authorization = request.headers.get("authorization")
        ok = False
        if x_api_key:
            ok = x_api_key == API_KEY
        elif authorization:
            parts = authorization.split(" ")
            ok = len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == API_KEY
        if not ok:
            return aiohttp.web.json_response({"detail": "Invalid API key"}, status=401)
        return aiohttp.web.json_response(
            {
                "id": "chatcmpl-abc123",
                "object": "chat.completion",
                "created": 1790500000,
                "model": TABBY_MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": USAGE,
            }
        )

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)

    async with _active_switcher(tmp_path, upstream_app) as (client, _swapper):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": TABBY_MODEL_ID,
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers=headers,
        )
        payload = await response.json()

    assert response.status == status
    expected = {"x-api-key": None, "authorization": None}
    expected.update({key.lower(): value for key, value in headers.items()})
    assert seen == [expected]
    if status == 401:
        assert payload == {"detail": "Invalid API key"}
    else:
        assert payload["model"] == TABBY_MODEL_ID


# ---------------------------------------------------------------------------
# AC6: documentation
# ---------------------------------------------------------------------------


def _document_section(path: str, heading: str) -> str:
    document = Path(path).read_text()
    return document.split(heading, 1)[1].split("\n## ", 1)[0]


def test_readme_documents_tabbyapi_launch_auth_and_limits():
    section = _document_section("README.md", "## TabbyAPI")

    for token in (
        TABBY_COMMIT,
        "main.py",
        "start.sh",
        "--config",
        "config.yml",
        "api_tokens.yml",
        "override values from `config.yml`",
        "--disable-auth true",
        "TABBY_LOG_CONSOLE_WIDTH",
        "--log-timestamps false",
        "--host 127.0.0.1 --port ${PORT}",
        "--model-name",
        "folder name",
        "inline_model_loading",
        "TabbyAPI admin",
        "api_key",
        "admin_key",
        "x-api-key",
        "Authorization: Bearer",
        "/health",
        "data: [DONE]",
        ": ping",
        "include_usage",
        "cached_tokens",
        "return_progress",
        "port + 1",
        "/dev/shm",
        "SIGTERM",
        "smoke check",
    ):
        assert token in section, token
    assert "executes commands directly" in section


def test_decision_records_generic_contract_changes_and_auth_choice():
    section = _document_section(
        "docs/decisions/runtime.md", "## P-20260927-01 — TabbyAPI Compatibility"
    )

    for token in (
        TABBY_COMMIT,
        "generic foreground-command contract",
        "No backend adapter",
        "main.py",
        "--config",
        "folder name",
        "Two Small Generic Changes",
        "SUCCESS",
        "prompt_progress",
        "Usage on any event is still applied",
        "--disable-auth true",
        "api_tokens.yml",
        "user decision 2026-09-27",
        "x-api-key",
        "deterministic loopback fakes",
    ):
        assert token in section, token
