"""Average aggregate (busy-time) generation and prompt speed (P-20260925-04)."""

from __future__ import annotations

import json

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest

from src.stats import StatsCollector
from tests.stats_helpers import _make_app, _stack


def _rates(snapshot, model=None):
    block = (
        snapshot["requests"] if model is None
        else snapshot["models"][model]["requests"]
    )
    return block["rates"]


def _gen(collector, model=None):
    return _rates(collector.snapshot(), model)["generation_tps"]["avg_aggregate"]


def _prompt(collector, model=None):
    return _rates(collector.snapshot(), model)["prompt_tps"]["avg_aggregate"]


def _decode(collector, model, rid, start, end, tokens, *, ok=True):
    collector.record_decode_start(model, rid, start)
    collector.record_decode_end(
        model, rid, ended_at=end, ok=ok, completion_tokens=tokens,
    )


class TestAverageAggregateGeneration:
    """rates.generation_tps.avg_aggregate from decode cohorts."""

    def test_null_before_any_data(self):
        collector = StatsCollector()
        assert _gen(collector) is None
        collector.record_model_start("m")
        assert _gen(collector, "m") is None
        assert _gen(collector) is None

    def test_single_stream_equals_stream_rate_with_concurrency_one(self):
        collector = StatsCollector()
        _decode(collector, "m", "a", 10.0, 12.0, 100)
        collector.record_request(
            "m", ok=True, completion_tokens=100, decode_s=2.0,
        )
        rates = _rates(collector.snapshot(), "m")["generation_tps"]
        assert rates["avg_aggregate"] == {"tps": 50.0, "concurrency": 1.0}
        assert rates["avg_aggregate"]["tps"] == rates["avg"]

    def test_two_overlapping_streams_use_busy_time(self):
        # A [0, 4) 40 tok, B [2, 6) 60 tok: busy 6 s, stream time 8 s.
        collector = StatsCollector()
        collector.record_decode_start("m", "a", 0.0)
        collector.record_decode_start("m", "b", 2.0)
        collector.record_decode_end(
            "m", "a", ended_at=4.0, ok=True, completion_tokens=40,
        )
        # B still decoding and overlaps A's cohort: not published yet.
        assert _gen(collector, "m") is None
        collector.record_decode_end(
            "m", "b", ended_at=6.0, ok=True, completion_tokens=60,
        )
        assert _gen(collector, "m") == pytest.approx(
            {"tps": 100 / 6, "concurrency": 8 / 6}
        )

    def test_separate_cohorts_sum_and_idle_gaps_are_excluded(self):
        # Cohort 1: [0, 2) 20 tok. Idle 2..10. Cohort 2: [10, 14) 60 tok
        # overlapping [11, 13) 20 tok. Busy 2 + 4 = 6 s, stream 2 + 4 + 2 = 8 s.
        collector = StatsCollector()
        _decode(collector, "m", "a", 0.0, 2.0, 20)
        assert _gen(collector, "m") == {"tps": 10.0, "concurrency": 1.0}
        collector.record_decode_start("m", "b", 10.0)
        collector.record_decode_start("m", "c", 11.0)
        collector.record_decode_end(
            "m", "c", ended_at=13.0, ok=True, completion_tokens=20,
        )
        collector.record_decode_end(
            "m", "b", ended_at=14.0, ok=True, completion_tokens=60,
        )
        assert _gen(collector, "m") == pytest.approx(
            {"tps": 100 / 6, "concurrency": 8 / 6}
        )

    def test_ineligible_decodes_are_excluded(self):
        collector = StatsCollector()
        _decode(collector, "m", "fail", 0.0, 1.0, 50, ok=False)
        _decode(collector, "m", "nousage", 0.0, 1.0, None)
        _decode(collector, "m", "zero", 0.0, 1.0, 0)
        _decode(collector, "m", "flat", 1.0, 1.0, 5)
        _decode(collector, "m", "bool", 0.0, 1.0, True)
        assert _gen(collector, "m") is None
        _decode(collector, "m", "good", 5.0, 7.0, 30)
        assert _gen(collector, "m") == {"tps": 15.0, "concurrency": 1.0}

    def test_global_sums_per_model_accumulators(self):
        # a: [0, 2) 20 tok; b: [0, 4) 40 tok. Global: 60 tok / 6 s busy.
        collector = StatsCollector()
        _decode(collector, "a", "x", 0.0, 2.0, 20)
        _decode(collector, "b", "y", 0.0, 4.0, 40)
        assert _gen(collector, "a") == {"tps": 10.0, "concurrency": 1.0}
        assert _gen(collector, "b") == {"tps": 10.0, "concurrency": 1.0}
        assert _gen(collector) == {"tps": 10.0, "concurrency": 1.0}

    def test_max_aggregate_is_unchanged_alongside_average(self):
        collector = StatsCollector()
        _decode(collector, "m", "a", 0.0, 2.0, 20)
        _decode(collector, "m", "b", 10.0, 11.0, 30)
        generation = _rates(collector.snapshot(), "m")["generation_tps"]
        assert generation["max_aggregate"] == {"tps": 30.0, "concurrency": 1}
        assert generation["avg_aggregate"] == {
            "tps": 50 / 3, "concurrency": 1.0,
        }


