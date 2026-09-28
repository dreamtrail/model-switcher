"""Server decode parsing, sample priority, and collector wiring (P-20260929-03).

Unit tests for src/decode.py, the paired-field and overflow fixes in
src/prefill.py, and the collector's use of decode samples (avg, TPOT,
server_timed_share, skipped_buffered, aggregate intervals of non-streaming
requests). Expected values are computed by hand.
"""

from __future__ import annotations

import json
import time

import pytest

from src.decode import (
    DecodeDecision,
    DecodeSample,
    ServerDecode,
    apply_server_decode,
    decode_interval,
    select_decode,
)
from src.prefill import ServerPrefill, apply_server_prefill, parse_duration
from src.proxy import _extract_usage, _process_sse_line
from src.stats import Outcome, StatsCollector

HUGE = 10 ** 400  # int too large for a float


def _server(**fields):
    server = ServerDecode()
    apply_server_decode(fields, server)
    return server


def _gen(snapshot, model=None):
    block = (
        snapshot["models"][model]["requests"] if model else snapshot["requests"]
    )
    return block["rates"]["generation_tps"]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_all_sources_parsed(self):
        server = _server(
            timings={"predicted_n": 12, "predicted_ms": 240},
            usage={"completion_time": 0.25},
            metrics={"generation_time_ms": 230.5},
        )
        assert server == ServerDecode(
            predicted_n=12, predicted_ms=240.0,
            completion_time_s=0.25, generation_time_ms=230.5,
        )

    @pytest.mark.parametrize("timings", [
        {"predicted_n": 5},
        {"predicted_ms": 50},
        {"predicted_n": 5, "predicted_ms": "50"},
        {"predicted_n": True, "predicted_ms": 50},
        {"predicted_n": -1, "predicted_ms": 50},
        {"predicted_n": 5, "predicted_ms": float("nan")},
        {"predicted_n": 5, "predicted_ms": -3},
        {"predicted_n": 5.0, "predicted_ms": 50},
        {"predicted_n": 5, "predicted_ms": HUGE},
    ])
    def test_predicted_needs_a_valid_pair(self, timings):
        server = ServerDecode(predicted_n=3, predicted_ms=30.0)
        apply_server_decode({"timings": timings}, server)
        assert (server.predicted_n, server.predicted_ms) == (3, 30.0)

    def test_last_valid_pair_wins(self):
        server = _server(timings={"predicted_n": 3, "predicted_ms": 30})
        apply_server_decode(
            {"timings": {"predicted_n": 9, "predicted_ms": 90}}, server,
        )
        assert (server.predicted_n, server.predicted_ms) == (9, 90.0)

    @pytest.mark.parametrize("value", [
        None, "0.3", True, -1, float("inf"), float("nan"), HUGE,
    ])
    def test_invalid_durations_ignored(self, value):
        server = _server(usage={"completion_time": value},
                         metrics={"generation_time_ms": value})
        assert server == ServerDecode()

    def test_overflow_does_not_skip_other_fields(self):
        server = _server(
            timings={"predicted_n": 4, "predicted_ms": 40},
            usage={"completion_time": HUGE},
            metrics={"generation_time_ms": 20},
        )
        assert server == ServerDecode(
            predicted_n=4, predicted_ms=40.0, generation_time_ms=20.0,
        )

    @pytest.mark.parametrize("data", [
        None, [], "x", 3, {"timings": [1]}, {"usage": "x"}, {"metrics": 1},
    ])
    def test_non_dict_shapes_are_ignored(self, data):
        server = ServerDecode()
        apply_server_decode(data, server)
        assert server == ServerDecode()


