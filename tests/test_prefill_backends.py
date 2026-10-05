"""Prompt speed through the real proxy/swapper per backend (P-20260929-01).

Response shapes follow the upstream sources pinned in the plan record:
- llama.cpp 1c47294: ``usage.prompt_tokens_details.cached_tokens`` and a
  top-level ``timings`` object (``cache_n``, ``prompt_n``, ``prompt_ms``) on
  the non-stream body and the last stream chunk.
- tabbyAPI 816c321 / exllamav3 v1.5.2: ``usage.prompt_time`` and
  ``prompt_tokens_per_sec`` plus llama.cpp-style ``timings``.
- vLLM 6a6c295: ``prompt_tokens_details`` with ``--enable-prompt-tokens-
  details``; top-level ``metrics.time_to_first_token_ms`` on the final usage
  chunk with ``--enable-per-request-metrics``.
- SGLang 26d7539: ``prompt_tokens_details`` only with
  ``--enable-cache-report`` and omitted when nothing was cached; no prefill
  duration in the OpenAI response.
- Strata 6f32ec0 (P-20261005-01): usage (with ``cached_tokens``) and
  llama.cpp-style ``timings`` ride on the final chunk that also carries
  ``finish_reason``; ``*_per_second`` is ``null`` when its count is 0.
- TensorFold 609ca419 (P-20261005-04): no ``timings``; a top-level
  ``tensorfold`` object (MLX ``prefill_seconds``, CUDA ``prefill_s``, both
  seconds) on the non-stream body and on the stream's finish chunk; with
  ``include_usage`` the usage follows in its own ``choices: []`` chunk.

Upstreams are loopback fakes; delays make the proxy TTFT much larger than
the server's own prefill time so the two sources are distinguishable.
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


def _streaming_app(final_events, *, role_first=True, delay=DELAY_S):
    """Role chunk, delay, content chunk, then the given final events."""
    async def chat(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        if role_first:
            await resp.write(_sse(_chunk({"role": "assistant",
                                          "content": None})))
        await asyncio.sleep(delay)
        await resp.write(_sse(_chunk({"content": "Hi"})))
        for event in final_events:
            await resp.write(_sse(event))
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp
    return _make_app(chat)


def _json_app(body, *, delay=DELAY_S):
    async def chat(request):
        await asyncio.sleep(delay)
        return web.json_response(body)
    return _make_app(chat)


async def _run(monkeypatch, app, requests):
    """Send one request per stream flag; return the /stats/json snapshot."""
    async with _stack(monkeypatch, {MODEL: app}, seed=MODEL) as stack:
        async with TestClient(stack.proxy) as client:
            for stream in requests:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": MODEL, "stream": stream,
                          "messages": [{"role": "user", "content": "x"}]},
                )
                assert resp.status == 200
                await resp.read()
            data = await (await client.get("/stats/json")).json()
        stats = stack.swapper.stats._models[MODEL]
        assert stats.active_requests == {}
        assert stats.prefill_frontier == []
    return data


def _blocks(data):
    return (data["requests"], data["models"][MODEL]["requests"])


# ---------------------------------------------------------------------------
# llama.cpp
# ---------------------------------------------------------------------------

LLAMA_TIMINGS = {
    "cache_n": 236,
    "prompt_n": 16,
    "prompt_ms": 32.0,
    "prompt_per_token_ms": 2.0,
    "prompt_per_second": 500.0,
    "predicted_n": 3,
    "predicted_ms": 30.0,
    "predicted_per_token_ms": 10.0,
    "predicted_per_second": 100.0,
}
LLAMA_USAGE = {
    "completion_tokens": 3,
    "prompt_tokens": 252,
    "total_tokens": 255,
    "prompt_tokens_details": {"cached_tokens": 236},
}


async def test_llamacpp_stream_uses_server_timings(monkeypatch):
    final = [
        _chunk({}, finish_reason="stop"),
        _chunk(choices=False, usage=LLAMA_USAGE, timings=LLAMA_TIMINGS),
    ]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["avg"] == pytest.approx(16 / 0.032)
        assert prompt["avg_aggregate"]["tps"] == pytest.approx(16 / 0.032)
        assert prompt["server_timed_share"] == 1.0
        assert block["tokens"]["prefill"] == 16
        assert block["tokens"]["cached"] == 236
        # Proxy TTFT stays the user-visible latency (role chunk ignored).
        assert block["ttft_ms"]["avg"] >= DELAY_S * 1000 * 0.75


async def test_llamacpp_non_stream_body_with_timings(monkeypatch):
    body = {
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "Hi"}}],
        "usage": LLAMA_USAGE, "timings": LLAMA_TIMINGS,
    }
    data = await _run(monkeypatch, _json_app(body), [False])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["avg"] == pytest.approx(500.0)
        # [start, start + 32 ms]: the response took ~200 ms.
        assert prompt["avg_aggregate"] == {
            "tps": pytest.approx(500.0), "concurrency": 1.0,
        }
        assert block["tokens"]["prefill"] == 16
        assert block["ttft_ms"] is None


async def test_llamacpp_cached_only_in_timings_cache_n(monkeypatch):
    # No prompt_tokens_details and no usable prompt_ms: the cached count
    # comes from timings.cache_n and is subtracted (proxy TTFT fallback).
    usage = {"completion_tokens": 3, "prompt_tokens": 1000,
             "total_tokens": 1003}
    final = [_chunk(choices=False, usage=usage, timings={"cache_n": 900})]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        assert block["tokens"]["cached"] == 900
        assert block["tokens"]["prefill"] == 100
        prompt = block["rates"]["prompt_tps"]
        assert prompt["server_timed_share"] == 0.0
        assert prompt["skipped_unknown_cache"] == 0
        # 100 tokens over >= ~200 ms of TTFT, never the full 1000 prompt.
        assert prompt["avg"] <= 100 / (DELAY_S * 0.75)


# ---------------------------------------------------------------------------
# TabbyAPI / ExLlamaV3
# ---------------------------------------------------------------------------

TABBY_USAGE = {
    "prompt_tokens": 12,
    "prompt_tokens_details": {"cached_tokens": 4},
    "prompt_time": 0.02,
    "prompt_tokens_per_sec": 400.0,
    "completion_tokens": 3,
    "completion_time": 0.03,
    "completion_tokens_per_sec": 100.0,
    "total_tokens": 15,
    "total_time": 0.05,
}
TABBY_TIMINGS = {
    "cache_n": 4, "prompt_n": 8, "prompt_ms": 20.0,
    "prompt_per_token_ms": 2.5, "prompt_per_second": 400.0,
    "predicted_n": 3, "predicted_ms": 30.0,
    "predicted_per_token_ms": 10.0, "predicted_per_second": 100.0,
}


async def test_tabbyapi_prompt_speed_matches_server(monkeypatch):
    # Tabby's usage chunk keeps a non-empty choices list.
    final = [_chunk({}, usage=TABBY_USAGE, timings=TABBY_TIMINGS,
                    finish_reason="stop")]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        # prompt_n / prompt_ms = 8 / 20 ms, not (12 - 4) / proxy TTFT.
        assert prompt["avg"] == pytest.approx(400.0)
        assert prompt["avg_aggregate"]["tps"] == pytest.approx(400.0)
        assert prompt["server_timed_share"] == 1.0
        assert block["tokens"]["prefill"] == 8
        assert block["ttft_ms"]["count"] == 1
        # TTFT is ~10x the server's 20 ms prefill.
        assert block["ttft_ms"]["avg"] >= DELAY_S * 1000 * 0.75


async def test_tabbyapi_prompt_time_without_timings(monkeypatch):
    final = [_chunk({}, usage=TABBY_USAGE, finish_reason="stop")]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        # (12 - 4) / 0.02 s
        assert block["rates"]["prompt_tps"]["avg"] == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# vLLM
# ---------------------------------------------------------------------------

async def test_vllm_metrics_ttft_with_prompt_details(monkeypatch):
    usage = {"prompt_tokens": 1000, "completion_tokens": 3,
             "total_tokens": 1003,
             "prompt_tokens_details": {"cached_tokens": 200}}
    final = [
        _chunk({"content": ""}, finish_reason="stop"),
        _chunk(choices=False, usage=usage,
               metrics={"time_to_first_token_ms": 40.0}),
    ]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["avg"] == pytest.approx(800 / 0.04)
        assert prompt["avg_aggregate"]["tps"] == pytest.approx(800 / 0.04)
        assert prompt["server_timed_share"] == 1.0
        assert block["tokens"]["prefill"] == 800


async def test_vllm_without_prompt_details_is_skipped(monkeypatch):
    usage = {"prompt_tokens": 1000, "completion_tokens": 3,
             "total_tokens": 1003, "prompt_tokens_details": None}
    final = [_chunk(choices=False, usage=usage,
                    metrics={"time_to_first_token_ms": 40.0})]
    data = await _run(monkeypatch, _streaming_app(final), [True, True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["avg"] is None
        assert prompt["avg_aggregate"] is None
        assert prompt["server_timed_share"] is None
        assert prompt["skipped_unknown_cache"] == 2
        assert block["tokens"]["prefill"] == 0
        assert block["tokens"]["prompt"] == 2000
        assert block["ttft_ms"]["count"] == 2


# ---------------------------------------------------------------------------
# SGLang
# ---------------------------------------------------------------------------

def _sglang_app():
    """First request reports 100 cached; later ones omit details (0)."""
    served = {"n": 0}

    async def chat(request):
        served["n"] += 1
        usage = {"prompt_tokens": 300, "completion_tokens": 3,
                 "total_tokens": 303}
        if served["n"] == 1:
            usage["prompt_tokens_details"] = {"cached_tokens": 100}
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        await resp.write(_sse(_chunk({"role": "assistant", "content": ""})))
        await asyncio.sleep(DELAY_S)
        await resp.write(_sse(_chunk({"content": "Hi"})))
        await resp.write(_sse(_chunk(choices=False, usage=usage)))
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp
    return _make_app(chat)


async def test_sglang_learns_cache_reporting(monkeypatch):
    data = await _run(monkeypatch, _sglang_app(), [True, True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert block["tokens"]["prefill"] == 200 + 300
        assert block["tokens"]["cached"] == 100
        assert prompt["skipped_unknown_cache"] == 0
        assert prompt["server_timed_share"] == 0.0
        assert prompt["avg"] <= 500 / (2 * DELAY_S * 0.75)


async def test_sglang_never_reporting_model_is_skipped(monkeypatch):
    usage = {"prompt_tokens": 5000, "completion_tokens": 3,
             "total_tokens": 5003, "prompt_tokens_details": None}
    final = [_chunk(choices=False, usage=usage)]
    data = await _run(
        monkeypatch, _streaming_app(final, delay=0.01), [True],
    )
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["skipped_unknown_cache"] == 1
        assert prompt["avg"] is None
        assert prompt["avg_aggregate"] is None
        assert block["tokens"]["prefill"] == 0


# ---------------------------------------------------------------------------
# Strata
# ---------------------------------------------------------------------------

STRATA_USAGE = {
    "prompt_tokens": 300, "completion_tokens": 3, "total_tokens": 303,
    "prompt_tokens_details": {"cached_tokens": 256},
}
STRATA_TIMINGS = {
    "cache_n": 256, "prompt_n": 44, "prompt_ms": 88.0,
    "prompt_per_token_ms": 2.0, "prompt_per_second": 500.0,
    "predicted_n": 3, "predicted_ms": 30.0,
    "predicted_per_token_ms": 10.0, "predicted_per_second": 100.0,
}


async def test_strata_finish_chunk_timings(monkeypatch):
    final = [_chunk({}, usage=STRATA_USAGE, timings=STRATA_TIMINGS,
                    finish_reason="stop")]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        # prompt_n / prompt_ms = 44 / 88 ms, not 44 / proxy TTFT.
        assert prompt["avg"] == pytest.approx(500.0)
        assert prompt["server_timed_share"] == 1.0
        assert block["tokens"]["prefill"] == 44
        assert block["tokens"]["cached"] == 256
        assert block["ttft_ms"]["count"] == 1


async def test_strata_fully_cached_prompt_with_null_rates(monkeypatch):
    # prompt_n = prompt_tokens - cache_n = 0: Strata sends null rates.
    usage = {"prompt_tokens": 256, "completion_tokens": 3,
             "total_tokens": 259,
             "prompt_tokens_details": {"cached_tokens": 256}}
    timings = {
        "cache_n": 256, "prompt_n": 0, "prompt_ms": 0.0,
        "prompt_per_token_ms": None, "prompt_per_second": None,
        "predicted_n": 3, "predicted_ms": 30.0,
        "predicted_per_token_ms": 10.0, "predicted_per_second": 100.0,
    }
    final = [_chunk({}, usage=usage, timings=timings, finish_reason="stop")]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["avg"] is None
        assert prompt["skipped_unknown_cache"] == 0
        assert block["tokens"]["prefill"] == 0
        assert block["tokens"]["cached"] == 256
        assert block["tokens"]["prompt"] == 256


# ---------------------------------------------------------------------------
# TensorFold
# ---------------------------------------------------------------------------

TF_USAGE = {
    "prompt_tokens": 300, "completion_tokens": 3, "total_tokens": 303,
    "prompt_tokens_details": {"cached_tokens": 256},
    "completion_tokens_details": {"reasoning_tokens": 0},
}
# MLX ``runtime`` block (server/app.py); seconds, not milliseconds.
TF_MLX = {
    "engine": "mlx", "tokens_per_second": 100.0, "seconds": 0.12,
    "prefill_seconds": 0.088, "time_to_first_token": 0.09,
    "drafts": True, "token_sha": "abc",
}
# CUDA engine stats (cuda/streams.py ``Stream.stats``).
TF_CUDA = {
    "prefill_s": 0.088, "decode_s": 0.03, "rounds": 2, "drafts": True,
    "cached": 256, "min_rows": 2, "drafted": 2, "accepted": 1,
    "token_sha": "abc",
}


def _tf_stream(stats):
    """MLX/CUDA with include_usage: stats on the finish chunk, then usage."""
    return [
        _chunk({}, finish_reason="stop", tensorfold=stats),
        _chunk(choices=False, usage=TF_USAGE),
    ]


@pytest.mark.parametrize("stats", [TF_MLX, TF_CUDA], ids=["mlx", "cuda"])
async def test_tensorfold_stream_uses_server_prefill(monkeypatch, stats):
    data = await _run(monkeypatch, _streaming_app(_tf_stream(stats)), [True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        # (300 - 256) / 0.088 s, not 44 / proxy TTFT (~0.2 s).
        assert prompt["avg"] == pytest.approx(500.0)
        assert prompt["server_timed_share"] == 1.0
        assert block["tokens"]["prefill"] == 44
        assert block["tokens"]["cached"] == 256
        assert block["ttft_ms"]["count"] == 1


@pytest.mark.parametrize("stats", [TF_MLX, TF_CUDA], ids=["mlx", "cuda"])
async def test_tensorfold_non_stream_uses_server_prefill(monkeypatch, stats):
    body = {
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant",
                                             "content": "Hi"},
                     "finish_reason": "stop"}],
        "usage": TF_USAGE, "exact_mode": "target-verified",
        "tensorfold": stats,
    }
    data = await _run(monkeypatch, _json_app(body, delay=0.01), [False])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["avg"] == pytest.approx(500.0)
        assert prompt["server_timed_share"] == 1.0
        assert block["tokens"]["prefill"] == 44


async def test_tensorfold_timings_take_priority(monkeypatch):
    # Not sent by TensorFold; pins the order if both ever appear.
    final = [
        _chunk({}, finish_reason="stop", tensorfold=TF_MLX,
               timings={"prompt_n": 40, "prompt_ms": 20.0}),
        _chunk(choices=False, usage=TF_USAGE),
    ]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        assert block["rates"]["prompt_tps"]["avg"] == pytest.approx(2000.0)
        assert block["tokens"]["prefill"] == 40


@pytest.mark.parametrize(
    "stats",
    [
        {"prefill_seconds": None},  # MLX: no prefill timestamp
        {"prefill_seconds": 0.0},
        {"prefill_s": 0.0},
        {"prefill_seconds": -1.0},
        {"prefill_seconds": "0.088"},
        {"prefill_seconds": True},
        {"seconds": 0.12, "batch_size": 2},  # MLX batched reply
        "not a dict",
    ],
)
async def test_tensorfold_invalid_prefill_falls_back_to_proxy(
    monkeypatch, stats,
):
    data = await _run(monkeypatch, _streaming_app(_tf_stream(stats)), [True])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["server_timed_share"] == 0.0
        # 44 tokens over >= ~200 ms of proxy TTFT.
        assert prompt["avg"] <= 44 / (DELAY_S * 0.75)
        assert block["tokens"]["prefill"] == 44


async def test_tensorfold_zero_prefill_seconds_uses_prefill_s(monkeypatch):
    stats = {"prefill_seconds": 0.0, "prefill_s": 0.088}
    data = await _run(monkeypatch, _streaming_app(_tf_stream(stats)), [True])
    for block in _blocks(data):
        assert block["rates"]["prompt_tps"]["avg"] == pytest.approx(500.0)


async def test_tensorfold_unknown_cache_follows_cache_learned(monkeypatch):
    # First reply reports cached tokens; the second omits the details, so
    # its cached count counts as 0 (prompt_time rule), not skipped.
    served = {"n": 0}

    async def chat(request):
        served["n"] += 1
        usage = {"prompt_tokens": 300, "completion_tokens": 3}
        if served["n"] == 1:
            usage["prompt_tokens_details"] = {"cached_tokens": 256}
        return web.json_response({
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "Hi"}}],
            "usage": usage, "tensorfold": {"prefill_seconds": 0.1},
        })

    data = await _run(monkeypatch, _make_app(chat), [False, False])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["skipped_unknown_cache"] == 0
        assert block["tokens"]["prefill"] == 44 + 300
        assert prompt["server_timed_share"] == 1.0


async def test_tensorfold_never_reporting_cache_is_skipped(monkeypatch):
    body = {
        "choices": [{"index": 0, "message": {"role": "assistant",
                                             "content": "Hi"}}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 3},
        "tensorfold": {"prefill_seconds": 0.1},
    }
    data = await _run(monkeypatch, _json_app(body, delay=0.01), [False])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["skipped_unknown_cache"] == 1
        assert prompt["avg"] is None
        assert block["tokens"]["prefill"] == 0


# ---------------------------------------------------------------------------
# First-token rule and non-streaming without server timing
# ---------------------------------------------------------------------------

async def test_role_only_first_chunk_does_not_end_ttft(monkeypatch):
    usage = {"prompt_tokens": 10, "completion_tokens": 3,
             "prompt_tokens_details": {"cached_tokens": 0}}
    final = [_chunk(choices=False, usage=usage)]
    data = await _run(monkeypatch, _streaming_app(final), [True])
    for block in _blocks(data):
        assert block["ttft_ms"]["count"] == 1
        assert block["ttft_ms"]["avg"] >= DELAY_S * 1000 * 0.75
        assert block["rates"]["prompt_tps"]["avg"] <= 10 / (DELAY_S * 0.75)


async def test_non_stream_without_server_timing_does_not_contribute(
    monkeypatch,
):
    body = {
        "choices": [{"index": 0, "message": {"role": "assistant",
                                             "content": "Hi"}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 3,
                  "prompt_tokens_details": {"cached_tokens": 10}},
    }
    data = await _run(monkeypatch, _json_app(body, delay=0.01), [False])
    for block in _blocks(data):
        prompt = block["rates"]["prompt_tps"]
        assert prompt["avg"] is None
        assert prompt["avg_aggregate"] is None
        assert prompt["skipped_unknown_cache"] == 0
        assert block["tokens"]["prefill"] == 0
        assert block["tokens"]["prompt"] == 50