def _prefill_start(collector, model, rid, start):
    collector.record_request_start(model, rid, start)


def _first_token(collector, model, rid, at):
    collector.record_decode_start(model, rid, at)


def _prefill_end(
    collector, model, rid, first, prompt, cached, *, ok=True,
):
    # The swapper closes the decode before the prefill; mirror that so any
    # first-token decode entry is released (ineligible here, so no cohort).
    collector.record_decode_end(
        model, rid, ended_at=first if first is not None else 0.0,
        ok=False, completion_tokens=None,
    )
    collector.record_prefill_end(
        model, rid, first_token_at=first, ok=ok,
        prompt_tokens=prompt, cached_tokens=cached,
    )


def _request(collector, model, rid, start, first, prompt, cached, *, ok=True):
    _prefill_start(collector, model, rid, start)
    if first is not None:
        _first_token(collector, model, rid, first)
    _prefill_end(collector, model, rid, first, prompt, cached, ok=ok)


def _leftover(collector, model):
    stats = collector._models[model]
    return (
        stats.active_requests, stats.prefill_frontier,
        stats.active_decodes, stats.decode_frontier,
    )


class TestAverageAggregatePrompt:
    """rates.prompt_tps.avg_aggregate from prefill intervals."""

    def test_null_before_any_data(self):
        collector = StatsCollector()
        assert _prompt(collector) is None
        _prefill_start(collector, "m", "a", 0.0)
        assert _prompt(collector, "m") is None
        assert _prompt(collector) is None

    def test_single_request_uses_uncached_tokens_over_ttft(self):
        # 1000 prompt - 200 cached = 800 over [0, 2): 400 t/s.
        collector = StatsCollector()
        _request(collector, "m", "a", 0.0, 2.0, 1000, 200)
        collector.record_request(
            "m", ok=True, prompt_tokens=1000, cached_tokens=200, ttft_s=2.0,
        )
        rates = _rates(collector.snapshot(), "m")["prompt_tps"]
        assert rates["avg_aggregate"] == {"tps": 400.0, "concurrency": 1.0}
        assert rates["avg_aggregate"]["tps"] == rates["avg"]

    def test_unknown_cached_uses_full_prompt(self):
        collector = StatsCollector()
        _request(collector, "m", "a", 0.0, 2.0, 1000, None)
        assert _prompt(collector, "m") == {"tps": 500.0, "concurrency": 1.0}

    def test_overlap_is_held_while_in_flight_then_published(self):
        # A [0, 2) 800 tok; B starts at 1 and has no first token yet.
        collector = StatsCollector()
        _prefill_start(collector, "m", "a", 0.0)
        _prefill_start(collector, "m", "b", 1.0)
        _first_token(collector, "m", "a", 2.0)
        _prefill_end(collector, "m", "a", 2.0, 800, 0)
        assert _prompt(collector, "m") is None
        # B's first token arrives at 4: still unknown token count, held.
        _first_token(collector, "m", "b", 4.0)
        assert _prompt(collector, "m") is None
        _prefill_end(collector, "m", "b", 4.0, 700, 100)
        # Union [0, 4) busy 4 s, tokens 1400, stream time 2 + 3 = 5 s.
        assert _prompt(collector, "m") == pytest.approx(
            {"tps": 350.0, "concurrency": 1.25}
        )
        assert _leftover(collector, "m") == ({}, [], {}, [])

    def test_in_flight_requests_that_cannot_overlap_do_not_hold(self):
        collector = StatsCollector()
        # C: prefill [-5, -4) done, still decoding while A runs.
        _prefill_start(collector, "m", "c", -5.0)
        _first_token(collector, "m", "c", -4.0)
        # D starts exactly when A's prefill ends (half-open: no overlap).
        _prefill_start(collector, "m", "a", 0.0)
        _prefill_start(collector, "m", "d", 2.0)
        _first_token(collector, "m", "a", 2.0)
        _prefill_end(collector, "m", "a", 2.0, 100, None)
        assert _prompt(collector, "m") == {"tps": 50.0, "concurrency": 1.0}
        # C closes later: its own group [-5, -4) 30 tok adds 1 s busy.
        _prefill_end(collector, "m", "c", -4.0, 30, 0)
        assert _prompt(collector, "m") == pytest.approx(
            {"tps": 130 / 3, "concurrency": 1.0}
        )
        _prefill_end(collector, "m", "d", None, 10, 0, ok=False)
        assert _leftover(collector, "m") == ({}, [], {}, [])

    def test_ineligible_requests_are_excluded_and_leave_no_state(self):
        collector = StatsCollector()
        # Overlapping eligible request A [0, 2) with several failures.
        _prefill_start(collector, "m", "a", 0.0)
        _request(collector, "m", "failed", 0.5, 1.5, 500, 0, ok=False)
        _request(collector, "m", "no-first-token", 0.5, None, 500, 0)
        _request(collector, "m", "error-before-token", 0.5, None, 500, 0,
                 ok=False)
        _request(collector, "m", "no-usage", 0.5, 1.5, None, None)
        _request(collector, "m", "all-cached", 0.5, 1.5, 500, 500)
        _request(collector, "m", "zero-prompt", 0.5, 1.5, 0, None)
        _request(collector, "m", "bool-prompt", 0.5, 1.5, True, None)
        _request(collector, "m", "flat", 1.0, 1.0, 500, 0)
        _request(collector, "m", "reversed", 1.0, 0.5, 500, 0)
        assert _prompt(collector, "m") is None
        _first_token(collector, "m", "a", 2.0)
        _prefill_end(collector, "m", "a", 2.0, 600, 0)
        assert _prompt(collector, "m") == {"tps": 300.0, "concurrency": 1.0}
        assert _leftover(collector, "m") == ({}, [], {}, [])

    def test_end_without_start_is_a_noop(self):
        collector = StatsCollector()
        _prefill_end(collector, "m", "ghost", 1.0, 100, 0)
        assert collector.snapshot()["models"] == {}
        _prefill_start(collector, "m", "a", 0.0)
        _prefill_end(collector, "m", "ghost", 1.0, 100, 0)
        assert _prompt(collector, "m") is None
        assert set(_leftover(collector, "m")[0]) == {"a"}

    def test_duplicate_start_keeps_first_and_duplicate_end_is_ignored(self):
        collector = StatsCollector()
        _prefill_start(collector, "m", "a", 0.0)
        _prefill_start(collector, "m", "a", 1.0)
        _prefill_end(collector, "m", "a", 2.0, 100, 0)
        _prefill_end(collector, "m", "a", 2.0, 100, 0)
        assert _prompt(collector, "m") == {"tps": 50.0, "concurrency": 1.0}

    def test_global_sums_per_model_accumulators(self):
        collector = StatsCollector()
        _request(collector, "a", "x", 0.0, 1.0, 100, None)
        _request(collector, "b", "y", 0.0, 3.0, 500, 200)
        assert _prompt(collector) == {"tps": 100.0, "concurrency": 1.0}