class TestPrefillFixes:
    def test_parse_duration_overflow_is_invalid(self):
        assert parse_duration(HUGE) is None
        assert parse_duration(2) == 2.0

    def test_prefill_overflow_does_not_skip_other_fields(self):
        server = ServerPrefill()
        apply_server_prefill({
            "timings": {"prompt_n": 8, "prompt_ms": HUGE, "cache_n": 4},
            "usage": {"prompt_time": HUGE},
            "metrics": {"time_to_first_token_ms": 7},
        }, server)
        assert server == ServerPrefill(cache_n=4, ttft_ms=7.0)

    def test_prompt_pair_never_mixes_events(self):
        server = ServerPrefill()
        apply_server_prefill({"timings": {"prompt_n": 8}}, server)
        apply_server_prefill({"timings": {"prompt_ms": 20}}, server)
        assert (server.prompt_n, server.prompt_ms) == (None, None)
        apply_server_prefill(
            {"timings": {"prompt_n": 8, "prompt_ms": 20}}, server,
        )
        assert (server.prompt_n, server.prompt_ms) == (8, 20.0)


class TestProxyParsing:
    def test_sse_and_non_stream_bodies_populate_outcome(self):
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[],"usage":{"completion_tokens":3,'
            b'"completion_time":0.03},"timings":{"predicted_n":3,'
            b'"predicted_ms":30},"metrics":{"generation_time_ms":29}}',
            outcome,
        )
        assert outcome.server_decode == ServerDecode(
            predicted_n=3, predicted_ms=30.0,
            completion_time_s=0.03, generation_time_ms=29.0,
        )
        outcome = Outcome()
        _extract_usage(json.dumps({
            "usage": {"completion_tokens": 5},
            "timings": {"predicted_n": 5, "predicted_ms": 50},
        }).encode(), outcome)
        assert outcome.completion_tokens == 5
        assert outcome.server_decode.predicted_n == 5

    def test_parser_failure_never_breaks_proxy_parsing(self, monkeypatch):
        import src.proxy as proxy_module

        def boom(data, server):
            raise RuntimeError("boom")

        monkeypatch.setattr(proxy_module, "apply_server_decode", boom)
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[],"usage":{"prompt_tokens":2,'
            b'"completion_tokens":3,"prompt_time":0.01}}',
            outcome,
        )
        assert outcome.completion_tokens == 3
        assert outcome.server_prefill.prompt_time_s == 0.01


# ---------------------------------------------------------------------------
# Sample selection and interval
# ---------------------------------------------------------------------------

class TestSelectDecode:
    FULL = dict(
        timings={"predicted_n": 8, "predicted_ms": 40},
        usage={"completion_time": 0.1},
        metrics={"generation_time_ms": 200},
    )

    def test_predicted_pair_first(self):
        assert select_decode(9, _server(**self.FULL), 1.0, 1) == (
            DecodeDecision(DecodeSample(8, 0.04, True))
        )

    def test_completion_time_second(self):
        server = _server(usage=self.FULL["usage"],
                         metrics=self.FULL["metrics"])
        assert select_decode(9, server, 1.0, 1).sample == (
            DecodeSample(9, 0.1, True)
        )

    def test_vllm_generation_time_third(self):
        server = _server(metrics=self.FULL["metrics"])
        assert select_decode(9, server, 1.0, 1).sample == (
            DecodeSample(9, 0.2, True)
        )

    def test_zero_server_values_fall_through(self):
        server = _server(
            timings={"predicted_n": 0, "predicted_ms": 40},
            usage={"completion_time": 0},
            metrics={"generation_time_ms": 0},
        )
        assert select_decode(9, server, 0.5, 4).sample == (
            DecodeSample(9, 0.5, False)
        )
        server = _server(timings={"predicted_n": 3, "predicted_ms": 0},
                         usage={"completion_time": 0.3})
        assert select_decode(9, server, None).sample == (
            DecodeSample(9, 0.3, True)
        )

    def test_proxy_needs_two_chunks(self):
        assert select_decode(9, ServerDecode(), 0.001, 1) == DecodeDecision(
            skipped_buffered=True,
        )
        assert select_decode(9, None, 0.001, 0).skipped_buffered
        assert select_decode(9, None, 0.5, 2).sample == (
            DecodeSample(9, 0.5, False)
        )
        # None = chunk count not checked (direct collector callers).
        assert select_decode(9, None, 0.5).sample == (
            DecodeSample(9, 0.5, False)
        )

    def test_no_time_no_sample_no_skip(self):
        assert select_decode(9, ServerDecode(), None, 0) == DecodeDecision()

    @pytest.mark.parametrize("completion", [None, 0, -1, True, "3", 2.0])
    def test_needs_completion_tokens(self, completion):
        assert select_decode(completion, _server(**self.FULL), 1.0, 5) == (
            DecodeDecision()
        )


