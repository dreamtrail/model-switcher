"""Live generation rate, estimate path — P-20260926-01, P-20260926-04.

Window 5 s since P-20261114-01 (engine-counter path: test_stats_live_engine).
Without engine counter samples, ``rates.generation_tps.live`` is calibrated
streamed output chunks in the last 5 s (chunks × tokens per chunk) divided
by busy time, with ``source: "estimate"``: the union of
ended and active decode intervals clipped to the window, floored at 1 s.
It is 0 while no decode is active. The exact-token windows (``last_60s`` /
``last_300s``) are separate and keep their 60 s / 300 s spans. Tokens per chunk is Σ exact completion
tokens / Σ chunks over the last 50 finished successful streams (default 1.0).
Global = Σ per-model calibrated tokens / union busy time across models.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from src.stats import StatsCollector

from tests.stats_helpers import _make_app, _stack


class _FakeClock:
    """Monotonic clock stand-in for src.stats (tests advance it by hand)."""

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


def _stream(collector, clock, model, rid, *, seconds, per_second,
            end=True):
    """Simulate one decode at ``per_second`` chunks/s for ``seconds``."""
    collector.record_decode_start(model, rid, clock.now)
    for _ in range(seconds):
        collector.record_gen_chunks(model, per_second)
        clock.now += 1.0
    if end:
        collector.record_decode_end(
            model, rid, ended_at=clock.now, ok=True, completion_tokens=None,
        )


class TestLiveRateCollector:
    def test_fresh_collector_reports_zero_estimate(self, clock):
        snap = StatsCollector().snapshot()
        assert _live(snap) == {
            "tps": 0.0, "window_s": 5, "estimate": True,
            "source": "estimate", "busy_s": 0.0, "tokens_per_chunk": None,
        }

    def test_idle_model_reports_default_ratio(self, clock):
        collector = StatsCollector()
        collector.record_model_start("idle")
        assert _live(collector.snapshot(), "idle") == {
            "tps": 0.0, "window_s": 5, "estimate": True,
            "source": "estimate", "busy_s": 0.0, "tokens_per_chunk": 1.0,
        }

    def test_ac1_idle_time_between_streams_is_not_counted(self, clock):
        # 3 s at 50 chunks/s, then 1 s with nothing generating; a new
        # stream has just started, so the model is active again.
        collector = StatsCollector()
        _stream(collector, clock, "m", "a", seconds=3, per_second=50)
        clock.now += 1.0
        collector.record_decode_start("m", "b", clock.now)

        live = _live(collector.snapshot(), "m")
        assert live["window_s"] == 5
        assert live["source"] == "estimate"
        assert live["busy_s"] == pytest.approx(3.0)
        assert live["tps"] == pytest.approx(50.0)  # not 150 / 5 = 30

    def test_ac2_concurrent_streams_aggregate(self, clock):
        collector = StatsCollector()
        for rid in range(8):
            collector.record_decode_start("m", rid, clock.now)
        for _ in range(4):
            for _rid in range(8):
                collector.record_gen_chunks("m", 50)
            clock.now += 1.0

        snap = collector.snapshot()
        assert _live(snap, "m")["busy_s"] == pytest.approx(4.0)
        assert _live(snap, "m")["tps"] == pytest.approx(400.0)
        assert _live(snap)["tps"] == pytest.approx(400.0)

    def test_ac3_no_active_decode_reports_zero(self, clock):
        collector = StatsCollector()
        _stream(collector, clock, "m", "a", seconds=5, per_second=20)

        snap = collector.snapshot()
        assert _live(snap, "m")["tps"] == 0.0
        assert _live(snap)["tps"] == 0.0
        # Busy time is still reported for the window.
        assert _live(snap, "m")["busy_s"] == pytest.approx(5.0)

    def test_ac4_calibrated_ratio_scales_chunks(self, clock):
        collector = StatsCollector()
        collector.record_gen_calibration("m", 300, 100)
        _stream(collector, clock, "m", "a", seconds=4, per_second=10,
                end=False)

        live = _live(collector.snapshot(), "m")
        assert live["tokens_per_chunk"] == pytest.approx(3.0)
        assert live["tps"] == pytest.approx(40 * 3 / 4)

    def test_ac5_default_ratio_and_cap_of_fifty(self, clock):
        collector = StatsCollector()
        assert _live(collector.snapshot())["tokens_per_chunk"] is None
        collector.record_model_start("m")
        assert _live(collector.snapshot(), "m")["tokens_per_chunk"] == 1.0
        for _ in range(10):
            collector.record_gen_calibration("m", 1000, 1)  # pushed out
        for _ in range(50):
            collector.record_gen_calibration("m", 4, 2)
        assert len(collector._models["m"].gen_calibration) == 50
        assert _live(collector.snapshot(), "m")["tokens_per_chunk"] == 2.0

    @pytest.mark.parametrize(
        ("tokens", "chunks"),
        [(0, 5), (5, 0), (None, 5), (5, None), (True, 5), (5, 1.5),
         (-1, 3), ("7", 2)],
    )
    def test_ac5_ac7_invalid_samples_ignored_without_raising(
        self, clock, tokens, chunks,
    ):
        collector = StatsCollector()
        collector.record_model_start("m")
        collector.record_gen_calibration("m", tokens, chunks)
        assert len(collector._models["m"].gen_calibration) == 0
        assert _live(collector.snapshot(), "m")["tokens_per_chunk"] == 1.0

    def test_ac6_overlap_counted_once(self, clock):
        collector = StatsCollector()
        start = clock.now
        collector.record_decode_start("m", "a", start)
        collector.record_decode_start("m", "b", start + 1)
        collector.record_decode_end(
            "m", "a", ended_at=start + 2, ok=True, completion_tokens=None,
        )
        collector.record_decode_end(
            "m", "b", ended_at=start + 3, ok=False, completion_tokens=None,
        )
        collector.record_decode_start("m", "c", start + 2.5)
        collector.record_gen_chunks("m", 60)
        clock.now = start + 4

        live = _live(collector.snapshot(), "m")
        # [0,2] ∪ [1,3] ∪ [2.5,4] = [0,4]
        assert live["busy_s"] == pytest.approx(4.0)
        assert live["tps"] == pytest.approx(15.0)

    def test_ac6_busy_clipped_to_window(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        clock.now += 100.0
        collector.record_gen_chunks("m", 120)

        live = _live(collector.snapshot(), "m")
        assert live["busy_s"] == pytest.approx(5.0)
        assert live["tps"] == pytest.approx(24.0)

    def test_ended_interval_straddling_window_start_is_clipped(self, clock):
        # Decode ran [1000, 1006]; at t = 1008 the window is [1003, 1008],
        # so only the last 3 s of that ended interval count as busy.
        collector = StatsCollector()
        start = clock.now
        collector.record_decode_start("m", "a", start)
        clock.now = start + 6
        collector.record_gen_chunks("m", 40)  # t = 1006, inside window
        collector.record_decode_end(
            "m", "a", ended_at=clock.now, ok=True, completion_tokens=None,
        )
        collector.record_decode_start("m", "b", start + 8)
        clock.now = start + 8

        live = _live(collector.snapshot(), "m")
        # Clipped [1003, 1006] plus the just-started [1008, 1008].
        assert live["busy_s"] == pytest.approx(3.0)
        assert live["tps"] == pytest.approx(40 / 3)
        assert len(collector._models["m"].live_decode_intervals) == 1

    def test_ac6_busy_floor_one_second(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        collector.record_gen_chunks("m", 5)
        clock.now += 0.2

        live = _live(collector.snapshot(), "m")
        assert live["busy_s"] == pytest.approx(0.2)
        assert live["tps"] == pytest.approx(5.0)

    def test_global_uses_union_busy_across_models(self, clock):
        collector = StatsCollector()
        start = clock.now
        collector.record_decode_start("a", 1, start)
        collector.record_decode_start("b", 2, start + 2)
        collector.record_gen_calibration("b", 20, 10)
        collector.record_gen_chunks("a", 100)
        collector.record_gen_chunks("b", 50)
        clock.now = start + 4

        snap = collector.snapshot()
        assert _live(snap, "a")["tps"] == pytest.approx(100 / 4)
        assert _live(snap, "b")["tps"] == pytest.approx(100 / 2)
        # (100 + 50 × 2) tokens over union [0, 4] = 50 t/s.
        assert _live(snap)["busy_s"] == pytest.approx(4.0)
        assert _live(snap)["tps"] == pytest.approx(50.0)
        assert _live(snap)["source"] == "estimate"
        assert _live(snap)["tokens_per_chunk"] is None

    def test_global_union_across_model_swap(self, clock):
        # Model A finished inside the window, then a swap; model B is
        # generating now. Global busy = union of A's ended and B's active
        # intervals; per-model A reads 0 because A has no active decode.
        collector = StatsCollector()
        start = clock.now
        _stream(collector, clock, "a", 1, seconds=2, per_second=30)  # [0,2]
        clock.now = start + 2.5  # swap gap [2, 2.5] is idle
        collector.record_decode_start("b", 2, clock.now)
        for _ in range(2):
            collector.record_gen_chunks("b", 10)
            clock.now += 1.0  # ends at start + 4.5

        snap = collector.snapshot()
        assert _live(snap, "a")["tps"] == 0.0
        assert _live(snap, "a")["busy_s"] == pytest.approx(2.0)
        assert _live(snap, "b")["busy_s"] == pytest.approx(2.0)
        assert _live(snap, "b")["tps"] == pytest.approx(20 / 2)
        # (60 + 20) tokens over [0, 2] ∪ [2.5, 4.5] = 4 s busy.
        assert _live(snap)["busy_s"] == pytest.approx(4.0)
        assert _live(snap)["tps"] == pytest.approx(80 / 4)

    def test_events_older_than_window_drop_out(self, clock):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        collector.record_gen_chunks("m", 60)   # t = 1000
        clock.now += 2.5
        collector.record_gen_chunks("m", 120)  # t = 1002.5

        clock.now += 2.5  # t = 1005: both still inside (edge inclusive)
        assert _live(collector.snapshot(), "m")["tps"] == pytest.approx(36.0)

        clock.now += 0.5  # first event now 5.5 s old
        snap = collector.snapshot()
        assert _live(snap, "m")["tps"] == pytest.approx(24.0)
        assert _live(snap)["tps"] == pytest.approx(24.0)
        # Pruned from memory, not just filtered.
        assert len(collector._models["m"].gen_chunk_events) == 1

        clock.now += 5.0
        snap = collector.snapshot()
        assert _live(snap, "m")["tps"] == 0.0
        assert len(collector._models["m"].gen_chunk_events) == 0

    def test_ended_intervals_pruned_after_window(self, clock):
        collector = StatsCollector()
        _stream(collector, clock, "m", "a", seconds=2, per_second=1)
        assert len(collector._models["m"].live_decode_intervals) == 1
        clock.now += 4.5
        assert _live(collector.snapshot(), "m")["busy_s"] == pytest.approx(0.5)
        clock.now += 1.0
        snap = collector.snapshot()
        assert _live(snap, "m")["busy_s"] == 0.0
        assert len(collector._models["m"].live_decode_intervals) == 0

    def test_recording_prunes_stale_events(self, clock):
        collector = StatsCollector()
        collector.record_gen_chunks("m", 5)
        clock.now += 16.0
        collector.record_gen_chunks("m", 7)
        assert list(collector._models["m"].gen_chunk_events) == [
            (clock.now, 7),
        ]

    @pytest.mark.parametrize("units", [0, -3, True, 1.5, None, "4"])
    def test_invalid_counts_are_ignored_without_raising(self, clock, units):
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        collector.record_gen_chunks("m", units)
        assert _live(collector.snapshot())["tps"] == 0.0

    def test_ac7_existing_metrics_unaffected(self, clock):
        # Chunks and calibration feed only the live estimate.
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        collector.record_gen_chunks("m", 50)
        collector.record_gen_calibration("m", 300, 100)
        clock.now += 5.0
        snap = collector.snapshot()
        gen = snap["requests"]["rates"]["generation_tps"]
        assert gen["last_60s"] == 0.0
        assert gen["last_300s"] == 0.0
        assert gen["avg"] is None
        assert gen["avg_aggregate"] is None
        assert snap["requests"]["tokens"]["completion"] == 0
        assert snap["requests"]["total"] == 0

    def test_exact_token_windows_keep_60s_and_300s(self, clock):
        # The live window is 5 s, but last_60s / last_300s stay 60 / 300 s:
        # an exact-token event 30 s old counts in last_60s, while chunks
        # 30 s old are outside the live window.
        collector = StatsCollector()
        collector.record_decode_start("m", "a", clock.now)
        collector.record_gen_chunks("m", 90)
        collector._model("m").token_events.append((clock.now, 90))
        collector.token_events.append((clock.now, 90))
        clock.now += 30.0

        snap = collector.snapshot()
        gen = snap["requests"]["rates"]["generation_tps"]
        assert gen["live"]["window_s"] == 5
        assert gen["live"]["tps"] == 0.0
        assert gen["last_60s"] == pytest.approx(90 / 60)
        assert gen["last_300s"] == pytest.approx(90 / 300)
        row = snap["models"]["m"]["requests"]["rates"]["generation_tps"]
        assert row["last_60s"] == pytest.approx(90 / 60)
        assert row["live"]["window_s"] == 5


class TestLiveRateThroughSwapper:
    async def test_live_rate_rises_before_stream_finishes(self, monkeypatch):
        # AC4: real swapper + streaming proxy; the stream is held open
        # after some output so the estimate is observed mid-request.
        release = asyncio.Event()
        sent = asyncio.Event()

        async def chat(request):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            )
            for piece in ("Hel", "lo", " there"):
                event = {"choices": [{"delta": {"content": piece}}]}
                await resp.write(f"data: {json.dumps(event)}\n\n".encode())
            await resp.write(
                b'data: {"choices":[{"delta":{"reasoning_content":"r"}}]}\n\n'
            )
            sent.set()
            await release.wait()
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":3,'
                b'"completion_tokens":5}}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            stats = stack.swapper.stats
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                await asyncio.wait_for(sent.wait(), timeout=5)

                loop = asyncio.get_running_loop()
                deadline = loop.time() + 5
                live = _live(stats.snapshot(), "model-a")
                while live["tps"] <= 0 and loop.time() < deadline:
                    await asyncio.sleep(0.01)
                    live = _live(stats.snapshot(), "model-a")

                # Mid-stream: the request has not completed yet; 4 chunks
                # (default 1 token each) over busy time floored at 1 s.
                mid = stats.snapshot()
                assert mid["requests"]["total"] == 0
                assert mid["requests"]["tokens"]["completion"] == 0
                mid_live = _live(mid, "model-a")
                assert mid_live["tokens_per_chunk"] == 1.0
                assert mid_live["busy_s"] > 0
                assert mid_live["tps"] == pytest.approx(
                    4 / max(mid_live["busy_s"], 1.0)
                )
                assert _live(mid)["tps"] == pytest.approx(mid_live["tps"])

                release.set()
                text = await resp.text()
                assert "[DONE]" in text

                data = await (await client.get("/stats/json")).json()

        # After completion no decode is active (rate 0); exact metrics are
        # recorded and calibrate tokens per chunk (5 tokens / 4 chunks).
        req = data["requests"]
        assert req["total"] == 1
        assert req["tokens"]["completion"] == 5
        assert req["rates"]["generation_tps"]["live"]["tps"] == 0.0
        assert req["rates"]["generation_tps"]["live"]["tokens_per_chunk"] is None
        row = data["models"]["model-a"]["requests"]["rates"]["generation_tps"]
        assert row["live"]["tps"] == 0.0
        assert row["live"]["tokens_per_chunk"] == pytest.approx(1.25)
        assert row["live"]["busy_s"] > 0

    async def test_ac4_finished_stream_calibrates_ratio(self, monkeypatch):
        async def chat(request):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            for _ in range(100):
                event = {"choices": [{"delta": {"content": "abc"}}]}
                await resp.write(f"data: {json.dumps(event)}\n\n".encode())
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":3,'
                b'"completion_tokens":300}}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                assert resp.status == 200
                await resp.read()
            stats = stack.swapper.stats
            snap = stats.snapshot()

        assert list(stats._models["model-a"].gen_calibration) == [(300, 100)]
        assert _live(snap, "model-a")["tokens_per_chunk"] == pytest.approx(3.0)

    @pytest.mark.parametrize("variant", ["no_usage", "http_500", "cut"])
    async def test_ac5_failed_or_usage_less_streams_add_no_sample(
        self, monkeypatch, variant,
    ):
        async def chat(request):
            resp = web.StreamResponse(
                status=500 if variant == "http_500" else 200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            for _ in range(3):
                await resp.write(
                    b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                )
            if variant == "cut":
                # Abort mid-stream: the proxy records a stream error.
                request.transport.close()
                return resp
            if variant != "no_usage":
                await resp.write(
                    b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                    b'"completion_tokens":9}}\n\n'
                )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "model-a", "stream": True},
                )
                try:
                    await resp.read()
                except Exception:
                    pass
            stats = stack.swapper.stats
            snap = stats.snapshot()

        assert snap["requests"]["total"] == 1
        expected_success = 1 if variant == "no_usage" else 0
        assert snap["requests"]["success"] == expected_success
        assert list(stats._models["model-a"].gen_calibration) == []
        assert _live(snap, "model-a")["tokens_per_chunk"] == 1.0

    async def test_buffered_request_leaves_live_rate_at_zero(self, monkeypatch):
        async def chat(request):
            return web.json_response({
                "choices": [{"message": {"content": "Hi"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            })

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                resp = await client.post(
                    "/v1/chat/completions", json={"model": "model-a"},
                )
                assert resp.status == 200
                await resp.read()
            snap = stack.swapper.stats.snapshot()

        assert snap["requests"]["tokens"]["completion"] == 1
        assert _live(snap)["tps"] == 0.0
        assert _live(snap, "model-a")["tps"] == 0.0
        # AC5: non-streaming requests never add a calibration sample.
        assert _live(snap, "model-a")["tokens_per_chunk"] == 1.0