class TestAverageAggregateJsonContract:

    def test_snapshot_exposes_both_averages_in_every_scope(self):
        collector = StatsCollector()
        _prefill_start(collector, "m", "a", 0.0)
        _first_token(collector, "m", "a", 1.0)
        collector.record_decode_end(
            "m", "a", ended_at=3.0, ok=True, completion_tokens=40,
        )
        _prefill_end(collector, "m", "a", 1.0, 300, 100)
        snapshot = collector.snapshot()
        json.dumps(snapshot, allow_nan=False)
        for rates in (_rates(snapshot), _rates(snapshot, "m")):
            assert rates["generation_tps"]["avg_aggregate"] == {
                "tps": 20.0, "concurrency": 1.0,
            }
            assert rates["prompt_tps"]["avg_aggregate"] == {
                "tps": 200.0, "concurrency": 1.0,
            }
        assert "max_aggregate" not in _rates(snapshot)["generation_tps"]

    async def test_real_stream_publishes_averages_and_clears_state(
        self, monkeypatch,
    ):
        async def chat(request):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(
                b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            )
            await resp.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":12,'
                b'"completion_tokens":9,'
                b'"prompt_tokens_details":{"cached_tokens":3}}}\n\n'
            )
            await resp.write_eof()
            return resp

        async with _stack(
            monkeypatch, {"model-a": _make_app(chat)}, seed="model-a"
        ) as stack:
            async with TestClient(stack.proxy) as client:
                for _ in range(2):
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={"model": "model-a", "stream": True},
                    )
                    assert resp.status == 200
                    await resp.text()
                data = await (await client.get("/stats/json")).json()
            stats = stack.swapper.stats._models["model-a"]
            assert stats.active_requests == {}
            assert stats.prefill_frontier == []
            assert stats.active_decodes == {}
            assert stats.decode_frontier == []

        for rates in (
            data["requests"]["rates"],
            data["models"]["model-a"]["requests"]["rates"],
        ):
            # Sequential requests never overlap: concurrency is exactly 1
            # and the busy-time average equals the per-request average.
            for key in ("generation_tps", "prompt_tps"):
                aggregate = rates[key]["avg_aggregate"]
                assert set(aggregate) == {"tps", "concurrency"}
                assert aggregate["concurrency"] == pytest.approx(1.0)
                assert aggregate["tps"] == pytest.approx(rates[key]["avg"])
