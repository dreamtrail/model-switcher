"""Prompt vs generation time split (P-20260930-01).

``requests.time_split`` sums prefill and decode seconds over requests that
have BOTH a prefill and a decode sample, so both sides cover the same
requests. Expected values follow the plan's definition, not the code.
"""

from __future__ import annotations

import pytest

from src.decode import ServerDecode
from src.prefill import ServerPrefill
from src.stats import StatsCollector


EMPTY = {
    "prefill_s": 0.0, "decode_s": 0.0,
    "prefill_share": None, "generation_share": None,
    "requests": 0,
}


def _split(collector: StatsCollector, model: str | None = None) -> dict:
    snap = collector.snapshot()
    block = snap["requests"] if model is None else snap["models"][model]["requests"]
    return block["time_split"]


def _paired_stream(collector, model="m", *, ttft_s=1.0, decode_s=3.0, **kw):
    """A successful stream with known cache: both samples are present."""
    args = {
        "ok": True, "prompt_tokens": 100, "cached_tokens": 20,
        "completion_tokens": 50, "ttft_s": ttft_s, "decode_s": decode_s,
        "gen_chunks": 10,
    }
    args.update(kw)
    collector.record_request(model, **args)


class TestTimeSplitCollector:

    def test_null_shares_before_any_paired_request(self):
        collector = StatsCollector()
        assert _split(collector) == EMPTY
        collector.record_request("m", ok=True)  # no timing at all
        assert _split(collector) == EMPTY
        assert _split(collector, "m") == EMPTY

    def test_paired_requests_sum_their_own_seconds(self):
        collector = StatsCollector()
        _paired_stream(collector, ttft_s=1.0, decode_s=3.0)
        _paired_stream(collector, ttft_s=0.5, decode_s=0.5)

        split = _split(collector, "m")
        assert split["prefill_s"] == pytest.approx(1.5)
        assert split["decode_s"] == pytest.approx(3.5)
        assert split["requests"] == 2
        assert split["prefill_share"] == pytest.approx(0.3)
        assert split["generation_share"] == pytest.approx(0.7)
        assert split["prefill_share"] + split["generation_share"] == (
            pytest.approx(1.0)
        )

    def test_server_timed_non_stream_request_is_paired(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=200, completion_tokens=40,
            server_prefill=ServerPrefill(prompt_n=200, prompt_ms=500.0),
            server_decode=ServerDecode(predicted_n=40, predicted_ms=2000.0),
        )
        split = _split(collector, "m")
        assert split["prefill_s"] == pytest.approx(0.5)
        assert split["decode_s"] == pytest.approx(2.0)
        assert split["requests"] == 1
        assert split["prefill_share"] == pytest.approx(0.2)

    def test_unknown_cache_prefill_skip_is_excluded_from_both_sides(self):
        collector = StatsCollector()
        # Model never reported a cached count: prefill sample is skipped.
        _paired_stream(collector, cached_tokens=None, decode_s=4.0)
        snap = collector.snapshot()
        rates = snap["models"]["m"]["requests"]["rates"]
        assert rates["prompt_tps"]["skipped_unknown_cache"] == 1
        # The decode sample still counts toward gen t/s, not the split.
        assert rates["generation_tps"]["avg"] == pytest.approx(50 / 4.0)
        assert _split(collector, "m") == EMPTY
        assert _split(collector) == EMPTY

    def test_buffered_stream_decode_skip_is_excluded_from_both_sides(self):
        collector = StatsCollector()
        _paired_stream(collector, gen_chunks=1, ttft_s=2.0)
        rates = collector.snapshot()["models"]["m"]["requests"]["rates"]
        assert rates["generation_tps"]["skipped_buffered"] == 1
        # The prefill sample still counts toward prompt t/s, not the split.
        assert rates["prompt_tps"]["avg"] == pytest.approx(80 / 2.0)
        assert _split(collector, "m") == EMPTY
        assert _split(collector) == EMPTY

    def test_non_stream_without_server_times_is_excluded(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=100, cached_tokens=0,
            completion_tokens=50,
        )
        assert _split(collector, "m") == EMPTY

    def test_fully_cached_prompt_has_no_prefill_sample_and_is_excluded(self):
        collector = StatsCollector()
        _paired_stream(collector, prompt_tokens=100, cached_tokens=100)
        assert _split(collector, "m") == EMPTY

    def test_failed_requests_are_excluded(self):
        collector = StatsCollector()
        _paired_stream(collector, ok=False, kind="502")
        assert _split(collector, "m") == EMPTY
        assert _split(collector) == EMPTY

    def test_mixed_requests_count_only_the_paired_ones(self):
        collector = StatsCollector()
        # Unknown cache first: after one reported count it would mean 0.
        _paired_stream(collector, cached_tokens=None, ttft_s=9.0, decode_s=9.0)
        _paired_stream(collector, ttft_s=1.0, decode_s=1.0)
        _paired_stream(collector, gen_chunks=1, ttft_s=9.0, decode_s=9.0)
        _paired_stream(collector, ok=False, ttft_s=9.0, decode_s=9.0)
        split = _split(collector, "m")
        assert split["prefill_s"] == pytest.approx(1.0)
        assert split["decode_s"] == pytest.approx(1.0)
        assert split["requests"] == 1
        assert split["prefill_share"] == pytest.approx(0.5)
        assert split["generation_share"] == pytest.approx(0.5)

    def test_global_matches_sum_of_models(self):
        collector = StatsCollector()
        _paired_stream(collector, "a", ttft_s=1.0, decode_s=2.0)
        _paired_stream(collector, "a", ttft_s=0.5, decode_s=0.5)
        _paired_stream(collector, "b", ttft_s=3.0, decode_s=1.0)
        a, b, total = (
            _split(collector, "a"), _split(collector, "b"), _split(collector)
        )
        assert total["prefill_s"] == pytest.approx(
            a["prefill_s"] + b["prefill_s"]
        )
        assert total["decode_s"] == pytest.approx(a["decode_s"] + b["decode_s"])
        assert total["requests"] == a["requests"] + b["requests"] == 3
        assert total["prefill_share"] == pytest.approx(4.5 / 8.0)
        assert total["generation_share"] == pytest.approx(3.5 / 8.0)

    def test_existing_rates_are_unchanged_by_pairing(self):
        collector = StatsCollector()
        _paired_stream(collector, cached_tokens=None, ttft_s=5.0, decode_s=5.0)
        _paired_stream(collector, ttft_s=1.0, decode_s=2.0)
        rates = collector.snapshot()["models"]["m"]["requests"]["rates"]
        # Prompt t/s: only the known-cache sample; gen t/s: both samples.
        assert rates["prompt_tps"]["avg"] == pytest.approx(80 / 1.0)
        assert rates["generation_tps"]["avg"] == pytest.approx(100 / 7.0)
