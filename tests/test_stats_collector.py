"""Statistics tests for plan P-20260913-01.

Covers the plan's §Testing strategy:
- Unit: StatsCollector percentile math, accounting, throughput windows,
  snapshot shape / null rules, inflight peak.
- Integration: /stats and /stats/json endpoints (local intercept, shadowing),
  proxied request recording (buffered/streaming/failures), rejections
  (400/404/503/504), post-drain failure path (AC12), swap/lifecycle metrics,
  concurrency peak.

Expected behavior is derived from the plan's metric definitions and the
/stats/json contract — not from the implementation.
"""


from __future__ import annotations

import time

import pytest

from src.stats import STATS_HTML, Outcome, StatsCollector, _percentile


# ---------------------------------------------------------------------------
# Unit tests: StatsCollector
# ---------------------------------------------------------------------------


class TestPercentileMath:
    """Nearest-rank semantics per plan: sorted[ceil(q*n)-1] clamped to range."""

    def test_odd_length_known_list(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert _percentile(samples, 0.50) == 30.0  # ceil(2.5)-1 = 2
        assert _percentile(samples, 0.95) == 50.0  # ceil(4.75)-1 = 4
        assert _percentile(samples, 1.00) == 50.0  # ceil(5)-1 = 4 (max)

    def test_even_length_known_list(self):
        samples = [1.0, 2.0, 3.0, 4.0]
        assert _percentile(samples, 0.50) == 2.0  # ceil(2)-1 = 1
        assert _percentile(samples, 0.95) == 4.0  # ceil(3.8)-1 = 3

    def test_single_sample(self):
        samples = [7.0]
        assert _percentile(samples, 0.50) == 7.0
        assert _percentile(samples, 0.95) == 7.0
        assert _percentile(samples, 1.00) == 7.0

    def test_empty_list_returns_zero(self):
        assert _percentile([], 0.50) == 0.0

    def test_q_zero_clamps_to_first_element(self):
        # ceil(0)-1 = -1 must clamp to index 0.
        assert _percentile([5.0, 6.0, 7.0], 0.0) == 5.0

    def test_n_ten_p95_is_last_element(self):
        samples = [float(i) for i in range(10)]
        assert _percentile(samples, 0.95) == 9.0  # ceil(9.5)-1 = 9


class TestStatsCollector:
    def test_success_failure_and_error_kind_accounting(self):
        collector = StatsCollector()
        collector.record_request("m", ok=True, latency_s=0.1)
        collector.record_request("m", ok=True, latency_s=0.2)
        collector.record_request("m", ok=False, kind="500", latency_s=0.3)

        report = collector.snapshot()["requests"]
        assert report["total"] == 3
        assert report["success"] == 2
        assert report["failure"] == 1
        assert report["success_rate"] == pytest.approx(2 / 3)
        assert report["errors"] == {"500": 1}

    def test_ok_false_without_kind_counts_failure_but_no_error_entry(self):
        collector = StatsCollector()
        collector.record_request(None, ok=False)

        report = collector.snapshot()["requests"]
        assert report["total"] == 1
        assert report["failure"] == 1
        assert report["errors"] == {}

    def test_rejection_without_model_is_global_only(self):
        collector = StatsCollector()
        collector.record_rejection("400")

        snap = collector.snapshot()
        assert snap["requests"]["total"] == 1
        assert snap["requests"]["errors"] == {"400": 1}
        assert snap["requests"]["success"] == 0
        assert snap["models"] == {}

    def test_rejection_with_model_is_attributed(self):
        collector = StatsCollector()
        collector.record_rejection("404", model="ghost")

        snap = collector.snapshot()
        row = snap["models"]["ghost"]
        assert row["requests"]["total"] == 1
        assert row["requests"]["errors"] == {"404": 1}
        assert row["requests"]["success"] == 0
        assert row["requests"]["latency_ms"] is None  # rejections not proxied
        # Global accounting mirrors the attribution.
        assert snap["requests"]["total"] == 1
        assert snap["requests"]["errors"] == {"404": 1}

    def test_swap_aggregates_null_when_zero(self):
        snap = StatsCollector().snapshot()
        assert snap["swaps"] == {
            "count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None
        }

    def test_swap_count_and_duration_aggregates(self):
        collector = StatsCollector()
        collector.record_swap(1.0)
        collector.record_swap(2.0)
        collector.record_swap(4.0)

        swaps = collector.snapshot()["swaps"]
        assert swaps["count"] == 3
        assert swaps["avg_ms"] == pytest.approx(7000.0 / 3)
        assert swaps["p95_ms"] == pytest.approx(4000.0)  # nearest rank
        assert swaps["max_ms"] == pytest.approx(4000.0)

    def test_model_start_stop_counters(self):
        collector = StatsCollector()
        collector.record_model_start("m1")
        collector.record_model_start("m1")
        collector.record_model_stop("m1")
        collector.record_model_stop("never-started")

        models = collector.snapshot()["models"]
        assert models["m1"]["starts"] == 2
        assert models["m1"]["stops"] == 1
        assert models["never-started"]["stops"] == 1
        assert models["never-started"]["starts"] == 0

    def test_throughput_windows_and_pruning(self):
        collector = StatsCollector()
        now = time.monotonic()
        # Chronological order (oldest first): pruning pops from the left.
        collector._request_times.extend([
            now - 4000.0,  # older than 3600s retention -> pruned
            now - 400.0,   # retained but outside both windows
            now - 100.0,   # last_300s only
            now - 20.0,    # last_60s
            now - 10.0,    # last_60s
        ])

        snap = collector.snapshot()
        assert snap["throughput"]["last_60s"] == 2
        assert snap["throughput"]["last_300s"] == 3
        # The >3600s entry is pruned by snapshot.
        assert len(collector._request_times) == 4

    def test_avg_rpm_matches_total_over_uptime(self):
        collector = StatsCollector()
        for _ in range(6):
            collector.record_request(None, ok=True)

        snap = collector.snapshot()
        expected = 6 / (snap["uptime_seconds"] / 60.0)
        assert snap["throughput"]["avg_rpm"] == pytest.approx(
            expected, rel=0.01
        )

    def test_snapshot_fresh_shape_and_null_rules(self):
        snap = StatsCollector().snapshot(current_model="llama", inflight=2)

        assert set(snap) == {
            "uptime_seconds", "current_model", "concurrency", "requests",
            "throughput", "swaps", "models",
        }
        assert snap["current_model"] == "llama"
        assert snap["uptime_seconds"] >= 0.0
        assert snap["concurrency"] == {"active": 2, "peak": 0}
        assert set(snap["requests"]) == {
            "total", "success", "failure", "success_rate", "errors",
            "latency_ms", "bytes_in", "bytes_out",
            "tokens", "rates", "ttft_ms", "tpot_ms", "time_split",
        }
        assert snap["requests"]["total"] == 0
        assert snap["requests"]["success"] == 0
        assert snap["requests"]["failure"] == 0
        assert snap["requests"]["success_rate"] is None  # total == 0
        assert snap["requests"]["errors"] == {}
        assert snap["requests"]["latency_ms"] is None  # no samples
        assert snap["requests"]["bytes_in"] == 0
        assert snap["requests"]["bytes_out"] == 0
        assert snap["requests"]["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0,
            "prefill": 0, "cache_hit_rate": None,
        }
        assert snap["requests"]["rates"] == {
            "prompt_tps": {
                "avg": None, "avg_aggregate": None,
                "server_timed_share": None, "skipped_unknown_cache": 0,
            },
            "generation_tps": {
                "avg": None, "last_60s": 0.0, "last_300s": 0.0,
                "avg_aggregate": None,
                "server_timed_share": None, "skipped_buffered": 0,
                "live": {
                    "tps": 0.0, "window_s": 15, "estimate": True,
                    "busy_s": 0.0, "tokens_per_chunk": None,
                },
            },
        }
        assert snap["requests"]["ttft_ms"] is None  # no samples
        assert snap["requests"]["tpot_ms"] is None  # no samples
        assert snap["requests"]["time_split"] == {
            "prefill_s": 0.0, "decode_s": 0.0, "prefill_share": None,
            "generation_share": None, "requests": 0,
        }
        assert snap["throughput"] == {
            "last_60s": 0, "last_300s": 0, "avg_rpm": 0.0,
        }
        assert snap["swaps"]["count"] == 0
        assert snap["models"] == {}

    def test_latency_report_known_samples(self):
        collector = StatsCollector()
        for latency in (0.1, 0.2, 0.3, 0.4):
            collector.record_request("m", ok=True, latency_s=latency)

        report = collector.snapshot()["requests"]["latency_ms"]
        assert report["count"] == 4
        assert report["avg"] == pytest.approx(250.0)
        assert report["p50"] == pytest.approx(200.0)  # ceil(2)-1 = 1
        assert report["p95"] == pytest.approx(400.0)  # ceil(3.8)-1 = 3
        assert report["max"] == pytest.approx(400.0)

    def test_latency_per_model_matches_global_samples(self):
        collector = StatsCollector()
        collector.record_request("m", ok=True, latency_s=0.5)
        collector.record_request(None, ok=True, latency_s=0.1)

        snap = collector.snapshot()
        assert snap["requests"]["latency_ms"]["count"] == 2
        assert snap["models"]["m"]["requests"]["latency_ms"]["count"] == 1
        assert snap["models"]["m"]["requests"]["latency_ms"]["max"] == \
            pytest.approx(500.0)

    def test_bytes_accumulate_global_and_per_model(self):
        collector = StatsCollector()
        collector.record_request("m", ok=True, bytes_in=10, bytes_out=100)
        collector.record_request("m", ok=True, bytes_in=10, bytes_out=100)
        collector.record_request(None, ok=True, bytes_in=10, bytes_out=100)

        snap = collector.snapshot()
        assert snap["requests"]["bytes_in"] == 30
        assert snap["requests"]["bytes_out"] == 300
        assert snap["models"]["m"]["requests"]["bytes_in"] == 20
        assert snap["models"]["m"]["requests"]["bytes_out"] == 200

    def test_observe_inflight_tracks_peak_only(self):
        collector = StatsCollector()
        collector.observe_inflight(3)
        collector.observe_inflight(2)  # decrease must not lower peak
        collector.observe_inflight(5)
        assert collector.snapshot()["concurrency"]["peak"] == 5

    def test_outcome_defaults(self):
        outcome = Outcome()
        assert outcome.stream_error is False
        assert outcome.bytes_out == 0
        outcome.stream_error = True
        outcome.bytes_out += 5
        assert outcome.stream_error is True
        assert outcome.bytes_out == 5


# ---------------------------------------------------------------------------
# Unit: token metrics collector derivations (P-20260913-02, AC5, AC6, AC7)
# ---------------------------------------------------------------------------


class TestTokenMetricCollector:
    """Collector-side derivations with synthetic values — exact math here.

    Expected values are computed by hand from the plan's metric
    definitions (§Metric definitions), never from the implementation.
    """

    @staticmethod
    def _record_four_streamed(collector):
        # (prompt, completion, cached, ttft_s, decode_s) per request.
        rows = [
            (100, 10, 2, 0.5, 2.0),
            (200, 5, None, 1.0, 0.8),
            (50, 2, None, 0.25, 0.2),
            (300, 8, None, 0.75, 1.4),
        ]
        for prompt, completion, cached, ttft, decode in rows:
            collector.record_request(
                "m", ok=True, latency_s=1.0,
                prompt_tokens=prompt, completion_tokens=completion,
                cached_tokens=cached, ttft_s=ttft, decode_s=decode,
            )

    def test_ttft_percentile_math_with_synthetic_inputs(self):
        # AC4: ttft_ms is a _latency_report over TTFT seconds -> ms.
        collector = StatsCollector()
        self._record_four_streamed(collector)

        report = collector.snapshot()["requests"]["ttft_ms"]

        # ttfts sorted: [0.25, 0.5, 0.75, 1.0]
        assert report["count"] == 4
        assert report["avg"] == pytest.approx(625.0)
        assert report["p50"] == pytest.approx(500.0)   # ceil(2)-1 = 1
        assert report["p95"] == pytest.approx(1000.0)  # ceil(3.8)-1 = 3
        assert report["max"] == pytest.approx(1000.0)

    def test_tpot_percentile_math_with_synthetic_inputs(self):
        # AC5: per-request TPOT = decode_s / (completion_tokens - 1).
        collector = StatsCollector()
        self._record_four_streamed(collector)

        report = collector.snapshot()["requests"]["tpot_ms"]

        # Per-request tpots: 2.0/9, 0.8/4, 0.2/1, 1.4/7
        #   = [0.2222, 0.2, 0.2, 0.2] sorted -> [0.2, 0.2, 0.2, 0.2222]
        assert report["count"] == 4
        assert report["avg"] == pytest.approx((0.6 + 2.0 / 9) / 4 * 1000.0)
        assert report["p50"] == pytest.approx(200.0)
        assert report["p95"] == pytest.approx(2.0 / 9 * 1000.0)  # ceil(3.8)-1=3
        assert report["max"] == pytest.approx(2.0 / 9 * 1000.0)

    def test_tps_aggregates_are_token_weighted(self):
        # AC6: gen t/s = Σcompletion / Σdecode; prompt t/s = Σprompt / ΣTTFT.
        # Mean-of-per-request rates would give 6.74 / 575 — distinct from
        # the token-weighted expected values below.
        collector = StatsCollector()
        self._record_four_streamed(collector)

        rates = collector.snapshot()["requests"]["rates"]

        assert rates["generation_tps"]["avg"] == pytest.approx(25 / 4.4)
        # Prompt processing excludes the cached reads (row 1: prompt 100,
        # cached 2): prefill = 98 + 200 + 50 + 300 = 648 over 2.5 s of TTFT.
        assert rates["prompt_tps"]["avg"] == pytest.approx(648 / 2.5)

    def test_prefill_excludes_cached_reads(self):
        # P-20260924-01: pp t/s counts the uncached prompt work, not the
        # full prompt. prompt=100 with cached=90 contributes only 10 tokens.
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=100, cached_tokens=90, ttft_s=0.5,
        )

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["prefill"] == 10
        assert snap["models"]["m"]["requests"]["tokens"]["prefill"] == 10
        assert snap["requests"]["rates"]["prompt_tps"]["avg"] \
            == pytest.approx(10 / 0.5)

    def test_fully_cached_request_contributes_no_prefill_work(self):
        # P-20260924-01: a fully-cached request adds neither numerator nor
        # time, so it cannot dilute the pp rate (no clamping of the rate).
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=50, cached_tokens=50, ttft_s=0.5,
        )
        # The only real work on the same collector:
        collector.record_request(
            "m", ok=True, prompt_tokens=100, ttft_s=0.5,
        )

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["prefill"] == 100
        assert snap["requests"]["rates"]["prompt_tps"]["avg"] \
            == pytest.approx(100 / 0.5)

    def test_oversized_cached_tokens_clamp_to_zero_prefill(self):
        # P-20260924-01: cumulative backends can report cached > prompt;
        # clamp to 0, never negative, and skip the request entirely.
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=10, cached_tokens=25, ttft_s=0.5,
        )

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["prefill"] == 0
        assert snap["models"]["m"]["requests"]["tokens"]["prefill"] == 0
        assert snap["requests"]["rates"]["prompt_tps"]["avg"] is None

    def test_prefill_skips_unknown_cache_on_never_reporting_model(self):
        # P-20260929-01 (replaces the P-20260924-01 full-prompt fallback):
        # a model that has never reported a cached count is excluded from
        # prompt speed, since the full prompt could be mostly cached.
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=200, ttft_s=1.0,
        )

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["prefill"] == 0
        assert snap["models"]["m"]["requests"]["tokens"]["prefill"] == 0
        assert snap["requests"]["rates"]["prompt_tps"]["avg"] is None
        for block in (snap["requests"], snap["models"]["m"]["requests"]):
            assert block["rates"]["prompt_tps"]["skipped_unknown_cache"] == 1

    def test_prefill_mirrors_per_model_and_needs_ttft(self):
        # P-20260924-01: tokens.prefill aggregates globally and per model,
        # and counts only prefill work with an observed first token (TTFT).
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=12, cached_tokens=3, ttft_s=0.5,
        )
        collector.record_request(
            "a", ok=True, prompt_tokens=8, cached_tokens=2, ttft_s=0.25,
        )
        # Buffered request (no TTFT): counters only, no prefill work.
        collector.record_request("m", ok=True, prompt_tokens=30,
                                 cached_tokens=10)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["prefill"] == (12 - 3) + (8 - 2)
        assert snap["models"]["m"]["requests"]["tokens"]["prefill"] == 12 - 3
        assert snap["models"]["a"]["requests"]["tokens"]["prefill"] == 8 - 2

    def test_tps_null_with_no_contributors(self):
        # AC6: both rates null when nothing contributed; empty windows 0.0.
        rates = StatsCollector().snapshot()["requests"]["rates"]

        assert rates["prompt_tps"]["avg"] is None
        assert rates["generation_tps"]["avg"] is None
        assert rates["generation_tps"]["last_60s"] == 0.0
        assert rates["generation_tps"]["last_300s"] == 0.0

    def test_tpot_null_when_completion_tokens_below_two(self):
        # AC5: TPOT undefined for completion_tokens == 1 (no sample), but
        # >= 1 still contributes to generation t/s (AC6).
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=10, completion_tokens=1,
            ttft_s=0.2, decode_s=0.3,
        )

        req = collector.snapshot()["requests"]

        assert req["tpot_ms"] is None
        assert req["rates"]["generation_tps"]["avg"] == pytest.approx(1 / 0.3)

    def test_zero_completion_tokens_contributes_to_no_rate(self):
        collector = StatsCollector()
        collector.record_request(
            "m", ok=True, prompt_tokens=0, completion_tokens=0,
            ttft_s=0.5, decode_s=0.5,
        )

        req = collector.snapshot()["requests"]

        assert req["rates"]["generation_tps"]["avg"] is None
        assert req["rates"]["prompt_tps"]["avg"] is None
        assert req["tpot_ms"] is None
        assert req["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0,
            "prefill": 0, "cache_hit_rate": None,
        }

    def test_cached_tokens_summed_when_present(self):
        # AC2/AC8: cached sums only when prompt_tokens_details is present.
        collector = StatsCollector()
        collector.record_request("m", ok=True, prompt_tokens=100,
                                 cached_tokens=30)
        collector.record_request("m", ok=True, prompt_tokens=50)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"] == {
            "prompt": 150, "completion": 0, "cached": 30,
            "prefill": 0,  # no TTFT: counter-only rows add no prefill work
            "cache_hit_rate": 0.2,
        }
        assert snap["models"]["m"]["requests"]["tokens"]["cached"] == 30
        assert snap["requests"]["tokens"]["cache_hit_rate"] == 0.2
        assert snap["models"]["m"]["requests"]["tokens"]["cache_hit_rate"] == 0.2

    def test_token_counters_recorded_regardless_of_status(self):
        # Plan §Metric definitions: counters over all requests where usage
        # was extractable, regardless of response status. Timing kwargs are
        # None here (swapper only passes them for successful requests).
        collector = StatsCollector()
        collector.record_request("m", ok=False, kind="stream_error",
                                 latency_s=0.2, prompt_tokens=10,
                                 completion_tokens=4, cached_tokens=1)

        req = collector.snapshot()["requests"]

        assert req["tokens"] == {
            "prompt": 10, "completion": 4, "cached": 1,
            "prefill": 0,  # no TTFT on failures: no prefill work
            "cache_hit_rate": 0.1,
        }
        assert req["errors"] == {"stream_error": 1}
        assert req["ttft_ms"] is None
        assert req["rates"]["generation_tps"]["avg"] is None
        # The completion tokens still feed the windowed rate (decision 8).
        assert req["rates"]["generation_tps"]["last_60s"] == \
            pytest.approx(4 / 60)

    def test_streamed_ok_without_usage_contributes_no_token_metrics(self):
        # AC3 null rule: no usage -> tokens null, no t/s or TPOT
        # contributions, and recording never raises. TTFT is still
        # observable without usage (AC4: first content event seen).
        collector = StatsCollector()
        collector.record_request("m", ok=True, latency_s=0.4,
                                 ttft_s=0.1, decode_s=0.2)

        req = collector.snapshot()["requests"]

        assert req["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0,
            "prefill": 0, "cache_hit_rate": None,
        }
        assert req["ttft_ms"]["count"] == 1  # observable without usage
        assert req["tpot_ms"] is None  # needs usage + completion >= 2
        assert req["rates"]["prompt_tps"]["avg"] is None
        assert req["rates"]["generation_tps"]["avg"] is None
        assert req["rates"]["generation_tps"]["last_60s"] == 0.0

    def test_record_request_with_no_usage_fields_never_raises(self):
        collector = StatsCollector()

        collector.record_request("m", ok=True)
        collector.record_request(None, ok=True)

        req = collector.snapshot()["requests"]

        assert req["total"] == 2
        assert req["tokens"] == {
            "prompt": 0, "completion": 0, "cached": 0,
            "prefill": 0, "cache_hit_rate": None,
        }
        assert req["ttft_ms"] is None
        assert req["tpot_ms"] is None
        assert req["rates"]["generation_tps"]["avg"] is None

    def test_tokens_without_model_are_global_only(self):
        collector = StatsCollector()
        collector.record_request(None, ok=True, prompt_tokens=5,
                                 completion_tokens=2)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["prompt"] == 5
        assert snap["requests"]["tokens"]["completion"] == 2
        assert snap["models"] == {}

    def test_token_counters_and_timing_mirror_per_model(self):
        collector = StatsCollector()
        collector.record_request("m", ok=True, latency_s=0.3,
                                 prompt_tokens=7, completion_tokens=3,
                                 cached_tokens=1, ttft_s=0.2, decode_s=0.2)

        snap = collector.snapshot()

        row = snap["models"]["m"]["requests"]
        assert row["tokens"] == snap["requests"]["tokens"]
        assert row["ttft_ms"]["count"] == 1
        assert row["tpot_ms"]["count"] == 1
        assert row["rates"]["generation_tps"]["avg"] == pytest.approx(15.0)

    def test_token_windows_math_and_retention_pruning(self):
        # AC7: seeded _token_events in chronological order (oldest first —
        # pruning pops from the left, tests/test_stats_collector.py convention).
        collector = StatsCollector()
        now = time.monotonic()
        collector.token_events.extend([
            (now - 4000.0, 100),  # older than 3600s retention -> pruned
            (now - 400.0, 50),    # retained, outside both windows
            (now - 100.0, 20),    # last_300s only
            (now - 20.0, 6),      # both windows
            (now - 10.0, 4),      # both windows
        ])

        gen = collector.snapshot()["requests"]["rates"]["generation_tps"]

        assert gen["last_60s"] == pytest.approx(10 / 60)
        assert gen["last_300s"] == pytest.approx(30 / 300)
        assert gen["avg"] is None  # no decode contributions
        # snapshot() pruned the >3600s event.
        assert len(collector.token_events) == 4

    def test_token_window_events_inside_window_counted(self):
        # Seeded safely inside the edges (snapshot re-reads the clock, so
        # seeding exactly at the edge would be flaky).
        collector = StatsCollector()
        now = time.monotonic()
        collector.token_events.extend([
            (now - 59.0, 60),    # inside last_60s
            (now - 299.0, 240),  # inside last_300s only
        ])

        gen = collector.snapshot()["requests"]["rates"]["generation_tps"]

        assert gen["last_60s"] == pytest.approx(1.0)   # 60 / 60
        assert gen["last_300s"] == pytest.approx(1.0)  # 300 / 300

    def test_token_window_events_just_past_edge_excluded(self):
        collector = StatsCollector()
        now = time.monotonic()
        collector.token_events.extend([
            (now - 60.5, 600),    # just past the 60s window...
            (now - 300.5, 3000),  # ...and just past the 300s window.
        ])

        gen = collector.snapshot()["requests"]["rates"]["generation_tps"]

        # Both events are outside the 60s window -> honest zero rate.
        assert gen["last_60s"] == 0.0
        # Only the 60.5s-old event is inside the 300s window; if the
        # 300.5s-old event leaked in, it would be (600+3000)/300 = 12.0.
        assert gen["last_300s"] == pytest.approx(600 / 300)

    def test_per_model_token_windows_seeded_chronologically(self):
        # AC8: per-model rows carry their own windowed rates.
        collector = StatsCollector()
        now = time.monotonic()
        ms = collector._model("m1")
        ms.token_events.extend([
            (now - 4000.0, 10),  # pruned by snapshot
            (now - 70.0, 20),    # last_300s only
            (now - 5.0, 30),     # both windows
        ])

        snap = collector.snapshot()

        gen = snap["models"]["m1"]["requests"]["rates"]["generation_tps"]
        assert gen["last_60s"] == pytest.approx(30 / 60)
        assert gen["last_300s"] == pytest.approx(50 / 300)
        assert len(ms.token_events) == 2  # pruned by snapshot
        # Global window is independent of per-model seeding.
        assert snap["requests"]["rates"]["generation_tps"]["last_60s"] == 0.0

    def test_cache_hit_rate_is_per_model_independent(self):
        # Each model's ratio derives from its own sums only.
        collector = StatsCollector()
        collector.record_request("a", ok=True, prompt_tokens=100,
                                 cached_tokens=50)   # 0.5
        collector.record_request("b", ok=True, prompt_tokens=200,
                                 cached_tokens=20)   # 0.1

        snap = collector.snapshot()

        assert snap["models"]["a"]["requests"]["tokens"]["cache_hit_rate"] \
            == 0.5
        assert snap["models"]["b"]["requests"]["tokens"]["cache_hit_rate"] \
            == 0.1
        # Global pools both models: 70 cached / 300 prompt.
        assert snap["requests"]["tokens"]["cache_hit_rate"] \
            == pytest.approx(70 / 300)

    def test_cache_hit_rate_null_without_prompt_tokens(self):
        # No usage recorded -> prompt sum 0 -> null, global and per-model.
        collector = StatsCollector()
        collector.record_request("m", ok=True)
        collector.record_request(None, ok=True)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["cache_hit_rate"] is None
        assert snap["models"]["m"]["requests"]["tokens"]["cache_hit_rate"] \
            is None

    def test_cache_hit_rate_null_when_cached_without_prompt_tokens(self):
        # Denominator-0 anomaly: cached reported while prompt sum stays 0.
        # Division is guarded, not clamped — exact counts, never estimate.
        collector = StatsCollector()
        collector.record_request("m", ok=True, cached_tokens=5)

        snap = collector.snapshot()

        assert snap["requests"]["tokens"]["cache_hit_rate"] is None
        assert snap["models"]["m"]["requests"]["tokens"]["cache_hit_rate"] \
            is None

    def test_dashboard_html_has_cache_hit_column(self):
        # Static gate: the compact dashboard table (P-20260925-01) keeps the
        # cache hit rate column; P-20260925-04 adds the generated and prefill
        # token counts after it. P-20260930-01 drops the meter: text only.
        assert "<th>Cache</th><th>Gen tok</th><th>Prefill tok</th>" in STATS_HTML
        assert "tokens.cache_hit_rate" in STATS_HTML
        assert "plainCell(fmtPct(cells[10]))" in STATS_HTML
        assert '"meter"' not in STATS_HTML


