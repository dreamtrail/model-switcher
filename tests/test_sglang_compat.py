"""SGLang v0.5.19 compatibility contracts for plan P-20260916-01.

The deterministic wire fixtures are derived from immutable SGLang release commit
0bcd822377da7b5718e674eaf9c870d349424dd1:
- readiness and model listing:
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/http_server.py
- ``ErrorResponse``, usage/stream options, and Chat response/delta fields:
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/protocol.py#L93-L98
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/protocol.py#L193-L206
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/protocol.py#L1114-L1208
- validation-to-error serialization (including top-level error fields):
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/serving_base.py#L82-L84
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/serving_base.py#L195-L216
- empty-message validation, Chat response construction, usage, and ``[DONE]``:
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/serving_chat.py#L853-L856
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/serving_chat.py#L1634-L1682
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/serving_chat.py#L1780-L1795
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/serving_chat.py#L1946-L1998
- exact ``msgspec`` SSE shape, including required ``reasoning_content``:
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/sse_utils.py#L13-L94
- conditional Responses API named events:
  https://github.com/sgl-project/sglang/blob/0bcd822377da7b5718e674eaf9c870d349424dd1/python/sglang/srt/entrypoints/openai/serving_responses.py

Tests use loopback HTTP fixtures and never invoke SGLang, download models, or
require accelerator hardware.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shlex
import sys

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest
import yaml

from src.config import load_config
from src.health import wait_for_ready
from src.process_manager import ProcessHandle
from src.swapper import Swapper


SGLANG_MODEL_ID = "qwen2.5-7b-sglang"

CHAT_RESPONSE = (
    b'{"id":"chatcmpl-sglang-1","object":"chat.completion",'
    b'"created":1789516800,"model":"qwen2.5-7b-sglang","choices":'
    b'[{"index":0,"message":{"role":"assistant","content":"Hello!",'
    b'"reasoning_content":null,"tool_calls":null},"logprobs":null,'
    b'"finish_reason":"stop","matched_stop":null}],"usage":{"prompt_tokens":8,'
    b'"total_tokens":10,"completion_tokens":2,"prompt_tokens_details":null,'
    b'"reasoning_tokens":0},"metadata":null}'
)

CHAT_ERROR_RESPONSE = (
    b'{"object":"error","message":"Messages cannot be empty.",'
    b'"type":"BadRequestError","param":null,"code":400}'
)

CHAT_SSE = b"".join(
    (
        b'data: {"id":"chatcmpl-sglang-stream","object":"chat.completion.chunk",'
        b'"created":1789516800,"model":"qwen2.5-7b-sglang","choices":'
        b'[{"index":0,"delta":{"reasoning_content":null,"role":"assistant",'
        b'"content":""},"logprobs":null,"finish_reason":null,'
        b'"matched_stop":null}]}' b"\n\n",
        b'data: {"id":"chatcmpl-sglang-stream","object":"chat.completion.chunk",'
        b'"created":1789516800,"model":"qwen2.5-7b-sglang","choices":'
        b'[{"index":0,"delta":{"reasoning_content":null,"content":"Hello"},'
        b'"logprobs":null,"finish_reason":null,"matched_stop":null}]}' b"\n\n",
        b'data: {"id":"chatcmpl-sglang-stream","object":"chat.completion.chunk",'
        b'"created":1789516800,"model":"qwen2.5-7b-sglang","choices":'
        b'[{"index":0,"delta":{"reasoning_content":null},"logprobs":null,'
        b'"finish_reason":"stop","matched_stop":null}]}' b"\n\n",
        b'data: {"id":"chatcmpl-sglang-stream","object":"chat.completion.chunk",'
        b'"created":1789516800,"model":"qwen2.5-7b-sglang","choices":[],'
        b'"usage":{"prompt_tokens":8,"total_tokens":9,"completion_tokens":1,'
        b'"prompt_tokens_details":null,"reasoning_tokens":0}}' b"\n\n",
        b"data: [DONE]\n\n",
    )
)

_RESPONSE_EVENTS = (
    ("response.created", 0),
    ("response.in_progress", 1),
    ("response.output_item.added", 2),
    ("response.content_part.added", 3),
    ("response.output_text.delta", 4),
    ("response.output_text.done", 5),
    ("response.content_part.done", 6),
    ("response.output_item.done", 7),
    ("response.completed", 8),
)
RESPONSES_SSE = b"".join(
    (
        f"event: {event}\n"
        f'data: {{"type":"{event}","sequence_number":{sequence}}}\n\n'
    ).encode()
    for event, sequence in _RESPONSE_EVENTS
)


@asynccontextmanager
async def _active_switcher(tmp_path, upstream_app):
    """Expose a real Swapper proxying to a deterministic loopback upstream."""
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    config_path = tmp_path / "sglang.yaml"
    config_path.write_text(f'{SGLANG_MODEL_ID}: "echo --port ${{PORT}}"\n')
    swapper = Swapper(load_config(config_path), idle_timeout=0)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    swapper._current = ProcessHandle(SGLANG_MODEL_ID, upstream_server.port, process)
    swapper._current_model = SGLANG_MODEL_ID
    swapper._healthy = True

    async def switcher_handler(request):
        return await swapper.handle_request(request)

    switcher_app = aiohttp.web.Application()
    switcher_app.router.add_route("*", "/{path:.*}", switcher_handler)
    switcher_server = TestServer(switcher_app)
    await switcher_server.start_server()
    try:
        async with TestClient(switcher_server) as client:
            yield client
    finally:
        await switcher_server.close()
        await swapper.shutdown()
        await upstream_server.close()


def test_example_recipe_aligns_served_name_and_substitutes_assigned_port(tmp_path):
    raw_config = yaml.safe_load(Path("config.example.yaml").read_text())
    recipes = {
        model_id: command
        for model_id, command in raw_config.items()
        if "sglang serve" in command
    }

    assert len(recipes) == 1
    model_id, command = next(iter(recipes.items()))
    tokens = shlex.split(command)
    assert tokens[:2] == ["sglang", "serve"]
    assert tokens[tokens.index("--served-model-name") + 1] == model_id
    assert tokens[tokens.index("--host") + 1] == "127.0.0.1"
    assert tokens[tokens.index("--model-path") + 1]
    assert tokens[tokens.index("--port") + 1] == "${PORT}"

    isolated_path = tmp_path / "sglang.yaml"
    isolated_path.write_text(yaml.safe_dump({model_id: command}))
    loaded = load_config(isolated_path).models[model_id]

    assert loaded.name == model_id
    assert 1 <= loaded.port <= 65535
    assert "${PORT}" not in loaded.cmd
    assert f"--port {loaded.port}" in loaded.cmd
    assert f"--served-model-name {model_id}" in loaded.cmd


async def test_configured_served_name_is_listed_by_both_local_endpoints(tmp_path):
    config_path = tmp_path / "sglang.yaml"
    config_path.write_text(f'{SGLANG_MODEL_ID}: "echo --port ${{PORT}}"\n')
    swapper = Swapper(load_config(config_path))

    async def handler(request):
        return await swapper.handle_request(request)

    app = aiohttp.web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            for endpoint in ("/v1/models", "/models"):
                response = await client.get(endpoint)
                payload = await response.json()
                assert response.status == 200
                assert [item["id"] for item in payload["data"]] == [SGLANG_MODEL_ID]
    finally:
        await server.close()
        await swapper.shutdown()


async def test_sglang_startup_waits_for_health_200_despite_models_200():
    ready = False
    first_health = asyncio.Event()
    health_count = 0
    models_count = 0

    async def handle_health(request):
        nonlocal health_count
        health_count += 1
        first_health.set()
        return aiohttp.web.Response(status=200 if ready else 503)

    async def handle_models(request):
        nonlocal models_count
        models_count += 1
        return aiohttp.web.json_response(
            {"object": "list", "data": [{"id": SGLANG_MODEL_ID}]}
        )

    app = aiohttp.web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    server = TestServer(app)
    await server.start_server()
    task = asyncio.create_task(
        wait_for_ready(server.port, timeout=2, interval=0.02)
    )
    try:
        await asyncio.wait_for(first_health.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert not task.done()
        assert models_count == 0

        async with aiohttp.ClientSession() as client:
            response = await client.get(
                f"http://127.0.0.1:{server.port}/v1/models"
            )
            assert response.status == 200
        assert not task.done()
        assert models_count == 1

        ready = True
        await asyncio.wait_for(task, timeout=1)
        assert health_count >= 2
        assert models_count == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await server.close()


@pytest.mark.parametrize(
    ("status", "body", "request_id", "messages"),
    (
        pytest.param(
            200,
            CHAT_RESPONSE,
            "req_sglang_chat_001",
            [{"role": "user", "content": "Hello"}],
            id="success",
        ),
        pytest.param(
            400,
            CHAT_ERROR_RESPONSE,
            "req_sglang_error_001",
            [],
            id="empty-messages-error",
        ),
    ),
)
async def test_chat_json_status_body_header_and_model_pass_through(
    tmp_path, status, body, request_id, messages
):
    received = []

    async def upstream_handler(request):
        received.append(await request.read())
        return aiohttp.web.Response(
            status=status,
            body=body,
            content_type="application/json",
            headers={"x-request-id": request_id},
        )

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
    request_body = json.dumps(
        {
            "model": SGLANG_MODEL_ID,
            "messages": messages,
        },
        separators=(",", ":"),
    ).encode()

    async with _active_switcher(tmp_path, upstream_app) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=request_body,
            headers={"Content-Type": "application/json"},
        )
        raw = await response.read()

    assert response.status == status
    assert raw == body
    assert response.headers["x-request-id"] == request_id
    assert received == [request_body]
    assert json.loads(received[0])["model"] == SGLANG_MODEL_ID


async def test_chat_stream_injects_usage_options_and_preserves_exact_sse(tmp_path):
    received = []

    async def upstream_handler(request):
        received.append(await request.json())
        response = aiohttp.web.StreamResponse(
            headers={"Content-Type": "text/event-stream"}
        )
        await response.prepare(request)
        for frame in CHAT_SSE.splitlines(keepends=True):
            await response.write(frame)
        await response.write_eof()
        return response

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)

    async with _active_switcher(tmp_path, upstream_app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": SGLANG_MODEL_ID,
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )
        raw = await response.read()

    assert response.status == 200
    assert received[0]["model"] == SGLANG_MODEL_ID
    assert received[0]["stream_options"] == {"include_usage": True}
    assert raw == CHAT_SSE
    assert raw.endswith(b"data: [DONE]\n\n")


async def test_conditional_responses_named_events_pass_through_exactly(tmp_path):
    received = []

    async def upstream_handler(request):
        received.append(await request.json())
        response = aiohttp.web.StreamResponse(
            headers={"Content-Type": "text/event-stream"}
        )
        await response.prepare(request)
        for event in RESPONSES_SSE.splitlines(keepends=True):
            await response.write(event)
        await response.write_eof()
        return response

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/responses", upstream_handler)

    async with _active_switcher(tmp_path, upstream_app) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": SGLANG_MODEL_ID, "input": "Hello", "stream": True},
        )
        raw = await response.read()

    blocks = raw.decode().strip().split("\n\n")
    names = [block.splitlines()[0].removeprefix("event: ") for block in blocks]
    assert response.status == 200
    assert received == [
        {"model": SGLANG_MODEL_ID, "input": "Hello", "stream": True}
    ]
    assert raw == RESPONSES_SSE
    assert names == [event for event, _ in _RESPONSE_EVENTS]
    assert names[-1] == "response.completed"


def _document_section(path: str, heading: str) -> str:
    document = Path(path).read_text()
    return document.split(heading, 1)[1].split("\n## ", 1)[0]


def test_readme_documents_sglang_operational_and_api_boundaries():
    section = _document_section("README.md", "## SGLang")

    for token in (
        "sglang serve",
        "--served-model-name",
        "--host 127.0.0.1",
        "--port ${PORT}",
        "/health",
        "503",
        "/v1/models",
        "404 or 405",
        "stream_options",
        "include_usage",
        "data: [DONE]",
        "POST /v1/responses",
        "optional",
        "not guaranteed",
        "/generate",
        "SIGTERM",
        "forced-stop fallback",
    ):
        assert token in section
    assert "config key" in section
    assert "API request `model` identical" in section
    assert "executes commands directly" in section


def test_decision_records_no_adapter_readiness_and_test_tradeoff():
    section = _document_section(
        "docs/DECISIONS.md", "## P-20260916-01 — SGLang Compatibility"
    )

    for token in (
        "generic foreground-command contract",
        "No backend adapter",
        "--served-model-name",
        "forward model IDs unchanged",
        "OpenAI-compatible routes",
        "optional endpoint",
        "/generate",
        "authoritative",
        "does not follow redirects",
        "original 404 or 405",
        "retains its existing redirect behavior",
        "deterministic loopback fixtures",
    ):
        assert token in section
