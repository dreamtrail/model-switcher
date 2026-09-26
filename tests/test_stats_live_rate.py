"""Live (estimated) generation rate — P-20260926-01 AC2 and AC4.

``rates.generation_tps.live`` counts streamed output chunks over a rolling
60 s window, per model and globally (global = sum of per-model values).
It is an estimate: one chunk is not always one token.
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


class TestLiveRateCollector:
    def test_fresh_collector_reports_zero_estimate(self, clock):
        snap = StatsCollector().snapshot()
        assert _live(snap) == {"tps": 0.0, "window_s": 60, "estimate": True}

    def test_per_model_and_global_sum(self, clock):
        collector = StatsCollector()
        collector.record_gen_chunks("a", 30)
        collector.record_gen_chunks("a", 30)
        collector.record_gen_chunks("b", 12)
        collector.record_model_start("idle")

        snap = collector.snapshot()

        assert _live(snap, "a")["tps"] == pytest.approx(60 / 60)
        assert _live(snap, "b")["tps"] == pytest.approx(12 / 60)
        assert _live(snap, "idle") == {
            "tps": 0.0, "window_s": 60, "estimate": True,
        }
        assert _live(snap)["tps"] == sum(
            _live(snap, name)["tps"] for name in snap["models"]
        )
        assert _live(snap)["tps"] == pytest.approx(72 / 60)
        assert _live(snap)["estimate"] is True

    def test_events_older_than_window_drop_out(self, clock):
        collector = StatsCollector()
        collector.record_gen_chunks("m", 60)   # t = 1000
        clock.now += 30.0
        collector.record_gen_chunks("m", 120)  # t = 1030

        clock.now += 30.0  # t = 1060: both still inside (edge inclusive)
        assert _live(collector.snapshot(), "m")["tps"] == pytest.approx(3.0)

        clock.now += 0.5  # first event now 60.5 s old
        snap = collector.snapshot()
        assert _live(snap, "m")["tps"] == pytest.approx(2.0)
        assert _live(snap)["tps"] == pytest.approx(2.0)
        # Pruned from memory, not just filtered.
        assert len(collector._models["m"].gen_chunk_events) == 1

        clock.now += 60.0
        snap = collector.snapshot()
        assert _live(snap, "m")["tps"] == 0.0
        assert _live(snap)["tps"] == 0.0
        assert len(collector._models["m"].gen_chunk_events) == 0

    def test_recording_prunes_stale_events(self, clock):
        collector = StatsCollector()
        collector.record_gen_chunks("m", 5)
        clock.now += 61.0
        collector.record_gen_chunks("m", 7)
        assert list(collector._models["m"].gen_chunk_events) == [
            (clock.now, 7),
        ]

    @pytest.mark.parametrize("units", [0, -3, True, 1.5, None, "4"])
    def test_invalid_counts_are_ignored_without_raising(self, clock, units):
        collector = StatsCollector()
        collector.record_gen_chunks("m", units)
        assert _live(collector.snapshot())["tps"] == 0.0

    def test_existing_metrics_unaffected(self, clock):
        # Chunks feed only the live estimate, never exact token metrics.
        collector = StatsCollector()
        collector.record_gen_chunks("m", 50)
        snap = collector.snapshot()
        gen = snap["requests"]["rates"]["generation_tps"]
        assert gen["last_60s"] == 0.0
        assert gen["last_300s"] == 0.0
        assert gen["avg"] is None
        assert snap["requests"]["tokens"]["completion"] == 0
        assert snap["requests"]["total"] == 0


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
                while live["tps"] < 4 / 60 and loop.time() < deadline:
                    await asyncio.sleep(0.01)
                    live = _live(stats.snapshot(), "model-a")

                # Mid-stream: the request has not completed yet.
                mid = stats.snapshot()
                assert mid["requests"]["total"] == 0
                assert mid["requests"]["tokens"]["completion"] == 0
                assert _live(mid, "model-a")["tps"] == pytest.approx(4 / 60)
                assert _live(mid)["tps"] == pytest.approx(4 / 60)

                release.set()
                text = await resp.text()
                assert "[DONE]" in text

                data = await (await client.get("/stats/json")).json()

        # After completion the estimate stays; exact metrics are recorded.
        req = data["requests"]
        assert req["total"] == 1
        assert req["tokens"]["completion"] == 5
        assert req["rates"]["generation_tps"]["live"] == {
            "tps": pytest.approx(4 / 60), "window_s": 60, "estimate": True,
        }
        row = data["models"]["model-a"]["requests"]
        assert row["rates"]["generation_tps"]["live"]["tps"] == (
            pytest.approx(4 / 60)
        )

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