class TestDecodeInterval:
    def test_server_timed_ends_at_request_end(self):
        sample = DecodeSample(10, 2.0, True)
        assert decode_interval(sample, 5.0, 9.5, 10.0) == (8.0, 10.0)
        # Clamped to the request start.
        assert decode_interval(sample, 9.0, None, 10.0) == (9.0, 10.0)
        # Unknown request start: not clamped.
        assert decode_interval(sample, None, None, 10.0) == (8.0, 10.0)

    def test_proxy_timed_is_first_token_to_end(self):
        sample = DecodeSample(10, 1.0, False)
        assert decode_interval(sample, 5.0, 9.0, 10.0) == (9.0, 10.0)
        assert decode_interval(sample, 5.0, None, 10.0) is None

    @pytest.mark.parametrize("ended_at", [None, "10", True, float("nan")])
    def test_invalid_end(self, ended_at):
        assert decode_interval(DecodeSample(1, 1.0, True), 0.0, None,
                               ended_at) is None

    def test_degenerate(self):
        assert decode_interval(DecodeSample(1, 1.0, True), 10.0, None,
                               10.0) is None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class TestCollectorSamples:
    def test_non_stream_server_timed_counts_in_avg_and_tpot(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, completion_tokens=11,
            server_decode=_server(usage={"completion_time": 0.5}),
            gen_chunks=0,
        )
        for gen in (_gen(collector.snapshot()),
                    _gen(collector.snapshot(), "m")):
            assert gen["avg"] == pytest.approx(22.0)
            assert gen["server_timed_share"] == 1.0
            assert gen["skipped_buffered"] == 0
        tpot = collector.snapshot()["requests"]["tpot_ms"]
        assert tpot["count"] == 1
        assert tpot["avg"] == pytest.approx(500 / 10)

    def test_predicted_n_is_the_token_count(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, completion_tokens=12,
            server_decode=_server(
                timings={"predicted_n": 10, "predicted_ms": 100},
            ),
        )
        snap = collector.snapshot()
        assert _gen(snap)["avg"] == pytest.approx(100.0)
        assert snap["requests"]["tpot_ms"]["avg"] == pytest.approx(100 / 9)

    def test_buffered_stream_is_skipped_per_model_and_global(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, completion_tokens=40, ttft_s=0.5, decode_s=0.001,
            gen_chunks=1,
        )
        snap = collector.snapshot()
        for gen in (_gen(snap), _gen(snap, "m")):
            assert gen["avg"] is None
            assert gen["server_timed_share"] is None
            assert gen["skipped_buffered"] == 1
        assert snap["requests"]["tpot_ms"] is None
        # TTFT is unaffected by the skip.
        assert snap["requests"]["ttft_ms"]["count"] == 1

    def test_failed_request_never_samples_or_skips(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=False, kind="500", completion_tokens=40,
            decode_s=0.001, gen_chunks=1,
            server_decode=_server(usage={"completion_time": 1.0}),
        )
        gen = _gen(collector.snapshot())
        assert gen["avg"] is None
        assert gen["skipped_buffered"] == 0

    def test_server_timed_share_mixes_sources(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, completion_tokens=30,
            server_decode=_server(usage={"completion_time": 1.0}),
        )
        collector.record_request(
            "m", ok=True, completion_tokens=10, decode_s=1.0, gen_chunks=5,
        )
        gen = _gen(collector.snapshot(), "m")
        assert gen["server_timed_share"] == pytest.approx(0.75)
        assert gen["avg"] == pytest.approx(40 / 2.0)


