"""Server prefill parsing, sample priority, and the learn rule (P-20260929-01).

Unit tests for src/prefill.py plus the collector-side learn state and the
first-token rule in the SSE parser. Expected values are computed by hand.
"""

from __future__ import annotations

import json
import math

import pytest

from src.prefill import (
    PrefillDecision,
    PrefillSample,
    ServerPrefill,
    apply_server_prefill,
    effective_cached,
    prefill_interval,
    select_prefill,
)
from src.proxy import _extract_usage, _process_sse_line
from src.stats import Outcome, StatsCollector


def _server(**fields):
    server = ServerPrefill()
    apply_server_prefill(fields, server)
    return server


class TestParsing:

    def test_reads_timings_prompt_time_and_metrics(self):
        server = _server(
            timings={"prompt_n": 8, "prompt_ms": 20.5, "cache_n": 4},
            usage={"prompt_time": 0.02},
            metrics={"time_to_first_token_ms": 12},
        )
        assert server == ServerPrefill(
            prompt_n=8, prompt_ms=20.5, cache_n=4,
            prompt_time_s=0.02, ttft_ms=12.0,
        )

    @pytest.mark.parametrize("bad", [
        True, False, "Indeterminate", "20", None, -1, float("nan"),
        float("inf"), [1], {"x": 1},
    ])
    def test_invalid_values_are_ignored(self, bad):
        server = _server(
            timings={"prompt_n": bad, "prompt_ms": bad, "cache_n": bad},
            usage={"prompt_time": bad},
            metrics={"time_to_first_token_ms": bad},
        )
        assert server == ServerPrefill()

    def test_counts_must_be_ints(self):
        server = _server(timings={"prompt_n": 8.0, "cache_n": 2.5})
        assert server.prompt_n is None and server.cache_n is None

    @pytest.mark.parametrize("data", [
        None, [], "x", 3, {"timings": [1]}, {"usage": "x"}, {"metrics": 1},
    ])
    def test_non_dict_shapes_are_ignored(self, data):
        server = ServerPrefill()
        apply_server_prefill(data, server)
        assert server == ServerPrefill()

    def test_last_valid_pair_wins(self):
        # P-20260929-03: prompt_n / prompt_ms update only as a valid pair
        # from one event, so an event with one bad half changes neither.
        server = ServerPrefill()
        apply_server_prefill({"timings": {"prompt_n": 3, "prompt_ms": 5}},
                             server)
        apply_server_prefill({"timings": {"prompt_n": 9, "prompt_ms": "x"}},
                             server)
        assert (server.prompt_n, server.prompt_ms) == (3, 5.0)
        apply_server_prefill({"timings": {"prompt_n": 9, "prompt_ms": 7}},
                             server)
        assert (server.prompt_n, server.prompt_ms) == (9, 7.0)

    def test_sse_and_non_stream_bodies_populate_outcome(self):
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[],"usage":{"prompt_tokens":12,'
            b'"prompt_time":0.03},"timings":{"prompt_n":8,"prompt_ms":20,'
            b'"cache_n":4},"metrics":{"time_to_first_token_ms":7}}',
            outcome,
        )
        assert outcome.server_prefill == ServerPrefill(
            prompt_n=8, prompt_ms=20.0, cache_n=4,
            prompt_time_s=0.03, ttft_ms=7.0,
        )
        outcome = Outcome()
        _extract_usage(json.dumps({
            "usage": {"prompt_tokens": 5}, "timings": {"cache_n": 2},
        }).encode(), outcome)
        assert outcome.prompt_tokens == 5
        assert outcome.server_prefill.cache_n == 2

    def test_parser_failure_never_breaks_proxy_parsing(self, monkeypatch):
        import src.proxy as proxy_module

        def boom(data, server):
            raise RuntimeError("parse failed")

        monkeypatch.setattr(proxy_module, "apply_server_prefill", boom)
        outcome = Outcome()
        _process_sse_line(
            b'data: {"choices":[{"delta":{"content":"x"}}],'
            b'"usage":{"prompt_tokens":3,"completion_tokens":1}}',
            outcome,
        )
        _extract_usage(b'{"usage":{"prompt_tokens":4}}', outcome)
        assert outcome.first_token_at is not None
        assert outcome.prompt_tokens == 4


class TestEffectiveCached:

    def test_details_win_over_cache_n(self):
        assert effective_cached(3, ServerPrefill(cache_n=9)) == 3

    def test_cache_n_fallback(self):
        assert effective_cached(None, ServerPrefill(cache_n=9)) == 9

    def test_unknown(self):
        assert effective_cached(None, ServerPrefill()) is None
        assert effective_cached(None, None) is None
        assert effective_cached(True, None) is None


