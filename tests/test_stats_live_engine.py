"""Live generation rate from the engine counter — P-20261114-01.

With engine counter samples (src/engine_metrics.py) in the 5 s window,
``rates.generation_tps.live`` is the counter increase between the oldest
and newest in-window samples divided by busy decode time in that span
(floored at 1 s), with ``source: "engine"`` and ``estimate: false``. With
fewer than two samples it falls back to the chunk estimate. It is 0 while
no decode is active. Global uses the current model's engine rate when no
other model is decoding.
"""

from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from src.stats import StatsCollector

from tests.stats_helpers import _make_app, _stack


class _FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr("src.stats.time.monotonic", fake)
    return fake


def _live(snap: dict, model: str | None = None) -> dict:
    scope = snap["requests"] if model is None else (
        snap["models"][model]["requests"]
    )
    return scope["rates"]["generation_tps"]["live"]


class TestEngineRateCollector:
    def test_a2_silent_stream_counts_engine_tokens(self, clock):
        # Regression (main-02, request 3222): first token, then ~25 s with
        # no streamed chunks (buffered tool call) while the engine keeps
        # generating at 100 t/s. The chunk estimate read ~0; the engine
        # counter shows the real speed for the whole silent span.
        collector = StatsCollector()
        collector.record_decode_start("m", "r", clock.now)
        collector.record_gen_chunks("m", 1)
        total = 5000.0
        for _ in range(25):
            collector.record_engine_tokens("m", total)
            clock.now += 1.0
            total += 100.0
        collector.record_engine_tokens("m", total)

        snap = collector.snapshot(current_model="m")
        live = _live(snap, "m")
        assert live["source"] == "engine"
        assert live["estimate"] is False
        assert live["window_s"] == 5
        assert live["tps"] == pytest.approx(100.0)
        assert _live(snap) == {
            "tps": pytest.approx(100.0), "window_s": 5, "estimate": False,
            "source": "engine", "busy_s": pytest.approx(5.0),
            "tokens_per_chunk": None,
        }

    def test_engine_rate_divides_by_busy_time_in_sampled_span(self, clock):
        # Samples every second over [1000, 1004]; a decode ran [1000, 1002]
        # and the next started at 1003, so 3 s of the 4 s span were busy.
        collector = StatsCollector()
        start = clock.now
        collector.record_decode_start("m", "a", start)
        values = [0, 50, 100, 100, 150]
        for i, value in enumerate(values):
            clock.now = start + i
            if i == 2:
                collector.record_decode_end(
                    "m", "a", ended_at=clock.now, ok=True,
                    completion_tokens=None,
                )
            if i == 3:
                collector.record_decode_start("m", "b", clock.now)
            collector.record_engine_tokens("m", value)

        live = _live(collector.snapshot(), "m")
        assert live["source"] == "engine"
        assert live["tps"] == pytest.approx(150 / 3)

    def test_span_uses_only_samples_inside_window(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        for value in (0, 1000, 1010, 1020, 1030, 1040, 1050, 1060):
            collector.record_engine_tokens("m", value)
            clock.now += 1.0
        clock.now -= 1.0  # last sample at "now"
        # Window [1002, 1007]: samples 1010 .. 1060 -> 50 tokens over 5 s.
        live = _live(collector.snapshot(), "m")
        assert live["tps"] == pytest.approx(10.0)
        assert len(collector._models["m"].engine_samples) == 6

    def test_counter_reset_discards_negative_delta(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        for value in (900, 1000, 20, 70):  # engine restarted after 1000
            collector.record_engine_tokens("m", value)
            clock.now += 1.0
        clock.now -= 1.0
        live = _live(collector.snapshot(), "m")
        assert live["source"] == "engine"
        assert live["tps"] == pytest.approx((100 + 50) / 3)

    def test_fewer_than_two_samples_falls_back_to_estimate(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        collector.record_gen_chunks("m", 30)
        collector.record_engine_tokens("m", 500)
        clock.now += 2.0

        snap = collector.snapshot(current_model="m")
        assert _live(snap, "m")["source"] == "estimate"
        assert _live(snap, "m")["estimate"] is True
        assert _live(snap, "m")["tps"] == pytest.approx(15.0)
        assert _live(snap)["source"] == "estimate"
        assert _live(snap)["tps"] == pytest.approx(15.0)

    def test_stale_samples_fall_back_to_estimate(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        collector.record_engine_tokens("m", 0)
        clock.now += 1.0
        collector.record_engine_tokens("m", 100)
        clock.now += 6.0  # both samples left the window
        live = _live(collector.snapshot(), "m")
        assert live["source"] == "estimate"
        assert len(collector._models["m"].engine_samples) == 0

    def test_a4_zero_when_no_decode_active(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        for value in (0, 100, 200):
            collector.record_engine_tokens("m", value)
            clock.now += 1.0
        collector.record_decode_end(
            "m", "a", ended_at=clock.now, ok=True, completion_tokens=None,
        )
        snap = collector.snapshot(current_model="m")
        assert _live(snap, "m")["tps"] == 0.0
        assert _live(snap, "m")["source"] == "engine"
        assert _live(snap)["tps"] == 0.0

    def test_a4_zero_while_only_prefill_runs(self, clock):
        # A request is in flight but has no first token yet: nothing is
        # generating, so the rate is 0 even with fresh counter samples.
        collector = StatsCollector()
        collector.record_request_start("m", "a", clock.now)
        for value in (0, 0, 1):
            collector.record_engine_tokens("m", value)
            clock.now += 1.0
        snap = collector.snapshot(current_model="m")
        assert _live(snap, "m")["tps"] == 0.0
        assert _live(snap)["tps"] == 0.0
        assert collector.has_active_decode("m") is False

    def test_mixed_prefill_and_decode_shows_slowed_rate(self, clock):
        # One stream decodes the whole time while the engine also runs a
        # prefill: the counter rises slower, and the rate shows it.
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        collector.record_request_start("m", "b", clock.now)
        for value in (0, 40, 80, 120):
            collector.record_engine_tokens("m", value)
            clock.now += 1.0
        clock.now -= 1.0
        assert _live(collector.snapshot(), "m")["tps"] == pytest.approx(40.0)

    def test_global_uses_estimate_when_current_model_has_no_counter(
        self, clock,
    ):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        for value in (0, 100, 200):
            collector.record_engine_tokens("m", value)
            clock.now += 1.0
        collector.record_gen_chunks("m", 20)
        clock.now -= 1.0
        for current in (None, "other"):
            snap = collector.snapshot(current_model=current)
            assert _live(snap)["source"] == "estimate"
            assert _live(snap)["tps"] == pytest.approx(20 / 2)
            assert _live(snap, "m")["source"] == "engine"

    def test_global_uses_estimate_while_another_model_decodes(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("old", "x", clock.now)
        collector.record_decode_start("m", "a", clock.now)
        for value in (0, 100, 200):
            collector.record_engine_tokens("m", value)
            clock.now += 1.0
        clock.now -= 1.0
        snap = collector.snapshot(current_model="m")
        assert _live(snap, "m")["tps"] == pytest.approx(100.0)
        assert _live(snap)["source"] == "estimate"

    @pytest.mark.parametrize(
        "value", [True, None, "7", float("nan"), float("inf"), -1],
    )
    def test_invalid_counter_values_ignored(self, clock, value):
        collector = StatsCollector()
        collector.record_model_start("m")
        collector.record_engine_tokens("m", value)
        assert len(collector._models["m"].engine_samples) == 0

    def test_clear_engine_tokens(self, clock):
        collector = StatsCollector()
        collector.record_engine_tokens("m", 1)
        collector.clear_engine_tokens("m")
        collector.clear_engine_tokens("unknown")  # no error, no new model
        assert len(collector._models["m"].engine_samples) == 0
        assert "unknown" not in collector._models


def _metrics_route(app: web.Application, handler) -> web.Application:
    app.router.add_get("/metrics", handler)
    return app


class TestEngineRateThroughSwapper:
    async def test_a2_silent_stream_through_proxy_uses_counter(
        self, monkeypatch,
    ):
        # Fake vLLM: one streamed chunk, then silence while the counter
        # grows at 200 t/s of wall time; the live rate follows the counter.
        release = asyncio.Event()
        sent = asyncio.Event()
        state = {"since": None, "calls": 0}

        async def chat(request):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            state["since"] = time.monotonic()
            event = {"choices": [{"delta": {"content": "x"}}]}
            await resp.write(f"data: {json.dumps(event)}\n\n".encode())
            sent.set()
            await release.wait()
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        async def metrics(request):
            state["calls"] += 1
            since = state["since"]
            value = 0.0 if since is None else 200 * (time.monotonic() - since)
            return web.Response(text=(
                "# TYPE vllm:generation_tokens_total counter\n"
                f'vllm:generation_tokens_total{{engine="0"}} {value:e}\n'
            ))

        app = _metrics_route(_make_app(chat), metrics)
        async with _stack(
            monkeypatch, {"model-a": app}, seed="model-a",
        ) as stack:
            stack.swapper._engine_poller._interval = 0.05
            stats = stack.swapper.stats
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                await asyncio.wait_for(sent.wait(), timeout=5)
                await asyncio.sleep(1.2)
                data = await (await client.get("/stats/json")).json()
                release.set()
                assert "[DONE]" in await resp.text()
            await asyncio.sleep(0.2)
            idle_calls = state["calls"]
            await asyncio.sleep(0.3)
            assert state["calls"] == idle_calls  # A4: no polls when idle
            after = stats.snapshot(current_model="model-a")

        live = data["requests"]["rates"]["generation_tps"]["live"]
        assert live["source"] == "engine"
        assert live["estimate"] is False
        assert live["window_s"] == 5
        # Chunk estimate would be 1 chunk / ~1.2 s; the counter says ~200.
        assert 150 < live["tps"] < 250
        row = data["models"]["model-a"]["requests"]["rates"]
        assert row["generation_tps"]["live"]["source"] == "engine"
        assert _live(after)["tps"] == 0.0

    @pytest.mark.parametrize("variant", ["no_route", "llamacpp", "http_500"])
    async def test_a3_backend_without_live_counter_uses_estimate(
        self, monkeypatch, variant,
    ):
        state = {"calls": 0}

        async def chat(request):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            for _ in range(5):
                await resp.write(
                    b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                )
                await asyncio.sleep(0.05)
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        async def metrics(request):
            state["calls"] += 1
            if variant == "http_500":
                raise web.HTTPInternalServerError()
            # Present, but only grows when a request finishes: not used.
            return web.Response(text="llamacpp:tokens_predicted_total 99\n")

        app = _make_app(chat)
        if variant != "no_route":
            _metrics_route(app, metrics)
        async with _stack(
            monkeypatch, {"model-a": app}, seed="model-a",
        ) as stack:
            stack.swapper._engine_poller._interval = 0.01
            stats = stack.swapper.stats
            async with TestClient(stack.proxy) as client:
                for _ in range(2):
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={"model": "model-a", "stream": True},
                    )
                    assert resp.status == 200
                    assert "[DONE]" in await resp.text()
            snap = stats.snapshot(current_model="model-a")

        if variant == "llamacpp":
            # No live counter: probed once for this process, not on every
            # poll or request.
            assert state["calls"] == 1
        elif variant == "http_500":
            # A server error is transient: retried while decoding.
            assert state["calls"] >= 1
        assert len(stats._models["model-a"].engine_samples) == 0
        assert snap["requests"]["success"] == 2
        assert _live(snap, "model-a")["source"] == "estimate"
        assert _live(snap, "model-a")["estimate"] is True
        assert _live(snap)["source"] == "estimate"

    async def test_a5_hanging_metrics_never_delays_proxied_stream(
        self, monkeypatch,
    ):
        # The stream waits until the poll is in flight, then finishes while
        # /metrics still hangs; the poll ends by its own timeout.
        release = asyncio.Event()
        polled = asyncio.Event()

        async def chat(request):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            )
            await asyncio.wait_for(polled.wait(), timeout=5)
            for _ in range(2):
                await resp.write(
                    b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                )
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":3}}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        async def metrics(request):
            polled.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return web.Response(text="vllm:generation_tokens_total 1\n")

        app = _metrics_route(_make_app(chat), metrics)
        loop = asyncio.get_running_loop()
        try:
            async with _stack(
                monkeypatch, {"model-a": app}, seed="model-a",
            ) as stack:
                poller = stack.swapper._engine_poller
                poller._timeout = 1.0
                poller._interval = 0.05
                async with TestClient(stack.proxy) as client:
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={"model": "model-a", "stream": True},
                    )
                    assert resp.status == 200
                    assert "[DONE]" in await resp.text()
                    # Finished while the poll is still waiting on /metrics.
                    assert not poller._task.done()
                    started = loop.time()
                    await asyncio.wait_for(asyncio.shield(poller._task), 3)
                    assert loop.time() - started < 1.5
                snap = stack.swapper.stats.snapshot()
        finally:
            release.set()

        assert snap["requests"]["success"] == 1
        assert snap["requests"]["tokens"]["completion"] == 3
        assert snap["requests"]["rates"]["generation_tps"]["live"][
            "source"] == "estimate"
