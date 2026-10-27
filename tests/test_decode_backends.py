"""Generation speed through the real proxy/swapper per backend (P-20260929-03).

Response shapes follow the upstream sources pinned in the plan record:
- llama.cpp 1c47294: top-level ``timings`` (``predicted_n``,
  ``predicted_ms``) on the non-stream body and the last stream chunk.
- tabbyAPI 816c321 / exllamav3 v1.5.2: ``usage.completion_time`` plus
  llama.cpp-style ``timings``. With tools, the tool call is held back and
  emitted whole in the final (finish) chunk of one generation pass.
- vLLM 6a6c295: top-level ``metrics.generation_time_ms`` on the final usage
  chunk (and the non-stream body) with ``--enable-per-request-metrics``.
- SGLang 26d7539: no decode duration in the OpenAI response.
- Strata 6f32ec0 (P-20261005-01): llama.cpp-style ``timings`` with usage on
  the final chunk that also carries ``finish_reason`` (and on the non-stream
  body); ``*_per_second`` is ``null`` when its count is 0.
- Splash 39a9738 (P-20261027-01): llama.cpp-style ``timings`` on the
  stream's finish chunk, but ``predicted_ms`` starts after the first
  emission while ``predicted_n`` counts it; ``metrics.decode.tokens``
  leaves it out and comes with usage in the later ``include_usage`` chunk
  (``server/metrics.py``, ``server/server.py``).

Upstreams are loopback fakes; delays make the proxy's own decode time
clearly different from the server's so the two sources are distinguishable.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from tests.stats_helpers import _make_app, _stack

MODEL = "model-a"
DELAY_S = 0.2


def _sse(event) -> bytes:
    return b"data: " + json.dumps(event).encode() + b"\n\n"


def _chunk(delta=None, *, usage=None, choices=True, finish_reason=None,
           **extra):
    event = {
        "id": "chatcmpl-1", "object": "chat.completion.chunk",
        "created": 0, "model": MODEL,
        "choices": (
            [{"index": 0, "delta": delta or {},
              "finish_reason": finish_reason}]
            if choices else []
        ),
    }
    if usage is not None:
        event["usage"] = usage
    event.update(extra)
    return event


def _completion(usage, **extra):
    body = {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "Hi"}}],
        "usage": usage,
    }
    body.update(extra)
    return body


def _stream_app(events):
    """Write each (delay_before, event) pair, then [DONE]."""
    async def chat(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        for delay, event in events:
            if delay:
                await asyncio.sleep(delay)
            await resp.write(_sse(event))
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp
    return chat


def _json_app(body, *, delay=DELAY_S):
    async def chat(request):
        await asyncio.sleep(delay)
        return web.json_response(body)
    return chat


def _router(stream_handler=None, json_handler=None):
    """One upstream serving streaming and non-streaming requests."""
    async def chat(request):
        body = await request.json()
        handler = stream_handler if body.get("stream") else json_handler
        return await handler(request)
    return _make_app(chat)


async def _post(client, stream):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "stream": stream,
              "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status == 200
    await resp.read()


async def _run(monkeypatch, app, requests, *, concurrent=False):
    """Send one request per stream flag; return (snapshot, model stats)."""
    async with _stack(monkeypatch, {MODEL: app}, seed=MODEL) as stack:
        async with TestClient(stack.proxy) as client:
            if concurrent:
                await asyncio.gather(
                    *(_post(client, stream) for stream in requests)
                )
            else:
                for stream in requests:
                    await _post(client, stream)
            data = await (await client.get("/stats/json")).json()
        stats = stack.swapper.stats._models[MODEL]
        assert stats.active_requests == {}
        assert stats.active_decodes == {}
        assert stats.decode_frontier == []
    return data, stats


def _blocks(data):
    return (data["requests"], data["models"][MODEL]["requests"])


# ---------------------------------------------------------------------------
# llama.cpp / TabbyAPI non-streaming (AC1)
# ---------------------------------------------------------------------------

LLAMA_TIMINGS = {
    "cache_n": 0, "prompt_n": 16, "prompt_ms": 32.0,
    "predicted_n": 3, "predicted_ms": 30.0,
    "predicted_per_token_ms": 10.0, "predicted_per_second": 100.0,
}
LLAMA_USAGE = {"completion_tokens": 3, "prompt_tokens": 16,
               "total_tokens": 19,
               "prompt_tokens_details": {"cached_tokens": 0}}

TABBY_USAGE = {
    "prompt_tokens": 12,
    "prompt_tokens_details": {"cached_tokens": 4},
    "prompt_time": 0.02,
    "completion_tokens": 3,
    "completion_time": 0.06,
    "completion_tokens_per_sec": 50.0,
    "total_tokens": 15,
    "total_time": 0.08,
}
TABBY_TIMINGS = {
    "cache_n": 4, "prompt_n": 8, "prompt_ms": 20.0,
    "predicted_n": 3, "predicted_ms": 60.0,
}


async def test_llamacpp_non_stream_timings_count(monkeypatch):
    app = _router(json_handler=_json_app(
        _completion(LLAMA_USAGE, timings=LLAMA_TIMINGS),
    ))
    data, _ = await _run(monkeypatch, app, [False])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        assert gen["avg"] == pytest.approx(100.0)
        assert gen["server_timed_share"] == 1.0
        assert gen["skipped_buffered"] == 0
        # Interval [end - 30 ms, end]: aggregate equals the server rate.
        assert gen["avg_aggregate"]["tps"] == pytest.approx(100.0)
        assert block["tpot_ms"]["avg"] == pytest.approx(30.0 / 2)
        assert block["ttft_ms"] is None
    maximum = data["models"][MODEL]["requests"]["rates"]["generation_tps"][
        "max_aggregate"
    ]
    assert maximum == {"tps": pytest.approx(100.0), "concurrency": 1}


@pytest.mark.parametrize("timings, expected", [
    (TABBY_TIMINGS, 3 / 0.06),  # timings.predicted_n / predicted_ms
    (None, 3 / 0.06),  # usage.completion_time alone
])
async def test_tabbyapi_non_stream_counts(monkeypatch, timings, expected):
    extra = {"timings": timings} if timings else {}
    app = _router(json_handler=_json_app(_completion(TABBY_USAGE, **extra)))
    data, _ = await _run(monkeypatch, app, [False])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        assert gen["avg"] == pytest.approx(expected)
        assert gen["avg_aggregate"]["tps"] == pytest.approx(expected)
        assert gen["server_timed_share"] == 1.0


# ---------------------------------------------------------------------------
# TabbyAPI buffered tool-call stream (AC2, AC3, AC6)
# ---------------------------------------------------------------------------

TOOL_CALL = [{
    "index": 0, "id": "call_1", "type": "function",
    "function": {"name": "get_weather", "arguments": "{\"city\": \"Oslo\"}"},
}]
TOOL_USAGE = {
    "prompt_tokens": 40, "prompt_tokens_details": {"cached_tokens": 0},
    "prompt_time": 0.01, "completion_tokens": 20,
    "completion_time": 0.1, "total_tokens": 60,
}
TOOL_TIMINGS = {
    "cache_n": 0, "prompt_n": 40, "prompt_ms": 10.0,
    "predicted_n": 20, "predicted_ms": 100.0,
}


def _tool_call_stream(*, usage, timings):
    """Role/empty chunks, generation time, then one tool-call burst."""
    extra = {"timings": timings} if timings else {}
    return _stream_app([
        (0, _chunk({"role": "assistant", "content": ""})),
        (0, _chunk({})),
        (DELAY_S, _chunk({"tool_calls": TOOL_CALL},
                         finish_reason="tool_calls")),
        (0, _chunk({}, usage=usage, **extra)),
    ])


async def test_tabbyapi_buffered_tool_call_uses_server_rate(monkeypatch):
    app = _router(stream_handler=_tool_call_stream(
        usage=TOOL_USAGE, timings=TOOL_TIMINGS,
    ))
    data, stats = await _run(monkeypatch, app, [True])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        # 20 tokens / 100 ms, not 20 tokens / (few ms between two writes).
        assert gen["avg"] == pytest.approx(200.0)
        assert gen["avg_aggregate"]["tps"] == pytest.approx(200.0)
        assert gen["server_timed_share"] == 1.0
        assert gen["skipped_buffered"] == 0
        assert block["tpot_ms"]["avg"] == pytest.approx(100.0 / 19)
        assert block["ttft_ms"]["count"] == 1
    maximum = data["models"][MODEL]["requests"]["rates"]["generation_tps"][
        "max_aggregate"
    ]
    assert maximum["tps"] == pytest.approx(200.0)
    # One chunk carried all output: not a calibration sample.
    assert list(stats.gen_calibration) == []


async def test_tabbyapi_buffered_tool_call_without_timing_is_skipped(
    monkeypatch,
):
    usage = {"prompt_tokens": 40, "completion_tokens": 20,
             "total_tokens": 60,
             "prompt_tokens_details": {"cached_tokens": 0}}
    app = _router(stream_handler=_tool_call_stream(usage=usage, timings=None))
    data, stats = await _run(monkeypatch, app, [True])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        assert gen["avg"] is None
        assert gen["avg_aggregate"] is None
        assert gen["server_timed_share"] is None
        assert gen["skipped_buffered"] == 1
        assert block["tpot_ms"] is None
        # TTFT and prompt speed still come from the proxy (AC6).
        assert block["ttft_ms"]["count"] == 1
        assert block["ttft_ms"]["avg"] >= DELAY_S * 1000 * 0.75
        assert block["rates"]["prompt_tps"]["avg"] is not None
        assert block["tokens"]["completion"] == 20
    row = data["models"][MODEL]["requests"]["rates"]["generation_tps"]
    assert row["max_aggregate"] is None
    assert row["live"]["tokens_per_chunk"] == 1.0  # no calibration sample
    assert list(stats.gen_calibration) == []


# ---------------------------------------------------------------------------
# vLLM (AC4)
# ---------------------------------------------------------------------------

VLLM_USAGE = {"prompt_tokens": 50, "completion_tokens": 10,
              "total_tokens": 60}
VLLM_METRICS = {"time_to_first_token_ms": 5.0, "generation_time_ms": 50.0}


async def test_vllm_stream_uses_generation_time(monkeypatch):
    app = _router(stream_handler=_stream_app([
        (0, _chunk({"role": "assistant", "content": ""})),
        (0, _chunk({"content": "Hel"})),
        (DELAY_S, _chunk({"content": "lo"}, finish_reason="stop")),
        (0, _chunk(choices=False, usage=VLLM_USAGE, metrics=VLLM_METRICS)),
    ]))
    data, stats = await _run(monkeypatch, app, [True])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        # 10 tokens / 50 ms, not 10 / ~200 ms of proxy decode time.
        assert gen["avg"] == pytest.approx(200.0)
        assert gen["server_timed_share"] == 1.0
    # A real two-chunk stream still calibrates the live rate.
    assert list(stats.gen_calibration) == [(10, 2)]


async def test_vllm_non_stream_uses_generation_time(monkeypatch):
    app = _router(json_handler=_json_app(
        _completion(VLLM_USAGE, metrics=VLLM_METRICS),
    ))
    data, _ = await _run(monkeypatch, app, [False])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        assert gen["avg"] == pytest.approx(200.0)
        assert gen["avg_aggregate"]["tps"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# SGLang (no server decode time)
# ---------------------------------------------------------------------------

SGLANG_USAGE = {"prompt_tokens": 30, "completion_tokens": 10,
                "total_tokens": 40}


def _sglang_stream(step=0.05, chunks=5):
    events = [(0, _chunk({"role": "assistant", "content": ""}))]
    events += [(0 if i == 0 else step, _chunk({"content": f"t{i}"}))
               for i in range(chunks)]
    events.append((0, _chunk(choices=False, usage=SGLANG_USAGE)))
    return _stream_app(events)


async def test_sglang_stream_keeps_proxy_timing(monkeypatch):
    app = _router(stream_handler=_sglang_stream())
    data, stats = await _run(monkeypatch, app, [True])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        # 10 tokens over >= 4 x 50 ms of streamed output.
        assert 0 < gen["avg"] <= 10 / (4 * 0.05 * 0.75)
        assert gen["server_timed_share"] == 0.0
        assert gen["skipped_buffered"] == 0
        assert gen["avg_aggregate"]["tps"] == pytest.approx(gen["avg"])
    assert list(stats.gen_calibration) == [(10, 5)]


async def test_sglang_non_stream_is_not_timed(monkeypatch):
    app = _router(json_handler=_json_app(_completion(SGLANG_USAGE),
                                         delay=0.01))
    data, _ = await _run(monkeypatch, app, [False])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        assert gen["avg"] is None
        assert gen["avg_aggregate"] is None
        assert gen["skipped_buffered"] == 0
        assert block["tokens"]["completion"] == 10


# ---------------------------------------------------------------------------
# Strata
# ---------------------------------------------------------------------------

STRATA_USAGE = {"prompt_tokens": 20, "completion_tokens": 10,
                "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 0}}
STRATA_TIMINGS = {
    "cache_n": 0, "prompt_n": 20, "prompt_ms": 10.0,
    "prompt_per_token_ms": 0.5, "prompt_per_second": 2000.0,
    "predicted_n": 10, "predicted_ms": 50.0,
    "predicted_per_token_ms": 5.0, "predicted_per_second": 200.0,
}


async def test_strata_stream_finish_chunk_timings(monkeypatch):
    app = _router(stream_handler=_stream_app([
        (0, _chunk({"role": "assistant", "content": ""})),
        (0, _chunk({"reasoning_content": "Think"})),
        (DELAY_S, _chunk({"content": "Hi"})),
        (0, _chunk({}, usage=STRATA_USAGE, timings=STRATA_TIMINGS,
                   finish_reason="stop")),
    ]))
    data, stats = await _run(monkeypatch, app, [True])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        # 10 tokens / 50 ms, not 10 / ~200 ms of proxy decode time.
        assert gen["avg"] == pytest.approx(200.0)
        assert gen["server_timed_share"] == 1.0
        assert gen["skipped_buffered"] == 0
        assert block["tokens"]["completion"] == 10
    # reasoning_content and content are two output chunks.
    assert list(stats.gen_calibration) == [(10, 2)]


async def test_strata_non_stream_body_timings(monkeypatch):
    app = _router(json_handler=_json_app(
        _completion(STRATA_USAGE, timings=STRATA_TIMINGS),
    ))
    data, _ = await _run(monkeypatch, app, [False])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        assert gen["avg"] == pytest.approx(200.0)
        assert gen["avg_aggregate"]["tps"] == pytest.approx(200.0)


async def test_strata_zero_output_null_rates_are_not_a_sample(monkeypatch):
    usage = {"prompt_tokens": 20, "completion_tokens": 0,
             "total_tokens": 20,
             "prompt_tokens_details": {"cached_tokens": 0}}
    timings = {
        "cache_n": 0, "prompt_n": 20, "prompt_ms": 10.0,
        "prompt_per_token_ms": 0.5, "prompt_per_second": 2000.0,
        "predicted_n": 0, "predicted_ms": 0.0,
        "predicted_per_token_ms": None, "predicted_per_second": None,
    }
    app = _router(json_handler=_json_app(
        _completion(usage, timings=timings), delay=0.01,
    ))
    data, _ = await _run(monkeypatch, app, [False])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        assert gen["avg"] is None
        assert gen["avg_aggregate"] is None
        assert block["rates"]["prompt_tps"]["avg"] == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# Splash
# ---------------------------------------------------------------------------

SPLASH_USAGE = {
    "prompt_tokens": 300, "completion_tokens": 10, "total_tokens": 310,
    "prompt_tokens_details": {"cached_tokens": 256},
    "completion_tokens_details": {"reasoning_tokens": 0},
}
# predicted_n counts all 10 tokens; predicted_ms starts after the first
# emission (here a speculative block of 2), so Splash's own rate is 8 / 40 ms.
SPLASH_TIMINGS = {
    "prompt_n": 300, "prompt_ms": 88.0, "prompt_per_second": 500.0,
    "predicted_n": 10, "predicted_ms": 40.0, "predicted_per_second": 200.0,
    "cache_n": 256,
}
SPLASH_METRICS = {
    "prefill": {"tokens": 44},
    "decode": {"tokens": 8},
    "request_latency": {
        "start_to_first_token_ms": 88.0, "first_token_to_done_ms": 40.0,
        "wall_ms": 130.0, "ttft_ms": 90.0, "queue_to_start_ms": 2.0,
        "stream_tokens_per_second": 200.0,
    },
    "cache": {"status": "hit", "matched_tokens": 256,
              "deduplicated_tokens": 0, "lane": 0},
}


async def test_splash_stream_uses_decode_tokens(monkeypatch):
    app = _router(stream_handler=_stream_app([
        (0, _chunk({"role": "assistant", "content": ""})),
        (0, _chunk({"content": "Hi"})),
        (DELAY_S, _chunk({"content": " there"})),
        (0, _chunk({}, finish_reason="stop", timings=SPLASH_TIMINGS)),
        (0, _chunk(choices=False, usage=SPLASH_USAGE,
                   metrics=SPLASH_METRICS)),
    ]))
    data, _ = await _run(monkeypatch, app, [True])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        # 8 decode tokens / 40 ms, not 10 / 40 ms or the proxy's ~200 ms.
        assert gen["avg"] == pytest.approx(200.0)
        assert gen["server_timed_share"] == 1.0
        assert block["tokens"]["completion"] == 10


async def test_splash_non_stream_body_uses_decode_tokens(monkeypatch):
    app = _router(json_handler=_json_app(_completion(
        SPLASH_USAGE, timings=SPLASH_TIMINGS, metrics=SPLASH_METRICS,
    ), delay=0.01))
    data, _ = await _run(monkeypatch, app, [False])
    for block in _blocks(data):
        gen = block["rates"]["generation_tps"]
        assert gen["avg"] == pytest.approx(200.0)
        assert block["tokens"]["completion"] == 10


def test_splash_decode_tokens_combine_across_events():
    from src.decode import ServerDecode, apply_server_decode, select_decode

    server = ServerDecode()
    apply_server_decode({"timings": SPLASH_TIMINGS}, server)
    assert select_decode(10, server, None).sample.tokens == 10
    apply_server_decode({"metrics": SPLASH_METRICS}, server)
    sample = select_decode(10, server, None).sample
    assert (sample.tokens, sample.seconds, sample.server_timed) == (
        8, 0.04, True
    )


@pytest.mark.parametrize("tokens", [None, -1, 2.5, True, "8"])
def test_splash_invalid_decode_tokens_are_ignored(tokens):
    from src.decode import ServerDecode, apply_server_decode, select_decode

    server = ServerDecode()
    apply_server_decode({"timings": SPLASH_TIMINGS,
                         "metrics": {"decode": {"tokens": tokens}}}, server)
    assert server.decode_tokens is None
    assert select_decode(10, server, None).sample.tokens == 10


def test_decode_tokens_without_timings_leave_vllm_rule():
    from src.decode import ServerDecode, apply_server_decode, select_decode

    server = ServerDecode()
    apply_server_decode({"metrics": {"generation_time_ms": 100.0,
                                     "decode": {"tokens": 3}}}, server)
    sample = select_decode(20, server, None).sample
    # vLLM rule: completion tokens over generation_time_ms.
    assert (sample.tokens, sample.seconds) == (20, 0.1)


def test_splash_single_emission_falls_through_to_proxy_rule():
    from src.decode import ServerDecode, apply_server_decode, select_decode

    # The whole reply came in the first emission: no decode interval.
    server = ServerDecode()
    apply_server_decode({
        "timings": {"predicted_n": 2, "predicted_ms": 0.0},
        "metrics": {"decode": {"tokens": 0}},
    }, server)
    decision = select_decode(2, server, 0.001, gen_chunks=1)
    assert decision.sample is None and decision.skipped_buffered


# ---------------------------------------------------------------------------
# Non-streaming overlapping a stream (AC5)
# ---------------------------------------------------------------------------

async def test_non_stream_overlapping_stream_joins_aggregate(monkeypatch):
    # Stream: 5 chunks 100 ms apart (~0.4 s of decode), proxy-timed.
    # Non-stream: returns after 0.3 s, server-timed 200 ms of decode, so
    # its interval [end - 0.2, end] lies inside the stream's decode.
    usage = {"prompt_tokens": 16, "completion_tokens": 30,
             "total_tokens": 46,
             "prompt_tokens_details": {"cached_tokens": 0}}
    timings = {"cache_n": 0, "prompt_n": 16, "prompt_ms": 5.0,
               "predicted_n": 30, "predicted_ms": 200.0}
    app = _router(
        stream_handler=_sglang_stream(step=0.1),
        json_handler=_json_app(_completion(usage, timings=timings),
                               delay=0.3),
    )
    data, stats = await _run(monkeypatch, app, [True, False],
                             concurrent=True)
    totals = stats.decode_aggregate
    # Both requests' tokens are in one busy group (before: stream only).
    assert totals.tokens == 10 + 30
    assert totals.stream_time > totals.busy_time  # overlap: concurrency > 1
    stream_only = 10 / totals.busy_time
    for block in _blocks(data):
        aggregate = block["rates"]["generation_tps"]["avg_aggregate"]
        assert aggregate["tps"] == pytest.approx(40 / totals.busy_time)
        assert aggregate["tps"] > 3 * stream_only
        assert aggregate["concurrency"] > 1
    maximum = data["models"][MODEL]["requests"]["rates"]["generation_tps"][
        "max_aggregate"
    ]
    assert maximum["concurrency"] == 2
    assert maximum["tps"] == pytest.approx(40 / totals.busy_time)