class TestSelectPriority:

    FULL = ServerPrefill(
        prompt_n=8, prompt_ms=20.0, cache_n=4, prompt_time_s=0.05,
        ttft_ms=100.0,
    )

    def test_timings_win(self):
        decision = select_prefill(12, 4, self.FULL, 1.0, cache_learned=False)
        assert decision == PrefillDecision(PrefillSample(8, 0.02, True))

    def test_prompt_time_next(self):
        server = ServerPrefill(prompt_time_s=0.05, ttft_ms=100.0)
        decision = select_prefill(12, 4, server, 1.0, cache_learned=False)
        assert decision == PrefillDecision(PrefillSample(8, 0.05, True))

    def test_vllm_ttft_next(self):
        server = ServerPrefill(ttft_ms=100.0)
        decision = select_prefill(12, 4, server, 1.0, cache_learned=False)
        assert decision == PrefillDecision(PrefillSample(8, 0.1, True))

    def test_proxy_ttft_last(self):
        decision = select_prefill(
            12, 4, ServerPrefill(), 1.0, cache_learned=False,
        )
        assert decision == PrefillDecision(PrefillSample(8, 1.0, False))

    @pytest.mark.parametrize("server", [
        ServerPrefill(prompt_n=0, prompt_ms=20.0),
        ServerPrefill(prompt_n=8, prompt_ms=0.0),
        ServerPrefill(prompt_n=8),
        ServerPrefill(prompt_time_s=0.0),
        ServerPrefill(ttft_ms=0.0),
    ])
    def test_unusable_server_values_fall_through(self, server):
        decision = select_prefill(12, 4, server, 1.0, cache_learned=False)
        assert decision == PrefillDecision(PrefillSample(8, 1.0, False))

    def test_no_time_no_sample(self):
        assert select_prefill(
            12, 4, None, None, cache_learned=True,
        ) == PrefillDecision()

    @pytest.mark.parametrize("prompt", [None, 0, True, -3, "12"])
    def test_invalid_prompt_no_sample(self, prompt):
        assert select_prefill(
            prompt, None, None, 1.0, cache_learned=False,
        ) == PrefillDecision()

    def test_fully_cached_no_sample(self):
        assert select_prefill(
            10, 25, ServerPrefill(prompt_time_s=0.1), None,
            cache_learned=True,
        ) == PrefillDecision()


class TestLearnRule:

    @pytest.mark.parametrize("server,proxy", [
        (ServerPrefill(prompt_time_s=0.5), None),
        (ServerPrefill(ttft_ms=500.0), None),
        (ServerPrefill(), 0.5),
    ])
    def test_unknown_cache_skipped_unless_learned(self, server, proxy):
        assert select_prefill(
            100, None, server, proxy, cache_learned=False,
        ) == PrefillDecision(skipped_unknown_cache=True)
        decision = select_prefill(
            100, None, server, proxy, cache_learned=True,
        )
        assert decision.sample.tokens == 100
        assert decision.sample.seconds == pytest.approx(0.5)

    def test_timings_prompt_n_ignores_learn_rule(self):
        server = ServerPrefill(prompt_n=5, prompt_ms=10.0)
        decision = select_prefill(100, None, server, 1.0, cache_learned=False)
        assert decision == PrefillDecision(PrefillSample(5, 0.01, True))

    def test_collector_learns_from_details_or_cache_n(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=100, ttft_s=1.0,
        )
        # timings.cache_n counts as a reported cached count.
        collector.record_request(
            "m", ok=True, prompt_tokens=100, ttft_s=1.0,
            server_prefill=ServerPrefill(cache_n=40),
        )
        collector.record_request(
            "m", ok=True, prompt_tokens=100, ttft_s=1.0,
        )
        snap = collector.snapshot()
        for block in (snap["requests"], snap["models"]["m"]["requests"]):
            assert block["tokens"]["prefill"] == 60 + 100
            assert block["tokens"]["cached"] == 40
            prompt = block["rates"]["prompt_tps"]
            assert prompt["avg"] == pytest.approx(160 / 2.0)
            assert prompt["skipped_unknown_cache"] == 1
            assert prompt["server_timed_share"] == 0.0
        assert collector._models["m"].cache_reported is True

    def test_learn_state_is_per_model(self):
        collector = StatsCollector()
        collector.record_request(
            "a", ok=True, prompt_tokens=10, cached_tokens=0, ttft_s=1.0,
        )
        collector.record_request("b", ok=True, prompt_tokens=10, ttft_s=1.0)
        snap = collector.snapshot()
        assert snap["models"]["a"]["requests"]["rates"]["prompt_tps"][
            "skipped_unknown_cache"] == 0
        assert snap["models"]["b"]["requests"]["rates"]["prompt_tps"][
            "skipped_unknown_cache"] == 1
        assert snap["requests"]["rates"]["prompt_tps"][
            "skipped_unknown_cache"] == 1

    def test_failed_requests_do_not_count_as_skipped(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=False, kind="500", prompt_tokens=10,
            server_prefill=ServerPrefill(prompt_time_s=0.1),
        )
        prompt = collector.snapshot()["requests"]["rates"]["prompt_tps"]
        assert prompt["skipped_unknown_cache"] == 0
        assert prompt["avg"] is None