def _finish(collector, model, rid, *, ended_at, ok=True, tokens=None,
            server=None, gen_chunks=None):
    """Close a request the way the swapper does (decode, then prefill)."""
    collector.record_decode_end(
        model, rid, ended_at=ended_at, ok=ok, completion_tokens=tokens,
        server_decode=server, gen_chunks=gen_chunks,
    )
    collector.record_prefill_end(
        model, rid, first_token_at=None, ok=ok, prompt_tokens=None,
        cached_tokens=None, ended_at=ended_at,
    )


class TestCollectorAggregates:
    def test_in_flight_non_stream_holds_and_joins_the_group(self):
        collector = StatsCollector()
        stream, plain = object(), object()
        collector.record_request_start("m", stream, 0.0)
        collector.record_decode_start("m", stream, 1.0)
        collector.record_request_start("m", plain, 0.5)
        _finish(collector, "m", stream, ended_at=3.0, tokens=20,
                gen_chunks=10)
        stats = collector._models["m"]
        # The in-flight non-stream request may still overlap [1, 3].
        assert len(stats.decode_frontier) == 1
        assert _gen(collector.snapshot())["avg_aggregate"] is None
        _finish(collector, "m", plain, ended_at=4.0, tokens=30,
                server=_server(usage={"completion_time": 3.0}))
        assert stats.decode_frontier == []
        assert stats.active_requests == {}
        # One component [1, 4]: 50 tokens over 3 s, stream time 2 + 3.
        gen = _gen(collector.snapshot(), "m")
        assert gen["avg_aggregate"]["tps"] == pytest.approx(50 / 3)
        assert gen["avg_aggregate"]["concurrency"] == pytest.approx(5 / 3)
        assert gen["max_aggregate"] == {
            "tps": pytest.approx(50 / 3), "concurrency": 2,
        }

    def test_failed_non_stream_cannot_bridge_groups(self):
        collector = StatsCollector()
        a, b, c = object(), object(), object()
        collector.record_request_start("m", b, 0.5)
        collector.record_request_start("m", a, 0.0)
        collector.record_decode_start("m", a, 1.0)
        _finish(collector, "m", a, ended_at=2.0, tokens=10, gen_chunks=5)
        collector.record_request_start("m", c, 2.5)
        collector.record_decode_start("m", c, 3.0)
        _finish(collector, "m", c, ended_at=5.0, tokens=10, gen_chunks=5)
        assert len(collector._models["m"].decode_frontier) == 2
        _finish(collector, "m", b, ended_at=4.0, ok=False, tokens=None,
                server=_server(usage={"completion_time": 3.5}))
        gen = _gen(collector.snapshot(), "m")
        # Two separate groups: 10 / 1 s and 10 / 2 s.
        assert gen["avg_aggregate"]["tps"] == pytest.approx(20 / 3)
        assert gen["avg_aggregate"]["concurrency"] == pytest.approx(1.0)
        assert gen["max_aggregate"]["tps"] == pytest.approx(10.0)

    def test_buffered_stream_adds_no_interval_but_live_busy_time(self):
        collector = StatsCollector()
        rid = object()
        now = time.monotonic()  # live intervals are pruned against now
        collector.record_request_start("m", rid, now - 1.0)
        collector.record_decode_start("m", rid, now - 0.002)
        _finish(collector, "m", rid, ended_at=now - 0.001, tokens=40,
                gen_chunks=1)
        stats = collector._models["m"]
        assert stats.decode_aggregate.tokens == 0
        assert stats.max_aggregate_tps is None
        assert list(stats.live_decode_intervals) == [
            (now - 0.002, now - 0.001),
        ]

    def test_non_stream_never_touches_live_rate(self):
        collector = StatsCollector()
        rid = object()
        collector.record_request_start("m", rid, 0.0)
        _finish(collector, "m", rid, ended_at=2.0, tokens=10,
                server=_server(usage={"completion_time": 1.0}))
        stats = collector._models["m"]
        assert list(stats.live_decode_intervals) == []
        assert stats.decode_aggregate.tokens == 10
        assert stats.decode_aggregate.busy_time == pytest.approx(1.0)
