"""NInfer compatibility contracts from approved plan P-20260829-01.

The wire fixtures follow NInfer commit fd48e2fae270200793cb26029851b7d37befad38.
They use loopback HTTP servers and never invoke NInfer or external services.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import sys

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest
import yaml

from src.config import load_config
from src.process_manager import ProcessHandle, _log_upstream_line, _upstream_log_level
from src.proxy import proxy_request, stream_proxy
from src.swapper import Swapper


NINFER_IDS = (
    "qwen3.6-27b",
    "qwen3.8-27b",
    "qwen3.6-35b-a3b",
)

CHAT_RESPONSE = (
    b'{"id":"chatcmpl-ninfer-1","object":"chat.completion","created":1787970000,'
    b'"model":"qwen3.8-27b","choices":[{"index":0,"message":{"role":"assistant",'
    b'"reasoning_content":"Count carefully.","content":"42","refusal":null},'
    b'"logprobs":null,"finish_reason":"stop"}],"usage":{"prompt_tokens":12,'
    b'"completion_tokens":4,"total_tokens":16,"prompt_tokens_details":'
    b'{"cached_tokens":3},"completion_tokens_details":{"reasoning_tokens":2}}}'
)

MODEL_NOT_FOUND_RESPONSE = (
    b'{"error":{"code":"model_not_found","message":"model \'QWEN3.8-27B\' not found",'
    b'"param":"model","type":"invalid_request_error"}}'
)

CHAT_SSE = b"".join(
    (
        b'data: {"id":"chatcmpl-ninfer-stream","object":"chat.completion.chunk",'
        b'"model":"qwen3.8-27b","choices":[{"index":0,"delta":'
        b'{"role":"assistant"},"logprobs":null,"finish_reason":null}],"usage":null}\n\n',
        b'data: {"id":"chatcmpl-ninfer-stream","object":"chat.completion.chunk",'
        b'"model":"qwen3.8-27b","choices":[{"index":0,"delta":'
        b'{"reasoning_content":"Think."},"logprobs":null,"finish_reason":null}],'
        b'"usage":null}\n\n',
        b'data: {"id":"chatcmpl-ninfer-stream","object":"chat.completion.chunk",'
        b'"model":"qwen3.8-27b","choices":[{"index":0,"delta":{"content":"42"},'
        b'"logprobs":null,"finish_reason":null}],"usage":null}\n\n',
        b'data: {"id":"chatcmpl-ninfer-stream","object":"chat.completion.chunk",'
        b'"model":"qwen3.8-27b","choices":[{"index":0,"delta":{},"logprobs":null,'
        b'"finish_reason":"stop"}],"usage":null}\n\n',
        b'data: {"id":"chatcmpl-ninfer-stream","object":"chat.completion.chunk",'
        b'"model":"qwen3.8-27b","choices":[],"usage":{"prompt_tokens":12,'
        b'"completion_tokens":4,"total_tokens":16}}\n\n',
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
async def _proxied_upstream(path, upstream_handler, *, stream: bool = False):
    """Run an upstream and the real proxy path on loopback sockets."""
    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post(path, upstream_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    async def proxy_handler(request):
        body = await request.read()
        upstream_url = f"http://{upstream_server.host}:{upstream_server.port}"
        proxy = stream_proxy if stream else proxy_request
        return await proxy(request, upstream_url, body, timeout=10)

    proxy_app = aiohttp.web.Application()
    proxy_app.router.add_post(path, proxy_handler)
    proxy_server = TestServer(proxy_app)
    await proxy_server.start_server()
    try:
        async with TestClient(proxy_server) as client:
            yield client
    finally:
        await proxy_server.close()
        await upstream_server.close()


def test_example_has_foreground_ninfer_recipe_without_model_id():
    raw_config = yaml.safe_load(Path("config.example.yaml").read_text())

    command = raw_config["qwen3.8-27b"]

    assert "ninfer-serve" in command
    assert ".ninfer" in command
    assert "--host 127.0.0.1" in command
    assert "--port ${PORT}" in command
    assert "--model-id" not in command


def test_exact_embedded_ids_and_port_substitution_are_preserved(tmp_path):
    path = tmp_path / "ninfer.yaml"
    path.write_text(
        "\n".join(
            f'{model_id}: "ninfer-serve /models/{index}.ninfer '
            f'--host 127.0.0.1 --port ${{PORT}}"'
            for index, model_id in enumerate(NINFER_IDS)
        )
    )

    config = load_config(path)

    assert tuple(config.models) == NINFER_IDS
    for model_id, model in config.models.items():
        assert model.name == model_id
        assert 1 <= model.port <= 65535
        assert "${PORT}" not in model.cmd
        assert f"--port {model.port}" in model.cmd
        assert "--model-id" not in model.cmd


async def test_exact_embedded_ids_are_listed_by_both_local_endpoints(tmp_path):
    path = tmp_path / "ninfer.yaml"
    path.write_text(
        "\n".join(f'{model_id}: "echo --port ${{PORT}}"' for model_id in NINFER_IDS)
    )
    swapper = Swapper(load_config(path))

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
                assert tuple(item["id"] for item in payload["data"]) == NINFER_IDS
    finally:
        await server.close()
        await swapper.shutdown()


@pytest.mark.parametrize("model_id", NINFER_IDS)
async def test_selected_embedded_id_reaches_upstream_body_unchanged(tmp_path, model_id):
    received = []

    async def upstream_handler(request):
        received.append(await request.read())
        return aiohttp.web.Response(body=b'{"ok":true}', content_type="application/json")

    upstream_app = aiohttp.web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    path = tmp_path / "ninfer.yaml"
    path.write_text(f'{model_id}: "echo --port ${{PORT}}"\n')
    swapper = Swapper(load_config(path), idle_timeout=0)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    swapper._current = ProcessHandle(model_id, upstream_server.port, process)
    swapper._current_model = model_id
    swapper._healthy = True

    async def switcher_handler(request):
        return await swapper.handle_request(request)

    switcher_app = aiohttp.web.Application()
    switcher_app.router.add_route("*", "/{path:.*}", switcher_handler)
    switcher_server = TestServer(switcher_app)
    await switcher_server.start_server()
    request_body = (
        f'{{"model":"{model_id}","messages":[{{"role":"user","content":"hi"}}]}}'
    ).encode()
    try:
        async with TestClient(switcher_server) as client:
            response = await client.post(
                "/v1/chat/completions",
                data=request_body,
                headers={"Content-Type": "application/json"},
            )
            assert response.status == 200
            assert await response.read() == b'{"ok":true}'
        assert received == [request_body]
        assert json.loads(received[0])["model"] == model_id
    finally:
        await switcher_server.close()
        await swapper.shutdown()
        await upstream_server.close()


async def test_ninfer_chat_json_raw_bytes_and_fields_pass_through():
    async def upstream_handler(request):
        return aiohttp.web.Response(
            status=200,
            body=CHAT_RESPONSE,
            content_type="application/json",
            headers={"x-request-id": "req_ninfer_chat_001"},
        )

    async with _proxied_upstream("/v1/chat/completions", upstream_handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen3.8-27b", "messages": []},
        )
        raw = await response.read()

    payload = json.loads(raw)
    assert response.status == 200
    assert raw == CHAT_RESPONSE
    assert response.headers["x-request-id"] == "req_ninfer_chat_001"
    assert payload["model"] == "qwen3.8-27b"
    assert payload["choices"][0]["message"]["reasoning_content"] == "Count carefully."


async def test_ninfer_openai_error_status_body_fields_and_request_id_pass_through():
    async def upstream_handler(request):
        return aiohttp.web.Response(
            status=404,
            body=MODEL_NOT_FOUND_RESPONSE,
            content_type="application/json",
            headers={"x-request-id": "req_ninfer_error_001"},
        )

    async with _proxied_upstream("/v1/chat/completions", upstream_handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "QWEN3.8-27B", "messages": []},
        )
        raw = await response.read()

    payload = json.loads(raw)
    assert response.status == 404
    assert raw == MODEL_NOT_FOUND_RESPONSE
    assert response.headers["x-request-id"] == "req_ninfer_error_001"
    assert payload["error"] == {
        "code": "model_not_found",
        "message": "model 'QWEN3.8-27B' not found",
        "param": "model",
        "type": "invalid_request_error",
    }


async def test_ninfer_chat_sse_order_and_done_pass_through_exactly():
    async def upstream_handler(request):
        response = aiohttp.web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for frame in CHAT_SSE.splitlines(keepends=True):
            await response.write(frame)
        await response.write_eof()
        return response

    async with _proxied_upstream(
        "/v1/chat/completions", upstream_handler, stream=True
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen3.8-27b", "messages": [], "stream": True},
        )
        raw = await response.read()

    blocks = raw.decode().strip().split("\n\n")
    assert response.status == 200
    assert raw == CHAT_SSE
    assert json.loads(blocks[0][6:])["choices"][0]["delta"] == {"role": "assistant"}
    assert "reasoning_content" in blocks[1]
    assert "\"content\":\"42\"" in blocks[2]
    assert blocks[-1] == "data: [DONE]"


async def test_ninfer_responses_named_sse_order_terminal_and_no_done_pass_through():
    async def upstream_handler(request):
        response = aiohttp.web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(RESPONSES_SSE)
        await response.write_eof()
        return response

    async with _proxied_upstream("/v1/responses", upstream_handler, stream=True) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": "qwen3.8-27b", "input": "hello", "stream": True},
        )
        raw = await response.read()

    blocks = raw.decode().strip().split("\n\n")
    names = [block.splitlines()[0][7:] for block in blocks]
    payloads = [json.loads(block.splitlines()[1][6:]) for block in blocks]
    assert response.status == 200
    assert raw == RESPONSES_SSE
    assert names == [event for event, _ in _RESPONSE_EVENTS]
    assert [payload["type"] for payload in payloads] == names
    assert [payload["sequence_number"] for payload in payloads] == list(range(9))
    assert names[-1] == "response.completed"
    assert b"[DONE]" not in raw


@pytest.mark.parametrize(
    ("line", "expected"),
    (
        ("[2026-08-29 01:02:03.004] [info] ninfer-serve: ready", logging.INFO),
        ("[2026-08-29 01:02:03.004] [warning] ninfer-serve: queue full", logging.WARNING),
        ("[2026-08-29 01:02:03.004] [error] ninfer-serve: load failed", logging.ERROR),
        ("[2026-08-29 01:02:03.004] [WaRnInG] ninfer-serve: mixed case", logging.WARNING),
        ("[2026-08-29 01:02:03.004] [ERROR] ninfer-serve: upper case", logging.ERROR),
    ),
)
def test_ninfer_outer_log_marker_classifies_case_insensitively(line, expected):
    assert _upstream_log_level(line) == expected


@pytest.mark.parametrize(
    ("line", "level", "plain_upstream"),
    (
        ("[2026-08-29 01:02:03.004] [info] ninfer-serve: ready", logging.INFO, True),
        (
            "[2026-08-29 01:02:03.004] [warning] ninfer-serve: queue full",
            logging.WARNING,
            False,
        ),
        (
            "[2026-08-29 01:02:03.004] [error] ninfer-serve: load failed",
            logging.ERROR,
            False,
        ),
    ),
)
def test_ninfer_log_keeps_full_text_and_plain_upstream_policy(
    caplog, line, level, plain_upstream
):
    caplog.set_level(logging.INFO, logger="src.process_manager")

    _log_upstream_line(line)

    record = next(record for record in caplog.records if record.message == line)
    assert record.levelno == level
    assert getattr(record, "plain_upstream", False) is plain_upstream


def test_outer_info_request_error_words_are_not_promoted(caplog):
    line = (
        '[2026-08-29 01:02:03.004] [info] ninfer-serve: request_rejected '
        'error={"code":"model_not_found","message":"request rejected"}'
    )
    caplog.set_level(logging.INFO, logger="src.process_manager")

    _log_upstream_line(line)

    record = next(record for record in caplog.records if record.message == line)
    assert record.levelno == logging.INFO
    assert record.plain_upstream is True


@pytest.mark.parametrize(
    "line",
    (
        "[2026-08-29 01:02:03] [error] ninfer-serve: missing milliseconds",
        "[2026-08-29 01:02:03.004] [debug] ninfer-serve: error in message",
        "[2026-08-29 01:02:03.004] [error] another-server: not ninfer",
        "prefix [2026-08-29 01:02:03.004] [error] ninfer-serve: midline marker",
        "plain upstream text mentions error but has no outer marker",
    ),
)
def test_malformed_or_non_ninfer_lines_are_not_promoted(line):
    assert _upstream_log_level(line) == logging.INFO


def _ninfer_readme_section() -> str:
    readme = Path("README.md").read_text()
    return readme.split("## NInfer", 1)[1].split("\n## ", 1)[0]


def test_readme_documents_ninfer_health_and_shutdown_contract():
    section = _ninfer_readme_section()

    assert "/health" in section
    assert "warm-up" in section
    assert "SIGTERM" in section
    assert "forced-stop fallback" in section


def test_readme_documents_exact_ids_and_shared_id_constraint():
    section = _ninfer_readme_section()

    for model_id in NINFER_IDS:
        assert f"`{model_id}`" in section
    assert "groupwise and NVFP4" in section
    assert "share the same embedded ID" in section
    assert "would need the same config key" in section
    assert "does not rewrite model IDs" in section