class TestServerTimedShare:

    def test_share_is_token_weighted(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=30, cached_tokens=0,
            server_prefill=ServerPrefill(prompt_n=30, prompt_ms=100.0),
        )
        collector.record_request(
            "m", ok=True, prompt_tokens=10, cached_tokens=0, ttft_s=0.5,
        )
        prompt = collector.snapshot()["requests"]["rates"]["prompt_tps"]
        assert prompt["server_timed_share"] == pytest.approx(0.75)
        assert prompt["avg"] == pytest.approx(40 / 0.6)


class TestPrefillInterval:

    def test_proxy_timed_spans_start_to_first_token(self):
        sample = PrefillSample(8, 2.0, False)
        assert prefill_interval(sample, 0.0, 2.0, 5.0) == (0.0, 2.0)

    def test_server_timed_stream_ends_at_first_token(self):
        sample = PrefillSample(8, 0.5, True)
        assert prefill_interval(sample, 0.0, 2.0, 5.0) == (1.5, 2.0)

    def test_server_timed_stream_clamped_to_start(self):
        sample = PrefillSample(8, 5.0, True)
        assert prefill_interval(sample, 0.0, 2.0, 5.0) == (0.0, 2.0)

    def test_non_stream_anchored_at_start_capped_at_end(self):
        assert prefill_interval(
            PrefillSample(8, 0.5, True), 1.0, None, 5.0,
        ) == (1.0, 1.5)
        assert prefill_interval(
            PrefillSample(8, 9.0, True), 1.0, None, 5.0,
        ) == (1.0, 5.0)

    @pytest.mark.parametrize("args", [
        (PrefillSample(8, 1.0, False), 0.0, None, 5.0),
        (PrefillSample(8, 1.0, False), 2.0, 2.0, 5.0),
        (PrefillSample(8, 1.0, True), math.inf, None, None),
        (PrefillSample(8, 1.0, True), 3.0, None, 3.0),
    ])
    def test_degenerate_intervals(self, args):
        assert prefill_interval(*args) is None

    def test_collector_publishes_non_stream_interval(self):
        collector = StatsCollector()
        collector.record_request_start("m", "a", 0.0)
        collector.record_prefill_end(
            "m", "a", first_token_at=None, ok=True,
            prompt_tokens=100, cached_tokens=20,
            server_prefill=ServerPrefill(prompt_time_s=0.4), ended_at=3.0,
        )
        aggregate = collector.snapshot()["models"]["m"]["requests"][
            "rates"]["prompt_tps"]["avg_aggregate"]
        assert aggregate == {"tps": pytest.approx(200.0), "concurrency": 1.0}


class TestFirstTokenRule:

    @pytest.mark.parametrize("line", [
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}',
        b'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}',
        b'data: {"choices":[{"delta":{}}]}',
        b'data: {"choices":[{"text":""}]}',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[]}}]}',
    ])
    def test_non_output_chunks_do_not_mark_first_token(self, line):
        outcome = Outcome()
        _process_sse_line(line, outcome)
        assert outcome.first_token_at is None

    @pytest.mark.parametrize("line", [
        b'data: {"choices":[{"delta":{"content":"a"}}]}',
        b'data: {"choices":[{"delta":{"reasoning_content":"a"}}]}',
        b'data: {"choices":[{"delta":{"reasoning":"a"}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0}]}}]}',
        b'data: {"choices":[{"text":"a"}]}',
    ])
    def test_output_chunks_mark_first_token(self, line):
        outcome = Outcome()
        _process_sse_line(line, outcome)
        assert outcome.first_token_at is not None
