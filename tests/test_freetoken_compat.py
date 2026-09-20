"""FreeToken v0.1.3 compatibility contracts for plan P-20260920-01.

The deterministic fixtures are anchored to release commit
cac247a860e316e06580d05aeb05f2e647bde214:
- CLI and launch contract:
  https://github.com/FlashML-org/FreeToken/blob/cac247a860e316e06580d05aeb05f2e647bde214/docs/cli.md
- lifecycle health payload:
  https://github.com/FlashML-org/FreeToken/blob/cac247a860e316e06580d05aeb05f2e647bde214/python/freetoken/server/control_api.py
- OpenAI JSON and ``[DONE]`` SSE:
  https://github.com/FlashML-org/FreeToken/blob/cac247a860e316e06580d05aeb05f2e647bde214/python/freetoken/server/openai_api.py
- Anthropic Messages JSON and named SSE:
  https://github.com/FlashML-org/FreeToken/blob/cac247a860e316e06580d05aeb05f2e647bde214/python/freetoken/server/anthropic_api.py
- Responses JSON and named SSE:
  https://github.com/FlashML-org/FreeToken/blob/cac247a860e316e06580d05aeb05f2e647bde214/python/freetoken/server/responses_api.py

Tests use deterministic loopback servers and never import or launch FreeToken,
download model weights, access a GPU, or claim live integration evidence.
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


FREETOKEN_COMMIT = "cac247a860e316e06580d05aeb05f2e647bde214"
FREETOKEN_MODEL_ID = "freetoken-model"

# Representative wire bytes from the pinned source contracts above. They stay
# intentionally small: generic proxy tests own exhaustive transport behavior.
CHAT_JSON = (
    b'{"id":"chatcmpl-ft-1","object":"chat.completion","model":"freetoken-model",'
    b'"choices":[{"index":0,"message":{"role":"assistant","content":"Hello"},'
    b'"finish_reason":"stop"}],"usage":{"prompt_tokens":4,'
    b'"completion_tokens":1,"total_tokens":5}}'
)
COMPLETION_JSON = (
    b'{"id":"cmpl-ft-1","object":"text_completion","model":"freetoken-model",'
    b'"choices":[{"index":0,"text":"Hello","finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}'
)
MESSAGES_JSON = (
    b'{"id":"msg_ft_1","type":"message","role":"assistant",'
    b'"model":"freetoken-model","content":[{"type":"text","text":"Hello"}],'
    b'"stop_reason":"end_turn","stop_sequence":null,'
    b'"usage":{"input_tokens":4,"output_tokens":1}}'
)
RESPONSES_JSON = (
    b'{"id":"resp_ft_1","object":"response","status":"completed",'
    b'"model":"freetoken-model","output":[{"type":"message","role":"assistant",'
    b'"content":[{"type":"output_text","text":"Hello"}]}],'
    b'"usage":{"input_tokens":4,"output_tokens":1,"total_tokens":5}}'
)

CHAT_SSE = b"".join(
    (
        b'data: {"id":"chatcmpl-ft-stream","object":"chat.completion.chunk",'
        b'"created":1,"model":"freetoken-model","choices":[{"delta":'
        b'{"role":"assistant","content":""},"index":0,"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-ft-stream","object":"chat.completion.chunk",'
        b'"created":1,"model":"freetoken-model","choices":[{"delta":'
        b'{"content":"Hello"},"index":0,"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-ft-stream","object":"chat.completion.chunk",'
        b'"created":1,"model":"freetoken-model","choices":[{"delta":{},'
        b'"index":0,"finish_reason":"stop"}]}\n\n',
        b'data: {"id":"chatcmpl-ft-stream","object":"chat.completion.chunk",'
        b'"created":1,"model":"freetoken-model","choices":[],"usage":'
        b'{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n',
        b"data: [DONE]\n\n",
    )
)
COMPLETION_SSE = b"".join(
    (
        b'data: {"id":"cmpl-ft-stream","object":"text_completion.chunk",'
        b'"created":1,"model":"freetoken-model","choices":[{"text":"Hello",'
        b'"index":0,"finish_reason":null,"logprobs":null}]}\n\n',
        b'data: {"id":"cmpl-ft-stream","object":"text_completion.chunk",'
        b'"created":1,"model":"freetoken-model","choices":[{"text":"",'
        b'"index":0,"finish_reason":"stop","logprobs":null}]}\n\n',
        b'data: {"id":"cmpl-ft-stream","object":"text_completion.chunk",'
        b'"created":1,"model":"freetoken-model","choices":[],"usage":'
        b'{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n',
        b"data: [DONE]\n\n",
    )
)
MESSAGES_SSE = b"".join(
    (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_ft_stream",'
        b'"type":"message","role":"assistant","model":"freetoken-model",'
        b'"content":[],"usage":{"input_tokens":0,"output_tokens":0}}}\n\n',
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":'
        b'{"type":"text","text":""}}\n\n',
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":'
        b'{"type":"text_delta","text":"Hello"}}\n\n',
        b"event: content_block_stop\n"
        b'data: {"type":"content_block_stop","index":0}\n\n',
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"input_tokens":4,"output_tokens":1}}\n\n',
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n',
    )
)
RESPONSES_SSE = b"".join(
    (
        b"event: response.created\n"
        b'data: {"type":"response.created","sequence_number":0,"response":'
        b'{"id":"resp_ft_stream","object":"response","created_at":1,'
        b'"status":"in_progress","error":null,"incomplete_details":null,'
        b'"model":"freetoken-model","output":[],"parallel_tool_calls":true,'
        b'"tool_choice":"auto","tools":[],"usage":null}}\n\n',
        b"event: response.in_progress\n"
        b'data: {"type":"response.in_progress","sequence_number":1,"response":'
        b'{"id":"resp_ft_stream","object":"response","created_at":1,'
        b'"status":"in_progress","error":null,"incomplete_details":null,'
        b'"model":"freetoken-model","output":[],"parallel_tool_calls":true,'
        b'"tool_choice":"auto","tools":[],"usage":null}}\n\n',
        b"event: response.output_item.added\n"
        b'data: {"type":"response.output_item.added","sequence_number":2,'
        b'"output_index":0,"item":{"id":"msg_ft_stream","role":"assistant",'
        b'"status":"in_progress","type":"message","content":[]}}\n\n',
        b"event: response.content_part.added\n"
        b'data: {"type":"response.content_part.added","sequence_number":3,'
        b'"item_id":"msg_ft_stream","output_index":0,"content_index":0,'
        b'"part":{"type":"output_text","text":"","annotations":[]}}\n\n',
        b"event: response.output_text.delta\n"
        b'data: {"type":"response.output_text.delta","sequence_number":4,'
        b'"item_id":"msg_ft_stream","output_index":0,"content_index":0,'
        b'"delta":"Hello","logprobs":[]}\n\n',
        b"event: response.output_text.done\n"
        b'data: {"type":"response.output_text.done","sequence_number":5,'
        b'"item_id":"msg_ft_stream","output_index":0,"content_index":0,'
        b'"text":"Hello","logprobs":[]}\n\n',
        b"event: response.content_part.done\n"
        b'data: {"type":"response.content_part.done","sequence_number":6,'
        b'"item_id":"msg_ft_stream","output_index":0,"content_index":0,'
        b'"part":{"type":"output_text","text":"Hello","annotations":[]}}\n\n',
        b"event: response.output_item.done\n"
        b'data: {"type":"response.output_item.done","sequence_number":7,'
        b'"output_index":0,"item":{"id":"msg_ft_stream","role":"assistant",'
        b'"status":"completed","type":"message","content":[{"type":'
        b'"output_text","text":"Hello","annotations":[]}]}}\n\n',
        b"event: response.completed\n"
        b'data: {"type":"response.completed","sequence_number":8,"response":'
        b'{"id":"resp_ft_stream","object":"response","created_at":1,'
        b'"status":"completed","error":null,"incomplete_details":null,'
        b'"model":"freetoken-model","output":[{"id":"msg_ft_stream",'
        b'"role":"assistant","status":"completed","type":"message",'
        b'"content":[{"type":"output_text","text":"Hello","annotations":[]}]}],'
        b'"parallel_tool_calls":true,"tool_choice":"auto","tools":[],"usage":'
        b'{"input_tokens":4,"output_tokens":1,"total_tokens":5,'
        b'"input_tokens_details":{"cached_tokens":0,"cache_write_tokens":0},'
        b'"output_tokens_details":{"reasoning_tokens":0}}}}\n\n',
    )
)


@asynccontextmanager
async def _active_switcher(tmp_path, upstream_app):
    """Expose a real Swapper/proxy path backed by a loopback fixture."""
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    config_path = tmp_path / "freetoken.yaml"
    config_path.write_text(
        f'{FREETOKEN_MODEL_ID}: "echo --port ${{PORT}}"\n'
    )
    swapper = Swapper(load_config(config_path), idle_timeout=0)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    swapper._current = ProcessHandle(
        FREETOKEN_MODEL_ID, upstream_server.port, process
    )
    swapper._current_model = FREETOKEN_MODEL_ID
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


def test_example_recipe_aligns_model_and_substitutes_port_through_load_config(
    tmp_path,
):
    source = Path("config.example.yaml").read_text()
    raw_config = yaml.safe_load(source)
    recipes = {
        model_id: command
        for model_id, command in raw_config.items()
        if shlex.split(command)[:2] == ["ft", "serve"]
    }

    assert len(recipes) == 1
    model_id, command = next(iter(recipes.items()))
    tokens = shlex.split(command)
    assert model_id == FREETOKEN_MODEL_ID
    assert tokens[tokens.index("--model") + 1]
    assert tokens[tokens.index("--served-model-name") + 1] == model_id
    assert tokens[tokens.index("--host") + 1] == "127.0.0.1"
    assert tokens[tokens.index("--port") + 1] == "${PORT}"
    assert "${PORT}" in source

    isolated_path = tmp_path / "recipe.yaml"
    isolated_path.write_text(yaml.safe_dump({model_id: command}))
    loaded = load_config(isolated_path).models[model_id]

    assert loaded.name == model_id
    assert 1 <= loaded.port <= 65534
    assert "${PORT}" not in loaded.cmd
    assert f"--port {loaded.port}" in loaded.cmd
    assert f"--served-model-name {model_id}" in loaded.cmd


async def test_switcher_lists_configured_freetoken_model_locally(tmp_path):
    config_path = tmp_path / "freetoken.yaml"
    config_path.write_text(
        f'{FREETOKEN_MODEL_ID}: "echo --port ${{PORT}}"\n'
    )
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
                assert [item["id"] for item in payload["data"]] == [
                    FREETOKEN_MODEL_ID
                ]
    finally:
        await server.close()
        await swapper.shutdown()


async def test_freetoken_loading_health_waits_for_ok_without_models_fallback():
    health_count = 0
    models_count = 0

    async def handle_health(request):
        nonlocal health_count
        health_count += 1
        status = "loading" if health_count == 1 else "ok"
        return aiohttp.web.json_response({"status": status})

    async def handle_models(request):
        nonlocal models_count
        models_count += 1
        return aiohttp.web.json_response({"object": "list", "data": []})

    app = aiohttp.web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    server = TestServer(app)
    await server.start_server()
    try:
        await wait_for_ready(server.port, timeout=1, interval=0.01)
        assert health_count == 2
        assert models_count == 0
    finally:
        await server.close()


_JSON_CASES = (
    pytest.param(
        "/v1/chat/completions",
        {
            "model": FREETOKEN_MODEL_ID,
            "messages": [{"role": "user", "content": "Hello"}],
        },
        CHAT_JSON,
        id="chat-completions",
    ),
    pytest.param(
        "/v1/completions",
        {"model": FREETOKEN_MODEL_ID, "prompt": "Hello"},
        COMPLETION_JSON,
        id="completions",
    ),
    pytest.param(
        "/v1/messages",
        {
            "model": FREETOKEN_MODEL_ID,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Hello"}],
        },
        MESSAGES_JSON,
        id="messages",
    ),
    pytest.param(
        "/v1/responses",
        {"model": FREETOKEN_MODEL_ID, "input": "Hello"},
        RESPONSES_JSON,
        id="responses",
    ),
)


@pytest.mark.parametrize(("route", "request_payload", "response_body"), _JSON_CASES)
async def test_four_route_json_matrix_passes_through_real_swapper(
    tmp_path, route, request_payload, response_body
):
    received = []

    async def upstream_handler(request):
        received.append(await request.json())
        return aiohttp.web.Response(
            body=response_body,
            content_type="application/json",
        )

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post(route, upstream_handler)

    async with _active_switcher(tmp_path, upstream_app) as client:
        response = await client.post(route, json=request_payload)
        raw = await response.read()

    assert response.status == 200
    assert raw == response_body
    assert received == [request_payload]


_OPENAI_STREAM_CASES = (
    pytest.param(
        "/v1/chat/completions",
        {
            "model": FREETOKEN_MODEL_ID,
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        },
        CHAT_SSE,
        "chat.completion.chunk",
        id="chat-completions",
    ),
    pytest.param(
        "/v1/completions",
        {"model": FREETOKEN_MODEL_ID, "prompt": "Hello", "stream": True},
        COMPLETION_SSE,
        "text_completion.chunk",
        id="completions",
    ),
)


@pytest.mark.parametrize(
    ("route", "request_payload", "stream_body", "chunk_object"),
    _OPENAI_STREAM_CASES,
)
async def test_openai_streams_inject_usage_and_preserve_usage_then_done(
    tmp_path, route, request_payload, stream_body, chunk_object
):
    received = []

    async def upstream_handler(request):
        received.append(await request.json())
        response = aiohttp.web.StreamResponse(
            headers={"Content-Type": "text/event-stream"}
        )
        await response.prepare(request)
        await response.write(stream_body)
        await response.write_eof()
        return response

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post(route, upstream_handler)

    async with _active_switcher(tmp_path, upstream_app) as client:
        response = await client.post(route, json=request_payload)
        raw = await response.read()

    assert response.status == 200
    assert received == [
        {**request_payload, "stream_options": {"include_usage": True}}
    ]
    payloads = [
        json.loads(record.removeprefix(b"data: "))
        for record in raw.strip().split(b"\n\n")[:-1]
    ]
    assert raw == stream_body
    assert {payload["object"] for payload in payloads} == {chunk_object}
    assert payloads[-2]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["choices"] == []
    assert payloads[-1]["usage"]["total_tokens"] > 0
    assert raw.endswith(b"data: [DONE]\n\n")


_NAMED_STREAM_CASES = (
    pytest.param(
        "/v1/messages",
        {
            "model": FREETOKEN_MODEL_ID,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        },
        MESSAGES_SSE,
        (
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ),
        id="messages",
    ),
    pytest.param(
        "/v1/responses",
        {"model": FREETOKEN_MODEL_ID, "input": "Hello", "stream": True},
        RESPONSES_SSE,
        (
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ),
        id="responses",
    ),
)


@pytest.mark.parametrize(
    ("route", "request_payload", "stream_body", "expected_events"),
    _NAMED_STREAM_CASES,
)
async def test_named_streams_preserve_terminal_event_without_done(
    tmp_path, route, request_payload, stream_body, expected_events
):
    received = []

    async def upstream_handler(request):
        received.append(await request.json())
        response = aiohttp.web.StreamResponse(
            headers={"Content-Type": "text/event-stream"}
        )
        await response.prepare(request)
        await response.write(stream_body)
        await response.write_eof()
        return response

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post(route, upstream_handler)

    async with _active_switcher(tmp_path, upstream_app) as client:
        response = await client.post(route, json=request_payload)
        raw = await response.read()

    blocks = raw.decode().strip().split("\n\n")
    names = [
        block.splitlines()[0].removeprefix("event: ") for block in blocks
    ]
    payloads = [
        json.loads(block.splitlines()[1].removeprefix("data: "))
        for block in blocks
    ]
    assert response.status == 200
    assert received == [request_payload]
    assert "stream_options" not in received[0]
    assert raw == stream_body
    assert names == list(expected_events)
    assert [payload["type"] for payload in payloads] == names
    if route == "/v1/responses":
        assert [payload["sequence_number"] for payload in payloads] == list(
            range(len(payloads))
        )
    assert b"[DONE]" not in raw


async def test_upstream_error_status_body_and_header_pass_through(tmp_path):
    error_body = (
        b'{"error":{"message":"unsupported response_format",'
        b'"type":"invalid_request_error"}}'
    )

    async def upstream_handler(request):
        return aiohttp.web.Response(
            status=422,
            body=error_body,
            content_type="application/json",
            headers={"x-request-id": "req_freetoken_error_001"},
        )

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)

    async with _active_switcher(tmp_path, upstream_app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": FREETOKEN_MODEL_ID, "messages": []},
        )
        raw = await response.read()

    assert response.status == 422
    assert raw == error_body
    assert response.headers["x-request-id"] == "req_freetoken_error_001"


def _document_section(path: str, heading: str) -> str:
    document = Path(path).read_text()
    return document.split(heading, 1)[1].split("\n## ", 1)[0]


def test_documentation_records_pinned_operational_and_transport_boundary():
    readme = _document_section("README.md", "## FreeToken")
    decision = _document_section(
        "docs/DECISIONS.md", "## P-20260920-01 — FreeToken v0.1.3 Compatibility"
    )

    for token in (
        "FreeToken v0.1.3",
        FREETOKEN_COMMIT,
        "ft serve",
        "--served-model-name",
        "--host 127.0.0.1",
        "--port ${PORT}",
        "base + 1",
        'status is exactly `ok`',
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/messages",
        "/v1/responses",
        "data: [DONE]",
        "message_stop",
        "response.completed",
        "not exhaustive",
        "SIGTERM",
        "non-gating",
    ):
        assert token in readme

    for token in (
        FREETOKEN_COMMIT,
        "generic foreground-command contract",
        "no backend registry",
        "loading",
        "error",
        "base and base + 1 simultaneously",
        "representative four-route matrix",
        "deterministic loopback fixtures",
    ):
        assert token in decision
